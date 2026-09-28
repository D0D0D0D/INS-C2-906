"""AgentCore Platform v1.0 — INS-C2-906 LLM invocation helpers"""

# docs/02_design.md §5-2 / §9 — LLM is used only for Lv2 cross-document
# contradiction detection, document structuring/summarization, and ingest
# column mapping. All calls use shared.services.llm.base_llm.BaseLLM's
# canonical complete(messages: list) -> dict contract (verified against the
# installed agenticstar-agentcore==1.0.0 wheel — this is NOT the simplified
# `complete(prompt) -> str` shown in some how-to examples).
#
# temperature 0.0 / structured-output enforcement is a prompt-engineering
# concern layered on top of this thin wrapper — the wrapper itself is
# provider-agnostic and test-injectable (tests pass a FakeLLM implementing
# the same complete()/bind_tools() shape).

from __future__ import annotations

import json
from typing import Any

from shared.services.llm.base_llm import BaseLLM


class LLMInvocationError(Exception):
    """Raised when an LLM call fails or returns an unparseable structured response."""


def call_structured(llm: BaseLLM, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Call the LLM with a system+user prompt pair and parse a JSON object response.

    Raises LLMInvocationError on transport failure or non-JSON content —
    callers (lv2_cross_doc, structure_docs, map_columns) are responsible for
    catching this and falling back per design §8 ("LLM 呼び出し失敗... 失敗時
    は Lv1 結果のみで所見を構成").
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    try:
        response = llm.complete(messages)
    except Exception as e:  # noqa: BLE001 — normalize all transport failures
        raise LLMInvocationError(f"LLM completion failed: {e}") from e

    content = response.get("content", "") if isinstance(response, dict) else ""
    if not content:
        raise LLMInvocationError("LLM completion returned empty content")

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError) as e:
        raise LLMInvocationError(f"LLM completion did not return valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        # Every caller reads the result as a mapping (`.get(...)` / `dict(...)`).
        # A model that answers with a bare array or scalar is still valid JSON,
        # so without this the value flows on and Lv2 raises AttributeError
        # outside the LLMInvocationError degradation path. Only the type name is
        # reported — the body is model-authored and may quote document text.
        raise LLMInvocationError(
            f"LLM completion returned a JSON {type(parsed).__name__}, not an object"
        )
    return parsed
