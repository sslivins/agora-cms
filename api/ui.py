import hmac
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from api.auth import (
    WebAuthRequired,
    clear_session,
    create_session,
    get_settings,
    require_web_auth,
)
from api.config import Settings
from shared.models import CurrentState, DesiredState
from shared.state import read_state

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    if hmac.compare_digest(username, settings.web_username) and hmac.compare_digest(
        password, settings.web_password
    ):
        response = RedirectResponse("/", status_code=303)
        create_session(response, username, settings)
        return response
    return templates.TemplateResponse(
        request,
        "login.html",
        context={"error": "Invalid credentials"},
        status_code=401,
    )


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    clear_session(response)
    return response


@router.get("/")
async def dashboard(
    request: Request,
    user: str = Depends(require_web_auth),
    settings: Settings = Depends(get_settings),
):
    current = read_state(settings.current_state_path, CurrentState)
    desired = read_state(settings.desired_state_path, DesiredState)
    asset_count = 0
    for subdir in [settings.videos_dir, settings.images_dir]:
        if subdir.exists():
            asset_count += sum(1 for f in subdir.iterdir() if f.is_file())
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "user": user,
            "current": current,
            "desired": desired,
            "asset_count": asset_count,
            "device_name": settings.device_name,
        },
    )


@router.get("/assets")
async def assets_page(
    request: Request,
    user: str = Depends(require_web_auth),
    settings: Settings = Depends(get_settings),
):
    from api.routers.assets import _list_assets

    assets = _list_assets(settings)
    return templates.TemplateResponse(
        request,
        "assets.html",
        context={"user": user, "assets": assets},
    )


@router.get("/playback")
async def playback_page(
    request: Request,
    user: str = Depends(require_web_auth),
    settings: Settings = Depends(get_settings),
):
    from api.routers.assets import _list_assets

    current = read_state(settings.current_state_path, CurrentState)
    assets = _list_assets(settings)
    return templates.TemplateResponse(
        request,
        "playback.html",
        context={"user": user, "current": current, "assets": assets},
    )
