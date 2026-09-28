"""The HTTP entry point must honour agent.yaml's generation_mode: "llm".

Regression: `_build_agent()` built the graph with no `llm`, so StructureDocsNode
returned empty fields for every document and Lv2CrossDocNode skipped itself —
both with status: success. The deployment reported success while running Lv1
only. Nothing in CI exercised the server's construction path, so it stayed
invisible (see also: startup is never exercised by `run-tests`).
"""

from __future__ import annotations

import pytest
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.services.azure_llm import AzureLLM, build_azure_llm

_GOOD = {
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://example.services.ai.azure.example",
    "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
}


@pytest.fixture
def no_azure_client_module(monkeypatch):
    """Stand in for a wheel that ships no Azure client at all.

    The registry wheel omitted ``azure_openai_client`` through 1.0.1 and ships
    it from 1.0.2 on, so its absence can no longer be observed by simply not
    installing anything: the module is made unimportable here instead, which is
    the condition ``build_azure_llm`` actually branches on.
    """

    import sys

    monkeypatch.setitem(sys.modules, "shared.services.llm.azure_openai_client", None)


@pytest.fixture
def azure_client_module(monkeypatch):
    """Stand in for the client that only the deployed image's wheel provides."""

    import sys
    from types import ModuleType

    module = ModuleType("shared.services.llm.azure_openai_client")

    class _Client:
        def __init__(self, config):
            self.config = dict(config)

        def bind_tools(self, tools):
            return self

    module.AzureOpenAIClient = _Client
    monkeypatch.setitem(sys.modules, "shared.services.llm.azure_openai_client", module)
    return module


def test_azure_llm_is_built_when_the_deployment_supplies_azure(azure_client_module):
    llm = build_azure_llm(InMemoryProvider(dict(_GOOD)))
    assert isinstance(llm, AzureLLM)


def test_no_llm_when_the_installed_wheel_has_no_azure_client(no_azure_client_module):
    """A wheel with no azure_openai_client degrades instead of raising.

    Degrading here (rather than raising) is what keeps the STG evidence run
    working; the nodes report "Lv1 results only" in error_log.
    """

    assert build_azure_llm(InMemoryProvider(dict(_GOOD))) is None


def test_no_azure_at_all_degrades_to_no_llm():
    """STG supplies no Azure; the nodes handle a missing LLM as Lv1-only."""

    assert build_azure_llm(InMemoryProvider({})) is None


@pytest.mark.parametrize("missing", sorted(_GOOD))
def test_a_partial_configuration_fails_instead_of_degrading(missing):
    """Half-configured is an operator mistake, not the STG path.

    Degrading here would reproduce the defect this seam exists to fix: a
    deployment that declares generation_mode "llm" and quietly runs Lv1.
    """

    values = {k: v for k, v in _GOOD.items() if k != missing}
    with pytest.raises(ValueError, match="partially configured"):
        build_azure_llm(InMemoryProvider(values))


@pytest.mark.parametrize("unclean", ["   ", "has space", "line\nbreak"])
def test_an_unclean_azure_value_fails_instead_of_degrading(unclean):
    with pytest.raises(ValueError):
        build_azure_llm(InMemoryProvider({**_GOOD, "AZURE_OPENAI_API_KEY": unclean}))


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
def test_an_unusable_azure_endpoint_shape_fails_instead_of_degrading(endpoint):
    """AzureOpenAIClient appends its own path; anything but a bare endpoint breaks.

    The error would surface inside complete(), where this agent's nodes catch it
    and degrade silently to Lv1 — so a configured-but-wrong endpoint has to stop
    the deployment, not quietly disable the LLM.
    """

    with pytest.raises(ValueError, match="bare"):
        build_azure_llm(InMemoryProvider({**_GOOD, "AZURE_OPENAI_ENDPOINT": endpoint}))


def test_client_retains_no_connection_values_between_calls(azure_client_module):
    """Values are resolved per call from the bound provider, never cached."""

    llm = build_azure_llm(InMemoryProvider(dict(_GOOD)))
    assert vars(llm) == {}
    first = InMemoryProvider({**_GOOD, "AZURE_OPENAI_API_KEY": "first-key"})
    second = InMemoryProvider({**_GOOD, "AZURE_OPENAI_API_KEY": "second-key"})
    with bound_secrets(first):
        assert llm._client().config["api_key"] == "first-key"
    with bound_secrets(second):
        assert llm._client().config["api_key"] == "second-key"
    assert vars(llm) == {}


def test_build_agent_passes_the_llm_into_the_graph_config(monkeypatch):
    """The seam has to reach the graph, not just exist."""

    from src.api import server

    sentinel = object()
    captured: dict = {}

    class _Agent:
        def __init__(self, config):
            captured.update(config)

        def compile(self):
            return None

        def provision_secrets(self, provider):
            return None

    monkeypatch.setattr(server, "ClaimConsistencyCheckAgent", _Agent)
    monkeypatch.setattr(server, "build_azure_llm", lambda provider: sentinel)
    monkeypatch.setattr(server, "create_assessment_case_repository", lambda path: object())
    monkeypatch.setattr(server, "secrets_factory", lambda **kw: InMemoryProvider({"ASSESSMENT_DB_PATH": "/tmp/x.sqlite3"}))

    server._build_agent()
    assert captured["llm"] is sentinel
