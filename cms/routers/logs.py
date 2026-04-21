"""Log collection API — request device logs and download as zip."""

import asyncio
import io
import logging
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_auth, require_permission, get_user_group_ids
from cms.database import get_db
from cms.permissions import LOGS_READ
from cms.models.device import Device
from cms.services.transport import get_transport
from cms.services.audit_service import audit_log

logger = logging.getLogger("agora.cms.logs")

router = APIRouter(prefix="/api/logs", dependencies=[Depends(require_auth)])


class LogDownloadRequest(BaseModel):
    device_ids: list[str] = []
    include_cms: bool = True
    services: list[str] | None = None
    since: str = "24h"


@router.post("/download", dependencies=[Depends(require_permission(LOGS_READ))])
async def download_logs(req: LogDownloadRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Collect logs from selected devices (+ optionally CMS) and return a zip file."""
    # Validate the requesting user has group access to every requested device
    user = getattr(request.state, "user", None)
    if user and req.device_ids:
        group_ids = await get_user_group_ids(user, db)
        if group_ids is not None:  # None = admin
            result = await db.execute(
                select(Device.id).where(
                    Device.id.in_(req.device_ids),
                    Device.group_id.notin_(group_ids) if group_ids else True,
                )
            )
            forbidden = [r[0] for r in result.all()]
            if forbidden:
                raise HTTPException(
                    status_code=403,
                    detail=f"Not authorised for device(s): {', '.join(forbidden)}",
                )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # ── Device logs ──
        tasks = {}
        for device_id in req.device_ids:
            if get_transport().is_connected(device_id):
                tasks[device_id] = get_transport().request_logs(
                    device_id,
                    services=req.services,
                    since=req.since,
                    timeout=30.0,
                )

        results = {}
        if tasks:
            gathered = await asyncio.gather(
                *tasks.values(), return_exceptions=True,
            )
            results = dict(zip(tasks.keys(), gathered))

        for device_id in req.device_ids:
            if device_id not in tasks:
                # Device not connected — add a note
                zf.writestr(
                    f"{device_id}/not_connected.txt",
                    f"Device {device_id} was not connected at the time of log collection.",
                )
                continue

            result = results[device_id]
            if isinstance(result, Exception):
                zf.writestr(
                    f"{device_id}/error.txt",
                    f"Failed to collect logs: {result}",
                )
            else:
                for service_name, log_text in result.items():
                    safe_name = service_name.replace("/", "_").replace("\\", "_")
                    zf.writestr(f"{device_id}/{safe_name}.log", log_text)

        # ── CMS logs ──
        if req.include_cms:
            from cms.main import _log_buffer
            cms_log_text = "\n".join(_log_buffer)
            zf.writestr("cms/cms.log", cms_log_text)

    buf.seek(0)
    filename = f"agora-logs-{timestamp}.zip"
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="logs.download", resource_type="logs",
        description=f"Downloaded logs ({len(req.device_ids)} device(s), cms={req.include_cms})",
        details={
            "device_ids": list(req.device_ids),
            "include_cms": req.include_cms,
            "services": req.services,
            "since": req.since,
        },
        request=request,
    )
    await db.commit()
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
