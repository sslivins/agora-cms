"""Shared imager catalog helpers.

The CMS API (PR 4) and the worker handlers (PR 3) both need to:

* enforce a hostname allowlist on every URL we hit — *including*
  every redirect Location — so a malicious catalog cannot point us at
  an internal address;
* parse the upstream catalog.json on demand.

Originally these helpers lived inside ``worker/imager_handlers.py``.
They were moved here so the API layer can resolve a catalog entry at
enqueue time (stamping ``source_url`` + ``expected_sha256`` onto the
``BaseImage`` row) without importing worker internals or reaching for
the upstream catalog twice.

This module is deliberately narrow: pure-async URL/IO helpers, no DB
state, no sleep loops, no logging side effects beyond what httpx
emits internally.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx


# Hard ceiling on how many redirect hops we will walk while validating
# every Location against the allowlist.  GitHub-Releases->blob normally
# resolves in <=2 hops.
MAX_REDIRECTS = 5

# Default per-call total HTTP timeout (catalog + download both).
HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=15.0)


class CatalogError(Exception):
    """Deterministic catalog-fetch failure -- do not retry.

    Raised for allowlist violations, non-https URLs, malformed JSON,
    or oversized redirect chains.  Callers translate this into either
    a 400/422 (API layer) or a TerminalImagerError (worker layer).
    """


def parse_allowed_hosts(raw: str | None) -> set[str]:
    """Split a comma-separated host list into a normalized set."""
    raw = (raw or "").strip()
    if not raw:
        return set()
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def validate_url(url: str, allowlist: set[str]) -> str:
    """Return ``url`` iff its scheme is https and host is allowlisted.

    Raises :class:`CatalogError` otherwise.  Centralised so the same
    rule applies to the catalog URL **and** every redirect target
    while streaming an image.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise CatalogError(
            f"refusing non-https url for imager fetch: scheme={parsed.scheme!r}"
        )
    host = (parsed.hostname or "").lower()
    if host not in allowlist:
        raise CatalogError(
            f"host {host!r} not in base_image_allowed_hosts ({sorted(allowlist)})"
        )
    return url


async def fetch_catalog(
    catalog_url: str,
    allowlist: set[str],
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Resolve + parse the upstream ``catalog.json``.

    Returns the parsed JSON dict.  Raises :class:`CatalogError` on
    allowlist violation, non-JSON body, or excessive redirects.
    Re-raises generic :mod:`httpx` exceptions on transient network
    errors so callers can decide whether to retry.
    """
    validate_url(catalog_url, allowlist)
    resp = await client.get(catalog_url, follow_redirects=False)
    hops = 0
    while resp.is_redirect and hops < MAX_REDIRECTS:
        loc = resp.headers.get("location")
        if not loc:
            raise httpx.RemoteProtocolError("redirect without Location")
        validate_url(loc, allowlist)
        resp = await client.get(loc, follow_redirects=False)
        hops += 1
    if resp.is_redirect:
        raise CatalogError(
            f"too many redirects ({MAX_REDIRECTS}) fetching catalog"
        )
    resp.raise_for_status()
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise CatalogError(f"catalog is not valid json: {e}") from e
