"""Headless-Chromium snapshot renderer for composed slides + webpages (worker).

Two public renderers, both backed by a single shared lazy-singleton
Chromium process (a fresh isolated context+page per render so state never
leaks):

* :func:`render_composed_to_png` — renders the self-contained
  composed-slide HTML produced by
  :func:`cms.composed.render.build_composed_html` with **all network
  blocked**: only ``data:`` URIs (already-inlined images / videos /
  fonts) ever load, so a slide can never make the worker reach out to an
  arbitrary origin while it is being snapshotted. The weather widget's
  live Open-Meteo fetch is blocked too, so it renders its offline
  fallback in the thumbnail — acceptable for a static snapshot.

* :func:`render_url_to_png` — navigates to a live webpage URL and
  screenshots it, so network **must** be allowed. To keep this from
  becoming an SSRF sink, the target host is DNS-resolved and rejected if
  any resolved IP is private / loopback / link-local / reserved /
  multicast / unspecified (the link-local check covers the
  169.254.169.254 cloud-metadata endpoint), and every navigation request
  (including redirects) is re-validated by the route handler.

``process_pending`` renders variants one at a time, so there is no
concurrent access to a shared page.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

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


class WebpageRenderError(Exception):
    """Raised when a webpage URL can't be safely or successfully rendered."""


def _host_is_safe(host: str) -> bool:
    """Whether ``host`` is safe to navigate to (synchronous; DNS-resolves).

    Rejects empty hosts, ``localhost`` and ``*.local`` by name, then
    resolves the host and rejects it if ANY resolved IP is private,
    loopback, link-local (covers the 169.254.169.254 cloud-metadata
    endpoint), reserved, multicast, or unspecified. IPv4-mapped IPv6
    addresses are unwrapped before classification.

    Wrap in ``asyncio.to_thread`` from async code — ``getaddrinfo`` blocks.
    """
    if not host:
        return False
    lowered = host.strip().lower().rstrip(".")
    if not lowered or lowered == "localhost" or lowered.endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(lowered, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        raw_ip = sockaddr[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").strip()


async def render_url_to_png(url: str) -> bytes:
    """Render a live webpage URL to a 1920×1080 PNG.

    Unlike :func:`render_composed_to_png`, network is ALLOWED so the live
    page can load. SSRF defense: the target host (and every redirected
    navigation host) is DNS-resolved and rejected if it maps to a
    private / loopback / link-local / reserved / multicast / unspecified
    address. Only ``http``/``https`` schemes are permitted. Raises
    :class:`WebpageRenderError` for an unsafe / disallowed target; other
    exceptions propagate so the caller marks the variant failed.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise WebpageRenderError(f"unsupported scheme for webpage render: {scheme!r}")

    target_host = (parts.hostname or "").strip()
    if not await asyncio.to_thread(_host_is_safe, target_host):
        raise WebpageRenderError(f"refusing to render unsafe webpage host: {target_host!r}")

    # Per-render DNS-safety cache so the route handler doesn't re-resolve
    # the same host on every sub-request. Navigation requests (including
    # redirect targets) are re-validated to catch redirect-based SSRF.
    safe_hosts: dict[str, bool] = {target_host.lower(): True}

    browser = await _get_browser()
    context = await browser.new_context(
        viewport=_VIEWPORT,
        device_scale_factor=1,
        java_script_enabled=True,
    )
    try:
        page = await context.new_page()

        async def _guard(route):
            request = route.request
            if not request.is_navigation_request():
                # Sub-resources (images/css/fonts/xhr) are allowed through;
                # they can't redirect the top frame to an internal host.
                await route.continue_()
                return
            host = _host_of(request.url).lower()
            if not host:
                await route.abort()
                return
            ok = safe_hosts.get(host)
            if ok is None:
                ok = await asyncio.to_thread(_host_is_safe, host)
                safe_hosts[host] = ok
            if ok:
                await route.continue_()
            else:
                logger.warning(
                    "webpage render: blocked navigation to unsafe host %s", host,
                )
                await route.abort()

        await page.route("**/*", _guard)
        page.set_default_timeout(_NAV_TIMEOUT_MS)

        await page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)

        try:
            await asyncio.wait_for(
                page.evaluate(_READINESS_JS), timeout=_READY_TIMEOUT_MS / 1000,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "webpage snapshot readiness wait failed/timed out; "
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
