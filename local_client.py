from __future__ import annotations

"""ALM WebFlow Local Client.

The client is deliberately outbound-only: it polls the hosted Studio over HTTP(S)
and never listens on a local port.  It can therefore automate applications that
are already reachable from the user's workstation without becoming a VPN,
proxy, port-forwarder, or generic remote shell.
"""

from pathlib import Path
from urllib import request, parse, error
from datetime import datetime, timezone
import argparse
import hashlib
import hmac
import json
import os
import platform
import socket
import sys
import tempfile
import threading
import time

from recorder import RecorderManager
from execution_engine import ExecutionManager
from hybrid_runtime import request_signature, task_signature


def utc_now(): return datetime.now(timezone.utc).isoformat()


class StudioClient:
    def __init__(self, base_url: str, client_id: str, secret: str):
        self.base = base_url.rstrip("/")
        self.client_id = client_id
        self.secret = secret

    def call(self, method: str, path: str, payload=None, signed=True):
        body = json.dumps(payload or {}).encode("utf-8") if method != "GET" else b""
        headers = {"Content-Type": "application/json"}
        if signed:
            ts = utc_now()
            headers.update({
                "X-WebFlow-Client-ID": self.client_id,
                "X-WebFlow-Timestamp": ts,
                "X-WebFlow-Signature": request_signature(self.secret, ts, method, path, body),
            })
        req = request.Request(self.base + path, data=body if method != "GET" else None, headers=headers, method=method)
        with request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8") or "{}")


