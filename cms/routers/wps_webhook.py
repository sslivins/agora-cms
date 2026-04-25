"""Azure Web PubSub upstream-webhook receiver.

Implements the CMS side of the CloudEvents 1.0 binary binding contract
Azure WPS uses to push events to us — see
https://learn.microsoft.com/azure/azure-web-pubsub/reference-cloud-events.

Endpoints:
  OPTIONS /internal/wps/events
      CloudEvents abuse-protection handshake.  Responds 200 with
      ``WebHook-Allowed-Origin`` echoing ``WebHook-Request-Origin`` (or
      the configured ``wps_webhook_allowed_origin`` if set).
  POST /internal/wps/events
      Verifies the ``ce-signature`` header (which signs the
      connection id, NOT the body), then:
        - ``azure.webpubsub.sys.connected``    -> register_remote + 204
        - ``azure.webpubsub.sys.disconnected`` -> disconnect + 204
        - ``azure.webpubsub.user.*``           -> dispatch_device_message + 204

The dispatch invocation mirrors ``cms/routers/ws.py`` so the two paths
stay in sync.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_settings
from cms.config import Settings
from cms.database import get_db
from cms.models.device import Device, DeviceGroup
from cms.services.alert_service import alert_service
from cms.services.device_inbound import InboundContext, dispatch_device_message
from cms.services.device_manager import device_manager
from cms.services.device_register import register_known_device
from cms.services.transport import get_transport
from shared.wps_signature import verify_signature

logger = logging.getLogger("agora.cms.wps_webhook")

router = APIRouter(prefix="/internal/wps", tags=["wps-webhook"])


def _extract_access_keys(connection_string: str | None) -> list[str]:
    """Pull every ``AccessKey=...`` value out of a WPS connection string.

    Azure-style connection strings are semicolon-separated
    ``Key=Value`` pairs.  Returns an empty list if the string is unset
    or carries no ``AccessKey``.
    """
    if not connection_string:
        return []
    keys: list[str] = []
    for part in connection_string.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        if name.strip().lower() == "accesskey" and value:
            keys.append(value.strip())
    return keys


def _get_asset_base_url(request: Request, settings: Settings) -> str:
    """Base URL for asset download links — mirrors ws.get_asset_base_url."""
    if settings.asset_base_url:
        return settings.asset_base_url.rstrip("/")
    host = request.headers.get("host")
    if host:
        scheme = "https" if request.url.scheme in ("https", "wss") else "http"
        return f"{scheme}://{host}"
    return str(request.base_url).rstrip("/")


@router.options("/events")
async def events_abuse_protection_handshake(
    request: Request,
    webhook_request_origin: str | None = Header(default=None, alias="WebHook-Request-Origin"),
):
    """Respond to the CloudEvents abuse-protection probe.

    Azure sends ``OPTIONS`` with ``WebHook-Request-Origin: <host>``; we
    must reply 200 with ``WebHook-Allowed-Origin`` to opt in.  The
    configured ``wps_webhook_allowed_origin`` (if set) wins over echoing.
    """
    settings = get_settings()
    allowed = settings.wps_webhook_allowed_origin or webhook_request_origin or "*"
    return Response(status_code=200, headers={"WebHook-Allowed-Origin": allowed})


@router.post("/events")
async def events_receiver(
    request: Request,
    ce_type: str | None = Header(default=None, alias="ce-type"),
    ce_connection_id: str | None = Header(default=None, alias="ce-connectionId"),
    ce_user_id: str | None = Header(default=None, alias="ce-userId"),
    ce_event_name: str | None = Header(default=None, alias="ce-eventName"),
    ce_signature: str | None = Header(default=None, alias="ce-signature"),
    db: AsyncSession = Depends(get_db),
):
    """Receive one CloudEvents binary-binding upstream webhook.

    Every event contributes a 204 on success.  Bad signature -> 401,
    missing/unknown headers -> 400.
    """
    settings = get_settings()

    if not ce_type or not ce_connection_id:
        return Response(status_code=400, content="missing ce-type or ce-connectionId")

    keys = _extract_access_keys(settings.wps_connection_string)
    if not keys:
        logger.error("WPS webhook called but no access keys configured")
        return Response(status_code=500, content="WPS not configured")

    if not verify_signature(ce_connection_id, ce_signature or "", keys):
        logger.warning(
            "WPS webhook signature mismatch (conn=%s, type=%s)",
            ce_connection_id, ce_type,
        )
        return Response(status_code=401, content="bad signature")

    body = await request.body()

    # ---- system events -------------------------------------------------

    if ce_type == "azure.webpubsub.sys.connected":
        if not ce_user_id:
            return Response(status_code=400, content="missing ce-userId")
        # Stage 2c: presence now lives in the DB.  The in-memory
        # ``device_manager.register_remote`` ghost entry was only kept
        # so the per-replica ``pending_log_requests`` map and the
        # existing dispatcher could look up the user; the pending-log
        # map is now addressed by request_id only, so we skip the ghost
        # entry entirely.
        from cms.services import device_presence
        await device_presence.mark_online(
            db, ce_user_id, connection_id=ce_connection_id,
        )
        return Response(status_code=204)

    if ce_type == "azure.webpubsub.sys.disconnected":
        if ce_user_id:
            from cms.services import device_presence
            # Guard the presence flip with the closing connection's id.
            # Under N>=2 an old socket on replica A can receive
            # ``sys.disconnected`` *after* the device has already
            # reconnected on replica B; without this guard we'd flip
            # ``online=false`` for the fresh session and fire a bogus
            # OFFLINE alert.  ``mark_offline_and_alert`` does the CAS
            # flip + ``alert_service.device_disconnected`` in one shot
            # — same helper used by the transport send-failure paths
            # (issue #406), so behaviour stays consistent.
            await device_presence.mark_offline_and_alert(
                db, ce_user_id, expected_connection_id=ce_connection_id,
            )
        return Response(status_code=204)

    # ---- user events ---------------------------------------------------

    if ce_type.startswith("azure.webpubsub.user."):
        if not ce_user_id:
            return Response(status_code=400, content="missing ce-userId")
        try:
            msg = json.loads(body or b"{}")
        except json.JSONDecodeError:
            logger.warning(
                "WPS user event with non-JSON body (user=%s, event=%s)",
                ce_user_id, ce_event_name,
            )
            return Response(status_code=400, content="body is not JSON")

        # Look up the device row.  The connect-token endpoint that
        # mints WPS URLs requires the device to exist + have a valid
        # X-Device-API-Key, so a device reaching us here should
        # already be provisioned.  If the row is missing we have a
        # race (row deleted between connect-token and the first
        # message), not a new-device bootstrap — drop and move on.
        result = await db.execute(select(Device).where(Device.id == ce_user_id))
        device = result.scalar_one_or_none()
        if device is None:
            logger.warning(
                "WPS message from unknown device %s — connect-token race "
                "or row was deleted mid-session", ce_user_id,
            )
            return Response(status_code=204)

        transport = get_transport()

        # ---- register handshake over WPS -----------------------------
        #
        # Mirrors the ``else`` (known-device) branch of
        # ``cms/routers/ws.py``: refresh metadata, verify/mint the
        # device_auth_token, auto-assign a profile.  Brand-new devices
        # still bootstrap over direct-WS — they can't hit connect-token
        # without an API key and a device row.
        if msg.get("type") == "register":
            # Capture pre-register firmware AND upgrade-claim token
            # before ``register_known_device`` mutates them.  Used
            # below to gate (and CAS-protect) clearing the
            # upgrade-in-progress claim on a real firmware-change.
            pre_register_fw = device.firmware_version or ""
            pre_register_upgrade_claim = device.upgrade_started_at
            reg_result = await register_known_device(device, msg, db)
            # Persist the device's LAN IP from the register payload.
            # ``sys.connected`` (handled above) has no body, so it can't
            # carry the IP — and the webhook origin we'd see at the HTTP
            # layer is Azure Web PubSub's egress, not the device's LAN
            # address.  The ``register`` user-message is the first hop
            # where the device itself reports ``ip_address``.  Calling
            # ``mark_online`` again is idempotent (it tolerates an
            # already-current ``connection_id``) and only writes
            # ``Device.ip_address`` when the value is non-empty.
            client_ip = msg.get("ip_address") or None
            if client_ip:
                from cms.services import device_presence
                await device_presence.mark_online(
                    db, ce_user_id,
                    connection_id=ce_connection_id,
                    ip_address=client_ip,
                )
            if reg_result.orphaned:
                # Over direct-WS we'd close 4004.  Over WPS we can't
                # close the WPS connection from here; the device is
                # now ORPHANED in the DB so subsequent inbound messages
                # will land the same way, and admins see the status.
                logger.warning(
                    "WPS device %s failed auth — marked ORPHANED", ce_user_id,
                )
                return Response(status_code=204)
            if reg_result.auth_assigned is not None:
                try:
                    await transport.send_to_device(
                        ce_user_id, reg_result.auth_assigned,
                    )
                except Exception:
                    logger.exception(
                        "Failed to push auth_assigned to WPS device %s", ce_user_id,
                    )
            # Clear any stale upgrade-in-progress claim so the device
            # can be upgraded again on a future request — but only
            # when this register represents an actual completed
            # upgrade (mirrors ``ws.py`` Stage-4 logic):
            #   - there was a claim at register time
            #     (``pre_register_upgrade_claim``),
            #   - both prior and reported firmware are non-empty, and
            #   - they differ (so the device booted into a new version).
            # The UPDATE uses compare-and-swap on
            # ``upgrade_started_at`` equal to the pre-register value,
            # so a successor upgrade claim written between our SELECT
            # and our clear isn't wiped.  Transient reconnects during
            # an upgrade (same firmware) leave the claim in place;
            # the upgrade-endpoint TTL is the safety net that releases
            # the claim if no firmware change is ever reported.
            if pre_register_upgrade_claim is not None:
                reported_fw = (msg.get("firmware_version") or "").strip()
                prior_fw = (pre_register_fw or "").strip()
                if reported_fw and prior_fw and reported_fw != prior_fw:
                    await db.execute(
                        update(Device)
                        .where(
                            Device.id == ce_user_id,
                            Device.upgrade_started_at == pre_register_upgrade_claim,
                        )
                        .values(upgrade_started_at=None)
                        .execution_options(synchronize_session=False)
                    )
                    await db.commit()
            # Notify alert service of reconnection — mirrors ws.py's
            # direct-WS register path.  This is what emits the ONLINE
            # device_event, clears ``offline_notified`` on
            # ``device_alert_state``, and (if applicable) fires the
            # "back online" notification.  Without this call, WPS
            # devices silently skip all alert-service bookkeeping.
            _group_id = str(device.group_id) if device.group_id else None
            _device_name = device.name or ce_user_id
            _device_status = device.status.value if device.status else "pending"
            _group_name = ""
            if device.group_id:
                g = await db.execute(
                    select(DeviceGroup.name).where(
                        DeviceGroup.id == device.group_id,
                    )
                )
                _group_name = g.scalar_one_or_none() or ""
            alert_service.device_reconnected(
                ce_user_id,
                device_name=_device_name,
                group_id=_group_id,
                group_name=_group_name,
                status=_device_status,
            )
            return Response(status_code=204)

        base_url = _get_asset_base_url(request, settings)

        # Parse CloudEvents 1.0 ``ce-time`` header (RFC 3339).  This is
        # the Azure server time the event was produced — we use it as
        # the monotonic timestamp for persisted temperature-alert state
        # so out-of-order webhook deliveries don't overwrite newer
        # state with older data.
        received_at: datetime | None = None
        ce_time_raw = request.headers.get("ce-time")
        if ce_time_raw:
            try:
                # fromisoformat accepts ``+00:00`` but not ``Z`` until
                # 3.11; normalize just in case.
                received_at = datetime.fromisoformat(
                    ce_time_raw.replace("Z", "+00:00"),
                )
                if received_at.tzinfo is None:
                    received_at = received_at.replace(tzinfo=timezone.utc)
            except ValueError:
                logger.warning(
                    "Malformed ce-time header '%s' on WPS webhook for %s "
                    "(conn=%s); falling back to server now()",
                    ce_time_raw, ce_user_id, ce_connection_id,
                )
        else:
            logger.warning(
                "Missing ce-time header on WPS webhook for %s (conn=%s); "
                "falling back to server now()",
                ce_user_id, ce_connection_id,
            )

        ctx = InboundContext(
            device_id=ce_user_id,
            device=device,
            base_url=base_url,
            settings=settings,
            group_id=str(device.group_id) if device.group_id else None,
            device_name=device.name or ce_user_id,
            device_status=device.status.value if device.status else "pending",
            group_name="",
            received_at=received_at,
        )

        async def _send(payload: dict) -> None:
            await transport.send_to_device(ce_user_id, payload)

        await dispatch_device_message(msg=msg, ctx=ctx, db=db, send=_send)
        return Response(status_code=204)

    logger.warning("Unknown WPS event type %s (conn=%s)", ce_type, ce_connection_id)
    return Response(status_code=400, content=f"unknown event type: {ce_type}")
