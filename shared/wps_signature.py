"""Azure Web PubSub CloudEvents webhook signature helpers.

Azure signs its upstream webhook requests with
``ce-signature: sha256=<hex>,sha256=<hex>`` where each entry is
``hex(HMAC_SHA256(access_key, connection_id))`` — note: the connection
ID is signed, **not** the body.  Multiple entries support primary +
secondary key rotation; a request is valid if any entry matches any
configured key.

See: https://learn.microsoft.com/azure/azure-web-pubsub/reference-cloud-events
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Iterable


def sign_connection_id(connection_id: str, keys: Iterable[str]) -> str:
    """Return a ``ce-signature`` header value.

    ``keys`` may be a single-element iterable (emit one signature) or a
    list of primary+secondary (emit a comma-separated multi-sig).
    """
    parts: list[str] = []
    payload = connection_id.encode("utf-8")
    for k in keys:
        digest = hmac.new(k.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        parts.append(f"sha256={digest}")
    if not parts:
        raise ValueError("at least one key is required")
    return ",".join(parts)


def verify_signature(
    connection_id: str, header_value: str, keys: Iterable[str]
) -> bool:
    """Return ``True`` if any entry in ``header_value`` matches any key.

    ``header_value`` is the raw ``ce-signature`` header contents
    (comma-separated ``sha256=<hex>`` entries).  Comparison uses
    ``hmac.compare_digest`` to avoid timing leaks.
    """
    if not header_value:
        return False
    presented: list[str] = []
    for entry in header_value.split(","):
        entry = entry.strip()
        if not entry.lower().startswith("sha256="):
            continue
        presented.append(entry[len("sha256="):])
    if not presented:
        return False
    payload = connection_id.encode("utf-8")
    for k in keys:
        want = hmac.new(k.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        for got in presented:
            if hmac.compare_digest(want, got):
                return True
    return False
