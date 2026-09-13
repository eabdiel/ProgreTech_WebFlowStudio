from __future__ import annotations

"""Hybrid local-client coordination for ALM WebFlow Studio.

Security model
--------------
The local client NEVER opens an inbound port and it never tunnels arbitrary
traffic through the corporate firewall.  It makes outbound HTTPS requests to
the Studio, polls for narrowly typed WebFlow tasks, verifies each task with a
per-client HMAC secret, and returns only WebFlow status/artifacts.

This is intentionally not a generic remote-shell protocol.  The task allowlist
is explicit in ``queue_task`` and should stay that way as new client-side
capabilities are added.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
import hashlib
import hmac
import json
import secrets
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def task_signature(secret: str, task: dict) -> str:
    """Sign the immutable task envelope sent from Studio to a local client."""
    body = stable_json(task).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def request_signature(secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    """Sign one client -> Studio HTTP request.

    The timestamp is part of the signature and the server rejects old requests,
    limiting replay of a captured heartbeat/result request.
    """
    digest = hashlib.sha256(body or b"").hexdigest()
    canonical = f"{timestamp}\n{method.upper()}\n{path}\n{digest}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


class HybridClientManager:
    """Persistent registry + typed task mailbox for local WebFlow clients.

    Storage is intentionally JSON for the standalone prototype.  On BTP the
    existing Object Store mirror persists this file just like other WebFlow
    state.  A production scale-out implementation can later replace the mailbox
    with a durable broker while preserving this API contract.
    """

    ALLOWED_TASKS = {
        "record_start", "record_pause", "record_resume", "record_stop",
        "execute_flow", "cancel_execution",
    }

    def __init__(self, root: Path, write_json_callback, recording_callback=None, result_callback=None):
        self.root = Path(root)
        self.write_json = write_json_callback
        self.recording_callback = recording_callback
        self.result_callback = result_callback
        self.data_dir = self.root / "data" / "hybrid"
        self.registry_file = self.data_dir / "clients.json"
        self.tasks_file = self.data_dir / "tasks.json"
        self.pair_file = self.data_dir / "pairing_tokens.json"
        self.lock = Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path, default in ((self.registry_file, []), (self.tasks_file, []), (self.pair_file, [])):
            if not path.exists():
                self.write_json(path, default)

    def _read(self, path: Path, fallback):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return fallback

    def create_pairing_token(self, session: dict, ttl_minutes: int = 10) -> dict:
        token = secrets.token_urlsafe(24)
        row = {
            "id": str(uuid.uuid4()),
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "created_at": utc_now(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=max(2, min(60, ttl_minutes)))).isoformat(),
            "created_by": session.get("user_name") or session.get("display_name") or "unknown",
            "used_at": None,
        }
        with self.lock:
            rows = self._read(self.pair_file, [])
            rows.append(row)
            self.write_json(self.pair_file, rows[-50:])
        # Plain token is returned once and is never persisted.
        return {"pairing_token": token, "expires_at": row["expires_at"]}

    def pair(self, token: str, client_name: str, metadata: dict | None = None) -> tuple[bool, dict]:
        token_hash = hashlib.sha256(str(token or "").encode()).hexdigest()
        now = datetime.now(timezone.utc)
        with self.lock:
            rows = self._read(self.pair_file, [])
            match = None
            for row in rows:
                try:
                    expires = datetime.fromisoformat(row.get("expires_at"))
                except Exception:
                    continue
                if not row.get("used_at") and expires > now and hmac.compare_digest(row.get("token_hash", ""), token_hash):
                    match = row; break
            if not match:
                return False, {"error": "invalid_pairing_token", "message": "Pairing token is invalid, expired, or already used."}
            match["used_at"] = utc_now()
            secret = secrets.token_urlsafe(40)
            client = {
                "id": str(uuid.uuid4()),
                "name": str(client_name or "WebFlow Local Client")[:120],
                "secret": secret,  # prototype persistence; replace with credential service/encryption at rest for production
                "paired_at": utc_now(),
                "paired_by": match.get("created_by"),
                "last_seen": None,
                "state": "offline",
                "capabilities": [],
                "metadata": metadata or {},
                "active_task_id": None,
                "active_recording_id": None,
                "status": {},
            }
            clients = self._read(self.registry_file, [])
            clients.append(client)
            self.write_json(self.registry_file, clients)
            self.write_json(self.pair_file, rows)
        return True, {"client_id": client["id"], "client_secret": secret, "name": client["name"]}

    def clients(self, include_secret: bool = False) -> list[dict]:
        rows = self._read(self.registry_file, [])
        result = []
        for row in rows:
            item = dict(row)
            if not include_secret:
                item.pop("secret", None)
            result.append(item)
        return result

    def get_client(self, client_id: str, include_secret: bool = False):
        row = next((x for x in self._read(self.registry_file, []) if x.get("id") == client_id), None)
        if not row: return None
        row = dict(row)
        if not include_secret: row.pop("secret", None)
        return row

    def verify_request(self, client_id: str, timestamp: str, method: str, path: str, body: bytes, signature: str) -> bool:
        client = self.get_client(client_id, include_secret=True)
        if not client or not signature or not timestamp: return False
        try:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if abs((datetime.now(timezone.utc) - ts).total_seconds()) > 300:
                return False
        except Exception:
            return False
        expected = request_signature(client["secret"], timestamp, method, path, body)
        return hmac.compare_digest(expected, signature)

    def queue_task(self, client_id: str, task_type: str, payload: dict, requested_by: dict | None = None) -> tuple[bool, dict]:
        if task_type not in self.ALLOWED_TASKS:
            return False, {"error": "task_type_not_allowed"}
        client = self.get_client(client_id, include_secret=True)
        if not client:
            return False, {"error": "client_not_found"}
        envelope = {
            "id": str(uuid.uuid4()),
            "type": task_type,
            "client_id": client_id,
            "created_at": utc_now(),
            "requested_by": {
                "user_name": (requested_by or {}).get("user_name"),
                "display_name": (requested_by or {}).get("display_name"),
                "source": (requested_by or {}).get("source"),
            },
            "payload": payload or {},
            "state": "queued",
            "claimed_at": None,
            "completed_at": None,
        }
        envelope["signature"] = task_signature(client["secret"], {k:v for k,v in envelope.items() if k != "signature"})
        with self.lock:
            tasks = self._read(self.tasks_file, [])
            tasks.append(envelope)
            self.write_json(self.tasks_file, tasks[-1000:])
        return True, {k:v for k,v in envelope.items() if k != "signature"}

    def poll(self, client_id: str) -> dict:
        with self.lock:
            tasks = self._read(self.tasks_file, [])
            task = next((t for t in tasks if t.get("client_id") == client_id and t.get("state") == "queued"), None)
            if not task:
                return {"task": None}
            task["state"] = "claimed"; task["claimed_at"] = utc_now()
            client = self.get_client(client_id, include_secret=True)
            if client:
                task["signature"] = task_signature(client["secret"], {k:v for k,v in task.items() if k != "signature"})
            self.write_json(self.tasks_file, tasks)
            return {"task": task}

    def heartbeat(self, client_id: str, payload: dict) -> dict:
        recording = payload.get("recording") if isinstance(payload, dict) else None
        with self.lock:
            clients = self._read(self.registry_file, [])
            row = next((x for x in clients if x.get("id") == client_id), None)
            if not row: return {"error": "client_not_found"}
            row.update({
                "last_seen": utc_now(),
                "state": payload.get("state") or "online",
                "capabilities": payload.get("capabilities") or row.get("capabilities") or [],
                "active_task_id": payload.get("active_task_id"),
                "active_recording_id": (((recording or {}).get("id") or payload.get("active_recording_id")) if (payload.get("status") or {}).get("state") in {"starting","recording","paused","stopping"} else None),
                "status": payload.get("status") or {},
            })
            self.write_json(self.registry_file, clients)
        if isinstance(recording, dict) and recording.get("id"):
            # The server is the system of record.  Local client snapshots are
            # normalized/persisted here so the cloud UI can preview them.
            if self.recording_callback:
                self.recording_callback(recording, artifacts=payload.get("recording_artifacts") or {}, final=recording.get("status") in {"stopped", "completed", "error"})
        return {"ok": True, "server_time": utc_now()}

    def get_task(self, task_id: str) -> dict | None:
        task = next((t for t in self._read(self.tasks_file, []) if t.get("id") == task_id), None)
        if not task: return None
        clean = dict(task); clean.pop("signature", None)
        return clean

    def complete_task(self, client_id: str, task_id: str, result: dict) -> dict:
        with self.lock:
            tasks = self._read(self.tasks_file, [])
            task = next((t for t in tasks if t.get("id") == task_id and t.get("client_id") == client_id), None)
            if not task: return {"error": "task_not_found"}
            task["state"] = "completed" if result.get("ok", True) else "failed"
            task["completed_at"] = utc_now()
            task["result"] = result
            self.write_json(self.tasks_file, tasks)
        if self.result_callback:
            try: self.result_callback(task, result)
            except Exception as exc: return {"ok": False, "error": "result_persist_failed", "message": str(exc)}
        return {"ok": True}

    def latest_recording_status(self) -> dict | None:
        clients = self.clients()
        active = [c for c in clients if c.get("active_recording_id") and (c.get("status") or {}).get("state") in {"starting","recording","paused","stopping"}]
        if active:
            c = sorted(active, key=lambda x: x.get("last_seen") or "", reverse=True)[0]
            st = dict(c.get("status") or {})
            st["client_id"] = c.get("id")
            st["client_name"] = c.get("name")
            st["recording_id"] = c.get("active_recording_id") or st.get("recording_id")
            st["execution_location"] = "local-client"
            return st
        # Preserve the Studio's "Starting" state while a newly-dispatched
        # record_start task is waiting for the client heartbeat. Otherwise the
        # hosted UI would briefly fall back to the server's idle recorder.
        tasks=self._read(self.tasks_file,[])
        pending=[t for t in tasks if t.get("type")=="record_start" and t.get("state") in {"queued","claimed"}]
        if pending:
            t=sorted(pending,key=lambda x:x.get("created_at") or "",reverse=True)[0]
            c=self.get_client(t.get("client_id")) or {}
            p=t.get("payload") or {}
            return {"state":"starting","recording_id":None,"flow_id":p.get("flow_id"),"url":p.get("url"),"steps":0,"error":None,"latest_step":None,"latest_screenshot":None,"client_id":t.get("client_id"),"client_name":c.get("name"),"execution_location":"local-client"}
        return None
