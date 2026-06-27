"""Layer 3 of the composed-thumbnail hardening: a *real* end-to-end render.

Every other composed-thumbnail test mocks ``build_composed_html`` /
``render_composed_to_png`` and runs in the CMS environment where the full
web stack is installed. That is exactly why the ``No module named
'fastapi'`` worker regression (and the markupsafe one before it) sailed
through CI: nothing actually executed the real render chain end-to-end the
way the worker does.

This test reproduces the worker's exact call sequence
(``worker/transcoder.py::_render_composed_thumbnail``):

    settings_shim = SimpleNamespace(asset_storage_path=<dir>)
    rendered = await build_composed_html(db, settings_shim, asset_id)
    png = await render_composed_to_png(rendered.html_bytes)

and asserts the output is a real, non-trivial PNG. It needs Playwright +
Chromium, so it is marked ``@pytest.mark.thumbnail`` and runs in its own
``composed-thumbnail-render`` CI job (wired into ci-gate) rather than the
fast unit shards. If a CMS-only dependency creeps onto the render chain,
this fails loudly with the real traceback instead of letting every
composed thumbnail break silently in the field.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from cms.composed.render import build_composed_html
from cms.composed.schema import Cell, WidgetInstance, empty_layout
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide

pytestmark = [pytest.mark.thumbnail, pytest.mark.asyncio]

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _text_layout(text: str = "hello thumbnail"):
    layout = empty_layout()
    layout.widgets.append(
        WidgetInstance(
            id=uuid.uuid4(),
            type="text",
            cell=Cell(row=1, col=1, rowspan=2, colspan=6),
            config={"text": text, "font_size_px": 96, "color": "#ffffff"},
            config_version=1,
        )
    )
    return layout


async def _make_composed(db_session, layout):
    asset = Asset(
        id=uuid.uuid4(),
        filename=f"composed-{uuid.uuid4()}",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.flush()
    db_session.add(
        ComposedSlide(
            asset_id=asset.id,
            layout_json=layout.model_dump(mode="json"),
            is_draft=False,
        )
    )
    await db_session.commit()
    return asset


async def test_composed_thumbnail_real_render_produces_png(db_session, tmp_path):
    """The worker's exact render sequence yields a valid, non-empty PNG.

    Runs the real ``build_composed_html`` + ``render_composed_to_png`` —
    no mocks — so any broken import on the render chain (the recurring
    worker ``No module named 'fastapi'`` class of bug) surfaces here.
    """
    asset = await _make_composed(db_session, _text_layout())

    # Mirror worker/transcoder.py::_render_composed_thumbnail exactly.
    from worker.composed_render import render_composed_to_png

    settings_shim = SimpleNamespace(asset_storage_path=str(tmp_path))
    rendered = await build_composed_html(db_session, settings_shim, asset.id)
    assert rendered.html_bytes, "build_composed_html returned empty HTML"

    png_bytes = await render_composed_to_png(rendered.html_bytes)

    assert png_bytes[:8] == _PNG_MAGIC, "render output is not a PNG"
    # A 1920×1080 rendered slide is well over 1 KB; a near-empty PNG would
    # signal a blank/failed render even if the magic bytes happen to match.
    assert len(png_bytes) > 1000, f"PNG suspiciously small ({len(png_bytes)} bytes)"
