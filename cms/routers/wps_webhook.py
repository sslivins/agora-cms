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

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_settings
from cms.config import Settings
from cms.database import get_db
from cms.models.device import Device
from cms.services.device_inbound import InboundContext, dispatch_device_message
from cms.services.device_manager import device_manager
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
        device_manager.register_remote(
            ce_user_id, connection_id=ce_connection_id, ip_address=None,
        )
        return Response(status_code=204)

    if ce_type == "azure.webpubsub.sys.disconnected":
        if ce_user_id:
            device_manager.disconnect(ce_user_id)
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

        # Look up the device row — dispatch_device_message wants a
        # fully-hydrated ORM instance on the context.  Stage 2b.2 does
        # NOT port the direct-WS register/adoption handshake over to the
        # webhook path, so if the device row is missing we log and drop.
        result = await db.execute(select(Device).where(Device.id == ce_user_id))
        device = result.scalar_one_or_none()
        if device is None:
            logger.warning(
                "WPS message from unknown device %s — registration over WPS "
                "not implemented yet (Stage 2b.2)", ce_user_id,
            )
            return Response(status_code=204)

        base_url = _get_asset_base_url(request, settings)
        ctx = InboundContext(
            device_id=ce_user_id,
            device=device,
            base_url=base_url,
            settings=settings,
            group_id=str(device.group_id) if device.group_id else None,
            device_name=device.name or ce_user_id,
            device_status=device.status.value if device.status else "pending",
            group_name="",
        )
        transport = get_transport()

        async def _send(payload: dict) -> None:
            await transport.send_to_device(ce_user_id, payload)

        await dispatch_device_message(msg=msg, ctx=ctx, db=db, send=_send)
        return Response(status_code=204)

    logger.warning("Unknown WPS event type %s (conn=%s)", ce_type, ce_connection_id)
    return Response(status_code=400, content=f"unknown event type: {ce_type}")
