"""Provider-neutral AI adapter layer for ALM WebFlow Studio.

Phase 13 deliberately keeps external AI providers *outside* the automation engine.
Adapters only translate a provider's HTTP contract into one small internal request /
response contract. The governed WebFlow/Engenie bridge remains responsible for
flow discovery, parameter requirements, destructive-action confirmation and job
submission.

Adding a provider later should normally require only:
  1. one adapter class in this module (or a new module imported here),
  2. one entry in ``config/ai-providers.json``, and
  3. environment variables / BTP bindings for credentials.

No provider secret is ever written to config files or returned by status APIs.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AIProviderError(RuntimeError):
    """Raised when an adapter cannot complete or normalize a provider request."""


@dataclass
class ProviderResult:
    """Small normalized result returned by every adapter.

    ``text`` is intentionally the only provider-generated field the orchestration
    layer needs. Raw provider payloads are optional diagnostics and are never
    required by WebFlow business logic.
    """

    text: str
    model: Optional[str] = None
    provider_request_id: Optional[str] = None
    raw: Optional[dict] = None


class BaseAIProvider:
    """Base class and extension contract for all Phase 13 provider adapters."""

    adapter_type = "base"

    def __init__(self, definition: dict):
        self.definition = dict(definition or {})
        self.id = str(self.definition.get("id") or self.adapter_type)
        self.name = str(self.definition.get("name") or self.id)
        self.model = str(self.definition.get("model") or "") or None
        self.timeout_seconds = int(self.definition.get("timeout_seconds") or 45)

    # ----- configuration helpers -------------------------------------------------
    # Provider definitions store *environment-variable names*, never actual keys.
    # That lets the same package run locally, in CF user-provided services, or with
    # a future BTP credential-vault adapter without changing the UI or core model.
    def env(self, key: str, default: Optional[str] = None) -> Optional[str]:
        env_name = self.definition.get(key)
        if not env_name:
            return default
        return os.getenv(str(env_name), default)

    def configured(self) -> bool:
        """Return True when the minimum credentials/config are present."""
        return True

    def status(self) -> dict:
        """Safe status information for the UI; never includes credential values."""
        return {
            "id": self.id,
            "name": self.name,
            "adapter": self.adapter_type,
            "enabled": bool(self.definition.get("enabled", True)),
            "configured": self.configured(),
            "model": self.model,
            "description": self.definition.get("description", ""),
            "credential_source": self.definition.get("credential_source", "environment"),
            "secret_env": self.definition.get("secret_env"),
            "base_url_env": self.definition.get("base_url_env"),
            "supports": list(self.definition.get("supports") or ["text"]),
        }

    def generate(self, *, system: str, messages: List[dict], response_format: str = "text") -> ProviderResult:
        raise NotImplementedError

    # ----- shared HTTP helper -----------------------------------------------------
    # Kept in the base class so enterprise adapters only need to define request
    # headers/body and response extraction. This avoids duplicated TLS/error code.
    def _post_json(self, url: str, headers: dict, body: dict) -> tuple[dict, dict]:
        payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=ssl.create_default_context()) as response:
                raw = response.read().decode("utf-8")
                parsed = json.loads(raw) if raw else {}
                meta = {k.lower(): v for k, v in response.headers.items()}
                return parsed, meta
        except urllib.error.HTTPError as exc:
            # Keep the returned error bounded. Provider responses can include prompt
            # fragments, so we do not persist or surface an unlimited raw body.
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1200]
            except Exception:
                detail = ""
            raise AIProviderError(f"{self.name} returned HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise AIProviderError(f"Could not reach {self.name}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise AIProviderError(f"{self.name} timed out after {self.timeout_seconds}s") from exc


class LocalDeterministicProvider(BaseAIProvider):
    """No-network adapter used for local testing and safe fallback.

    It does not try to be an LLM. It returns the user's message verbatim so the
    Phase 13 orchestrator can fall back to the deterministic Engenie bridge. Having
    this adapter means provider selection/status can be tested without API keys.
    """

    adapter_type = "local-deterministic"

    def generate(self, *, system: str, messages: List[dict], response_format: str = "text") -> ProviderResult:
        text = ""
        for message in reversed(messages or []):
            if str(message.get("role")) == "user":
                text = str(message.get("content") or "")
                break
        return ProviderResult(text=text, model="deterministic", raw={"fallback": True})


class OpenAIResponsesProvider(BaseAIProvider):
    """Adapter for OpenAI's Responses-style HTTP contract.

    Endpoint/model/key are environment-driven so deployments can override them
    without changing WebFlow code. The adapter intentionally uses the Python
    standard library rather than an SDK; this keeps provider packages optional.
    """

    adapter_type = "openai-responses"
    DEFAULT_URL = "https://api.openai.com/v1/responses"

    def configured(self) -> bool:
        return bool(self.env("secret_env"))

    def generate(self, *, system: str, messages: List[dict], response_format: str = "text") -> ProviderResult:
        key = self.env("secret_env")
        if not key:
            raise AIProviderError(f"{self.name} is not configured. Set {self.definition.get('secret_env') or 'its API-key environment variable'}.")
        url = self.env("base_url_env", self.definition.get("base_url") or self.DEFAULT_URL)
        input_items = []
        if system:
            input_items.append({"role": "system", "content": system})
        input_items.extend({"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")} for m in messages)
        body: Dict[str, Any] = {"model": self.model or "gpt-5-mini", "input": input_items}
        # JSON mode is requested only for the orchestration command envelope. If a
        # future model/provider uses another shape, only this adapter must change.
        if response_format == "json":
            body["text"] = {"format": {"type": "json_object"}}
        data, meta = self._post_json(url, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, body)
        text = data.get("output_text")
        if not text:
            # Defensive extraction for providers/proxies that omit output_text.
            chunks = []
            for item in data.get("output") or []:
                for content in item.get("content") or []:
                    if content.get("type") in {"output_text", "text"} and content.get("text"):
                        chunks.append(str(content.get("text")))
            text = "\n".join(chunks)
        if not text:
            raise AIProviderError(f"{self.name} returned no text output.")
        return ProviderResult(text=str(text), model=str(data.get("model") or self.model or ""), provider_request_id=data.get("id") or meta.get("x-request-id"), raw=data)


class AnthropicMessagesProvider(BaseAIProvider):
    """Adapter for Anthropic's Messages-style HTTP contract."""

    adapter_type = "anthropic-messages"
    DEFAULT_URL = "https://api.anthropic.com/v1/messages"

    def configured(self) -> bool:
        return bool(self.env("secret_env"))

    def generate(self, *, system: str, messages: List[dict], response_format: str = "text") -> ProviderResult:
        key = self.env("secret_env")
        if not key:
            raise AIProviderError(f"{self.name} is not configured. Set {self.definition.get('secret_env') or 'its API-key environment variable'}.")
        url = self.env("base_url_env", self.definition.get("base_url") or self.DEFAULT_URL)
        body = {
            "model": self.model or "claude-sonnet-4-5",
            "max_tokens": int(self.definition.get("max_tokens") or 1200),
            "system": system,
            "messages": [{"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")} for m in messages if m.get("role") in {"user", "assistant"}],
        }
        data, meta = self._post_json(url, {"x-api-key": key, "anthropic-version": str(self.definition.get("api_version") or "2023-06-01"), "Content-Type": "application/json"}, body)
        text = "\n".join(str(x.get("text")) for x in (data.get("content") or []) if x.get("type") == "text" and x.get("text"))
        if not text:
            raise AIProviderError(f"{self.name} returned no text output.")
        return ProviderResult(text=text, model=str(data.get("model") or self.model or ""), provider_request_id=data.get("id") or meta.get("request-id"), raw=data)


