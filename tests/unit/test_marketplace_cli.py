"""Marketplace adapter wiring, lifecycle, and read-only boundary tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from src.graph.graph import ClaimConsistencyCheckAgent
from src.nodes.inner.persist_case_node import PersistCaseNode
from src.nodes.post_process_node import OpinionComposeNode
from src.services.assessment_case_service import ReadOnlyStoreError


class CountingLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append(messages)
        if "extracting structured facts" in messages[0]["content"]:
            content = json.dumps(
                {
                    "incident_date": "2026-04-01",
                    "first_visit_date": "2026-04-02",
                    "injury_name": "捻挫",
                    "claimed_amount": 75000,
                    "claim_target": "入通院給付金",
                    "incident_description": "転倒",
                    "party_role": "被保険者",
                },
                ensure_ascii=False,
            )
        else:
            content = '{"flags": []}'
        return {"content": content, "tool_calls": [], "model": "fake", "usage": {}}

    def stream(self, messages: list[dict[str, Any]]):
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list[Any]) -> "CountingLLM":
        del tools
        return self


class InjectedCaseService:
    def insert_case_revision(self, record: dict[str, Any]) -> None:
        del record


class RecordingSeedService:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], dict[str, Any]] = {}

    def get_revision(self, case_id: str, revision: int) -> dict[str, Any] | None:
        return self.rows.get((case_id, revision))

    def insert_case_revision(self, record: dict[str, Any]) -> None:
        self.rows[(record["case_id"], record["revision"])] = dict(record)


def _config() -> dict[str, Any]:
    return load_config(str(Path(__file__).parents[2] / "config" / "config.yaml"))


def _set_environment(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> None:
    values = {
        "AZURE_OPENAI_API_KEY": "test-key",
        "AZURE_OPENAI_ENDPOINT": "https://example.invalid",
        "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
        "ASSESSMENT_DB_PATH": str(database_path),
        "CLAIMANT_HASH_SALT": "test-salt",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _payload(*, mode: str = "assess", text: str = "請求額 75000円、傷病名は捻挫") -> str:
    return json.dumps(
        {
            "mode": mode,
            "documents": [{"doc_id": "DEMO-DOC-1", "doc_type": "invoice", "text": text}],
            "contract": {
                "policy_type": "傷害保険",
                "coverage_items": ["入通院給付金"],
                "coverage_limit": 200000,
                "deductible": 0,
                "policy_start": "2026-01-01",
                "policy_end": "2026-12-31",
            },
        },
        ensure_ascii=True,
    )


def _valid_sample_record(**overrides: Any) -> dict[str, Any]:
    record = {
        "case_id": "SAMPLE-TEST-001",
        "revision": 1,
        "policy_type": "傷害保険",
        "coverage_item": "入通院給付金",
        "final_verdict": "paid",
        "claimed_amount": 50000,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "source_row_hash": "SAMPLE-ROW-TEST-001",
    }
    record.update(overrides)
    return record


def _use_sample_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: Any,
) -> Path:
    sample_path = tmp_path / "test.sample.json"
    sample_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(cli, "_SAMPLE_CASES_PATH", sample_path)
    return sample_path


def test_config_mapping_is_replaced_by_llm_and_temperature_is_preserved() -> None:
    agent = cli.MarketplaceGraph(config=_config())
    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)
    assert agent.config["llm"]._temperature == 0.0


def test_injected_llm_is_preserved() -> None:
    injected = CountingLLM()
    config = _config()
    config["llm"] = injected
    agent = cli.MarketplaceGraph(config=config)
    assert agent.config["llm"] is injected


def test_startup_gate_rejects_dirty_required_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_environment(monkeypatch, tmp_path / "assessment.sqlite3")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", " dirty-key")
    config = _config()
    config["llm"] = CountingLLM()
    config["case_service"] = InjectedCaseService()
    agent = cli.MarketplaceGraph(config=config)
    with pytest.raises(ValueError, match="AZURE_OPENAI_API_KEY"):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    endpoint: str,
) -> None:
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_environment(monkeypatch, tmp_path / "assessment.sqlite3")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", " dirty-key")
    config = _config()
    config["llm"] = CountingLLM()
    config["case_service"] = InjectedCaseService()
    agent = cli.MarketplaceGraph(config=config)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())


def test_invoke_rebuilds_envelope_forces_assess_and_disables_hitl(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def recorder(self, user_input, session_id="", ctx=None, input_context=None):
        del self
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "success", "output": {"captured": True}}

    monkeypatch.setattr(ClaimConsistencyCheckAgent, "invoke", recorder)
    config = _config()
    config["llm"] = CountingLLM()
    config["case_service"] = InjectedCaseService()
    agent = cli.MarketplaceGraph(config=config)
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True)
    agent.invoke(
        _payload(mode="feedback"),
        ctx=ctx,
        input_context={"conversation_history": [{"role": "user", "content": "CANARY"}]},
    )

    assert captured["user_input"] == ""
    assert captured["ctx"].hitl_allowed is False
    assert captured["input_context"]["mode"] == "assess"
    assert "conversation_history" not in captured["input_context"]
    assert "CANARY" not in json.dumps(captured, ensure_ascii=False, default=str)


@pytest.mark.parametrize("provision_first", [False, True], ids=["sdk-1.0.1", "sdk-1.0.2"])
def test_runner_lifecycle_reads_samples_without_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provision_first: bool
) -> None:
    database_path = tmp_path / f"assessment-{provision_first}.sqlite3"
    _set_environment(monkeypatch, database_path)
    config = _config()
    llm = CountingLLM()
    config["llm"] = llm
    agent = cli.MarketplaceGraph(config=config)

    if provision_first:
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
        agent.compile()
    else:
        agent.compile()
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())

    with sqlite3.connect(database_path) as connection:
        rows_before = connection.execute("SELECT COUNT(*) FROM assessment_cases").fetchone()[0]

    ctx = InvocationContext(caller_id="marketplace-test", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(
            _payload(mode="ingest"),
            ctx=ctx,
            input_context={"conversation_history": [{"role": "user", "content": "CANARY"}]},
        )

    with sqlite3.connect(database_path) as connection:
        rows_after = connection.execute("SELECT COUNT(*) FROM assessment_cases").fetchone()[0]

    lv3_result = result["output"]["findings"]["lv3_result"]
    assert result["status"] == "success"
    assert result["output"]
    assert len(llm.calls) == 2
    assert rows_before == rows_after == 32
    assert lv3_result["population_n"] == 32
    assert any(case["case_id"].startswith("SAMPLE-CLAIM-") for case in lv3_result["similar_cases"])
    assert result["output"]["persistence_status"] == "read_only"
    assert "CANARY" not in json.dumps(result, ensure_ascii=False)


def test_seed_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database_path = tmp_path / "assessment.sqlite3"
    _set_environment(monkeypatch, database_path)
    for _ in range(2):
        agent = cli.MarketplaceGraph(config=_config())
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM assessment_cases").fetchone()[0] == 32


def test_bundled_sample_is_valid_and_nonempty() -> None:
    records = cli._load_sample_cases()
    assert len(records) == 32
    assert all(record["case_id"].startswith("SAMPLE-") for record in records)


def test_missing_sample_file_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.sample.json"
    monkeypatch.setattr(cli, "_SAMPLE_CASES_PATH", missing_path)
    with pytest.raises(cli.SampleDataError, match="was not found"):
        cli._seed_sample_cases(RecordingSeedService())


def test_invalid_sample_json_fails_loudly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sample_path = tmp_path / "invalid.sample.json"
    sample_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(cli, "_SAMPLE_CASES_PATH", sample_path)
    with pytest.raises(cli.SampleDataError, match="contains invalid JSON"):
        cli._seed_sample_cases(RecordingSeedService())


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "root must be a JSON object"),
        ({}, "must contain a 'cases' key"),
        ({"cases": {}}, "'cases' must be a list"),
        ({"cases": []}, "must contain at least one case"),
        ({"cases": ["not-an-object"]}, "must be a JSON object"),
        ({"cases": [{"case_id": "SAMPLE-INCOMPLETE"}]}, "missing required keys"),
    ],
)
def test_invalid_sample_shape_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: Any,
    message: str,
) -> None:
    _use_sample_payload(monkeypatch, tmp_path, payload)
    with pytest.raises(cli.SampleDataError, match=message):
        cli._seed_sample_cases(RecordingSeedService())


@pytest.mark.parametrize(
    ("field", "value"),
    [("case_id", "CLAIM-001"), ("source_row_hash", "ROW-001")],
)
def test_sample_provenance_prefix_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    _use_sample_payload(
        monkeypatch,
        tmp_path,
        {"cases": [_valid_sample_record(**{field: value})]},
    )
    with pytest.raises(cli.SampleDataError, match="must start with 'SAMPLE-'"):
        cli._seed_sample_cases(RecordingSeedService())


def test_sample_pii_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_sample_payload(
        monkeypatch,
        tmp_path,
        {"cases": [_valid_sample_record(incident_type="連絡先 080-1234-5678")]},
    )
    with pytest.raises(cli.SampleDataError, match="contains prohibited PII types .*phone_jp"):
        cli._seed_sample_cases(RecordingSeedService())


def test_sample_path_is_independent_of_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    service = RecordingSeedService()
    assert cli._seed_sample_cases(service) == 32
    assert len(service.rows) == 32


def test_startup_validates_sample_before_opening_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_environment(monkeypatch, tmp_path / "assessment.sqlite3")
    monkeypatch.setattr(cli, "_SAMPLE_CASES_PATH", tmp_path / "missing.sample.json")
    repository_calls = 0

    def forbidden_repository_open(sqlite_path):
        nonlocal repository_calls
        del sqlite_path
        repository_calls += 1
        raise AssertionError("SQLite must not be opened before sample validation")

    monkeypatch.setattr(cli, "create_assessment_case_repository", forbidden_repository_open)
    agent = cli.MarketplaceGraph(config=_config())
    with pytest.raises(cli.SampleDataError, match="was not found"):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
    assert repository_calls == 0


def test_read_only_facade_rejects_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database_path = tmp_path / "assessment.sqlite3"
    _set_environment(monkeypatch, database_path)
    service = cli._LazyReadOnlyAssessmentCaseService()
    service.initialize(str(database_path))
    with pytest.raises(ReadOnlyStoreError):
        service.insert_case_revision({"case_id": "MUST-NOT-WRITE", "revision": 1})


def test_persist_node_does_not_retry_policy_rejection(base_state: dict[str, Any]) -> None:
    class ReadOnlyService:
        read_calls = 0

        def insert_case_revision(self, record):
            del record
            raise ReadOnlyStoreError("read only")

        def get_latest_revision(self, case_id):
            del case_id
            self.read_calls += 1
            return None

    service = ReadOnlyService()
    node = PersistCaseNode(case_service=service)
    result = node.execute(
        {
            **base_state,
            "case_id": "case-1",
            "revision": 1,
            "prior_flags": [],
            "lv1_flags": [],
            "lv2_flags": [],
            "contract": {"policy_type": "傷害保険", "coverage_items": ["入通院給付金"]},
            "documents": [{"fields": {"claimed_amount": 75000}}],
            "lv3_result": {},
        }
    )
    assert result["persistence_status"] == "read_only"
    assert service.read_calls == 0


def test_read_only_warning_is_explained_in_output(base_state: dict[str, Any]) -> None:
    result = OpinionComposeNode().execute(
        {
            **base_state,
            "mode": "assess",
            "result": {
                "case_id": "case-1",
                "revision": 1,
                "lv1_flags": [],
                "lv2_flags": [],
                "lv3_result": {},
                "checked_items": [],
                "revision_diff": {},
                "persistence_status": "read_only",
            },
        }
    )
    markdown = result["formatted_output"]["opinion_markdown"]
    assert "読み取り専用環境" in markdown
    assert "/feedback" in markdown


def test_ascii_escaped_json_is_decoded_before_pii_tokenization(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def recorder(self, user_input, session_id="", ctx=None, input_context=None):
        del self, user_input, session_id, ctx
        captured.update(input_context)
        return {"status": "success", "output": {}}

    monkeypatch.setattr(ClaimConsistencyCheckAgent, "invoke", recorder)
    config = _config()
    config["llm"] = CountingLLM()
    config["case_service"] = InjectedCaseService()
    agent = cli.MarketplaceGraph(config=config)
    escaped = _payload(text="連絡先 080-1234-5678")
    assert "\\u9023" in escaped
    agent.invoke(escaped)
    assert "080-1234-5678" not in captured["documents"][0]["text"]
    assert "[PHONE_1]" in captured["documents"][0]["text"]


def test_compat_client_sends_configured_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            del args

        def read(self):
            return b'{"choices":[{"message":{"content":"{}"}}],"model":"fake"}'

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)
    client = cli._AzureOpenAIClientCompat(
        {
            "api_key": "test-key",
            "azure_endpoint": "https://example.invalid",
            "azure_deployment": "test-deployment",
            "temperature": 0.0,
        }
    )
    client.complete([{"role": "user", "content": "test"}])
    assert captured["body"]["temperature"] == 0.0


def test_main_passes_loaded_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def recorder(graph_class, *, agent_name, namespace, config):
        captured.update(
            graph_class=graph_class,
            agent_name=agent_name,
            namespace=namespace,
            config=config,
        )

    monkeypatch.setattr(cli, "run_agent_marketplace", recorder)
    cli.main()
    assert captured["graph_class"] is cli.MarketplaceGraph
    assert captured["agent_name"] == "INS-C2-906"
    assert captured["namespace"] == "ins"
    assert captured["config"]["llm"] == {"temperature": 0.0}