class LocalAgent:
    def __init__(self, studio: StudioClient, root: Path):
        self.studio = studio
        self.root = root
        self.active_task_id = None
        self.recorder = RecorderManager(root, self._write_json, None)
        self.executor = ExecutionManager(root, self._write_json, None)
        self.last_sent_fingerprint = None

    @staticmethod
    def _write_json(path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        tmp.replace(path)

    def capabilities(self):
        return ["record", "execute", "playwright-chromium", "signed-tasks"]

    def verify_task(self, task: dict) -> bool:
        signature = task.get("signature") or ""
        unsigned = {k:v for k,v in task.items() if k != "signature"}
        expected = task_signature(self.studio.secret, unsigned)
        return hmac.compare_digest(signature, expected)

    def _collect_recording_artifacts(self, recording: dict) -> dict:
        import base64
        artifacts={}
        shots_root=(self.root/"data"/"recordings"/"screenshots").resolve()
        for step in (recording or {}).get("steps",[]) or []:
            shot=step.get("screenshot")
            if not shot: continue
            rel=str(shot).lstrip("/").replace("\\","/")
            fp=(self.root/rel).resolve()
            try: fp.relative_to(shots_root)
            except Exception: continue
            if fp.exists() and fp.is_file() and fp.stat().st_size<=5*1024*1024:
                artifacts[rel]=base64.b64encode(fp.read_bytes()).decode("ascii")
        return artifacts

    def send_heartbeat(self):
        st = self.recorder.status()
        rec = self.recorder.recording_data(st.get("recording_id")) if st.get("recording_id") else None
        # Avoid uploading the same potentially-large recording snapshot every poll.
        fingerprint = hashlib.sha256(json.dumps(rec or {}, sort_keys=True).encode()).hexdigest() if rec else None
        include_rec = rec if fingerprint != self.last_sent_fingerprint else None
        payload = {
            "state": "busy" if self.active_task_id else "online",
            "capabilities": self.capabilities(),
            "active_task_id": self.active_task_id,
            "active_recording_id": st.get("recording_id") if st.get("state") in {"starting","recording","paused","stopping"} else None,
            "status": st,
            "recording": include_rec,
            "recording_artifacts": self._collect_recording_artifacts(include_rec) if include_rec else {},
        }
        self.studio.call("POST", "/api/hybrid/client/heartbeat", payload)
        if include_rec is not None: self.last_sent_fingerprint = fingerprint

    def _collect_run_artifacts(self, run: dict) -> dict:
        """Collect only WebFlow-created run screenshots for upload to Studio.

        This is intentionally not a general file-upload primitive.  Paths must
        resolve under this client's data/runs directory.
        """
        import base64
        artifacts={}
        runs_root=(self.root/"data"/"runs").resolve()
        for step in run.get("steps",[]) or []:
            shot=step.get("screenshot")
            if not shot: continue
            rel=str(shot).lstrip("/").replace("\\","/")
            fp=(self.root/rel).resolve()
            try:
                fp.relative_to(runs_root)
            except Exception:
                continue
            if fp.exists() and fp.is_file() and fp.stat().st_size <= 5*1024*1024:
                artifacts[rel]=base64.b64encode(fp.read_bytes()).decode("ascii")
        return artifacts

    def _execute_task(self, p: dict):
        rec=p.get("recording") or {}
        ok,run=self.executor.start(
            rec,flow_id=p.get("flow_id") or rec.get("flow_id"),
            headless=bool(p.get("headless",True)),timeout_ms=p.get("timeout_ms",12000),
            retries=p.get("retries",1),failure_policy=p.get("failure_policy","stop"),
            variables=p.get("variables") or {},logic_steps=p.get("logic_steps"),subflows=p.get("subflows") or {}
        )
        if not ok: return False,run
        while True:
            st=self.executor.status()
            # Keep Studio aware that this signed task is still alive.
            self.send_heartbeat()
            if st.get("state") not in {"starting","running","cancelling"}: break
            # Poll only for an explicit typed cancellation command while running.
            try:
                incoming=self.studio.call("GET","/api/hybrid/client/poll").get("task")
                if incoming:
                    if self.verify_task(incoming) and incoming.get("type")=="cancel_execution":
                        self.executor.cancel()
                        self.studio.call("POST","/api/hybrid/client/result",{"task_id":incoming.get("id"),"result":{"ok":True,"state":"cancelling"}})
                    else:
                        self.studio.call("POST","/api/hybrid/client/result",{"task_id":incoming.get("id"),"result":{"ok":False,"error":"client_busy"}})
            except Exception:
                pass
            time.sleep(.5)
        run=self.executor.status()
        return run.get("state")=="completed", {"run":run,"artifacts":self._collect_run_artifacts(run)}

    def handle_task(self, task: dict):
        if not self.verify_task(task):
            raise RuntimeError("Studio task signature verification failed")
        self.active_task_id = task.get("id")
        typ = task.get("type")
        p = task.get("payload") or {}
        ok = True; result = {}
        try:
            if typ == "record_start":
                rr = self.recorder.start(p.get("flow_id") or None, p.get("url") or "", recording_name=p.get("recording_name") or "New Recording")
                ok, result = rr.ok, rr.payload
                # Publish the active recording ID before marking the start task
                # complete, avoiding a brief hosted-UI fallback to idle.
                if rr.ok: self.send_heartbeat()
            elif typ == "record_pause":
                rr = self.recorder.pause(); ok, result = rr.ok, rr.payload
            elif typ == "record_resume":
                rr = self.recorder.resume(); ok, result = rr.ok, rr.payload
            elif typ == "record_stop":
                rr = self.recorder.stop(); ok, result = rr.ok, rr.payload
                # Force a final recording upload after stop/finalize.
                self.last_sent_fingerprint = None
                self.send_heartbeat()
            elif typ == "execute_flow":
                ok, result = self._execute_task(p)
            elif typ == "cancel_execution":
                ok, result = self.executor.cancel()
            else:
                ok = False; result = {"error": "unsupported_local_task", "type": typ}
        except Exception as exc:
            ok = False; result = {"error": "client_task_failed", "message": str(exc)}
        self.studio.call("POST", "/api/hybrid/client/result", {"task_id": task.get("id"), "result": {"ok": ok, **result}})
        self.active_task_id = None

    def run(self):
        print(f"[WebFlow Local Client] connected to {self.studio.base}")
        print("[WebFlow Local Client] outbound-only mode; no listening port is opened")
        while True:
            try:
                self.send_heartbeat()
                response = self.studio.call("GET", "/api/hybrid/client/poll")
                task = response.get("task")
                if task: self.handle_task(task)
            except KeyboardInterrupt:
                print("\n[WebFlow Local Client] stopped")
                return
            except Exception as exc:
                print("[WebFlow Local Client]", exc)
            time.sleep(1.0)



def _config_path() -> Path:
    """Return a per-user credential/config location.

    The downloaded EXE can therefore be launched by a non-technical user after
    the first bootstrap without environment variables or command-line flags.
    """
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "ALM WebFlow Studio" / "local-client.json"
    return Path(os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "alm-webflow-studio" / "local-client.json"


def _load_json_file(path: Path) -> dict:
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else {}
    except Exception:
        return {}


def _save_client_config(path: Path, data: dict):
    """Persist the paired client identity for the current OS user only.

    The file contains the machine/client HMAC credential and should be protected
    by normal workstation profile permissions.  Enterprise deployments can swap
    this for Windows Credential Manager/DPAPI without changing the transport
    contract.
    """
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,indent=2),encoding='utf-8')
    tmp.replace(path)
    try:
        os.chmod(path,0o600)
    except Exception:
        pass


