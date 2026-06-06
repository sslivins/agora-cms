"""Headless-Chromium snapshot renderer for composed slides (worker side).

Renders the self-contained composed-slide HTML — produced by
:func:`cms.composed.render.build_composed_html` — to a PNG using
Playwright's bundled Chromium. All network is blocked: only ``data:``
URIs (the already-inlined images / videos / fonts) ever load, so a slide
can never make the worker reach out to an arbitrary origin while it is
being snapshotted. The weather widget's live Open-Meteo fetch is blocked
too, so it renders its offline fallback in the thumbnail — acceptable for
a static snapshot.

A single Chromium process is reused for the life of the worker (lazy
singleton); every render gets a fresh, isolated browser context + page so
state never leaks between slides. ``process_pending`` renders variants
one at a time, so there is no concurrent access to a shared page.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("agora.worker.composed_render")

_VIEWPORT = {"width": 1920, "height": 1080}
_NAV_TIMEOUT_MS = 15000
_READY_TIMEOUT_MS = 8000
# Short settle after readiness so late CSS transitions / clock paints land
# before the screenshot.
_SETTLE_MS = 200

# Lazy singletons — created on first render, reused thereafter.
_pw = None
_browser = None
_browser_lock = asyncio.Lock()

# Wait until web fonts are ready and every <img>/<video> has either loaded
# or errored, so the snapshot isn't taken mid-load. Each element has its
# own bounded fallback so a single stuck resource can't hang the render
# past the outer page timeout.
_READINESS_JS = """
async () => {
  if (document.fonts && document.fonts.ready) {
    try { await document.fonts.ready; } catch (e) {}
  }
  const imgs = Array.from(document.images || []);
  await Promise.all(imgs.map(img => {
    if (img.complete && img.naturalWidth > 0) return Promise.resolve();
    return new Promise(res => {
      img.addEventListener('load', res, {once: true});
      img.addEventListener('error', res, {once: true});
      setTimeout(res, 3000);
    });
  }));
  const vids = Array.from(document.querySelectorAll('video'));
  await Promise.all(vids.map(v => {
    if (v.readyState >= 2 || v.error) return Promise.resolve();
    return new Promise(res => {
      v.addEventListener('loadeddata', res, {once: true});
      v.addEventListener('error', res, {once: true});
      setTimeout(res, 3000);
    });
  }));
}
"""


async def _get_browser():
    """Return the shared Chromium instance, launching it on first use."""
    global _pw, _browser
    if _browser is not None and _browser.is_connected():
        return _browser
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        from playwright.async_api import async_playwright

        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--force-color-profile=srgb",
                "--hide-scrollbars",
            ],
        )
        logger.info("Launched headless Chromium for composed-slide snapshots")
        return _browser


async def render_composed_to_png(html_bytes: bytes) -> bytes:
    """Render self-contained composed-slide HTML to a 1920×1080 PNG.

    Blocks all network (only inline ``data:`` URIs load), waits for fonts
    and media to settle, then screenshots the full canvas. Raises on a
    hard render failure (browser launch / navigation timeout) — the caller
    marks the variant failed.
    """
    html = html_bytes.decode("utf-8")
    browser = await _get_browser()
    context = await browser.new_context(
        viewport=_VIEWPORT,
        device_scale_factor=1,
        java_script_enabled=True,
    )
    try:
        page = await context.new_page()

        async def _block(route):
            # Inline data: URIs are resolved internally and rarely hit this
            # handler; everything else (http/https/file/blob/ws) is aborted
            # so the snapshot render is fully offline and side-effect free.
            url = route.request.url
            if url.startswith("data:"):
                await route.continue_()
            else:
                await route.abort()

        await page.route("**/*", _block)
        page.set_default_timeout(_NAV_TIMEOUT_MS)

        await page.set_content(html, wait_until="load", timeout=_NAV_TIMEOUT_MS)

        try:
            await asyncio.wait_for(
                page.evaluate(_READINESS_JS), timeout=_READY_TIMEOUT_MS / 1000,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "composed snapshot readiness wait failed/timed out; "
                "screenshotting current state", exc_info=True,
            )

        await page.wait_for_timeout(_SETTLE_MS)

        return await page.screenshot(
            type="png",
            clip={"x": 0, "y": 0, "width": 1920, "height": 1080},
        )
    finally:
        await context.close()


async def shutdown() -> None:
    """Tear down the shared browser (best-effort) on worker shutdown."""
    global _pw, _browser
    try:
        if _browser is not None:
            await _browser.close()
    except Exception:  # noqa: BLE001
        logger.debug("composed render browser close failed", exc_info=True)
    finally:
        _browser = None
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:  # noqa: BLE001
        logger.debug("composed render playwright stop failed", exc_info=True)
    finally:
        _pw = None
