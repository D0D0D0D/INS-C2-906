"""AgentCore Platform v1.0 — INS-C2-906 standalone HTTP API"""

# docs/02_design.md §2-2 / §6-1 — three HTTP routes, ALL invoking the SAME
# compiled graph instance (single-entry design, not three separate graphs):
#   POST /ingest    -> mode="ingest"
#   POST /invoke    -> mode="assess" (default; optional case_id for recheck)
#   POST /feedback  -> mode="feedback"
#
# This is the ONLY layer that ever sees raw PII / claimant identifiers.
# Before building input_context for agent.invoke(), this module:
#   1. Tokenizes direct identifiers in document text via PIIVault
#      (request-scoped, in-memory — never persisted, never shared across
#      requests).
#   2. Derives claimant_key = HMAC-SHA256(CLAIMANT_HASH_SALT, claimant_id)
#      here (the only layer that can see the raw identifier) — the graph
#      never receives claimant_identifier, only the derived key.
#   3. Resolves tokens back to real values ONLY in the response formatting
#      path (never inside a node/graph return value — hard invariant,
#      design §6-1).
#
# Business logic and PII handling both live outside the graph's business
# logic proper (nodes/graph); this module remains an adapter, except for the
# PII tokenization step, which is deliberately placed here because the graph
# must never see raw PII (design §6-1).
#
# Entry authentication (`entry_adapter.auth`):
#   - a verified middleware trust (request.state.trust_level) is used as-is;
#   - otherwise INVOKE_AUTH_TOKEN Bearer auth promotes ANONYMOUS →
#     VERIFIED_EXTERNAL (_resolve_trust raises 401 on token mismatch);
#   - an unauthenticated caller (no verified middleware trust AND no token
#     promotion) is rejected with 401. All three routes (/invoke, /ingest,
#     /feedback) previously defaulted such callers to VERIFIED_EXTERNAL —
#     a fail-open S-1 bypass — and now go through the _ctx() auth core.

from __future__ import annotations

import threading
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.entry_adapter.auth import _require_verified_trust, _resolve_trust
from src.graph.graph import ClaimConsistencyCheckAgent
from src.services.assessment_case_repository_factory import create_assessment_case_repository
from src.services.azure_llm import build_azure_llm
from src.services.pii_vault import PIIVault, derive_claimant_key

app = FastAPI(title="ClaimConsistencyCheckAgent")

# The repository path is a secret, so neither it nor the agent is resolved at
# module import. The first business request constructs the complete graph once;
# /health and module discovery remain independent of secret provisioning.
_agent: ClaimConsistencyCheckAgent | None = None
_agent_lock = threading.Lock()


def _build_agent() -> ClaimConsistencyCheckAgent:
    secrets_provider = secrets_factory(
        namespace="ins", agent_name="ins-c2-906-claim-consistency-check-agent"
    )
    case_repository = create_assessment_case_repository(
        secrets_provider.require("ASSESSMENT_DB_PATH")
    )
    # agent.yaml declares generation_mode: "llm". Without this the graph ran
    # Lv1-only (empty structured fields, Lv2 skipped) while still reporting
    # success, so the declared contract and the deployed behaviour disagreed.
    # build_azure_llm() returns None when the deployment has no Azure (STG),
    # which the nodes already handle as "Lv1 results only".
    llm = build_azure_llm(secrets_provider)
    built_agent = ClaimConsistencyCheckAgent(
        config={
            "max_retry": 3,
            "llm": llm,
            "case_service": case_repository,
            "lv3": {"min_population_n": 30, "similar_cases_top_n": 5, "repeat_lookback_days": 365},
            "limits": {"max_documents": 12, "max_total_chars": 400_000},
        }
    )
    built_agent.compile()
    built_agent.provision_secrets(secrets_provider)
    return built_agent


def _get_agent() -> ClaimConsistencyCheckAgent:
    global _agent
    if _agent is None:
        with _agent_lock:
            if _agent is None:
                _agent = _build_agent()
    return _agent


