"""Log collection API.

New async flow (see sslivins/agora-cms#345 Stage 3):

* ``POST   /api/logs/requests``                  — enqueue per-device
* ``GET    /api/logs/requests/{id}``             — poll status
* ``GET    /api/logs/requests/{id}/download``    — download bundle
* ``GET    /api/cms/logs``                       — CMS in-memory log buffer

The legacy synchronous ``POST /api/logs/download`` endpoint was removed
on 2026-04-22.  It did a blocking ``request_logs`` call per device
which cannot work under N>1 CMS replicas (the WS target may live on
a different replica than the one handling the HTTP request).  The UI
has migrated to the async outbox flow and client-side zip bundling,
so the endpoint was deleted with no bake period.
"""

import io
import logging
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_auth, require_permission
from cms.database import get_db
from cms.permissions import LOGS_READ
from cms.services.audit_service import audit_log

logger = logging.getLogger("agora.cms.logs")

router = APIRouter(prefix="/api/logs", dependencies=[Depends(require_auth)])

# Separate router for ``/api/cms/logs`` so it's not nested under ``/api/logs``.
cms_logs_router = APIRouter(prefix="/api/cms", dependencies=[Depends(require_auth)])


# ── CMS-only log download (new async UI flow) ───────────────────────

@cms_logs_router.get(
    "/logs",
    dependencies=[Depends(require_permission(LOGS_READ))],
)
async def download_cms_logs(request: Request, db: AsyncSession = Depends(get_db)):
    """Download the CMS in-memory log buffer as a zip.

    Small, synchronous endpoint used by the new UI flow to fetch CMS
    logs separately from per-device log bundles.  Does not reach
    across the network to devices, so it is safe to call on any
    replica.

    Replica-local caveat: under any multi-replica deployment behind a
    load balancer (including Azure Container Apps), the load balancer
    routes each HTTP request to an arbitrary replica, so the returned
    buffer reflects only that replica's recent logs.  In single-replica
    deployments this is the full CMS log buffer.  For a complete view
    across all replicas in a multi-replica deployment, use the Azure
    Log Analytics workspace (container stdout is shipped there).
    """
    from cms.main import _log_buffer

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("cms/cms.log", "\n".join(_log_buffer))
    buf.seek(0)

    filename = f"agora-cms-logs-{timestamp}.zip"
    user = getattr(request.state, "user", None)
    await audit_log(
        db, user=user,
        action="logs.download_cms", resource_type="logs",
        description="Downloaded CMS log buffer",
        request=request,
    )
    await db.commit()
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
