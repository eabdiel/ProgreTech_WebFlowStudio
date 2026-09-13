from __future__ import annotations

"""Phase 14 governance and pilot-hardening services for ALM WebFlow Studio.

This module is intentionally independent from Playwright and the WebFlow managers.
It is the policy layer that sits *around* recording/execution rather than becoming
part of the automation engine. That separation keeps future BTP security changes
from forcing changes to recorded-flow semantics.

The policy contract is deliberately small:
  * role checks for mutations,
  * domain allow-list evaluation,
  * destructive-flow classification + confirmation,
  * resource limits,
  * redacted/tamper-evident audit events,
  * retention cleanup + pilot readiness reporting.

Nothing in this module stores passwords, run-time variable values, uploaded file
contents, provider tokens, or generated scripts in the audit log.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
import fnmatch
import hashlib
import json
import os
import re
import shutil


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_GOVERNANCE = {
    "schema_version": "webflow-governance/1",
    "enforce_roles": True,
    "enforce_domain_allowlist": False,
    "allowed_domains": ["*.arthrex.com", "localhost", "127.0.0.1"],
    "require_destructive_confirmation": True,
    "destructive_keywords": ["approve", "submit", "sign", "delete", "remove", "post", "publish", "release", "create user", "provision"],
    "block_destructive_performance_tests": True,
    "limits": {
        "max_upload_mb": 25,
        "max_timeout_ms": 120000,
        "max_retries": 3,
        "max_performance_runs": 100,
        "max_performance_concurrency": 4,
        "max_queue_workers": 4
    },
    "retention": {
        "audit_days": 90,
        "run_artifact_days": 30,
        "performance_days": 30,
        "batch_days": 30
    },
    "pilot": {
        "require_persistent_storage_in_cloud": True,
        "require_playwright": True,
        "require_domain_allowlist_in_cloud": True,
        "require_xsuaa_roles_in_cloud": True
    }
}


class GovernanceManager:
    def __init__(self, root: Path, write_json):
        self.root = Path(root)
        self.write_json = write_json
        self.config_path = self.root / "config" / "governance.json"
        self.audit_path = self.root / "data" / "audit" / "audit.jsonl"
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.write_json(self.config_path, DEFAULT_GOVERNANCE)
        self.config = self._load_config()

    def _load_config(self) -> dict:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        # Merge shallow top-level + nested settings so older configs remain valid.
        cfg = json.loads(json.dumps(DEFAULT_GOVERNANCE))
        for key, value in raw.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        return cfg

    def get_config(self) -> dict:
        self.config = self._load_config()
        return self.config

    def update_config(self, patch: dict) -> dict:
        cfg = self.get_config()
        allowed = {"enforce_roles", "enforce_domain_allowlist", "allowed_domains", "require_destructive_confirmation", "block_destructive_performance_tests", "limits", "retention", "pilot"}
        for key, value in (patch or {}).items():
            if key not in allowed:
                continue
            if key in {"limits", "retention", "pilot"} and isinstance(value, dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
        # Normalize domains to compact strings; do not permit accidental blank wildcards.
        cfg["allowed_domains"] = [str(x).strip().lower() for x in cfg.get("allowed_domains", []) if str(x).strip()]
        self.write_json(self.config_path, cfg)
        self.config = cfg
        return cfg

    # ------------------------------ roles ------------------------------
    def authorize(self, session: dict, required: str) -> tuple[bool, dict | None]:
        """Check the product role hierarchy.

        XSUAA/JWT verification belongs to the authentication adapter. This method
        consumes the resulting role list and does not decode or trust tokens itself.
        """
        if not self.get_config().get("enforce_roles", True):
            return True, None
        roles = set(session.get("roles") or [])
        hierarchy = {
            "viewer": {"WebFlowViewer", "WebFlowRunner", "WebFlowAdmin"},
            "runner": {"WebFlowRunner", "WebFlowAdmin"},
            "admin": {"WebFlowAdmin"},
        }
        allowed = hierarchy.get(required, {required})
        if roles.intersection(allowed):
            return True, None
        return False, {"error": "forbidden", "message": f"{required.title()} access is required.", "required_role": required, "roles": sorted(roles)}

    # ------------------------------ URLs/domains ------------------------------
    def _domain_matches(self, host: str, pattern: str) -> bool:
        host = (host or "").lower().rstrip(".")
        pattern = (pattern or "").lower().strip().rstrip(".")
        if not host or not pattern:
            return False
        # fnmatch handles *.arthrex.com while exact host names remain exact.
        return fnmatch.fnmatch(host, pattern)

    def validate_url(self, url: str) -> tuple[bool, dict | None]:
        cfg = self.get_config()
        if not cfg.get("enforce_domain_allowlist"):
            return True, None
        try:
            parsed = urlparse(str(url or ""))
            host = parsed.hostname or ""
        except Exception:
            host = ""
        if not host:
            return False, {"error": "domain_not_allowed", "message": "The target URL does not contain a valid host.", "url": str(url or "")[:300]}
        if any(self._domain_matches(host, p) for p in cfg.get("allowed_domains", [])):
            return True, None
        return False, {"error": "domain_not_allowed", "message": f"Target domain '{host}' is outside the WebFlow allow-list.", "host": host}

    def validate_recording_domains(self, recording: dict | None) -> tuple[bool, dict | None]:
        if not recording:
            return False, {"error": "recording_not_found"}
        urls = []
        if recording.get("starting_url"):
            urls.append(recording.get("starting_url"))
        for step in recording.get("steps", []) or []:
            for candidate in (step.get("page_url"), step.get("url")):
                if candidate and str(candidate).startswith(("http://", "https://")):
                    urls.append(candidate)
            if str(step.get("action") or "").lower() == "navigate":
                value = step.get("value") or step.get("target_url")
                if value and str(value).startswith(("http://", "https://")):
                    urls.append(value)
        for url in dict.fromkeys(urls):
            ok, issue = self.validate_url(str(url))
            if not ok:
                return False, issue
        return True, None

    # ------------------------------ destructive governance ------------------------------
    def classify_destructive(self, flow: dict | None, recording: dict | None, design: dict | None = None) -> dict:
        """Classify risk from explicit metadata first, then recorded/designer actions.

        We intentionally avoid classifying solely from a flow title. A flow named
        "CAB Approval Validation" should not become destructive just because the
        noun "Approval" appears in its title.
        """
        flow = flow or {}
        if flow.get("destructive") is True or str(flow.get("risk") or "").lower() in {"destructive", "high"}:
            return {"destructive": True, "reason": "Flow metadata marks this automation as destructive."}
        keywords = [str(x).lower() for x in self.get_config().get("destructive_keywords", [])]
        evidence = []
        for step in (recording or {}).get("steps", []) or []:
            target = step.get("target") or {}
            text = " ".join(str(x or "") for x in [step.get("action"), target.get("label"), target.get("aria_label"), target.get("text"), step.get("training_title")]).lower()
            for kw in keywords:
                if kw and re.search(r"\b" + re.escape(kw) + r"\b", text):
                    evidence.append({"step_id": step.get("id"), "keyword": kw, "text": text[:180]})
                    break
        def walk(nodes):
            for node in nodes or []:
                text = " ".join(str(x or "") for x in [node.get("type"), node.get("name"), node.get("action")]).lower()
                for kw in keywords:
                    if kw and re.search(r"\b" + re.escape(kw) + r"\b", text):
                        evidence.append({"node_id": node.get("id"), "keyword": kw, "text": text[:180]})
                        break
                walk(node.get("then")); walk(node.get("else")); walk(node.get("steps"))
        if design:
            walk(design.get("steps"))
            for nodes in (design.get("subflows") or {}).values():
                walk(nodes)
        return {"destructive": bool(evidence), "reason": "Potentially destructive browser action detected." if evidence else "No destructive action detected.", "evidence": evidence[:10]}

    def execution_guard(self, flow: dict | None, recording: dict | None, design: dict | None, confirmed: bool, purpose: str = "execution") -> tuple[bool, dict | None]:
        ok, issue = self.validate_recording_domains(recording)
        if not ok:
            return False, issue
        risk = self.classify_destructive(flow, recording, design)
        if purpose == "performance" and risk["destructive"] and self.get_config().get("block_destructive_performance_tests", True):
            return False, {"error": "destructive_performance_blocked", "message": "Performance testing is blocked for flows that contain destructive actions.", "risk": risk}
        if risk["destructive"] and self.get_config().get("require_destructive_confirmation", True) and not confirmed:
            return False, {"error": "confirmation_required", "message": "This flow contains a potentially destructive action. Confirm explicitly before continuing.", "confirmation_required": True, "risk": risk}
        return True, None

    # ------------------------------ resource limits ------------------------------
    def clamp_execution(self, body: dict) -> dict:
        cfg = self.get_config().get("limits", {})
        result = dict(body or {})
        result["timeout_ms"] = max(1000, min(int(result.get("timeout_ms", 12000) or 12000), int(cfg.get("max_timeout_ms", 120000))))
        result["retries"] = max(0, min(int(result.get("retries", 0) or 0), int(cfg.get("max_retries", 3))))
        return result

    def validate_performance(self, body: dict) -> tuple[bool, dict | None, dict]:
        cfg = self.get_config().get("limits", {})
        result = self.clamp_execution(body)
        result["runs"] = max(1, min(int(result.get("runs", 10) or 10), int(cfg.get("max_performance_runs", 100))))
        result["warmups"] = max(0, min(int(result.get("warmups", 1) or 0), 10))
        result["concurrency"] = max(1, min(int(result.get("concurrency", 1) or 1), int(cfg.get("max_performance_concurrency", 4))))
        return True, None, result

    def validate_upload_size(self, content_base64: str) -> tuple[bool, dict | None]:
        # Base64 is ~4/3 raw bytes; checking encoded length avoids decoding huge input first.
        max_bytes = int(self.get_config().get("limits", {}).get("max_upload_mb", 25)) * 1024 * 1024
        estimated = int(len(content_base64 or "") * 0.75)
        if estimated > max_bytes:
            return False, {"error": "upload_too_large", "message": f"Upload exceeds the {max_bytes // (1024*1024)} MB pilot limit."}
        return True, None

    # ------------------------------ audit ------------------------------
    def redact(self, value):
        sensitive_fragments = ("secret", "password", "token", "authorization", "api_key", "apikey", "content_base64", "variables", "cookie")
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                key = str(k)
                out[key] = "[REDACTED]" if any(x in key.lower() for x in sensitive_fragments) else self.redact(v)
            return out
        if isinstance(value, list):
            return [self.redact(x) for x in value[:50]]
        if isinstance(value, str):
            return value[:500]
        return value

    def _last_hash(self) -> str:
        try:
            with self.audit_path.open("rb") as fh:
                lines = fh.readlines()
            if not lines:
                return ""
            return str(json.loads(lines[-1].decode("utf-8")).get("event_hash") or "")
        except Exception:
            return ""

    def audit(self, event: str, session: dict | None = None, details: dict | None = None, outcome: str = "success") -> dict:
        session = session or {}
        prev_hash = self._last_hash()
        record = {
            "schema_version": "webflow-audit-event/1",
            "time": utc_now(),
            "event": str(event),
            "outcome": str(outcome),
            "actor": {
                "user_name": session.get("user_name") or "system",
                "display_name": session.get("display_name") or session.get("user_name") or "System",
                "source": session.get("source") or "runtime",
                "roles": list(session.get("roles") or []),
            },
            "details": self.redact(details or {}),
            "previous_hash": prev_hash,
        }
        stable = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        record["event_hash"] = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def list_audit(self, limit: int = 100) -> list[dict]:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        out = []
        for line in reversed(lines[-max(1, min(int(limit), 500)):]):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out

    def verify_audit_chain(self) -> dict:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
        prev = ""
        checked = 0
        for idx, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
                event_hash = record.pop("event_hash", "")
                if record.get("previous_hash", "") != prev:
                    return {"ok": False, "events": checked, "broken_at": idx, "message": "Audit chain previous-hash mismatch."}
                stable = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                expected = hashlib.sha256(stable.encode("utf-8")).hexdigest()
                if expected != event_hash:
                    return {"ok": False, "events": checked, "broken_at": idx, "message": "Audit event hash mismatch."}
                prev = event_hash; checked += 1
            except Exception as exc:
                return {"ok": False, "events": checked, "broken_at": idx, "message": str(exc)}
        return {"ok": True, "events": checked, "message": "Audit hash chain is valid."}

    # ------------------------------ retention / pilot readiness ------------------------------
    def cleanup_retention(self) -> dict:
        cfg = self.get_config().get("retention", {})
        now = datetime.now(timezone.utc)
        removed_files = 0
        removed_bytes = 0
        roots = [
            (self.root / "data" / "runs", int(cfg.get("run_artifact_days", 30))),
            (self.root / "data" / "performance", int(cfg.get("performance_days", 30))),
            (self.root / "data" / "batches", int(cfg.get("batch_days", 30))),
        ]
        for base, days in roots:
            if not base.exists():
                continue
            cutoff = now - timedelta(days=max(1, days))
            for path in list(base.iterdir()):
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                    if mtime >= cutoff:
                        continue
                    if path.is_dir():
                        size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
                        shutil.rmtree(path)
                    else:
                        size = path.stat().st_size; path.unlink()
                    removed_files += 1; removed_bytes += size
                except Exception:
                    pass
        # Audit retention rewrites only events inside the configured time window.
        audit_days = int(cfg.get("audit_days", 90))
        cutoff = now - timedelta(days=max(1, audit_days))
        kept = []
        for event in reversed(self.list_audit(limit=500)):
            try:
                if datetime.fromisoformat(event.get("time")) >= cutoff:
                    kept.append(event)
            except Exception:
                pass
        # Do not rewrite the hash chain here; keep append-only audit semantics. Pilot
        # cleanup reports audit retention as a policy but leaves archival/deletion to
        # platform lifecycle rules in BTP Object Store.
        return {"removed_items": removed_files, "removed_bytes": removed_bytes, "audit_retention_days": audit_days, "audit_note": "Audit log remains append-only; configure Object Store lifecycle for archival/deletion."}

    def pilot_readiness(self, runtime: dict, storage: dict, playwright_ready: bool, worker_status: dict, audit_chain: dict | None = None, browser_ready: bool | None = None, hybrid_ready: bool = False) -> dict:
        cfg = self.get_config()
        cloud = bool(runtime.get("cloud_foundry"))
        checks = []
        def add(cid, label, ok, detail, warning_only=False):
            checks.append({"id": cid, "label": label, "status": "ready" if ok else ("warning" if warning_only else "blocked"), "detail": detail})
        add("ui", "Approved UI contract", True, "v0.4-approved / Phase 0 shell preserved")
        add("roles", "Role enforcement", bool(cfg.get("enforce_roles")), "Viewer / Runner / Admin")
        allow_ok = bool(cfg.get("enforce_domain_allowlist") and cfg.get("allowed_domains"))
        add("domains", "Domain allow-list", allow_ok or not cloud, ", ".join(cfg.get("allowed_domains") or []) or "Not configured", warning_only=not cloud)
        storage_ok = bool(storage.get("persistent")) or not cloud
        add("storage", "Persistent cloud storage", storage_ok, storage.get("adapter") or "local-json", warning_only=not cloud)
        if cloud:
            add("playwright", "Local browser runtime", bool(hybrid_ready), "Paired outbound-only Local Client provides Playwright/Chromium")
            if browser_ready is not None:
                add("chromium", "Browser execution boundary", bool(browser_ready), "Chromium remains on Local Client; hosted Studio does not launch the corporate browser session")
        else:
            add("playwright", "Playwright Python package", bool(playwright_ready), "Automation library import")
            if browser_ready is not None:
                add("chromium", "Chromium browser binary", bool(browser_ready), "Required for recorder/execution browser launch")
        add("audit", "Tamper-evident audit chain", bool((audit_chain or {}).get("ok", True)), (audit_chain or {}).get("message", "Audit available"))
        max_workers = int((worker_status.get("settings") or {}).get("max_workers", worker_status.get("max_workers", 0)) or 0)
        add("workers", "Bounded worker concurrency", 1 <= max_workers <= int(cfg.get("limits", {}).get("max_queue_workers", 4)), f"{max_workers} worker(s)")
        add("destructive", "Destructive confirmation", bool(cfg.get("require_destructive_confirmation")), "Explicit caller confirmation required")
        add("performance", "Destructive performance guard", bool(cfg.get("block_destructive_performance_tests")), "Destructive flows blocked from synthetic load")
        blockers = [c for c in checks if c["status"] == "blocked"]
        warnings = [c for c in checks if c["status"] == "warning"]
        return {"schema_version": "webflow-pilot-readiness/1", "ready": not blockers, "environment": runtime.get("environment"), "checks": checks, "blockers": [x["label"] for x in blockers], "warnings": [x["label"] for x in warnings], "time": utc_now()}