def _ctx(request: Request, session_id: str) -> InvocationContext:
    """Build the InvocationContext AFTER entry authentication.

    Scope note: the entry_adapter part also exposes a tenant/org scope contract,
    but this template has NO scope model. Claimant isolation is enforced by
    per-request PII tokenization (PIIVault) and the HMAC-derived claimant_key —
    NOT by a per-request tenant/org scope key. There is no code path that reads
    request tenant/org scope, so enforcing authenticate_scope() would gate on a
    scope nothing consumes (a "default" fallback would silently match
    everything). Only the auth core (_resolve_trust /
    _require_verified_trust) is wired.
    """
    trust, token_authenticated = _resolve_trust(request)
    # Unauthenticated = trust is still ANONYMOUS after resolution and no token
    # promotion. Test the trust VALUE, not attribute presence — a middleware
    # that explicitly stamps ANONYMOUS must still be treated as unauthenticated
    # (→ 401), not silently accepted as "verified because the attr exists".
    if trust is TrustLevel.ANONYMOUS and not token_authenticated:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    _require_verified_trust(trust)
    return InvocationContext(
        session_id=session_id or str(uuid4()),
        caller_trust_level=trust,
        caller_id=getattr(request.state, "caller_id", ""),
    )


class InvokeRequest(BaseModel):
    case_id: str = ""
    documents: list[dict[str, Any]] = []
    contract: dict[str, Any] = {}
    claimant_identifier: str = ""
    session_id: str = ""


class IngestRequest(BaseModel):
    rows: list[dict[str, Any]] = []
    dry_run: bool = False
    session_id: str = ""


class FeedbackRequest(BaseModel):
    case_id: str
    revision: int | None = None
    final_verdict: str | None = None
    flag_verdicts: dict[str, Any] = {}
    assessor_edited: bool = False
    action: str = "update"
    session_id: str = ""


def _tokenize_documents(vault: PIIVault, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**doc, "text": vault.tokenize(str(doc.get("text", "")))} for doc in documents]


def _resolve_response(vault: PIIVault, response: dict[str, Any]) -> dict[str, Any]:
    """Resolve vault tokens back to real values — ONLY done here, in the
    response formatting path. No node or graph return value performs this
    (design §6-1 hard invariant)."""
    return vault.resolve(response)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    ctx = _ctx(request, req.session_id)
    agent = _get_agent()
    with bound_secrets(agent._secrets_provider):
        vault = PIIVault()
        tokenized_documents = _tokenize_documents(vault, req.documents)

        claimant_key = ""
        if req.claimant_identifier:
            salt = ctx.secrets.require("CLAIMANT_HASH_SALT")
            claimant_key = derive_claimant_key(salt, req.claimant_identifier)

        input_context = {
            "mode": "assess",
            "case_id": req.case_id,
            "documents": tokenized_documents,
            "contract": req.contract,
            "claimant_key": claimant_key,
        }

        response = agent.invoke("", session_id=ctx.session_id, ctx=ctx, input_context=input_context)
        return _resolve_response(vault, response)


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    ctx = _ctx(request, req.session_id)
    agent = _get_agent()
    with bound_secrets(agent._secrets_provider):
        vault = PIIVault()
        # Ingest rows are already coded/minimized upstream (design §7-3) —
        # tokenize any free-text-shaped values defensively.
        tokenized_rows = [
            {k: (vault.tokenize(v) if isinstance(v, str) else v) for k, v in row.items()} for row in req.rows
        ]

        input_context = {"mode": "ingest", "rows": tokenized_rows, "dry_run": req.dry_run}
        response = agent.invoke("", session_id=ctx.session_id, ctx=ctx, input_context=input_context)
        return _resolve_response(vault, response)


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    ctx = _ctx(request, req.session_id)
    agent = _get_agent()
    with bound_secrets(agent._secrets_provider):
        vault = PIIVault()
        input_context = {
            "mode": "feedback",
            "case_id": req.case_id,
            "revision": req.revision,
            "final_verdict": req.final_verdict,
            "flag_verdicts": req.flag_verdicts,
            "assessor_edited": req.assessor_edited,
            "action": req.action,
        }

        response = agent.invoke("", session_id=ctx.session_id, ctx=ctx, input_context=input_context)
        return _resolve_response(vault, response)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "INS-C2-906"}
