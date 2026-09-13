"""External-provider orchestration for ALM WebFlow Studio Phase 13.

The orchestrator is intentionally narrow: an AI provider may *interpret* what the
user wants, but it never bypasses the governed ``EngenieBridge``. All actions are
translated into the same list/describe/prepare/invoke/status/tutorial operations
implemented in Phase 12.

This separation is the key future-proofing rule for provider adapters:

    provider -> normalized command -> EngenieBridge -> WebFlow queue/runtime

Changing GPT/Claude/Joule/Copilot/1minAI therefore cannot change selector logic,
execution rules, destructive-action confirmation, secret handling, or job state.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from ai_providers import AIProviderError, AIProviderRegistry


ALLOWED_ACTIONS = {"list", "describe", "prepare", "invoke", "status", "tutorial", "clarify"}


class AIOrchestrator:
    SCHEMA = "webflow-ai-orchestrator/1"

    def __init__(self, registry: AIProviderRegistry, engenie_bridge):
        self.registry = registry
        self.engenie = engenie_bridge

    def _catalog_for_prompt(self) -> list[dict]:
        """Return only safe, compact discovery fields to the external provider."""
        discovery = self.engenie.list_flows(runnable_only=False)
        items = []
        for flow in discovery.get("flows") or []:
            items.append({
                "flow_id": flow.get("flow_id"),
                "name": flow.get("name"),
                "description": flow.get("description"),
                "runnable": bool(flow.get("runnable")),
                "confirmation_required": bool(flow.get("confirmation_required")),
            })
        return items

    def _system_prompt(self) -> str:
        """Provider-neutral command-generation prompt.

        The model is not asked to execute tools directly. It chooses a small JSON
        command that WebFlow validates locally. This makes providers with and
        without native function calling behave the same way.
        """
        catalog = json.dumps(self._catalog_for_prompt(), ensure_ascii=False)
        return (
            "You are an intent adapter for ALM WebFlow Studio. Return ONLY one JSON object. "
            "Never invent a flow_id. Never include passwords, API keys, session tokens, raw auth headers, "
            "or other secrets. You may choose only these actions: list, describe, prepare, invoke, status, "
            "tutorial, clarify. For run requests, prefer prepare first unless the request explicitly contains "
            "confirmed=true and all required non-secret variables. For destructive flows, never set confirmed=true "
            "on the user's behalf. Fields allowed in the JSON object: action, flow_id, flow_name, query, job_id, "
            "variables, confirmed, headless, message. The current safe flow catalog is: " + catalog
        )

    @staticmethod
    def _extract_json(text: str) -> dict:
        """Parse a provider response while tolerating a fenced JSON block."""
        raw = str(text or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
            raw = re.sub(r"\s*```$", "", raw)
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except Exception:
            # Last-resort extraction of the first JSON-looking object. This stays
            # local and bounded; invalid output falls back to deterministic parsing.
            match = re.search(r"\{.*\}", raw, flags=re.S)
            if match:
                try:
                    value = json.loads(match.group(0))
                    return value if isinstance(value, dict) else {}
                except Exception:
                    pass
            return {}

    @staticmethod
    def _sanitize_command(command: dict) -> dict:
        """Whitelist command fields and reject provider attempts to widen scope."""
        allowed_fields = {"action", "flow_id", "flow_name", "query", "job_id", "variables", "confirmed", "headless", "message"}
        clean = {k: v for k, v in (command or {}).items() if k in allowed_fields}
        action = str(clean.get("action") or "clarify").lower()
        clean["action"] = action if action in ALLOWED_ACTIONS else "clarify"
        if not isinstance(clean.get("variables", {}), dict):
            clean["variables"] = {}
        # An external model is never allowed to infer/force destructive approval.
        # Only a user's explicit request payload may carry confirmed=True later.
        clean["confirmed"] = bool(clean.get("confirmed", False))
        return clean

    def _execute_command(self, command: dict, *, explicit_confirmation: bool = False) -> dict:
        action = command.get("action")
        if action == "list":
            return {"action": action, "data": self.engenie.list_flows(query=command.get("query"), runnable_only=False)}
        if action == "describe":
            flow = self.engenie._find_flow(command.get("flow_id"), command.get("flow_name"), command.get("query"))
            description = self.engenie.describe(flow)
            return {"action": action, "flow": description, "error": None if description else "flow_not_resolved"}
        if action in {"prepare", "invoke"}:
            payload = {
                "flow_id": command.get("flow_id"),
                "flow_name": command.get("flow_name"),
                "query": command.get("query"),
                "variables": command.get("variables") or {},
                # Confirmation can only come from the caller's explicit boolean,
                # never merely because the model generated confirmed=true.
                "confirmed": bool(explicit_confirmation),
                "headless": bool(command.get("headless", True)),
            }
            if action == "prepare":
                ok, data = self.engenie.prepare(payload)
                return {"action": action, "ok": ok, "preparation": data}
            ok, data, status = self.engenie.invoke(payload)
            return {"action": action, "ok": ok, "status_code": status, "invocation": data}
        if action == "status":
            status = self.engenie.job_status(str(command.get("job_id") or ""))
            return {"action": action, "status": status, "error": None if status else "job_not_found"}
        if action == "tutorial":
            tutorial = self.engenie.tutorial_provider() if self.engenie.tutorial_provider else None
            return {"action": action, "tutorial": tutorial}
        return {"action": "clarify", "message": command.get("message") or "Please clarify which WebFlow or action you want."}

    def message(self, payload: dict) -> tuple[dict, int]:
        """Interpret one user message with the selected provider and execute safely."""
        payload = payload or {}
        text = str(payload.get("message") or "").strip()
        provider_id = str(payload.get("provider_id") or self.registry.default_provider_id)
        if not text:
            return {"error": "message_required"}, 400

        provider = self.registry.get(provider_id)
        if not provider:
            return {"error": "provider_not_found", "provider_id": provider_id}, 404
        status = provider.status()
        if not status.get("enabled"):
            return {"error": "provider_disabled", "provider_id": provider_id}, 409
        if not status.get("configured"):
            return {"error": "provider_not_configured", "provider": status}, 409

        # Local deterministic provider intentionally delegates to the Phase 12
        # parser. It gives developers a no-key test route for the entire UI/API.
        if provider.adapter_type == "local-deterministic":
            deterministic = self.engenie.message(text)
            return {
                "schema": self.SCHEMA,
                "provider": {"id": provider.id, "name": provider.name, "adapter": provider.adapter_type},
                "mode": "deterministic-fallback",
                "reply": deterministic.get("reply"),
                "result": deterministic,
            }, 200

        try:
            generated = provider.generate(
                system=self._system_prompt(),
                messages=[{"role": "user", "content": text}],
                response_format="json",
            )
        except AIProviderError as exc:
            return {"error": "provider_request_failed", "provider_id": provider_id, "message": str(exc)}, 502

        command = self._sanitize_command(self._extract_json(generated.text))
        # Even if the model emitted confirmed=true, only the request field below
        # can approve a destructive run. The UI exposes this as an explicit action.
        explicit_confirmation = bool(payload.get("confirmed", False))
        result = self._execute_command(command, explicit_confirmation=explicit_confirmation)
        return {
            "schema": self.SCHEMA,
            "provider": {"id": provider.id, "name": provider.name, "adapter": provider.adapter_type, "model": generated.model},
            "mode": "external-provider",
            "command": {k: v for k, v in command.items() if k != "variables"},
            # Variable values are intentionally excluded from the returned command
            # because they may eventually contain runtime credentials/secrets.
            "result": result,
            "provider_request_id": generated.provider_request_id,
        }, 200

    def test_provider(self, provider_id: str) -> tuple[dict, int]:
        """Low-risk connectivity test used by Settings / Runtime."""
        provider = self.registry.get(provider_id)
        if not provider:
            return {"error": "provider_not_found", "provider_id": provider_id}, 404
        if not provider.status().get("configured"):
            return {"error": "provider_not_configured", "provider": provider.status()}, 409
        if provider.adapter_type == "local-deterministic":
            return {"ok": True, "provider": provider.status(), "message": "Local deterministic adapter is ready; no network call was required."}, 200
        try:
            result = provider.generate(system="Return exactly the word READY.", messages=[{"role": "user", "content": "Connectivity test"}], response_format="text")
            return {"ok": True, "provider": provider.status(), "message": "Provider responded successfully.", "model": result.model, "response_preview": str(result.text)[:120]}, 200
        except AIProviderError as exc:
            return {"ok": False, "provider": provider.status(), "error": "provider_request_failed", "message": str(exc)}, 502
