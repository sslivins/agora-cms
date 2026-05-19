"""Regression test for SMTP From-header rendering with a configurable display
name.

The bug fixed alongside this test: ``cms/services/email_service.py``
hardcoded ``msg["From"] = smtp_cfg["from_email"]``, so the welcome email
shipped with just the bare address as the sender. We now read a separate
``from_name`` setting and emit ``"Display Name" <addr@host>`` when it's set.

What this test guards:

1. ``from_name`` set       -> header is ``"Pretty Name" <bare@addr>``.
2. ``from_name`` missing   -> header is the bare email (back-compat).
3. ``from_name`` only-whitespace -> treated as missing.
4. ``from_name`` with CR/LF -> CR/LF stripped before formatting (defense
   in depth against header injection -- ``email.policy.compat32`` does not
   detect CRLF, and even though the setting is admin-only, scrubbing it is
   one line of code).
5. SMTP envelope-from (the first arg to ``server.sendmail``) is always the
   bare email -- some relays reject angle-bracketed forms in MAIL FROM.
"""

from unittest.mock import MagicMock, patch

from cms.services.email_service import _send_email


def _base_cfg(**overrides):
    cfg = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "user",
        "password": "pass",
        "from_email": "noreply@example.com",
        "from_name": None,
        "use_tls": True,
    }
    cfg.update(overrides)
    return cfg


def _send_and_capture(cfg):
    """Invoke _send_email with a mocked smtplib server; return the (server,
    sent_message) pair so the caller can assert on either side."""
    with patch("cms.services.email_service.smtplib.SMTP") as smtp_cls:
        server = MagicMock()
        smtp_cls.return_value = server
        ok, err = _send_email(cfg, "to@example.com", "subj", "<p>html</p>", "text")
    assert ok, f"_send_email failed: {err!r}"
    # server.sendmail(envelope_from, [to], rfc822_string)
    assert server.sendmail.called, "sendmail was never called"
    envelope_from, _recipients, rfc822 = server.sendmail.call_args.args
    return envelope_from, rfc822


def _from_header(rfc822: str) -> str:
    """Extract the From: header value from a serialized MIME message."""
    for line in rfc822.splitlines():
        if line.lower().startswith("from:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no From header in message:\n{rfc822}")


def test_from_header_uses_name_when_configured():
    envelope_from, rfc822 = _send_and_capture(
        _base_cfg(from_name="Goodwill Digital Signage")
    )
    assert _from_header(rfc822) == 'Goodwill Digital Signage <noreply@example.com>'
    # Envelope must stay bare per RFC 5321.
    assert envelope_from == "noreply@example.com"


def test_from_header_is_bare_when_name_missing():
    envelope_from, rfc822 = _send_and_capture(_base_cfg(from_name=None))
    assert _from_header(rfc822) == "noreply@example.com"
    assert envelope_from == "noreply@example.com"


def test_from_header_is_bare_when_name_is_empty_string():
    envelope_from, rfc822 = _send_and_capture(_base_cfg(from_name=""))
    assert _from_header(rfc822) == "noreply@example.com"
    assert envelope_from == "noreply@example.com"


def test_from_header_is_bare_when_name_is_whitespace_only():
    envelope_from, rfc822 = _send_and_capture(_base_cfg(from_name="   "))
    assert _from_header(rfc822) == "noreply@example.com"
    assert envelope_from == "noreply@example.com"


def test_from_name_strips_crlf_to_prevent_header_injection():
    # Classic CRLF injection attempt: try to smuggle a Bcc into the message.
    # With CR/LF scrubbed, the whole malicious string becomes a single quoted
    # display name -- never breaks out into its own header.
    import email as email_pkg
    envelope_from, rfc822 = _send_and_capture(
        _base_cfg(from_name="Evil\r\nBcc: attacker@example.com")
    )
    parsed = email_pkg.message_from_string(rfc822)
    # No injected header reached the parsed message:
    assert parsed.get("Bcc") is None, f"Bcc header was injected: {parsed.get_all('Bcc')!r}"
    assert parsed.get_all("To") == ["to@example.com"], (
        f"unexpected To headers: {parsed.get_all('To')!r}"
    )
    # The From header itself must be a single logical line -- the parser
    # normalises folding, so getting one string back means no CRLF leak.
    from_hdr = parsed.get("From") or ""
    assert "\r" not in from_hdr and "\n" not in from_hdr, (
        f"raw CRLF leaked into From header: {from_hdr!r}"
    )
    # And the address part must still parse correctly.
    from email.utils import parseaddr
    _name, addr = parseaddr(from_hdr)
    assert addr == "noreply@example.com", f"address corrupted: {from_hdr!r}"
    assert envelope_from == "noreply@example.com"


def test_from_name_with_special_chars_is_quoted_correctly():
    # formataddr should quote a name containing commas or quotes per RFC 5322.
    _envelope, rfc822 = _send_and_capture(
        _base_cfg(from_name='Smith, John "JS"')
    )
    header = _from_header(rfc822)
    assert header.endswith("<noreply@example.com>")
    # The address part must still be parseable -- the display name is what
    # gets quoted; the bracketed address must be intact.
    assert "<noreply@example.com>" in header
