"""Client for the Mailpit HTTP API.

Mailpit is a maintained successor to MailHog. It captures all SMTP traffic
and exposes it via a JSON API at http://<host>:8025/api/v1/.

Docs: https://github.com/axllent/mailpit/wiki/API-v1
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class MailpitMessage:
    id: str
    subject: str
    from_addr: str
    to: list[str]
    text: str
    html: str

    def find_link(self, pattern: str = r"https?://\S+") -> Optional[str]:
        """Return the first URL in the message body matching pattern."""
        for body in (self.html, self.text):
            if not body:
                continue
            match = re.search(pattern, body)
            if match:
                return match.group(0).rstrip('">\').,')
        return None


class MailpitClient:
    """Minimal wrapper over the Mailpit HTTP API."""

    def __init__(self, base_url: str, *, timeout: float = 5.0):
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self._base, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MailpitClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def list_messages(self) -> list[dict]:
        r = self._client.get("/api/v1/messages")
        r.raise_for_status()
        return r.json().get("messages", [])

    def get_message(self, message_id: str) -> MailpitMessage:
        r = self._client.get(f"/api/v1/message/{message_id}")
        r.raise_for_status()
        data = r.json()
        return MailpitMessage(
            id=data["ID"],
            subject=data.get("Subject", ""),
            from_addr=(data.get("From") or {}).get("Address", ""),
            to=[t.get("Address", "") for t in (data.get("To") or [])],
            text=data.get("Text", "") or "",
            html=data.get("HTML", "") or "",
        )

    def delete_all(self) -> None:
        """Empty the mailbox (useful between tests)."""
        self._client.delete("/api/v1/messages").raise_for_status()

    def wait_for_email(
        self,
        *,
        to: Optional[str] = None,
        subject_contains: Optional[str] = None,
        timeout: float = 10.0,
        poll_interval: float = 0.25,
    ) -> MailpitMessage:
        """Block until a matching email arrives or timeout expires."""
        deadline = time.monotonic() + timeout
        last_seen = 0
        while time.monotonic() < deadline:
            messages = self.list_messages()
            for m in messages:
                if to and not any(
                    (t.get("Address") or "").lower() == to.lower()
                    for t in (m.get("To") or [])
                ):
                    continue
                if subject_contains and subject_contains.lower() not in (
                    m.get("Subject", "").lower()
                ):
                    continue
                return self.get_message(m["ID"])
            last_seen = len(messages)
            time.sleep(poll_interval)
        raise TimeoutError(
            f"No matching email in Mailpit after {timeout}s "
            f"(to={to}, subject_contains={subject_contains}, total_messages={last_seen})"
        )

    def is_ready(self) -> bool:
        try:
            return self._client.get("/readyz").status_code == 200
        except httpx.HTTPError:
            return False
