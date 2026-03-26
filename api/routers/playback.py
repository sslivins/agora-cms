from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from api.auth import get_settings, require_auth
from api.config import Settings
from shared.models import DesiredState, PlaybackMode, PlayRequest
from shared.state import write_state

router = APIRouter(dependencies=[Depends(require_auth)])


def _resolve_asset(asset: str, settings: Settings) -> str:
    """Verify asset exists in any asset directory."""
    for subdir in [settings.videos_dir, settings.images_dir, settings.splash_dir]:
        if (subdir / asset).is_file():
            return asset
    raise HTTPException(status_code=404, detail=f"Asset not found: {asset}")


@router.post("/play")
async def play(body: PlayRequest, settings: Settings = Depends(get_settings)):
    _resolve_asset(body.asset, settings)
    state = DesiredState(
        mode=PlaybackMode.PLAY,
        asset=body.asset,
        loop=body.loop,
        timestamp=datetime.now(timezone.utc),
    )
    write_state(settings.desired_state_path, state)
    return {"status": "ok", "desired": state.model_dump(mode="json")}


@router.post("/stop")
async def stop(settings: Settings = Depends(get_settings)):
    state = DesiredState(
        mode=PlaybackMode.STOP,
        timestamp=datetime.now(timezone.utc),
    )
    write_state(settings.desired_state_path, state)
    return {"status": "ok", "desired": state.model_dump(mode="json")}


@router.post("/splash")
async def splash(settings: Settings = Depends(get_settings)):
    state = DesiredState(
        mode=PlaybackMode.SPLASH,
        timestamp=datetime.now(timezone.utc),
    )
    write_state(settings.desired_state_path, state)
    return {"status": "ok", "desired": state.model_dump(mode="json")}