class OpenAICompatibleChatProvider(BaseAIProvider):
    """Generic adapter for OpenAI-compatible ``/chat/completions`` gateways.

    This is the intended low-friction route for providers such as internal model
    gateways, API proxies, or services that expose OpenAI-compatible chat syntax.
    Configure the endpoint and API-key environment-variable names in JSON; no new
    WebFlow code is needed for each compatible provider.
    """

    adapter_type = "openai-compatible-chat"

    def configured(self) -> bool:
        return bool(self.env("base_url_env", self.definition.get("base_url"))) and (not self.definition.get("secret_env") or bool(self.env("secret_env")))

    def generate(self, *, system: str, messages: List[dict], response_format: str = "text") -> ProviderResult:
        url = self.env("base_url_env", self.definition.get("base_url"))
        if not url:
            raise AIProviderError(f"{self.name} needs a base URL.")
        key = self.env("secret_env") if self.definition.get("secret_env") else None
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body: Dict[str, Any] = {
            "model": self.model or self.definition.get("default_model") or "default",
            "messages": ([{"role": "system", "content": system}] if system else []) + [{"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")} for m in messages],
        }
        if response_format == "json":
            body["response_format"] = {"type": "json_object"}
        data, meta = self._post_json(url, headers, body)
        choices = data.get("choices") or []
        text = (((choices[0] if choices else {}).get("message") or {}).get("content"))
        if not text:
            raise AIProviderError(f"{self.name} returned no chat message content.")
        return ProviderResult(text=str(text), model=str(data.get("model") or self.model or ""), provider_request_id=data.get("id") or meta.get("x-request-id"), raw=data)


