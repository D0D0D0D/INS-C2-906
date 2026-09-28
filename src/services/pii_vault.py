"""AgentCore Platform v1.0 — INS-C2-906 request-scoped PII tokenization"""

# docs/02_design.md §6-1 — API adapter layer (src/api/server.py) tokenizes
# direct identifiers (name, contact, address, policy number, personally
# identifying medical identifiers) BEFORE the graph ever sees the payload.
# Tokens are request-scoped (in-memory map) and never persisted; resolve
# (token -> real value) happens ONLY in server.py's response formatting —
# no node or graph return value may resolve a token (hard invariant,
# design §6-1 / organizational precedent, see reference memory
# "Cat2 sensitive data must never resolve inside the graph").
#
# This module is intentionally graph-independent: it is imported only by
# src/api/server.py and by ClaimIntakeNode._extra_security_gate_input()
# (which re-scans for residual PII that survived tokenization — it does
# NOT tokenize itself, only detects leftover raw PII and fails closed).

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from shared.security.pii_detector import detect_pii

# ---------------------------------------------------------------------------
# Supplementary detection layer — closes known gaps in the vendored
# shared.security.pii_detector.detect_pii without modifying that dependency.
# This pass runs in addition to detect_pii():
#   1. No Japanese personal-name detection — detect_pii's `name` pattern is
#      a Latin-script Title-Case heuristic and never matches e.g. "田中太郎".
#      Supplement: honorific-suffixed Japanese names (様/氏/殿/さん/くん/
#      ちゃん) — the honorific is what makes the span identifiably a person
#      reference, keeping precision high (no over-redaction of place names /
#      generic nouns).
#   2. detect_pii patterns ending in a trailing `\b` (`email`, `phone_jp`,
#      `phone_us`, ...) silently fail when the identifier is immediately
#      followed by Japanese text ("080-1234-5678です",
#      "yamada@example.comにも共有"): the final ASCII word char and the
#      following Japanese char are both Unicode word chars, so no boundary
#      exists and the WHOLE match fails. Supplement: same email/phone shapes
#      minus the failing trailing \b.
# The same trailing-`\b` failure applied to this template's own policy-number
# pattern ("証券番号AB123456789です") — fixed below with lookarounds instead
# of \b (same defect family, found while applying this fix).
#
# KNOWN LIMIT (documented in docs/07 Troubleshooting): bare Japanese names
# WITHOUT an honorific (e.g. a standalone "田中太郎") are still not detected
# by either layer.
_SUPPLEMENTARY_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # Insurance policy numbers (design §6-2 "証券番号形式") — lookarounds
    # instead of \b so a policy number embedded in Japanese text matches.
    ("policy_number", re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{8,12}(?!\d)")),
    # Email without the failing trailing \b.
    ("email", re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")),
    # JP phone number without the failing trailing \b.
    ("phone_jp", re.compile(r"(?<!\d)(?:\+81[-\s]?\d{1,4}[-\s]\d{1,4}[-\s]\d{4}|0\d{1,4}[-\s]\d{1,4}[-\s]\d{4})")),
    # Japanese personal name with an honorific suffix (high precision).
    ("name", re.compile(r"[一-鿿぀-ゟ゠-ヿ]{2,10}(?:さん|様|氏|殿|くん|ちゃん)")),
)


def detect_pii_extended(text: str) -> list[dict[str, Any]]:
    """detect_pii() + the supplementary patterns above, overlap-deduplicated.

    Returns findings in detect_pii's dict shape ({type, value, start, end}),
    sorted by start offset and guaranteed non-overlapping (a supplementary
    match that overlaps a detect_pii finding is dropped — detect_pii wins).
    Used by PIIVault.tokenize() (server.py adapter layer) and by the S-3
    residual-PII rescan in OpinionComposeNode (design §6-4 (b)).
    """
    if not text:
        return []
    findings: list[dict[str, Any]] = list(detect_pii(text))
    spans: list[tuple[int, int]] = [(f["start"], f["end"]) for f in findings]
    for pii_type, pattern in _SUPPLEMENTARY_PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < end and start < m.end() for start, end in spans):
                continue  # overlaps an already-accepted finding
            findings.append({"type": pii_type, "value": m.group(), "start": m.start(), "end": m.end()})
            spans.append((m.start(), m.end()))
    findings.sort(key=lambda f: f["start"])
    return findings

_TOKEN_TYPE_PREFIX = {
    "name": "PERSON",
    "email": "EMAIL",
    "phone_jp": "PHONE",
    "phone_us": "PHONE",
    "credit_card": "CARD",
    "ssn_us": "SSN",
    "my_number_jp": "MYNUMBER",
    "policy_number": "POLICY_NO",
}


class PIIVault:
    """Request-scoped tokenization vault. One instance per HTTP request.

    Never persisted, never shared across requests — instantiate fresh in
    each server.py route handler.
    """

    def __init__(self) -> None:
        self._token_to_value: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def tokenize(self, text: str) -> str:
        """Replace direct identifiers in `text` with vault tokens.

        Medical/injury descriptive content is intentionally left untouched
        (design §6-1 — needed for Lv2 checks; only direct identifiers are
        tokenized).
        """
        if not text:
            return text

        findings = detect_pii_extended(text)

        if not findings:
            return text

        masked = text
        for finding in reversed(findings):  # reverse to preserve offsets
            token = self._make_token(finding["type"], finding["value"])
            masked = masked[: finding["start"]] + token + masked[finding["end"] :]
        return masked

    def _make_token(self, pii_type: str, value: str) -> str:
        # Reuse an existing token for the same raw value within this request
        # so repeated mentions of the same identifier collapse to one token.
        for token, stored_value in self._token_to_value.items():
            if stored_value == value:
                return token
        prefix = _TOKEN_TYPE_PREFIX.get(pii_type, "PII")
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        token = f"[{prefix}_{self._counters[prefix]}]"
        self._token_to_value[token] = value
        return token

    def resolve(self, text: Any) -> Any:
        """Resolve vault tokens back to real values.

        MUST be called only in src/api/server.py's response formatting path
        — never inside a node or graph return value (design §6-1 invariant).
        """
        if isinstance(text, str):
            resolved = text
            for token, value in self._token_to_value.items():
                resolved = resolved.replace(token, value)
            return resolved
        if isinstance(text, list):
            return [self.resolve(item) for item in text]
        if isinstance(text, dict):
            return {k: self.resolve(v) for k, v in text.items()}
        return text


def derive_claimant_key(salt: str, claimant_identifier: str) -> str:
    """Derive the one-way pseudonymized claimant_key (design §6-1 / §6-3).

    claimant_key = HMAC-SHA256(salt, claimant_identifier). Computed ONLY in
    server.py (the adapter layer that can see the raw identifier) — the
    graph never receives claimant_identifier, only the derived key.
    """
    return hmac.new(salt.encode("utf-8"), claimant_identifier.encode("utf-8"), hashlib.sha256).hexdigest()
