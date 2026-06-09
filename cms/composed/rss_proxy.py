"""SSRF-guarded RSS/Atom feed proxy for the composed-slide RSS widget.

The RSS widget renders a self-contained device bundle that fetches its
headlines *at runtime* from this proxy (mirroring how the weather widget
fetches Open-Meteo at runtime).  The device cannot fetch arbitrary feeds
directly: the bundle is served from the device's own local shell HTTP
server, so a cross-origin ``fetch()`` of a third-party feed is browser
CORS-blocked (most feeds send no ``Access-Control-Allow-Origin``).  This
proxy fetches + parses the feed server-side and returns CORS-enabled
JSON the device can read.

Threat model & guard
--------------------
This endpoint is **unauthenticated** in v1 (the device has no easy way
to authenticate a runtime ``fetch`` from inside the sandboxed bundle
iframe, exactly like the weather widget's keyless Open-Meteo call).  To
keep an unauthenticated outbound fetcher from becoming an SSRF pivot we:

* allow only ``http`` / ``https`` schemes;
* resolve the target host via :func:`socket.getaddrinfo` and reject the
  request if *any* resolved address is private, loopback, link-local,
  reserved, multicast or unspecified (this blocks ``127.0.0.1``, RFC1918,
  ``169.254.169.254`` cloud-metadata, ``::1``, ``fc00::/7`` etc.);
* disable httpx auto-redirects and follow them manually, re-resolving and
  re-validating every hop (max :data:`_MAX_REDIRECTS`);
* cap the response body size and the total request time;
* parse the payload strictly as RSS/Atom XML via the stdlib
  :mod:`xml.etree.ElementTree` and emit only a small, fixed JSON shape.

Residual risk: classic TOCTOU DNS-rebinding (the IP we validate may
differ from the IP httpx ultimately connects to).  Accepted for a v1 dev
feature given the RSS-only parse, the size/time caps, and that the only
data exfiltrated would be the parsed-XML projection of a URL the caller
already controls.  Hardening (pin the validated IP into the connection)
is a documented follow-up.

This module is intentionally free of any FastAPI import so it can be
unit-tested in isolation; the router maps :class:`RssProxyError` to HTTP
status codes.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx

# Tunables (conservative defaults; v1 dev).
_REQUEST_TIMEOUT_S = 8.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB
_MAX_REDIRECTS = 3
_DEFAULT_ITEM_COUNT = 10
_MAX_ITEM_COUNT = 30
_ALLOWED_SCHEMES = ("http", "https")
_USER_AGENT = "agora-cms-rss-proxy/1.0 (+composed-slide widget)"

# Child element local-names we treat as a single feed entry.
_ITEM_TAGS = ("item", "entry")
_TITLE_TAGS = ("title",)
_LINK_TAGS = ("link",)
# RSS uses pubDate; Atom uses published/updated; Dublin Core uses date.
_DATE_TAGS = ("pubdate", "published", "updated", "date")


class RssProxyError(Exception):
    """A feed fetch/parse failure with an HTTP status the router echoes.

    ``status_code`` is the status the proxy route should return;
    ``detail`` is a short, user-safe message (no internal host/IP leak).
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _localname(tag: str) -> str:
    """Strip any ``{namespace}`` prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1].lower()


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_target(url: str) -> None:
    """Raise :class:`RssProxyError` if ``url`` is unsafe to fetch.

    Validates scheme, then resolves the host and rejects the request if
    any resolved IP is in a blocked range.  Called for every redirect
    hop, not just the initial URL.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise RssProxyError(400, "Only http and https feed URLs are allowed")
    host = parsed.hostname
    if not host:
        raise RssProxyError(400, "Feed URL is missing a host")

    # A bare IP literal: validate it directly (getaddrinfo would echo it
    # back anyway, but this is clearer and avoids a needless lookup).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(literal):
            raise RssProxyError(400, "Feed host is not allowed")
        return

    try:
        infos = socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise RssProxyError(400, "Feed host could not be resolved") from exc
    if not infos:
        raise RssProxyError(400, "Feed host could not be resolved")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            # An address family we can't reason about — refuse rather
            # than risk fetching something we couldn't validate.
            raise RssProxyError(400, "Feed host is not allowed")
        if _ip_is_blocked(ip):
            raise RssProxyError(400, "Feed host is not allowed")


def clamp_item_count(raw: int | None) -> int:
    """Clamp a requested item count into the supported range."""
    if raw is None:
        return _DEFAULT_ITEM_COUNT
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_ITEM_COUNT
    return max(1, min(_MAX_ITEM_COUNT, n))


def parse_feed(body: bytes, *, count: int) -> list[dict[str, str]]:
    """Parse RSS/Atom ``body`` into a list of ``{title, link, pubDate}``.

    Namespace-agnostic (works for RSS 2.0, RSS 1.0/RDF and Atom).  Items
    missing a title are skipped; ``link`` / ``pubDate`` are best-effort
    and omitted when absent.  Returns at most ``count`` entries.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise RssProxyError(502, "Feed is not valid XML") from exc

    items: list[ET.Element] = [
        el for el in root.iter() if _localname(el.tag) in _ITEM_TAGS
    ]

    out: list[dict[str, str]] = []
    for item in items:
        title: str | None = None
        link: str | None = None
        date: str | None = None
        for child in item:
            ln = _localname(child.tag)
            if title is None and ln in _TITLE_TAGS:
                title = (child.text or "").strip() or None
            elif link is None and ln in _LINK_TAGS:
                # RSS: link text. Atom: <link href="..."/> (prefer the
                # alternate/href attribute, fall back to text).
                href = child.get("href")
                link = (href or (child.text or "").strip()) or None
            elif date is None and ln in _DATE_TAGS:
                date = (child.text or "").strip() or None
        if not title:
            continue
        entry: dict[str, str] = {"title": title}
        if link:
            entry["link"] = link
        if date:
            entry["pubDate"] = date
        out.append(entry)
        if len(out) >= count:
            break
    return out


async def fetch_feed_items(url: str, *, count: int) -> list[dict[str, str]]:
    """Fetch ``url`` (SSRF-guarded) and return parsed feed items.

    Raises :class:`RssProxyError` on any scheme/host/size/timeout/parse
    failure with an HTTP status the caller can return verbatim.
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5"}
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT_S, follow_redirects=False
        ) as client:
            for _hop in range(_MAX_REDIRECTS + 1):
                _validate_target(current)
                async with client.stream("GET", current, headers=headers) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise RssProxyError(502, "Feed redirect was malformed")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise RssProxyError(
                            502, f"Feed returned HTTP {resp.status_code}"
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_RESPONSE_BYTES:
                            raise RssProxyError(502, "Feed response is too large")
                        chunks.append(chunk)
                    return parse_feed(b"".join(chunks), count=count)
            raise RssProxyError(502, "Feed had too many redirects")
    except httpx.TimeoutException as exc:
        raise RssProxyError(504, "Feed request timed out") from exc
    except httpx.HTTPError as exc:
        raise RssProxyError(502, "Feed could not be fetched") from exc
