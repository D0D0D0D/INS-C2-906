"""Azure OpenAI seam shared by the HTTP entry point.

`config/agent.yaml` declares ``generation_mode: "llm"``, but `src/api/server.py`
built the graph without an ``llm``: every document was structured with empty
fields and the Lv2 cross-document check skipped itself, both returning
``status: success``. The declared contract and the deployed behaviour disagreed,
and nothing failed loudly.

The client resolves its connection values on **every call** through
``current_secrets()`` (the entry point binds them with ``bound_secrets``), so no
credential is retained on the instance.

``build_azure_llm()`` returns ``None`` when the three Azure values are absent or
unusable. That degradation is deliberate: ``deploy-stg`` does not provide Azure,
and the STG evidence run must keep working — the nodes already treat a missing
LLM as "Lv1 results only" and say so in ``error_log``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import urlsplit

from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Deployment environment variable {key!r} must be a clean non-empty string")
    return value


def require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()`` — where this agent's nodes catch it
    and degrade to Lv1. Fail closed here instead. The value is reported, never
    repaired.
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
    ):
        raise ValueError(
            "Deployment environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class AzureLLM(BaseLLM):  # type: ignore[misc]
    """Create a fresh Azure client per operation; retain no connection values."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _client(self) -> BaseLLM:
        provider = current_secrets()
        connection = {
            "api_key": _require_clean(provider, "AZURE_OPENAI_API_KEY"),
            "azure_endpoint": require_azure_endpoint(provider),
            "azure_deployment": _require_clean(provider, "AZURE_OPENAI_DEPLOYMENT"),
        }
        # Lazy: the registry wheel used by CI/STG has no azure_openai_client.
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        client = cast(BaseLLM, AzureOpenAIClient(connection))
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "AzureLLM":
        return type(self)(tools=tuple(tools))


def build_azure_llm(provider: SecretProvider) -> AzureLLM | None:
    """Return a lazy Azure client, or ``None`` when this deployment has no Azure.

    Absent configuration degrades (that is the STG path). Configuration that is
    present but partial or malformed raises: quietly degrading there would
    reproduce the very defect this seam was added to fix.
    """

    present = []
    for key in AZURE_OPENAI_SECRET_KEYS:
        try:
            provider.require(key)
        except Exception:
            continue
        present.append(key)

    if not present:
        # No Azure at all: deploy-stg supplies none, and the nodes treat a
        # missing LLM as "Lv1 results only" and say so in error_log.
        return None

    # Something was configured. A partial or malformed configuration is an
    # operator mistake, and degrading quietly here would reproduce exactly the
    # failure this agent already suffered: a deployment that declares
    # generation_mode "llm" and silently runs Lv1.
    missing = [key for key in AZURE_OPENAI_SECRET_KEYS if key not in present]
    if missing:
        raise ValueError(
            "Azure OpenAI is partially configured; missing " + ", ".join(sorted(missing))
        )
    for key in AZURE_OPENAI_SECRET_KEYS:
        _require_clean(provider, key)
    require_azure_endpoint(provider)

    try:
        import shared.services.llm.azure_openai_client  # noqa: F401
    except ModuleNotFoundError:
        # A capability of the installed wheel, not an operator mistake: the
        # registry wheel CI and STG install has no Azure client at all.
        return None
    return AzureLLM()