def _default_bootstrap_path() -> Path:
    """Look beside the executable/script for the one-time bootstrap file."""
    base = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
    return base / 'webflow-client.bootstrap.json'


def _consume_bootstrap(path: Path) -> dict:
    data=_load_json_file(path)
    if data:
        # The pairing token is one-time.  Remove the bootstrap as soon as it has
        # been consumed successfully so it is not left lying around on disk.
        data['_bootstrap_path']=str(path)
    return data

def pair(base: str, token: str, name: str) -> dict:
    payload = json.dumps({
        "pairing_token": token,
        "name": name,
        "metadata": {"host": socket.gethostname(), "platform": platform.platform(), "python": sys.version.split()[0]},
    }).encode("utf-8")
    req = request.Request(base.rstrip("/") + "/api/hybrid/client/pair", data=payload, headers={"Content-Type":"application/json"}, method="POST")
    with request.urlopen(req, timeout=30) as res:
        return json.loads(res.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="ALM WebFlow outbound-only local execution client")
    ap.add_argument("--studio", help="Hosted Studio URL. Optional when a bootstrap/config file is present.")
    ap.add_argument("--pair", help="Advanced/manual first-use pairing token")
    ap.add_argument("--bootstrap", help="Optional path to webflow-client.bootstrap.json")
    ap.add_argument("--name", default=f"{socket.gethostname()} WebFlow Client")
    ap.add_argument("--client-id", default=os.getenv("WEBFLOW_CLIENT_ID"))
    ap.add_argument("--secret", default=os.getenv("WEBFLOW_CLIENT_SECRET"))
    ap.add_argument("--reset", action="store_true", help="Forget the saved pairing on this workstation")
    args = ap.parse_args()

    cfg_path=_config_path()
    if args.reset:
        try: cfg_path.unlink(missing_ok=True)
        except Exception: pass
        print("Saved WebFlow Local Client pairing removed.")
        return

    saved=_load_json_file(cfg_path)
    bootstrap_path=Path(args.bootstrap).resolve() if args.bootstrap else _default_bootstrap_path()
    bootstrap=_consume_bootstrap(bootstrap_path) if bootstrap_path.exists() else {}

    studio=(args.studio or bootstrap.get('studio_url') or saved.get('studio_url') or '').rstrip('/')
    client_id=args.client_id or saved.get('client_id')
    secret=args.secret or saved.get('client_secret')
    pair_token=args.pair or bootstrap.get('pairing_token')
    client_name=bootstrap.get('client_name') or saved.get('client_name') or args.name

    if not studio:
        ap.error("No Studio URL was found. Download the Local Client bundle from WebFlow Studio or provide --studio.")

    if pair_token:
        data = pair(studio, pair_token, client_name)
        client_id, secret = data["client_id"], data["client_secret"]
        _save_client_config(cfg_path,{
            'studio_url':studio,
            'client_id':client_id,
            'client_secret':secret,
            'client_name':data.get('name') or client_name,
            'paired_at':utc_now(),
        })
        try:
            if bootstrap.get('_bootstrap_path'):
                Path(bootstrap['_bootstrap_path']).unlink(missing_ok=True)
        except Exception:
            pass
        print(f"WebFlow Local Client paired successfully as {data.get('name') or client_name}.")

    if not client_id or not secret:
        ap.error("This workstation is not paired. Download a Local Client bundle from WebFlow Studio.")

    root = Path(tempfile.gettempdir()) / "alm-webflow-local-client"
    root.mkdir(parents=True, exist_ok=True)
    LocalAgent(StudioClient(studio, client_id, secret), root).run()


if __name__ == "__main__": main()
