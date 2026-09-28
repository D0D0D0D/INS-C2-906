# INS-C2-906 — ClaimConsistencyCheckAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: INS

## Overview

ClaimConsistencyCheckAgent takes a set of insurance claim documents — an invoice, an accident
report, a medical certificate — and returns a structured first-pass review draft for a human
assessor: a Markdown opinion plus machine-readable findings.

It checks the same claim at three levels, and the levels are deliberately unequal in what they are
allowed to assert:

- **Lv1 is deterministic.** Amounts against the coverage limit, the incident date against the
  policy period, required fields per document type. No model is involved, so these findings are
  reproducible and quotable.
- **Lv2 is the language model's job, and only that.** It reads the documents against each other
  and reports contradictions *in what the documents say*. It does not judge medical plausibility
  or physical causation.
- **Lv3 is statistical.** The claim is scored against the population of previously settled cases
  held in the agent's own store. Below `min_population_n` settled cases it reports
  `insufficient_population` rather than a score — a percentile computed from a handful of rows
  would be worse than no percentile.

Re-invoking with the same `case_id` produces a new revision and a `revision_diff` marking which
findings were resolved, which are new and which persist — which is the normal shape of claims
work, where documents arrive late.

Two properties are worth stating up front because they constrain deployment:

- **Document text is never stored.** The agent keeps the assessment record, not the documents. A
  re-check therefore requires resending the full document set, existing pages included.
- **The graph never sees raw identifiers.** The HTTP layer tokenises direct identifiers in
  document text through a request-scoped in-memory vault and derives `claimant_key` as an HMAC of
  the claimant identifier under `CLAIMANT_HASH_SALT`. Tokens are resolved back to real values only
  when the response is formatted — never inside a node's return value, so nothing raw can reach a
  checkpoint or the assessment store.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- `ASSESSMENT_DB_PATH` — a SQLite file on a persistent volume, holding the settled-case population
  and the revision history. It is the agent's memory; a fresh file means Lv3 degrades until enough
  cases accumulate;
- `CLAIMANT_HASH_SALT` — the salt for the one-way claimant key used to spot repeat claims;
- `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` — the Lv2 model.
  Supply all three or none: a *partial* configuration raises at start-up rather than degrading,
  and the endpoint must be a bare resource endpoint (scheme and host, no path, query or fragment),
  because the client appends its own path and a malformed one fails only later, inside a call the
  nodes would catch;
- caller authentication in front of the entry adapter.

With no Azure configuration at all the agent still runs and returns Lv1 and Lv3 results, saying so
in `error_log`. That degradation is deliberate — a staging deployment has no model — but it means
a production deployment must check that the model really is wired: `generation_mode` is declared
as `llm`, and Lv2-silently-off is exactly the failure this seam exists to make visible.

Runtime parameters live in `config/config.yaml` (`limits.max_documents`, `limits.max_total_chars`,
`lv3.min_population_n`, `lv3.similar_cases_top_n`, `lv3.repeat_lookback_days`,
`retention.assessment_case_retention_days`, `max_retry`). Be aware of which entry point reads
them: the Marketplace adapter (`cli.py`) loads the file and passes it to the graph, while the
standalone server in `src/api/server.py` builds the graph from equivalent literals in
`_build_agent()`. Changing a value in the file therefore takes effect on the platform path; on the
standalone path, change `_build_agent()` too.

The `retention.assessment_case_retention_days` default of 1825 days is a placeholder, not advice.
Settled-claim data is regulated personal data in most jurisdictions; set this from your own legal
review before ingesting anything real.

`config.yaml` also carries an `llm: {temperature: 0.0}` block. That is a *parameter*, not a
client — a registry deployment passes the file straight into `Graph(config=...)`, so a graph that
reads `config["llm"]` gets a dict. The Marketplace adapter detects this and substitutes a real
client; a custom entry point must do the same or inject a `BaseLLM` itself.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is built to be served by the platform. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

There is no degraded mode and no bundled framework. `pyproject.toml` declares no runtime
dependencies, so `pip install -e .` installs this template's code and nothing else: without the
AgentCore wheel from the platform registry, importing the agent fails at the first
`from framework...` import.

With the framework present, the standalone server in `src/api/server.py` starts and serves
`/health` without the rest of the platform. It does not build the graph at import time — the first
business request does, because the store path is a secret. A deployment missing
`ASSESSMENT_DB_PATH` therefore starts cleanly and fails on the first real request, so check
`/health` *and* one authenticated call before declaring a deployment good. The test suite runs
with no platform connection, no database and no model.

## Input

Three routes, all invoking the same compiled graph with a different `mode`:

| Route | Mode | Purpose |
|---|---|---|
| `POST /invoke` | `assess` | Check a claim. Pass `case_id` to add a revision to an existing case |
| `POST /ingest` | `ingest` | Load already-settled cases as the Lv3 population. `dry_run` validates without writing |
| `POST /feedback` | `feedback` | Record the assessor's verdict on a case revision |

