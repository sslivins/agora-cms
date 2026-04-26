"""Tests for normalize_pairing_secret() in cms.services.device_bootstrap.

The device generates an 8-char Crockford base32 code and shows it on
screen as ``XXXX-XXXX``.  Admins can type/paste it back in any
combination of case, with or without separators, and with the usual
confusable substitutes.  This normalizer canonicalizes all those forms
to the exact 8 uppercase chars the device sent on /register so the
sha256 lookup matches.
"""

from __future__ import annotations

from cms.services.device_bootstrap import normalize_pairing_secret


class TestNormalizePairingSecret:
    def test_canonical_8char_passes_through(self):
        assert normalize_pairing_secret("7K3Q4M2P") == "7K3Q4M2P"

    def test_lowercase_uppercased(self):
        assert normalize_pairing_secret("7k3q4m2p") == "7K3Q4M2P"

    def test_hyphenated_stripped(self):
        assert normalize_pairing_secret("7K3Q-4M2P") == "7K3Q4M2P"

    def test_lowercase_hyphenated(self):
        assert normalize_pairing_secret("7k3q-4m2p") == "7K3Q4M2P"

    def test_spaces_stripped(self):
        assert normalize_pairing_secret("7K3Q 4M2P") == "7K3Q4M2P"

    def test_outer_whitespace_stripped(self):
        assert normalize_pairing_secret("  7K3Q-4M2P\n") == "7K3Q4M2P"

    def test_crockford_i_to_one(self):
        # Admin types 'I' instead of '1' (visually identical in many fonts).
        assert normalize_pairing_secret("ABCDEF1I") == "ABCDEF11"
        assert normalize_pairing_secret("abcdef-1i") == "ABCDEF11"

    def test_crockford_l_to_one(self):
        assert normalize_pairing_secret("ABCDEFL1") == "ABCDEF11"

    def test_crockford_o_to_zero(self):
        assert normalize_pairing_secret("ABCDEF0O") == "ABCDEF00"

    def test_long_legacy_secret_passes_through(self):
        # 26-char legacy / future-format secrets must NOT be Crockford-mangled.
        legacy = "JBSWY3DPEHPK3PXPABCDEFGHIJ"
        assert len(legacy) == 26
        assert normalize_pairing_secret(legacy) == legacy

    def test_long_legacy_with_O_and_I_passes_through(self):
        # Legacy RFC-4648 base32 contains O and I; must not be touched.
        legacy = "OOOIIIJBSWY3DPEHPK3PXPABCD"
        assert len(legacy) == 26
        assert normalize_pairing_secret(legacy) == legacy

    def test_8_chars_with_invalid_chars_returns_stripped(self):
        # Looks like a short code by length but has out-of-alphabet chars
        # after substitution.  Don't pretend it's canonical; let lookup miss.
        out = normalize_pairing_secret("ABCD@EFG")
        # Whitespace stripped, but stays as-is (no alphabet match).
        assert out == "ABCD@EFG"

    def test_non_string_input_passes_through(self):
        assert normalize_pairing_secret(None) is None  # type: ignore[arg-type]

    def test_empty_string(self):
        assert normalize_pairing_secret("") == ""

    def test_short_input_below_8_chars(self):
        # Too short to be a short code; return stripped unchanged.
        assert normalize_pairing_secret("abc") == "abc"
