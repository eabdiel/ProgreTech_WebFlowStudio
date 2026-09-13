# ALM WebFlow Studio — AI Provider Adapter Guide

Phase 13 keeps every external AI integration behind one provider-neutral boundary:

`Provider -> normalized command -> EngenieBridge -> WebFlow queue/runtime`

This is intentional. Provider code must **not** call Playwright, inspect selectors, bypass destructive-action confirmation, persist secrets, or invent its own job model.

## Add an OpenAI-compatible provider without writing Python

Add an entry to `config/ai-providers.json` using the `openai-compatible-chat` adapter and point `base_url_env` / `secret_env` at environment-variable names. Do not put actual credentials in the JSON file.

Example:

```json
{
  "id": "internal-gateway",
  "name": "Internal AI Gateway",
  "adapter": "openai-compatible-chat",
  "enabled": true,
  "model": "approved-model",
  "secret_env": "WEBFLOW_INTERNAL_AI_KEY",
  "base_url_env": "WEBFLOW_INTERNAL_AI_CHAT_URL"
}
```

## Add a provider with a different API contract

1. Subclass `BaseAIProvider` in `ai_providers.py`.
2. Give it a unique `adapter_type`.
3. Implement `configured()` and `generate()`.
4. Return `ProviderResult(text=..., model=..., provider_request_id=...)`.
5. Register the class in `ADAPTER_TYPES`.
6. Add a configuration entry in `config/ai-providers.json`.

The orchestrator and WebFlow UI do not need to change.

## Credentials

Provider definitions store the **name** of an environment variable, never the value. Locally, set the required variable in the process environment. In SAP BTP, map the same values from an approved secret/service binding. The provider status API reports only whether configuration is present.

## Enterprise assistants (Joule / Copilot / 1minAI)

The baseline ships `generic-enterprise-json` entries as integration seams because enterprise endpoints, OAuth flows and API Management facades are tenant-specific. If the approved endpoint accepts `{system, messages, model}` and returns a text field, only configuration is needed. Otherwise create a small provider subclass and keep the same registry/orchestrator contract.

## Safety rules that adapters must preserve

- External providers receive a compact, safe flow catalog only.
- They do not receive stored secret values, passwords, tokens or raw authentication headers.
- Model output is whitelisted into a small command schema before execution.
- The provider cannot self-confirm a destructive action.
- Flow execution still goes through `EngenieBridge` and the shared worker queue.
- Provider response bodies are not persisted as part of flow/run state by this layer.
