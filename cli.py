"""AGENTIC STAR Marketplace entry adapter for INS-C2-906."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.security.pii_detector import detect_pii
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility.
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility.
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility.
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for older AgentCore wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.graph.graph import ClaimConsistencyCheckAgent
from src.services.assessment_case_repository_factory import create_assessment_case_repository
from src.services.assessment_case_service import (
    AssessmentCaseService,
    ReadOnlyStoreError,
)
from src.services.pii_vault import PIIVault, derive_claimant_key

_NAMESPACE = "ins"
_AGENT_NAME = "INS-C2-906"
_PROVIDER_AGENT_NAME = "ins-c2-906-claim-consistency-check-agent"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_REQUIRED_SECRET_KEYS = _AZURE_OPENAI_SECRET_KEYS + (
    "ASSESSMENT_DB_PATH",
    "CLAIMANT_HASH_SALT",
)
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)
_SAMPLE_CASES_PATH = Path(__file__).resolve().parent / "config" / "assessment_cases.sample.json"
_REQUIRED_SAMPLE_CASE_KEYS = frozenset(
    {
        "case_id",
        "revision",
        "policy_type",
        "coverage_item",
        "final_verdict",
        "claimed_amount",
        "created_at",
        "updated_at",
    }
)


class SampleDataError(ValueError):
    """Raised when the bundled Marketplace demonstration population is invalid."""


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without trimming or repair."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completion client for pre-Marketplace AgentCore wheels."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21"
        request_body: dict[str, Any] = {"messages": messages}
        if "temperature" in self._config:
            request_body["temperature"] = self._config["temperature"]
        request = Request(
            url,
            data=json.dumps(request_body).encode("utf-8"),
            headers={"api-key": self._config["api_key"], "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-owned Azure endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": payload.get("model", self._config["azure_deployment"]),
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Resolve Azure values and create a fresh client for every operation."""

    def __init__(self, *, temperature: Any = None, tools: tuple[Any, ...] = ()) -> None:
        if temperature is not None:
            self._temperature = temperature
        if tools:
            self._tools = tools

    def _client(self) -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility.
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        client_config: dict[str, Any] = {
            "api_key": values["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
        }
        if hasattr(self, "_temperature"):
            client_config["temperature"] = self._temperature
        client = cast(BaseLLM, AzureOpenAIClient(client_config))
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(temperature=getattr(self, "_temperature", None), tools=tuple(tools))


def _record_string_values(value: Any, location: str) -> Iterator[tuple[str, str]]:
    """Yield every nested string value without exposing it in error messages."""

    if isinstance(value, str):
        yield location, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _record_string_values(item, f"{location}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _record_string_values(item, f"{location}.{key}")


def _validate_sample_records(records: Any) -> list[dict[str, Any]]:
    """Validate sample records, including provenance and PII invariants."""

    if not isinstance(records, list):
        raise SampleDataError(
            f"Marketplace sample data 'cases' must be a list: {_SAMPLE_CASES_PATH}"
        )
    if not records:
        raise SampleDataError(
            f"Marketplace sample data must contain at least one case: {_SAMPLE_CASES_PATH}"
        )

    validated_records: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        location = f"cases[{index}]"
        if not isinstance(record, dict):
            raise SampleDataError(
                f"Marketplace sample data record {location} must be a JSON object: {_SAMPLE_CASES_PATH}"
            )
        missing_keys = sorted(_REQUIRED_SAMPLE_CASE_KEYS.difference(record))
        if missing_keys:
            raise SampleDataError(
                f"Marketplace sample data record {location} is missing required keys "
                f"{missing_keys}: {_SAMPLE_CASES_PATH}"
            )

        case_id = record["case_id"]
        if not isinstance(case_id, str) or not case_id.startswith("SAMPLE-"):
            raise SampleDataError(
                f"Marketplace sample data record {location}.case_id must start with 'SAMPLE-': "
                f"{_SAMPLE_CASES_PATH}"
            )
        source_row_hash = record.get("source_row_hash")
        if source_row_hash is not None and (
            not isinstance(source_row_hash, str) or not source_row_hash.startswith("SAMPLE-")
        ):
            raise SampleDataError(
                f"Marketplace sample data record {location}.source_row_hash must start with "
                f"'SAMPLE-': {_SAMPLE_CASES_PATH}"
            )

        for value_location, string_value in _record_string_values(record, location):
            findings = detect_pii(string_value)
            if findings:
                finding_types = sorted({str(finding.get("type", "unknown")) for finding in findings})
                raise SampleDataError(
                    f"Marketplace sample data record {value_location} contains prohibited PII "
                    f"types {finding_types}: {_SAMPLE_CASES_PATH}"
                )
        validated_records.append(record)

    return validated_records


def _load_sample_cases() -> list[dict[str, Any]]:
    """Load and validate the bundled sample population, failing loudly."""

    try:
        raw_payload = _SAMPLE_CASES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SampleDataError(
            f"Marketplace sample data file was not found: {_SAMPLE_CASES_PATH}"
        ) from exc
    except OSError as exc:
        raise SampleDataError(
            f"Marketplace sample data file could not be read: {_SAMPLE_CASES_PATH} ({exc})"
        ) from exc

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise SampleDataError(
            f"Marketplace sample data file contains invalid JSON: {_SAMPLE_CASES_PATH} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc

    if not isinstance(payload, dict):
        raise SampleDataError(
            f"Marketplace sample data root must be a JSON object: {_SAMPLE_CASES_PATH}"
        )
    if "cases" not in payload:
        raise SampleDataError(
            f"Marketplace sample data must contain a 'cases' key: {_SAMPLE_CASES_PATH}"
        )
    return _validate_sample_records(payload["cases"])


def _seed_sample_cases(
    service: AssessmentCaseService,
    records: list[dict[str, Any]] | None = None,
) -> int:
    """Insert validated bundled demonstration rows idempotently."""

    records = _load_sample_cases() if records is None else _validate_sample_records(records)
    inserted = 0
    for record in records:
        case_id = str(record["case_id"])
        revision = int(record["revision"])
        if service.get_revision(case_id, revision) is None:
            service.insert_case_revision(record)
            inserted += 1
    return inserted


class _LazyReadOnlyAssessmentCaseService:
    """Lazily initialized read-only facade over the seeded SQLite repository."""

    def __init__(self) -> None:
        self._service: AssessmentCaseService | None = None
        self._lock = threading.Lock()

    def initialize(self, sqlite_path: str, sample_records: list[dict[str, Any]] | None = None) -> None:
        with self._lock:
            if self._service is not None:
                return
            records = _load_sample_cases() if sample_records is None else sample_records
            writable_service = create_assessment_case_repository(sqlite_path)
            _seed_sample_cases(writable_service, records)
            self._service = writable_service

    def _read_service(self) -> AssessmentCaseService:
        service = self._service
        if service is None:
            raise RuntimeError("Marketplace assessment store has not been provisioned")
        return service

    def get_latest_revision(self, case_id: str) -> dict[str, Any] | None:
        return self._read_service().get_latest_revision(case_id)

    def get_revision(self, case_id: str, revision: int) -> dict[str, Any] | None:
        return self._read_service().get_revision(case_id, revision)

    def query_population(
        self, policy_type: str, coverage_item: str, exclude_case_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self._read_service().query_population(policy_type, coverage_item, exclude_case_id)

    def query_claimant_records(self, claimant_key: str, lookback_days: int) -> list[dict[str, Any]]:
        return self._read_service().query_claimant_records(claimant_key, lookback_days)

    def row_hash_exists(self, source_row_hash: str) -> bool:
        return self._read_service().row_hash_exists(source_row_hash)

    def insert_case_revision(self, record: dict[str, Any]) -> None:
        del record
        raise ReadOnlyStoreError("Marketplace assessment history is read-only")

    def update_case_verdict(
        self, case_id: str, revision: int, updates: dict[str, Any]
    ) -> dict[str, Any]:
        del case_id, revision, updates
        raise ReadOnlyStoreError("Marketplace assessment history is read-only")

    def delete_case_revision(self, case_id: str, revision: int) -> None:
        del case_id, revision
        raise ReadOnlyStoreError("Marketplace assessment history is read-only")

    def insert_ingest_row(self, record: dict[str, Any]) -> bool:
        del record
        raise ReadOnlyStoreError("Marketplace assessment history is read-only")


class MarketplaceGraph(ClaimConsistencyCheckAgent):
    """Marketplace-only invoke boundary with read-only persistence and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = dict(config or {})
        configured_llm = merged.get("llm")
        if not callable(getattr(configured_llm, "complete", None)):
            temperature = configured_llm.get("temperature") if isinstance(configured_llm, Mapping) else None
            merged["llm"] = _AzureMarketplaceLLM(temperature=temperature)

        self._marketplace_case_service: _LazyReadOnlyAssessmentCaseService | None = None
        if merged.get("case_service") is None:
            self._marketplace_case_service = _LazyReadOnlyAssessmentCaseService()
            merged["case_service"] = self._marketplace_case_service
        super().__init__(config=merged)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_context
        try:
            decoded = json.loads(user_input)
        except (json.JSONDecodeError, TypeError):
            decoded = {}
        payload = decoded if isinstance(decoded, dict) else {}

        vault = PIIVault()
        documents = payload.get("documents", [])
        if isinstance(documents, list):
            documents = [
                {**document, "text": vault.tokenize(str(document.get("text", "")))}
                if isinstance(document, dict)
                else document
                for document in documents
            ]

        claimant_key = ""
        claimant_identifier = payload.get("claimant_identifier")
        if isinstance(claimant_identifier, str) and claimant_identifier:
            salt = _require_clean(current_secrets(), "CLAIMANT_HASH_SALT")
            claimant_key = derive_claimant_key(salt, claimant_identifier)

        marketplace_input_context = {
            "mode": "assess",
            "case_id": payload.get("case_id", ""),
            "documents": documents,
            "contract": payload.get("contract", {}),
            "claimant_key": claimant_key,
        }
        marketplace_ctx = replace(ctx, hitl_allowed=False) if ctx is not None else None
        result = cast(
            dict[str, Any],
            super().invoke(
                "",
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context=marketplace_input_context,
            ),
        )
        return cast(dict[str, Any], vault.resolve(result))

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            _require_clean(environment_provider, key)
        sample_records = _load_sample_cases()
        if self._marketplace_case_service is not None:
            sqlite_path = cast(str, environment_provider.get("ASSESSMENT_DB_PATH"))
            self._marketplace_case_service.initialize(sqlite_path, sample_records)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    config = load_agent_config(Path(__file__).resolve().parent)
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