Callers must be authenticated on every route. The entry adapter uses a verified trust level
stamped by upstream middleware when one is present; otherwise, when `INVOKE_AUTH_TOKEN` is set on
the server environment, a matching `Authorization: Bearer` header promotes the caller from
`ANONYMOUS` to `VERIFIED_EXTERNAL`. A caller that is neither middleware-verified nor
token-authenticated is rejected with **401**, including one that middleware explicitly stamped as
anonymous.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "documents": [
      {"doc_id": "d1", "doc_type": "invoice", "text": "..."},
      {"doc_id": "d2", "doc_type": "accident_report", "text": "..."}
    ],
    "contract": {
      "coverage_limit": 5000000,
      "policy_type": "auto",
      "coverage_items": ["collision"],
      "policy_start": "2026-01-01",
      "policy_end": "2026-12-31"
    },
    "claimant_identifier": "optional-claimant-reference",
    "session_id": "example-session"
  }'
```

`claimant_identifier` is optional and is never forwarded: it is consumed at the HTTP layer to
derive `claimant_key`. Document count and total character length are bounded by `limits.*`, and a
request over either bound is rejected rather than truncated.

## Output

The standard AgentCore envelope is returned — `output`, `status`, `trace_id`, `correlation_id`,
`node_history` — with `output` carrying the review draft:

```json
{
  "output": {
    "mode": "assess",
    "case_id": "5f1a2b3c-...",
    "revision": 1,
    "opinion_markdown": "# 査定所見ドラフト — case_id: 5f1a2b3c... (revision 1) ...",
    "findings": {
      "lv1_flags": [
        {"kind": "LV1_MISSING_FIELD", "flag_key": "...", "severity": "medium",
         "reason": "required fields missing for doc_type 'invoice': ['claimed_amount']",
         "evidence": [{"doc_id": "d1", "field": "claimed_amount", "quote": ""}]}
      ],
      "lv2_flags": [],
      "lv3_result": {"anomaly_score": null, "percentile": null, "population_n": 0,
                     "median": null, "similar_cases": [], "repeat_claim": null,
                     "degraded": "insufficient_population"},
      "checked_items": [
        {"check": "amount_within_coverage_limit", "level": "Lv1", "passed": null,
         "detail": {"reason": "insufficient data (coverage_limit or claimed_amount missing)"}}
      ],
      "revision_diff": {}
    },
    "persistence_status": "ok"
  },
  "status": "success"
}
```

Every flag carries its own evidence — the document it came from, the field, and the quoted span —
so an assessor can go straight to the page rather than re-reading the file to find out what the
agent meant. `checked_items` records what was examined and whether it could be evaluated at all, so
a check that was skipped for missing data is visible instead of looking like a pass.
`persistence_status` reports separately from `status`: `read_only` means the review completed but
was not recorded, which is the normal state on a read-only deployment and not an error.

Values above are illustrative; real output depends on the documents supplied and the accumulated
population.

## Security and Limitations

Every node requires verified-external trust, so an unauthenticated caller is stopped before any
node body runs. Document text is scanned on the way in, and the identifier tokenisation described
in *Overview* keeps raw personal data out of the graph, the state and the store.

**Write modes are unreachable from the shared Marketplace entry point, on purpose.** That entry
point serves any verified external caller and carries no per-caller authorisation for writing, so
the Marketplace adapter pins `mode` to `assess` regardless of what the payload asks for, and backs
it with a read-only view over the store that refuses inserts and updates explicitly rather than
dropping them. `ingest` and `feedback` remain available only through the authenticated HTTP
routes. A fork that needs Marketplace-hosted writes must add an authorisation model first — not
relax this check.

Known limits worth stating plainly:

- **Lv2 checks agreement between documents, not truth.** Two documents that are consistently wrong
  produce no flag.
- **A missing model degrades quietly by design.** Lv1 and Lv3 still run and `error_log` says so,
  but the invocation reports `success` — monitor `error_log`, not just `status`.
- **Lv3 needs a population.** Below `min_population_n` settled cases there is no score, and the
  score is only as representative as the cases ingested.
- **Definitive payment language is softened, not adjudicated.** A conclusive phrase in the draft
  is rewritten toward "requires confirmation"; that is a drafting guard, not a decision.
- **Nothing here decides a claim.** The output is a first-pass draft for an assessor.

Output is machine-generated and assistive. It does not substitute for expert review, and any
decision that matters should be made against the primary documents by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2) — a five-node outer backbone whose `main`
  slot wraps the inner three-level workflow graph
- Generation mode: `llm` for Lv2; Lv1 and Lv3 are deterministic
- HITL: not enabled
- Persistence: a SQLite assessment store supplied by the deployment; no checkpointer is configured
- Secrets: `ASSESSMENT_DB_PATH`, `CLAIMANT_HASH_SALT`, and the three `AZURE_OPENAI_*` values when
  Lv2 is wanted

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, without a model and without a database — the store and the
model are both injected. Running the agent needs at least `ASSESSMENT_DB_PATH`.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the three check levels, the
personal-data handling and the security design. `docs/03_test_spec.md` records the test
specification and the verification results, including a run against a live model.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