class GenericEnterpriseJSONProvider(BaseAIProvider):
    """Commented extension point for Joule, Copilot and enterprise agent gateways.

    Enterprise assistants frequently require organization-specific OAuth tokens,
    tenant URLs, custom request envelopes, or an API Management facade. Rather than
    hard-code one tenant's contract, this adapter supports the common case where a
    gateway accepts ``{system, messages, model}`` and returns a configurable text
    field. If the eventual API differs, subclass ``BaseAIProvider`` and reuse the
    registry/orchestrator unchanged.
    """

    adapter_type = "generic-enterprise-json"

    def configured(self) -> bool:
        return bool(self.env("base_url_env", self.definition.get("base_url"))) and (not self.definition.get("secret_env") or bool(self.env("secret_env")))

    @staticmethod
    def _read_path(data: Any, path: str) -> Any:
        value = data
        for part in str(path or "text").split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value

    def generate(self, *, system: str, messages: List[dict], response_format: str = "text") -> ProviderResult:
        url = self.env("base_url_env", self.definition.get("base_url"))
        if not url:
            raise AIProviderError(f"{self.name} needs its enterprise gateway URL configured.")
        headers = {"Content-Type": "application/json"}
        token = self.env("secret_env") if self.definition.get("secret_env") else None
        header_name = str(self.definition.get("auth_header") or "Authorization")
        if token:
            prefix = str(self.definition.get("auth_prefix") or "Bearer ")
            headers[header_name] = prefix + token
        body = {"system": system, "messages": messages, "model": self.model, "response_format": response_format}
        data, meta = self._post_json(url, headers, body)
        text = self._read_path(data, str(self.definition.get("response_text_path") or "text"))
        if not text:
            raise AIProviderError(f"{self.name} response did not contain '{self.definition.get('response_text_path') or 'text'}'.")
        return ProviderResult(text=str(text), model=str(data.get("model") or self.model or ""), provider_request_id=data.get("id") or meta.get("x-request-id"), raw=data)


ADAPTER_TYPES = {
    LocalDeterministicProvider.adapter_type: LocalDeterministicProvider,
    OpenAIResponsesProvider.adapter_type: OpenAIResponsesProvider,
    AnthropicMessagesProvider.adapter_type: AnthropicMessagesProvider,
    OpenAICompatibleChatProvider.adapter_type: OpenAICompatibleChatProvider,
    GenericEnterpriseJSONProvider.adapter_type: GenericEnterpriseJSONProvider,
}


class AIProviderRegistry:
    """Loads adapter definitions and exposes one provider-neutral registry API."""

    SCHEMA = "webflow-ai-provider-registry/1"

    def __init__(self, config_path):
        self.config_path = config_path
        self._config = self._load_config()
        self._providers: Dict[str, BaseAIProvider] = {}
        self.reload()

    def _load_config(self) -> dict:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            return {"schema": self.SCHEMA, "default_provider": "local", "providers": []}

    def reload(self) -> None:
        self._config = self._load_config()
        self._providers = {}
        for definition in self._config.get("providers") or []:
            if not isinstance(definition, dict) or not definition.get("id"):
                continue
            cls = ADAPTER_TYPES.get(str(definition.get("adapter") or ""))
            if not cls:
                # Unknown adapter types are reported by status but not instantiated.
                continue
            provider = cls(definition)
            self._providers[provider.id] = provider

    @property
    def default_provider_id(self) -> str:
        configured_default = os.getenv("WEBFLOW_AI_PROVIDER") or self._config.get("default_provider") or "local"
        return str(configured_default)

    def get(self, provider_id: Optional[str] = None) -> Optional[BaseAIProvider]:
        return self._providers.get(str(provider_id or self.default_provider_id))

    def status(self) -> dict:
        providers = []
        definitions = {str(x.get("id")): x for x in (self._config.get("providers") or []) if isinstance(x, dict)}
        for provider_id, definition in definitions.items():
            provider = self._providers.get(provider_id)
            if provider:
                item = provider.status()
            else:
                item = {
                    "id": provider_id,
                    "name": definition.get("name") or provider_id,
                    "adapter": definition.get("adapter"),
                    "enabled": bool(definition.get("enabled", True)),
                    "configured": False,
                    "model": definition.get("model"),
                    "description": definition.get("description", ""),
                    "error": "adapter_type_not_registered",
                }
            item["default"] = provider_id == self.default_provider_id
            providers.append(item)
        return {
            "schema": self.SCHEMA,
            "default_provider": self.default_provider_id,
            "count": len(providers),
            "configured_count": sum(1 for x in providers if x.get("configured") and x.get("enabled")),
            "providers": providers,
            "updated_at": utc_now(),
        }
