from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import io
import json
import os
import threading
import shutil
import zipfile
import webbrowser
import uuid
import html as html_lib
from datetime import datetime, timezone, timedelta

from recorder import RecorderManager
from object_repository import ObjectRepository
from execution_engine import ExecutionManager
from data_manager import DataManager
from flow_designer import FlowDesignerManager
from script_manager import ScriptManager
from performance_manager import PerformanceManager
from scheduler_manager import SchedulerManager
from cloud_runtime import BTPRuntime, BTPStorageMirror
from engenie_bridge import EngenieBridge
from ai_providers import AIProviderRegistry
from ai_orchestrator import AIOrchestrator
from governance_manager import GovernanceManager
from hybrid_runtime import HybridClientManager

ROOT = Path(__file__).resolve().parent
BTP = BTPRuntime(ROOT)
RUNTIME = BTP.profile()
HOST = RUNTIME.host
PORT = RUNTIME.port
STORAGE = BTPStorageMirror(ROOT)
# Hydrate persisted cloud state before managers read their JSON/files.
STORAGE.hydrate()
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"
FLOWS_FILE = DATA_DIR / "flows.json"
CONFIG_FILE = CONFIG_DIR / "app.json"

DEFAULT_FLOWS = []

# Legacy demo IDs from the original UI prototype. They are hidden unless a real
# recording/design has been attached to them, so old installations migrate
# cleanly without deleting user-created data.
LEGACY_DEMO_FLOW_IDS = {
    "cab-approval-validation","contract-signature-process","supplier-portal-data-extract",
    "user-provisioning","s4-material-master-check","invoice-download-archive",
    "web-performance-check","custom-test-flow"
}

def visible_flows():
    flows=read_json(FLOWS_FILE,[])
    designs_dir=DATA_DIR/"designs"
    out=[]
    for f in flows:
        if f.get("id") not in LEGACY_DEMO_FLOW_IDS:
            out.append(f); continue
        has_recording=bool(f.get("recording_id"))
        has_design=(designs_dir/f"{f.get('id')}.json").exists()
        if has_recording or has_design:
            out.append(f)
    return out

DEFAULT_CONFIG = {
    "app_name": "ALM WebFlow Studio",
    "phase": 14,
    "phase_total": 14,
    "environment": RUNTIME.environment,
    "storage_adapter": STORAGE.adapter,
    "authentication_adapter": RUNTIME.auth_adapter,
    "hub_plugin_schema": "webflow-hub-plugin/1",
    "engenie_manifest_schema": "engenie-app-manifest/2",
    "ai_provider_registry_schema": "webflow-ai-provider-registry/1",
    "ai_orchestrator_schema": "webflow-ai-orchestrator/1",
    "governance_schema": "webflow-governance/1",
    "audit_schema": "webflow-audit-event/1",
    "pilot_readiness_schema": "webflow-pilot-readiness/1",
    "browser_engine": "playwright-chromium",
    "pwa_enabled": True,
    "ui_contract": "v0.4-approved",
    "recording_schema": "webflow-recording/1",
    "object_schema": "webflow-object/1",
    "run_schema": "webflow-run/1",
    "dataset_schema": "webflow-dataset/1",
    "mapping_schema": "webflow-data-mapping/1",
    "batch_schema": "webflow-batch/1",
    "flow_schema": "webflow-flow/1",
    "script_manifest_schema": "webflow-script-manifest/1",
    "script_state_schema": "webflow-script-state/1",
    "performance_schema": "webflow-performance-test/1",
    "queue_schema": "webflow-queue-job/1",
    "schedule_schema": "webflow-schedule/1",
    "worker_schema": "webflow-worker-status/1",
    "api_version": "v1"
}

def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    tmp.replace(path)



def _client_bundle_status():
    """Describe what the Studio can put in a one-click Local Client bundle."""
    exe=ROOT / "client-dist" / "WebFlowLocalClient.exe"
    return {
        "exe_available": exe.exists() and exe.is_file(),
        "exe_name": exe.name,
        "distribution_folder": "client-dist",
        "bundle_format": "zip+one-time-bootstrap",
    }


def _client_bundle(session: dict, studio_url: str, client_name: str | None = None):
    """Create a user-friendly Local Client download bundle.

    We intentionally do *not* build a unique executable containing credentials.
    Embedding a long-lived secret in an EXE makes rotation and code signing much
    harder and the secret can still be extracted.  Instead, the same signed EXE
    is distributed to everybody and a tiny adjacent bootstrap file carries a
    short-lived, one-time pairing token.  The client consumes/deletes that file
    on first launch, then stores its machine identity in the user's profile.
    """
    pair=HYBRID.create_pairing_token(session,ttl_minutes=30)
    bootstrap={
        "schema":"webflow-local-client-bootstrap/1",
        "studio_url":studio_url.rstrip('/'),
        "pairing_token":pair["pairing_token"],
        "expires_at":pair["expires_at"],
        "client_name":client_name or "WebFlow Local Client",
    }
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        exe=ROOT / "client-dist" / "WebFlowLocalClient.exe"
        if exe.exists() and exe.is_file():
            z.write(exe,exe.name)
        else:
            # Developer fallback: once an enterprise-signed EXE is dropped into
            # client-dist/, it automatically replaces these source files.
            z.write(ROOT/'local_client.py','local_client.py')
            z.writestr('requirements-client.txt','playwright>=1.55,<2\npsutil>=6,<8\n')
            z.writestr('RUN_LOCAL_CLIENT.txt',
                'No WebFlowLocalClient.exe is installed in client-dist yet.\n'
                'For developer testing only: install requirements-client.txt and run local_client.py.\n'
                'Production users should receive the signed EXE.\n')
        z.writestr('webflow-client.bootstrap.json',json.dumps(bootstrap,indent=2))
        z.writestr('README-FIRST.txt',
            'ALM WebFlow Local Client\n\n'
            '1. Extract this ZIP to a normal local folder.\n'
            '2. Double-click WebFlowLocalClient.exe.\n'
            '3. The client pairs automatically with the Studio that created this download.\n'
            '4. Keep the client running while recording or executing WebFlows.\n\n'
            'The bootstrap token is one-time and expires automatically. The client is outbound-only.\n')
    return out.getvalue(), pair

RECORDING_RETENTION_DEFAULT_DAYS = 1
RECORDING_RETENTION_MAX_DAYS = 30
RUN_SCREENSHOT_RETENTION_HOURS = 24

def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def _bounded_retention_days(value):
    try:
        return max(1, min(RECORDING_RETENTION_MAX_DAYS, int(value or RECORDING_RETENTION_DEFAULT_DAYS)))
    except Exception:
        return RECORDING_RETENTION_DEFAULT_DAYS

def _ensure_recording_retention(rec: dict, *, reset_from_now=False) -> dict:
    days=_bounded_retention_days(rec.get("retention_days"))
    rec["retention_days"]=days
    rec.setdefault("retention_policy","hub-temporary")
    if reset_from_now or _parse_dt(rec.get("retention_until")) is None:
        rec["retention_until"]=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
    return rec

def cleanup_expired_artifacts() -> dict:
    """Delete transient recording/run images while preserving reporting metadata."""
    now=datetime.now(timezone.utc); removed_recordings=[]; removed_run_images=[]; remote_deleted=0
    rec_root=DATA_DIR/"recordings"
    if rec_root.exists():
        for path in list(rec_root.glob("*.json")):
            try: rec=json.loads(path.read_text(encoding="utf-8"))
            except Exception: continue
            _ensure_recording_retention(rec)
            until=_parse_dt(rec.get("retention_until"))
            if not until or until>now: continue
            rid=str(rec.get("id") or path.stem)
            try: path.unlink(missing_ok=True)
            except Exception: pass
            shot_root=rec_root/"screenshots"
            try:
                folder=shot_root/rid
                if folder.exists(): shutil.rmtree(folder)
                for shot in shot_root.glob(f"{rid}-*.png"):
                    shot.unlink(missing_ok=True)
            except Exception: pass
            if STORAGE.enabled:
                try:
                    if STORAGE.delete_relative(f"recordings/{path.name}"): remote_deleted+=1
                    remote_deleted += STORAGE.delete_prefix_relative(f"recordings/screenshots/{rid}")
                except Exception: pass
            removed_recordings.append(rid)
            flows=read_json(FLOWS_FILE,[]); changed=False
            for flow in flows:
                if flow.get("recording_id")==rid:
                    flow["recording_id"]=None; flow["recording_expired_at"]=now.isoformat(); changed=True
            if changed: _write_json(FLOWS_FILE,flows)
    runs_root=DATA_DIR/"runs"; cutoff=now-timedelta(hours=RUN_SCREENSHOT_RETENTION_HOURS)
    if runs_root.exists():
        for shot_dir in runs_root.glob("*/screenshots"):
            images=list(shot_dir.glob("*.png"))
            if not images: continue
            try: latest=max(datetime.fromtimestamp(x.stat().st_mtime,timezone.utc) for x in images)
            except Exception: continue
            if latest>cutoff: continue
            run_id=shot_dir.parent.name
            try: shutil.rmtree(shot_dir)
            except Exception: pass
            if STORAGE.enabled:
                try: remote_deleted += STORAGE.delete_prefix_relative(f"runs/{run_id}/screenshots")
                except Exception: pass
            removed_run_images.append(run_id)
    return {"removed_recordings":removed_recordings,"removed_run_screenshot_sets":removed_run_images,"remote_objects_deleted":remote_deleted,"recording_default_days":1,"recording_max_days":30,"run_screenshot_hours":24}

def _recording_download_zip(rec: dict):
    buf=io.BytesIO(); rid=str(rec.get("id") or "recording")
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        z.writestr("recording.json",json.dumps(rec,indent=2))
        for step in rec.get("steps",[]) or []:
            raw=str(step.get("screenshot") or step.get("screenshot_path") or "")
            rel=raw.lstrip("/").replace("\\","/")
            if not rel.startswith("data/recordings/screenshots/"): continue
            src=(ROOT/rel).resolve()
            try: src.relative_to((DATA_DIR/"recordings"/"screenshots").resolve())
            except Exception: continue
            if src.exists() and src.is_file(): z.write(src,"screenshots/"+src.name)
        z.writestr("README.txt","Hub recording copies are temporary: 24 hours by default, up to 30 days when explicitly saved. Keep this archive locally for longer retention.\n")
    safe=''.join(c if c.isalnum() or c in '-_.' else '-' for c in str(rec.get('recording_name') or 'webflow-recording'))[:80].strip('-') or 'webflow-recording'
    return buf.getvalue(),f"{safe}.webflow-recording.zip"

def list_recordings():
    cleanup_expired_artifacts()
    root = DATA_DIR / 'recordings'
    items=[]
    if root.exists():
        for path in sorted(root.glob('*.json'), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                rec=read_json(path,{})
                if not rec: continue
                flow=next((f for f in read_json(FLOWS_FILE,[]) if f.get('id')==rec.get('flow_id')),{})
                _ensure_recording_retention(rec); items.append({'id':rec.get('id'),'recording_name':rec.get('recording_name') or flow.get('name') or f"Recording {str(rec.get('id') or '')[:8]}",'flow_id':rec.get('flow_id'),'flow_name':flow.get('name') or rec.get('recording_name') or 'Recorded WebFlow','starting_url':rec.get('starting_url',''),'status':rec.get('status','saved'),'started_at':rec.get('started_at'),'ended_at':rec.get('ended_at'),'steps':len(rec.get('steps',[])),'retention_days':rec.get('retention_days',1),'retention_until':rec.get('retention_until'),'retention_policy':rec.get('retention_policy','hub-temporary')})
            except Exception:
                pass
    return items

def persist_recording(rec):
    rid=str(rec.get('id') or '').strip()
    if not rid: return False
    _ensure_recording_retention(rec)
    _write_json(DATA_DIR/'recordings'/f'{rid}.json',rec)
    return True

def training_html(rec):
    flows=read_json(FLOWS_FILE,[])
    flow=next((f for f in flows if f.get('id')==rec.get('flow_id')),{})
    title=html_lib.escape(flow.get('name') or 'WebFlow Training Replay')
    rows=[]
    for i,step in enumerate(rec.get('steps',[]),1):
        if step.get('training_hidden'): continue
        target=step.get('target') or {}
        note=step.get('training_note') or 'Follow the recorded action shown below.'
        action=html_lib.escape(str(step.get('action') or 'Step')).title()
        name=html_lib.escape(str(step.get('training_title') or target.get('label') or target.get('aria_label') or target.get('text') or target.get('tag') or 'Page action'))
        value='••••••••' if step.get('secret') or step.get('classification')=='Secret' else html_lib.escape(str(step.get('value') or ''))
        value_html=('<p><b>Value:</b> '+value+'</p>') if value else ''
        shot=step.get('screenshot') or step.get('screenshot_path') or ''
        shot_html=('<img src="'+html_lib.escape(str(shot))+'" alt="Step screenshot">') if shot else '<div class="no-shot">No screenshot captured for this step.</div>'
        rows.append('<section class="step"><div class="num">'+str(i)+'</div><div><h2>'+action+' - '+name+'</h2><p>'+html_lib.escape(str(note))+'</p>'+value_html+shot_html+'</div></section>')
    body=''.join(rows) or '<p>No visible recorded steps.</p>'
    return '<!doctype html><html><head><meta charset="utf-8"><title>'+title+' - Training Replay</title><style>body{font-family:Segoe UI,Arial,sans-serif;background:#f4f7fb;color:#14243b;margin:0;padding:32px}main{max-width:1050px;margin:auto}header{background:linear-gradient(135deg,#0c2c4f,#0a6ed1);color:white;padding:26px;border-radius:18px}header h1{margin:0 0 6px}.step{display:grid;grid-template-columns:48px 1fr;gap:15px;background:white;border:1px solid #dce5ef;border-radius:14px;padding:18px;margin:16px 0}.num{width:38px;height:38px;border-radius:50%;display:grid;place-items:center;background:#e8f3ff;color:#0a6ed1;font-weight:800}h2{font-size:17px;margin:3px 0 7px}p{color:#53657a;line-height:1.5}img{display:block;max-width:100%;border:1px solid #dce5ef;border-radius:10px;margin-top:12px}.no-shot{padding:28px;background:#f7f9fc;border-radius:10px;color:#7b8b9d}</style></head><body><main><header><h1>'+title+'</h1><div>ALM WebFlow Studio • Training Replay</div></header>'+body+'</main></body></html>'

def ensure_state():
    DATA_DIR.mkdir(exist_ok=True)
    CONFIG_DIR.mkdir(exist_ok=True)
    if not FLOWS_FILE.exists(): _write_json(FLOWS_FILE, DEFAULT_FLOWS)
    if not CONFIG_FILE.exists(): _write_json(CONFIG_FILE, DEFAULT_CONFIG)
    else:
        cfg=read_json(CONFIG_FILE,{})
        cfg.update({"phase":14,"phase_total":14,"environment":RUNTIME.environment,"storage_adapter":STORAGE.adapter,"authentication_adapter":RUNTIME.auth_adapter,"hub_plugin_schema":"webflow-hub-plugin/1","engenie_manifest_schema":"engenie-app-manifest/2","ai_provider_registry_schema":"webflow-ai-provider-registry/1","ai_orchestrator_schema":"webflow-ai-orchestrator/1","governance_schema":"webflow-governance/1","audit_schema":"webflow-audit-event/1","pilot_readiness_schema":"webflow-pilot-readiness/1","browser_engine":"playwright-chromium","recording_schema":"webflow-recording/1","object_schema":"webflow-object/1","run_schema":"webflow-run/1","flow_schema":"webflow-flow/1","script_manifest_schema":"webflow-script-manifest/1","script_state_schema":"webflow-script-state/1","performance_schema":"webflow-performance-test/1","queue_schema":"webflow-queue-job/1","schedule_schema":"webflow-schedule/1","worker_schema":"webflow-worker-status/1"})
        _write_json(CONFIG_FILE,cfg)

def read_json(path: Path, fallback):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return fallback

def finalize_flow_recording(recording):
    """Attach a finished recording to a flow, creating the flow when needed.

    Recording is now allowed to be the first action in the product.  A user can
    simply name a recording and start; on stop that recording becomes the first
    version of a new Draft flow.  Existing flows can still be re-recorded when a
    flow_id is supplied by an advanced/re-record path.
    """
    if str(recording.get("retention_policy") or "hub-temporary")=="hub-temporary":
        _ensure_recording_retention(recording,reset_from_now=True)
    flows=read_json(FLOWS_FILE,[])
    flow_id=recording.get("flow_id")
    for i,flow in enumerate(flows):
        if flow.get("id")==flow_id:
            flows[i]={**flow,
                "recording_id":recording.get("id"),
                "recorded_steps":len(recording.get("steps",[])),
                "updated_at":datetime.now(timezone.utc).isoformat(),
                "status":"Draft" if flow.get("status") not in {"Ready","Testing"} else flow.get("status")
            }
            _write_json(FLOWS_FILE,flows); return flows[i]
    # No pre-existing flow: recording-first workflow.
    new_id=str(uuid.uuid4())
    recording["flow_id"]=new_id
    flow={
        "id":new_id,
        "name":str(recording.get("recording_name") or "Recorded WebFlow"),
        "description":"Created from a browser recording",
        "status":"Draft",
        "last_run":"—",
        "success_rate":None,
        "owner":"EA",
        "starting_url":str(recording.get("starting_url") or ""),
        "recording_id":recording.get("id"),
        "recorded_steps":len(recording.get("steps",[])),
        "created_at":datetime.now(timezone.utc).isoformat(),
        "updated_at":datetime.now(timezone.utc).isoformat(),
    }
    flows.insert(0,flow); _write_json(FLOWS_FILE,flows)
    _write_json(DATA_DIR/"recordings"/f"{recording.get('id')}.json",recording)
    return flow

ensure_state()
OBJECTS=ObjectRepository(ROOT,_write_json)

def finalize_flow_recording_phase3(recording):
    finalize_flow_recording(recording)
    try: OBJECTS.rebuild()
    except Exception as exc: print("[WebFlow] object rebuild failed:",exc)

RECORDER=RecorderManager(ROOT,_write_json,finalize_flow_recording_phase3)

def _accept_hybrid_recording(recording, artifacts=None, final=False):
    """Persist a local-client recorder snapshot into the hosted Studio.

    Values marked secret are already masked by RecorderManager before transport.
    Screenshot uploads are restricted to the recorder screenshot namespace; this
    endpoint cannot be used as a generic workstation file uploader.
    """
    import base64
    if not isinstance(recording,dict) or not recording.get("id"): return
    _ensure_recording_retention(recording)
    shot_root=(DATA_DIR/"recordings"/"screenshots").resolve()
    for rel,b64 in (artifacts or {}).items():
        rel=str(rel).lstrip("/").replace("\\","/")
        if not rel.startswith("data/recordings/screenshots/"): continue
        dest=(ROOT/rel).resolve()
        try: dest.relative_to(shot_root)
        except Exception: continue
        try:
            raw=base64.b64decode(str(b64),validate=True)
            if len(raw)>5*1024*1024: continue
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
        except Exception: continue
    persist_recording(recording)
    if final:
        finalize_flow_recording_phase3(recording)

def _accept_hybrid_result(task, result):
    """Normalize one Local Client execution result into the standard run store.

    The Local Client may return screenshots, but only paths rooted under the
    WebFlow run-artifact namespace are accepted. This is not a generic remote
    file-write endpoint.
    """
    import base64
    run=(result or {}).get("run")
    if not isinstance(run,dict) or not run.get("id"):
        return
    artifacts=(result or {}).get("artifacts") or {}
    run_root=(DATA_DIR/"runs").resolve()
    for rel,b64 in artifacts.items():
        rel=str(rel).lstrip("/").replace("\\","/")
        if not rel.startswith("data/runs/"):
            continue
        dest=(ROOT/rel).resolve()
        try: dest.relative_to(run_root)
        except Exception: continue
        try:
            raw=base64.b64decode(str(b64),validate=True)
            if len(raw)>5*1024*1024: continue
            dest.parent.mkdir(parents=True,exist_ok=True); dest.write_bytes(raw)
        except Exception:
            continue
    _write_json(DATA_DIR/"runs"/str(run["id"])/"run.json",run)
    try: finalize_flow_run(run)
    except Exception: pass
    try:
        GOV.audit("hybrid_execution_result",task.get("requested_by") or {},{"task_id":task.get("id"),"client_id":task.get("client_id"),"run_id":run.get("id"),"flow_id":run.get("flow_id"),"state":run.get("state")},"success" if run.get("state")=="completed" else "failed")
    except Exception: pass

HYBRID=HybridClientManager(ROOT,_write_json,_accept_hybrid_recording,_accept_hybrid_result)

def finalize_flow_run(run):
    flows=read_json(FLOWS_FILE,[])
    now=datetime.now(timezone.utc).isoformat()
    for i,flow in enumerate(flows):
        if flow.get("id")==run.get("flow_id"):
            previous=list(flow.get("run_history",[]) or [])[-19:]
            previous.append({"run_id":run.get("id"),"state":run.get("state"),"ended_at":run.get("ended_at"),"duration_ms":run.get("duration_ms")})
            successes=sum(1 for x in previous if x.get("state")=="completed")
            rate=round(successes/len(previous)*100) if previous else None
            flows[i]={**flow,"last_run":"just now","last_run_at":now,"last_run_id":run.get("id"),"success_rate":rate,"run_history":previous,"status":flow.get("status") or "Draft"}
            _write_json(FLOWS_FILE,flows)
            break

EXECUTOR=ExecutionManager(ROOT,_write_json,finalize_flow_run,object_provider=OBJECTS.all)
DATA=DataManager(ROOT,_write_json,EXECUTOR,RECORDER.recording_data)
DESIGNER=FlowDesignerManager(ROOT,_write_json,RECORDER.recording_data)
SCRIPTS=ScriptManager(ROOT,_write_json,lambda: read_json(FLOWS_FILE,DEFAULT_FLOWS),DESIGNER.get,RECORDER.recording_data,OBJECTS.all)
PERFORMANCE=PerformanceManager(ROOT,_write_json,object_provider=OBJECTS.all)
GOV=GovernanceManager(ROOT, _write_json)

def _scheduler_policy_guard(flow_id, recording_id, approved, source):
    flow=next((f for f in visible_flows() if f.get("id")==flow_id),None)
    rec=RECORDER.recording_data(recording_id) if recording_id else None
    design=DESIGNER.get(flow_id) if flow_id else None
    ok,issue=GOV.execution_guard(flow,rec,design,approved,purpose="queue")
    if not ok:
        GOV.audit("worker_policy_block",details={"flow_id":flow_id,"recording_id":recording_id,"source":source,"issue":issue},outcome="blocked")
    return ok,issue

def _hybrid_scheduler_dispatch(job, rec, design):
    # Scheduler adapter for outbound-only Local Clients. A cancellation request
    # uses the same typed-task channel and never becomes a generic command.
    if job.get("cancel_target_task_id"):
        return HYBRID.queue_task(str(job.get("client_id") or ""),"cancel_execution",{"target_task_id":job.get("cancel_target_task_id")},job.get("requested_by") or {})
    client_id=str(job.get("client_id") or "")
    if not client_id: return False,{"error":"local_client_required"}
    payload={
        "flow_id":job.get("flow_id"),"recording":rec or {},"headless":bool(job.get("headless",True)),
        "timeout_ms":job.get("timeout_ms",12000),"retries":job.get("retries",1),
        "failure_policy":job.get("failure_policy","stop"),"variables":job.get("variables") or {},
        "logic_steps":(design or {}).get("steps") if design else None,"subflows":(design or {}).get("subflows") if design else {},
    }
    return HYBRID.queue_task(client_id,"execute_flow",payload,job.get("requested_by") or {})

SCHEDULER=SchedulerManager(ROOT,_write_json,RECORDER.recording_data,DESIGNER.get,object_provider=OBJECTS.all,finalize_run=finalize_flow_run,job_guard=_scheduler_policy_guard,external_dispatcher=_hybrid_scheduler_dispatch,external_status_provider=HYBRID.get_task,local_execution_enabled=not RUNTIME.cloud_foundry)


def _engenie_policy_guard(description, confirmed, purpose):
    flow_id=str((description or {}).get("flow_id") or "")
    flow=next((f for f in visible_flows() if f.get("id")==flow_id),None)
    rid=str((description or {}).get("recording_id") or (flow or {}).get("recording_id") or "")
    rec=RECORDER.recording_data(rid) if rid else None
    design=DESIGNER.get(flow_id) if flow_id else None
    return GOV.execution_guard(flow,rec,design,confirmed,purpose=purpose)

def _engenie_tutorial():
    return BTP.manifest("engenie-tutorial.json",{})

def _engenie_flows():
    items=[dict(x) for x in read_json(FLOWS_FILE,DEFAULT_FLOWS)]
    latest={}
    for r in list_recordings():
        fid=r.get("flow_id")
        if fid and fid not in latest: latest[fid]=r
    for f in items:
        if not f.get("recording_id") and f.get("id") in latest:
            f["recording_id"]=latest[f.get("id")].get("id")
            f["recorded_steps"]=latest[f.get("id")].get("steps",0)
    return items

ENGENIE=EngenieBridge(
    flow_provider=_engenie_flows,
    recording_provider=lambda rid: RECORDER.recording_data(rid),
    design_provider=lambda fid: DESIGNER.get(fid),
    scheduler=SCHEDULER,
    run_provider=lambda rid: EXECUTOR.get(rid),
    variable_provider=lambda rec: DATA.infer_variables(rec),
    tutorial_provider=_engenie_tutorial,
    policy_guard=_engenie_policy_guard,
    execution_normalizer=GOV.clamp_execution,
)

# Phase 13 — external AI providers are registered independently from Engenie.
# The orchestrator allows a provider to interpret user intent, but every action
# still passes through the governed Phase 12 EngenieBridge before reaching the
# queue/execution engine. Provider additions therefore do not touch core runtime.
AI_PROVIDERS=AIProviderRegistry(CONFIG_DIR / "ai-providers.json")
AI=AIOrchestrator(AI_PROVIDERS, ENGENIE)

try:
    cleanup_expired_artifacts()
except Exception as exc:
    print("[WebFlow] retention cleanup failed:",exc)

def _artifact_retention_loop():
    while True:
        import time as _time
        _time.sleep(3600)
        try: cleanup_expired_artifacts()
        except Exception as exc: print("[WebFlow] scheduled retention cleanup failed:",exc)

threading.Thread(target=_artifact_retention_loop,name="webflow-artifact-retention",daemon=True).start()

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(ROOT), **kwargs)
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # The SAP Application Router remains the cloud authentication trust boundary.
        frame_ancestors=os.getenv("WEBFLOW_FRAME_ANCESTORS", "'self' https://*.arthrex.com")
        self.send_header("Content-Security-Policy", f"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors {frame_ancestors}")
        super().end_headers()
    def log_message(self, fmt, *args): print("[WebFlow]", fmt % args)
    def _json(self, payload, status=200):
        # Every mutating API response receives a redacted audit event. This gives
        # us broad coverage without coupling each feature manager to audit code.
        audit_path=urlparse(self.path).path
        noisy_client_transport=audit_path in {"/api/hybrid/client/heartbeat","/api/hybrid/client/result"}
        if self.command in {"POST","PUT","DELETE"} and not noisy_client_transport and not getattr(self,"_audit_written",False):
            try:
                GOV.audit("api_mutation",self._session(),{"method":self.command,"path":urlparse(self.path).path,"status":status,"request":getattr(self,"_request_body",None)},"success" if status < 400 else "rejected")
                self._audit_written=True
            except Exception as exc:
                print("[WebFlow] audit write failed:",exc)
        body=json.dumps(payload,indent=2).encode("utf-8"); self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def _body(self):
        length=int(self.headers.get("Content-Length","0") or "0")
        if not length:
            self._request_body = {}
            return {}
        # Hard cap request bodies before JSON parsing. File-upload endpoints also
        # apply a decoded-size limit through GovernanceManager.
        max_request = int(GOV.get_config().get("limits",{}).get("max_upload_mb",25)) * 1024 * 1024 * 2
        if length > max_request:
            self._request_body = {"_rejected":"request_too_large"}
            return None
        try:
            raw=self.rfile.read(length)
            self._raw_body=raw
            value=json.loads(raw.decode("utf-8"))
            self._request_body=value
            return value
        except Exception:
            self._request_body=None
            return None

    def _session(self):
        return BTP.session(self.headers)

    def _require_role(self, required):
        ok, issue=GOV.authorize(self._session(), required)
        if not ok:
            self._json(issue,403)
            return False
        return True

    def _flow_context(self, flow_id, recording_id=""):
        flows=read_json(FLOWS_FILE,DEFAULT_FLOWS)
        flow=next((f for f in flows if f.get("id")==flow_id),None)
        rid=str(recording_id or (flow or {}).get("recording_id") or "")
        rec=RECORDER.recording_data(rid) if rid else None
        design=DESIGNER.get(flow_id or (rec or {}).get("flow_id")) if (flow_id or rec) else None
        return flow,rec,design

    def _guard_execution(self, flow_id, recording_id, confirmed=False, purpose="execution"):
        flow,rec,design=self._flow_context(flow_id,recording_id)
        ok,issue=GOV.execution_guard(flow,rec,design,bool(confirmed),purpose=purpose)
        if not ok:
            GOV.audit("governance_block",self._session(),{"purpose":purpose,"flow_id":flow_id,"recording_id":recording_id,"issue":issue},"blocked")
            self._json(issue,409 if issue.get("error") in {"confirmation_required","destructive_performance_blocked"} else 403)
            return None
        return flow,rec,design

    def do_GET(self):
        parsed=urlparse(self.path); path=parsed.path; query=parse_qs(parsed.query)
        if path=="/api/health": return self._json({"status":"ok","app":"ALM WebFlow Studio","phase":"14/14","environment":RUNTIME.environment,"playwright":RECORDER.dependency_status(),"executor":EXECUTOR.dependency_status(),"data_engine":DATA.dependency_status(),"worker":SCHEDULER.status(),"hybrid_clients":len(HYBRID.clients()),"storage":STORAGE.status(),"time":datetime.now(timezone.utc).isoformat()})
        if path=="/api/config": return self._json(read_json(CONFIG_FILE,DEFAULT_CONFIG))
        if path=="/api/runtime":
            return self._json({"profile":RUNTIME.__dict__,"storage":STORAGE.status(),"services":BTP.service_summary()})
        if path=="/api/session": return self._json(BTP.session(self.headers))
        if path=="/api/btp/readiness":
            ready=BTP.readiness(STORAGE.status(), RECORDER.dependency_status())
            online=[c for c in HYBRID.clients() if c.get("last_seen") and c.get("state") in {"online","busy"}]
            ready["checks"].append({"id":"local-client","label":"WebFlow Local Client","status":"ready" if online else ("local" if not RUNTIME.cloud_foundry else "warning"),"detail":f"{len(online)} online / {len(HYBRID.clients())} paired; outbound-only signed browser runtime"})
            ready["warnings"]=[c["label"]+": "+c["detail"] for c in ready["checks"] if c.get("status")=="warning"]
            ready["ready"]=len(ready["warnings"])==0
            return self._json(ready)
        if path=="/api/hub/plugin":
            manifest=BTP.manifest("hub-plugin.json",{})
            if RUNTIME.public_url: manifest={**manifest,"route":RUNTIME.public_url}
            return self._json(manifest)
        if path=="/api/ai/providers": return self._json(AI_PROVIDERS.status())
        if path.startswith("/api/ai/providers/"):
            pid=path.rsplit("/",1)[-1]; provider=AI_PROVIDERS.get(pid)
            return self._json(provider.status() if provider else {"error":"provider_not_found"},200 if provider else 404)
        if path=="/api/engenie/manifest": return self._json(BTP.manifest("engenie.manifest.json",{}))
        if path=="/api/engenie/tutorial": return self._json(BTP.manifest("engenie-tutorial.json",{}))
        if path=="/api/governance": return self._json(GOV.get_config())
        if path=="/api/audit":
            if not self._require_role("admin"): return
            return self._json({"events":GOV.list_audit(limit=int((query.get("limit") or [100])[0])),"chain":GOV.verify_audit_chain()})
        if path=="/api/pilot/readiness":
            infra=BTP.readiness(STORAGE.status(),RECORDER.dependency_status())
            chromium_ready=any(c.get("id")=="chromium" and c.get("status")=="ready" for c in infra.get("checks",[]))
            local_client_ready=any(c.get("last_seen") and c.get("state") in {"online","busy"} for c in HYBRID.clients())
            return self._json(GOV.pilot_readiness(RUNTIME.__dict__,STORAGE.status(),RECORDER.dependency_status(),SCHEDULER.status(),GOV.verify_audit_chain(),browser_ready=(chromium_ready or local_client_ready),hybrid_ready=local_client_ready))
        if path=="/api/hybrid/clients":
            return self._json(HYBRID.clients())
        if path=="/api/hybrid/client-package":
            return self._json(_client_bundle_status())
        if path.startswith("/api/hybrid/tasks/"):
            tid=path.rsplit("/",1)[-1]; task=HYBRID.get_task(tid)
            return self._json(task or {"error":"task_not_found"},200 if task else 404)
        if path=="/api/hybrid/client/poll":
            cid=self.headers.get("X-WebFlow-Client-ID","")
            ts=self.headers.get("X-WebFlow-Timestamp","")
            sig=self.headers.get("X-WebFlow-Signature","")
            if not HYBRID.verify_request(cid,ts,"GET",path,b"",sig): return self._json({"error":"invalid_client_signature"},401)
            return self._json(HYBRID.poll(cid))
        if path=="/api/engenie/tools": return self._json(ENGENIE.tools())
        if path=="/api/engenie/flows": return self._json(ENGENIE.list_flows(query=(query.get("q") or [None])[0],runnable_only=str((query.get("runnable") or ["false"])[0]).lower()=="true"))
        if path.startswith("/api/engenie/flows/"):
            fid=path.rsplit("/",1)[-1]; flow=next((f for f in visible_flows() if f.get("id")==fid),None); d=ENGENIE.describe(flow); return self._json(d or {"error":"flow_not_found"},200 if d else 404)
        if path.startswith("/api/engenie/jobs/"):
            jid=path.rsplit("/",1)[-1]; st=ENGENIE.job_status(jid); return self._json(st or {"error":"job_not_found"},200 if st else 404)
        if path=="/api/flows": return self._json(visible_flows())
        if path.startswith("/api/flows/"):
            flow_id=path.rsplit("/",1)[-1]; flow=next((f for f in visible_flows() if f.get("id")==flow_id),None)
            return self._json(flow if flow else {"error":"flow_not_found"},200 if flow else 404)
        if path=="/api/runs": return self._json(EXECUTOR.list_runs())
        if path=="/api/executor/status": return self._json(EXECUTOR.status())
        if path.startswith("/api/runs/"):
            run_id=path.rsplit("/",1)[-1]; data=EXECUTOR.get(run_id)
            return self._json(data or {"error":"run_not_found"},200 if data else 404)
        if path=="/api/data/datasets": return self._json(DATA.list_datasets())
        if path.startswith("/api/data/datasets/"):
            did=path.rsplit("/",1)[-1]; d=DATA.get_dataset(did, sheet=(query.get("sheet") or [None])[0]); return self._json(d or {"error":"dataset_not_found"},200 if d else 404)
        if path.startswith("/api/data/variables/"):
            rid=path.rsplit("/",1)[-1]; rec=RECORDER.recording_data(rid)
            return self._json(DATA.infer_variables(rec) if rec else {"error":"recording_not_found"},200 if rec else 404)
        if path=="/api/data/mappings": return self._json(DATA.list_mappings())
        if path=="/api/data/batch/status": return self._json(DATA.status())
        if path=="/api/data/batches": return self._json(DATA.list_batches())
        if path.startswith("/api/data/batches/"):
            bid=path.rsplit("/",1)[-1]; b=DATA.get_batch(bid); return self._json(b or {"error":"batch_not_found"},200 if b else 404)
        if path=="/api/performance/status": return self._json(PERFORMANCE.status())
        if path=="/api/performance/tests": return self._json(PERFORMANCE.list_tests())
        if path.startswith("/api/performance/tests/"):
            tid=path.rsplit("/",1)[-1]; t=PERFORMANCE.get(tid); return self._json(t or {"error":"performance_test_not_found"},200 if t else 404)
        if path=="/api/worker/status": return self._json(SCHEDULER.status())
        if path=="/api/worker/jobs": return self._json(SCHEDULER.list_jobs(limit=int((query.get("limit") or [100])[0])))
        if path.startswith("/api/worker/jobs/"):
            jid=path.rsplit("/",1)[-1]; j=SCHEDULER.get_job(jid); return self._json(j or {"error":"job_not_found"},200 if j else 404)
        if path=="/api/schedules": return self._json(SCHEDULER.list_schedules())
        if path=="/api/scripts":
            items=[]
            for f in visible_flows():
                try:
                    d=SCRIPTS.get(f.get("id")); items.append({k:d.get(k) for k in ("flow_id","mode","design_hash","current_design_hash","drift","persisted","updated_at","validation","designer_steps")}|{"flow_name":f.get("name")})
                except Exception:
                    continue
            return self._json(items)
        if path.startswith("/api/scripts/") and path.endswith("/download"):
            fid=path.strip("/").split("/")[2]
            try: payload,name=SCRIPTS.project_zip(fid)
            except Exception as exc: return self._json({"error":"script_export_failed","message":str(exc)},400)
            self.send_response(200); self.send_header("Content-Type","application/zip"); self.send_header("Content-Disposition",f'attachment; filename="{name}"'); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        if path.startswith("/api/scripts/"):
            fid=path.rsplit("/",1)[-1]
            try: return self._json(SCRIPTS.get(fid))
            except Exception as exc: return self._json({"error":"script_unavailable","message":str(exc)},400)
        if path=="/api/designs": return self._json(DESIGNER.list())
        if path.startswith("/api/designs/"):
            fid=path.rsplit("/",1)[-1]; doc=DESIGNER.get(fid); return self._json(doc or {"error":"design_not_found"},200 if doc else 404)
        if path=="/api/recordings": return self._json(list_recordings())
        if path.startswith("/api/recordings/") and path.endswith("/download"):
            rid=path.strip("/").split("/")[2]; rec=RECORDER.recording_data(rid)
            if not rec: return self._json({"error":"recording_not_found"},404)
            payload,name=_recording_download_zip(rec)
            self.send_response(200); self.send_header("Content-Type","application/zip"); self.send_header("Content-Disposition",f'attachment; filename="{name}"'); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        if path.startswith("/api/training/export/"):
            rid=path.rsplit("/",1)[-1]; rec=RECORDER.recording_data(rid)
            if not rec: return self._json({"error":"recording_not_found"},404)
            body=training_html(rec).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Disposition",f'attachment; filename="webflow-training-{rid[:8]}.html"'); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path=="/api/objects": return self._json(OBJECTS.all())
        if path.startswith("/api/objects/") and path.endswith("/diagnostics"):
            oid=path.split("/")[3]; data=OBJECTS.diagnostics(oid); return self._json(data or {"error":"object_not_found"},200 if data else 404)
        if path.startswith("/api/objects/"):
            oid=path.rsplit("/",1)[-1]; data=OBJECTS.get(oid); return self._json(data or {"error":"object_not_found"},200 if data else 404)
        if path=="/api/recorder/status":
            remote=HYBRID.latest_recording_status()
            return self._json(remote or RECORDER.status())
        if path=="/api/recorder/current":
            remote=HYBRID.latest_recording_status()
            data=RECORDER.recording_data((remote or {}).get("recording_id")) if remote else RECORDER.recording_data()
            return self._json(data or {"error":"no_recording"},200 if data else 404)
        if path.startswith("/api/recordings/"):
            rid=path.rsplit("/",1)[-1]; data=RECORDER.recording_data(rid)
            return self._json(data or {"error":"recording_not_found"},200 if data else 404)
        return super().do_GET()

    def do_POST(self):
        self._audit_written=False
        path=urlparse(self.path).path; body=self._body()
        admin_prefixes=("/api/flows","/api/scripts/","/api/designs/","/api/objects","/api/recordings/")
        runner_prefixes=("/api/executor","/api/worker","/api/schedules","/api/performance","/api/data","/api/engenie/invoke","/api/ai/")
        if path=="/api/hybrid/client/pair" or path in {"/api/hybrid/client/heartbeat","/api/hybrid/client/result"}:
            pass  # authenticated below with one-time pairing token or client HMAC
        elif path=="/api/hybrid/pairing-token":
            if not self._require_role("admin"): return
        elif path=="/api/hybrid/client-download":
            # Every Runner may provision their own outbound-only workstation client.
            # Manual pairing-token creation remains an Admin/support function.
            if not self._require_role("runner"): return
        elif path=="/api/hybrid/execute" or (path.startswith("/api/hybrid/tasks/") and path.endswith("/cancel")):
            if not self._require_role("runner"): return
        elif path=="/api/recorder/start" or path in {"/api/recorder/pause","/api/recorder/resume","/api/recorder/stop"}:
            if not self._require_role("runner"): return
        elif path.startswith(admin_prefixes):
            if not self._require_role("runner"): return
        elif path.startswith(runner_prefixes):
            if not self._require_role("runner"): return
        if path=="/api/hybrid/client-download":
            if not isinstance(body,dict): body={}
            forwarded=str(self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
            scheme=forwarded or ("https" if RUNTIME.cloud_foundry else "http")
            host=str(self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip()
            studio_url=str(self.headers.get("X-WebFlow-Public-Base") or "").strip() or RUNTIME.public_url or (f"{scheme}://{host}" if host else f"http://127.0.0.1:{PORT}")
            payload,pair=_client_bundle(self._session(),studio_url,str(body.get("client_name") or "").strip() or None)
            GOV.audit("local_client_bundle_downloaded",self._session(),{"expires_at":pair.get("expires_at"),"exe_available":_client_bundle_status().get("exe_available")})
            name="ALM-WebFlow-Local-Client.zip"
            self.send_response(200); self.send_header("Content-Type","application/zip"); self.send_header("Content-Disposition",f'attachment; filename="{name}"'); self.send_header("Content-Length",str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        if path=="/api/hybrid/pairing-token":
            ttl=int((body or {}).get("ttl_minutes",10)) if isinstance(body,dict) else 10
            return self._json(HYBRID.create_pairing_token(self._session(),ttl),201)
        if path=="/api/hybrid/client/pair":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            ok,payload=HYBRID.pair(str(body.get("pairing_token") or ""),str(body.get("name") or "WebFlow Local Client"),body.get("metadata") or {})
            return self._json(payload,201 if ok else 401)
        if path=="/api/hybrid/client/heartbeat":
            cid=self.headers.get("X-WebFlow-Client-ID",""); ts=self.headers.get("X-WebFlow-Timestamp",""); sig=self.headers.get("X-WebFlow-Signature","")
            if not HYBRID.verify_request(cid,ts,"POST",path,getattr(self,"_raw_body",b""),sig): return self._json({"error":"invalid_client_signature"},401)
            return self._json(HYBRID.heartbeat(cid,body if isinstance(body,dict) else {}))
        if path=="/api/hybrid/client/result":
            cid=self.headers.get("X-WebFlow-Client-ID",""); ts=self.headers.get("X-WebFlow-Timestamp",""); sig=self.headers.get("X-WebFlow-Signature","")
            if not HYBRID.verify_request(cid,ts,"POST",path,getattr(self,"_raw_body",b""),sig): return self._json({"error":"invalid_client_signature"},401)
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            return self._json(HYBRID.complete_task(cid,str(body.get("task_id") or ""),body.get("result") or {}))
        if path=="/api/governance/cleanup":
            if not self._require_role("admin"): return
            result=GOV.cleanup_retention(); result["webflow_artifacts"]=cleanup_expired_artifacts(); GOV.audit("retention_cleanup",self._session(),result); return self._json(result)
        if path=="/api/audit/verify":
            if not self._require_role("admin"): return
            return self._json(GOV.verify_audit_chain())
        if path=="/api/ai/message":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            payload,status=AI.message(body); return self._json(payload,status)
        if path.startswith("/api/ai/providers/") and path.endswith("/test"):
            if not isinstance(body,dict): body={}
            pid=path.strip("/").split("/")[3]; payload,status=AI.test_provider(pid); return self._json(payload,status)
        if path=="/api/engenie/prepare":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            ok,payload=ENGENIE.prepare(body); return self._json(payload,200 if ok else (404 if payload.get("error")=="flow_not_resolved" else 409))
        if path=="/api/engenie/invoke":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            ok,payload,status=ENGENIE.invoke(body); return self._json(payload,status)
        if path=="/api/engenie/message":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            return self._json(ENGENIE.message(body.get("message") or ""))
        if path=="/api/flows":
            if not isinstance(body,dict) or not str(body.get("name","")).strip(): return self._json({"error":"name_required"},400)
            flows=read_json(FLOWS_FILE,[]); flow={"id":str(body.get("id") or uuid.uuid4()),"name":str(body["name"]).strip(),"description":str(body.get("description","")),"status":str(body.get("status","Draft")),"last_run":"—","success_rate":None,"owner":str(body.get("owner","EA")),"starting_url":str(body.get("starting_url","")),"created_at":datetime.now(timezone.utc).isoformat()}
            flows.insert(0,flow); _write_json(FLOWS_FILE,flows); return self._json(flow,201)
        if path.startswith("/api/scripts/") and path.endswith("/regenerate"):
            fid=path.strip("/").split("/")[2]
            try: return self._json(SCRIPTS.regenerate(fid),201)
            except Exception as exc: return self._json({"error":"script_generation_failed","message":str(exc)},400)
        if path.startswith("/api/scripts/") and path.endswith("/validate"):
            fid=path.strip("/").split("/")[2]
            try: return self._json(SCRIPTS.validate_current(fid))
            except Exception as exc: return self._json({"error":"script_validation_failed","message":str(exc)},400)
        if path.startswith("/api/scripts/") and path.endswith("/import"):
            fid=path.strip("/").split("/")[2]
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            ok_upload,upload_issue=GOV.validate_upload_size(str(body.get("content_base64") or ""))
            if not ok_upload: return self._json(upload_issue,413)
            try:
                result=SCRIPTS.import_upload(fid,str(body.get("name") or "automation.py"),str(body.get("content_base64") or ""))
                return self._json(result,201 if result.get("accepted") else 400)
            except Exception as exc: return self._json({"error":"script_import_failed","message":str(exc)},400)
        if path.startswith("/api/designs/") and path.endswith("/import-recording"):
            fid=path.strip("/").split("/")[2]
            flow=next((f for f in read_json(FLOWS_FILE,[]) if f.get("id")==fid),None)
            rid=str((body or {}).get("recording_id") or (flow or {}).get("recording_id") or "")
            rec=RECORDER.recording_data(rid) if rid else None
            if not rec: return self._json({"error":"recording_not_found","message":"Record this flow first, then import its steps into Designer."},404)
            return self._json(DESIGNER.from_recording(fid,rid),201)
        if path=="/api/objects/rebuild": return self._json(OBJECTS.rebuild())
        if path.startswith("/api/recordings/") and "/steps/" in path and path.endswith("/classify"):
            parts=path.strip("/").split("/"); rid=parts[2]; sid=parts[4]
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            rec=RECORDER.recording_data(rid)
            if not rec: return self._json({"error":"recording_not_found"},404)
            allowed={None,"Input","Output","Assertion","Secret","Loop Anchor","Optional"}; cls=body.get("classification")
            if cls not in allowed: return self._json({"error":"invalid_classification"},400)
            step=next((x for x in rec.get("steps",[]) if x.get("id")==sid),None)
            if not step: return self._json({"error":"step_not_found"},404)
            step["classification"]=cls
            # Persist through recorder when current, otherwise directly write historical recording.
            if RECORDER.recording and RECORDER.recording.get("id")==rid:
                for x in RECORDER.recording.get("steps",[]):
                    if x.get("id")==sid: x["classification"]=cls
                RECORDER._persist()
            else:
                _write_json(DATA_DIR/"recordings"/f"{rid}.json",rec)
            OBJECTS.rebuild(); return self._json(step)
        if path=="/api/data/upload":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            ok_upload,upload_issue=GOV.validate_upload_size(str(body.get("content_base64") or ""))
            if not ok_upload: return self._json(upload_issue,413)
            try: return self._json(DATA.upload(str(body.get("name") or ""),str(body.get("content_base64") or "")),201)
            except Exception as exc: return self._json({"error":"upload_failed","message":str(exc)},400)
        if path=="/api/data/mappings":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            rec=RECORDER.recording_data(str(body.get("recording_id") or ""))
            if not rec: return self._json({"error":"recording_not_found"},404)
            try: return self._json(DATA.save_mapping(body,rec),201)
            except Exception as exc: return self._json({"error":"mapping_failed","message":str(exc)},400)
        if path=="/api/data/batch/start":
            if RUNTIME.cloud_foundry: return self._json({"error":"local_client_required","message":"Data-driven browser batches are disabled in the hosted runtime until Local Client batch dispatch is selected."},409)
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            rec=RECORDER.recording_data(str(body.get("recording_id") or "")); mapping=next((m for m in DATA.list_mappings() if m.get("id")==str(body.get("mapping_id") or "")),None); dataset=DATA.get_dataset(str(body.get("dataset_id") or ""),preview_rows=1)
            if not rec: return self._json({"error":"recording_not_found"},404)
            if not mapping: return self._json({"error":"mapping_not_found"},404)
            if not dataset: return self._json({"error":"dataset_not_found"},404)
            flow_id=str(rec.get("flow_id") or "")
            guarded=self._guard_execution(flow_id,rec.get("id"),bool(body.get("confirmed",False)),purpose="data-batch")
            if not guarded: return
            body=GOV.clamp_execution(body)
            ok,payload=DATA.start_batch(rec,mapping,dataset,row_numbers=body.get("row_numbers"),headless=bool(body.get("headless",True)),timeout_ms=body.get("timeout_ms",12000),retries=body.get("retries",1),failure_policy=str(body.get("failure_policy") or "continue"))
            return self._json(payload,201 if ok else 409)
        if path=="/api/data/batch/cancel":
            ok,payload=DATA.cancel_batch(); return self._json(payload,200 if ok else 409)
        if path=="/api/performance/start":
            if RUNTIME.cloud_foundry: return self._json({"error":"local_client_required","message":"Synthetic browser performance runs must execute on a Local Client in hosted mode."},409)
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            recording_id=str(body.get("recording_id") or "")
            flow_id=str(body.get("flow_id") or "")
            rec=RECORDER.recording_data(recording_id) if recording_id else None
            if not rec and flow_id:
                flow=next((f for f in read_json(FLOWS_FILE,[]) if f.get("id")==flow_id),None)
                if flow and flow.get("recording_id"): rec=RECORDER.recording_data(flow.get("recording_id"))
            if not rec: return self._json({"error":"recording_not_found","message":"Select a flow with a saved recording."},404)
            flow_id=flow_id or rec.get("flow_id")
            guarded=self._guard_execution(flow_id,rec.get("id"),bool(body.get("confirmed",False)),purpose="performance")
            if not guarded: return
            _,rec,design=guarded
            _,_,body=GOV.validate_performance(body)
            ok,payload=PERFORMANCE.start(rec,flow_id=flow_id,logic_steps=(design or {}).get("steps") if body.get("use_designer",True) else None,subflows=(design or {}).get("subflows") if design else None,runs=body.get("runs",10),warmups=body.get("warmups",1),concurrency=body.get("concurrency",1),headless=bool(body.get("headless",True)),timeout_ms=body.get("timeout_ms",12000),retries=body.get("retries",0))
            return self._json(payload,201 if ok else 409)
        if path=="/api/performance/cancel":
            ok,payload=PERFORMANCE.cancel(); return self._json(payload,200 if ok else 409)
        if path=="/api/worker/enqueue":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            recording_id=str(body.get("recording_id") or ""); flow_id=str(body.get("flow_id") or "")
            if not recording_id and flow_id:
                flow=next((f for f in read_json(FLOWS_FILE,[]) if f.get("id")==flow_id),None); recording_id=str((flow or {}).get("recording_id") or "")
            guarded=self._guard_execution(flow_id,recording_id,bool(body.get("confirmed",False)),purpose="queue")
            if not guarded: return
            body=GOV.clamp_execution(body)
            client_id=str(body.get("client_id") or "")
            if RUNTIME.cloud_foundry and not client_id: return self._json({"error":"local_client_required","message":"Hosted browser queue jobs must target a paired Local Client."},409)
            ok,payload=SCHEDULER.enqueue(flow_id=flow_id,recording_id=recording_id,headless=bool(body.get("headless",True)),timeout_ms=body.get("timeout_ms",12000),retries=body.get("retries",1),failure_policy=str(body.get("failure_policy") or "stop"),variables=body.get("variables") or {},use_designer=bool(body.get("use_designer",True)),source=str(body.get("source") or "manual"),not_before=body.get("not_before"),governance_approved=bool(body.get("confirmed",False)),client_id=client_id or None,requested_by=self._session())
            return self._json(payload,201 if ok else 409)
        if path.startswith("/api/worker/jobs/") and path.endswith("/cancel"):
            jid=path.strip("/").split("/")[3]; ok,payload=SCHEDULER.cancel_job(jid); return self._json(payload,200 if ok else 409)
        if path.startswith("/api/worker/jobs/") and path.endswith("/retry"):
            jid=path.strip("/").split("/")[3]; ok,payload=SCHEDULER.retry_job(jid); return self._json(payload,201 if ok else 409)
        if path=="/api/worker/cleanup":
            return self._json(SCHEDULER.cleanup())
        if path=="/api/schedules":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            if not str(body.get("flow_id") or ""): return self._json({"error":"flow_required"},400)
            if not str(body.get("recording_id") or ""):
                flow=next((f for f in read_json(FLOWS_FILE,[]) if f.get("id")==str(body.get("flow_id") or "")),None); body["recording_id"]=(flow or {}).get("recording_id")
            if not body.get("recording_id"): return self._json({"error":"recording_required","message":"Schedule a flow after recording it."},400)
            guarded=self._guard_execution(str(body.get("flow_id") or ""),str(body.get("recording_id") or ""),bool(body.get("confirmed",False)),purpose="schedule")
            if not guarded: return
            body=GOV.clamp_execution(body)
            body["requested_by"]={"user_name":self._session().get("user_name"),"display_name":self._session().get("display_name"),"source":self._session().get("source")}
            if RUNTIME.cloud_foundry and not str(body.get("client_id") or ""): return self._json({"error":"local_client_required","message":"Hosted browser schedules must target a paired Local Client."},409)
            return self._json(SCHEDULER.save_schedule(body),201)
        if path.startswith("/api/schedules/") and path.endswith("/toggle"):
            sid=path.strip("/").split("/")[2]; sched=SCHEDULER.toggle_schedule(sid,bool((body or {}).get("enabled",True))); return self._json(sched or {"error":"schedule_not_found"},200 if sched else 404)
        if path.startswith("/api/data/batches/") and path.endswith("/retry-failed"):
            bid=path.strip("/").split("/")[3]; old=DATA.get_batch(bid)
            if not old: return self._json({"error":"batch_not_found"},404)
            rec=RECORDER.recording_data(old.get("recording_id")); mapping=next((m for m in DATA.list_mappings() if m.get("id")==old.get("mapping_id")),None); dataset=DATA.get_dataset(old.get("dataset_id"),preview_rows=1)
            if not rec or not mapping or not dataset: return self._json({"error":"batch_dependencies_missing"},409)
            guarded=self._guard_execution(str(rec.get("flow_id") or ""),rec.get("id"),bool((body or {}).get("confirmed",False)),purpose="data-batch-retry")
            if not guarded: return
            ok,payload=DATA.retry_failed(bid,rec,mapping,dataset); return self._json(payload,201 if ok else 409)
        if path=="/api/executor/start":
            if RUNTIME.cloud_foundry: return self._json({"error":"local_client_required","message":"Direct browser execution is disabled in the hosted runtime. Use /api/hybrid/execute with a paired Local Client."},409)
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            recording_id=str(body.get("recording_id") or "")
            flow_id=str(body.get("flow_id") or "")
            rec=RECORDER.recording_data(recording_id) if recording_id else None
            if not rec and flow_id:
                flow=next((f for f in read_json(FLOWS_FILE,[]) if f.get("id")==flow_id),None)
                if flow and flow.get("recording_id"): rec=RECORDER.recording_data(flow.get("recording_id"))
            if not rec: return self._json({"error":"recording_not_found","message":"Select a flow with a saved recording."},404)
            flow_id=flow_id or rec.get("flow_id")
            guarded=self._guard_execution(flow_id,rec.get("id"),bool(body.get("confirmed",False)),purpose="execution")
            if not guarded: return
            _,rec,design=guarded
            body=GOV.clamp_execution(body)
            ok,payload=EXECUTOR.start(rec,flow_id=flow_id,headless=bool(body.get("headless",True)),timeout_ms=body.get("timeout_ms",12000),retries=body.get("retries",1),failure_policy=str(body.get("failure_policy") or "stop"),start_step=body.get("start_step",0),variables=body.get("variables") or {},logic_steps=(design or {}).get("steps") if body.get("use_designer",True) else None,subflows=(design or {}).get("subflows") if design else None)
            return self._json(payload,201 if ok else 409 if payload.get("error")=="runner_busy" else 400)
        if path=="/api/executor/cancel":
            ok,payload=EXECUTOR.cancel(); return self._json(payload,200 if ok else 409)
        if path.startswith("/api/runs/") and path.endswith("/retry-failed"):
            parts=path.strip("/").split("/"); run_id=parts[2]; old=EXECUTOR.get(run_id)
            if not old: return self._json({"error":"run_not_found"},404)
            rec=RECORDER.recording_data(old.get("recording_id"))
            if not rec: return self._json({"error":"recording_not_found"},404)
            guarded=self._guard_execution(str(old.get("flow_id") or rec.get("flow_id") or ""),rec.get("id"),bool((body or {}).get("confirmed",False)),purpose="retry")
            if not guarded: return
            ok,payload=EXECUTOR.retry_failed(run_id,rec); return self._json(payload,201 if ok else 409)
        if path=="/api/hybrid/execute":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            client_id=str(body.get("client_id") or "")
            if not client_id: return self._json({"error":"local_client_required","message":"Select a paired Local Client for browser execution."},409)
            recording_id=str(body.get("recording_id") or ""); flow_id=str(body.get("flow_id") or "")
            rec=RECORDER.recording_data(recording_id) if recording_id else None
            if not rec and flow_id:
                flow=next((f for f in read_json(FLOWS_FILE,[]) if f.get("id")==flow_id),None); recording_id=str((flow or {}).get("recording_id") or ""); rec=RECORDER.recording_data(recording_id) if recording_id else None
            if not rec: return self._json({"error":"recording_not_found"},404)
            flow_id=flow_id or str(rec.get("flow_id") or "")
            guarded=self._guard_execution(flow_id,rec.get("id"),bool(body.get("confirmed",False)),purpose="local-client-execution")
            if not guarded: return
            _,rec,design=guarded; body=GOV.clamp_execution(body)
            payload={
                "flow_id":flow_id,"recording":rec,"headless":bool(body.get("headless",True)),
                "timeout_ms":body.get("timeout_ms",12000),"retries":body.get("retries",1),
                "failure_policy":str(body.get("failure_policy") or "stop"),"variables":body.get("variables") or {},
                "logic_steps":(design or {}).get("steps") if body.get("use_designer",True) else None,
                "subflows":(design or {}).get("subflows") if design else {},
            }
            ok,task=HYBRID.queue_task(client_id,"execute_flow",payload,self._session())
            if ok:
                GOV.audit("hybrid_execution_dispatched",self._session(),{"task_id":task.get("id"),"client_id":client_id,"flow_id":flow_id,"recording_id":rec.get("id")})
                return self._json({"task_id":task.get("id"),"state":"queued","flow_id":flow_id,"recording_id":rec.get("id"),"client_id":client_id,"requested_by":task.get("requested_by")},202)
            return self._json(task,409)
        if path.startswith("/api/hybrid/tasks/") and path.endswith("/cancel"):
            tid=path.strip("/").split("/")[3]; task=HYBRID.get_task(tid)
            if not task: return self._json({"error":"task_not_found"},404)
            client_id=str(task.get("client_id") or "")
            ok,cancel=HYBRID.queue_task(client_id,"cancel_execution",{"target_task_id":tid},self._session())
            return self._json({"state":"cancelling","task_id":tid,"cancel_task_id":cancel.get("id") if ok else None},202 if ok else 409)
        if path=="/api/recorder/start":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            ok_url,url_issue=GOV.validate_url(str(body.get("url") or ""))
            if not ok_url: return self._json(url_issue,403)
            name=str(body.get("recording_name") or "New Recording").strip() or "New Recording"
            client_id=str(body.get("client_id") or "")
            # Hosted/BTP Studio never launches Chromium inside Cloud Foundry.
            # It dispatches a signed, typed recording task to a paired local client.
            if RUNTIME.cloud_foundry or client_id:
                if not client_id:
                    online=[c for c in HYBRID.clients() if c.get("state") in {"online","busy"}]
                    if len(online)==1: client_id=online[0].get("id")
                if not client_id: return self._json({"error":"local_client_required","message":"Pair and select a WebFlow Local Client to record from the hosted Studio."},409)
                ok,payload=HYBRID.queue_task(client_id,"record_start",{"url":str(body.get("url") or ""),"recording_name":name,"flow_id":body.get("flow_id") or None},self._session())
                if ok:
                    payload={**payload,"state":"starting","execution_location":"local-client","client_id":client_id,"url":str(body.get("url") or ""),"steps":0}
                return self._json(payload,202 if ok else 409)
            result=RECORDER.start(body.get("flow_id") or None,str(body.get("url") or ""),recording_name=name); return self._json(result.payload,200 if result.ok else 409 if result.payload.get("error")=="recorder_busy" else 400)
        if path in {"/api/recorder/pause","/api/recorder/resume","/api/recorder/stop"}:
            client_id=str((body or {}).get("client_id") or "") if isinstance(body,dict) else ""
            remote=HYBRID.latest_recording_status()
            if RUNTIME.cloud_foundry or client_id or remote:
                client_id=client_id or (remote or {}).get("client_id") or ""
                task_type={"/api/recorder/pause":"record_pause","/api/recorder/resume":"record_resume","/api/recorder/stop":"record_stop"}[path]
                ok,payload=HYBRID.queue_task(client_id,task_type,{},self._session()) if client_id else (False,{"error":"local_client_required"})
                return self._json({**payload,"state":"stopping" if task_type=="record_stop" else "paused" if task_type=="record_pause" else "recording","client_id":client_id,"execution_location":"local-client"},202 if ok else 409)
            result={"/api/recorder/pause":RECORDER.pause,"/api/recorder/resume":RECORDER.resume,"/api/recorder/stop":RECORDER.stop}[path]()
            return self._json(result.payload,200 if result.ok else 409)
        return self._json({"error":"not_found"},404)

    def do_PUT(self):
        self._audit_written=False
        path=urlparse(self.path).path; body=self._body()
        if path in {"/api/governance","/api/worker/settings"}:
            if not self._require_role("admin"): return
        elif path.startswith(("/api/designs/","/api/objects/","/api/flows/","/api/recordings/")):
            if not self._require_role("runner"): return
        elif path.startswith("/api/schedules/"):
            if not self._require_role("runner"): return
        if path=="/api/governance":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            updated=GOV.update_config(body); GOV.audit("governance_updated",self._session(),{"keys":list(body.keys())}); return self._json(updated)
        if path=="/api/worker/settings":
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            body=dict(body); body["max_workers"]=min(int(body.get("max_workers",2) or 2),int(GOV.get_config().get("limits",{}).get("max_queue_workers",4)))
            return self._json(SCHEDULER.update_settings(body))
        if path.startswith("/api/schedules/"):
            sid=path.rsplit("/",1)[-1]
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            body["id"]=sid; return self._json(SCHEDULER.save_schedule(body))
        if path.startswith("/api/designs/"):
            fid=path.rsplit("/",1)[-1]
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            return self._json(DESIGNER.save(fid,body))
        if path.startswith("/api/recordings/") and "/steps/" not in path:
            rid=path.rsplit("/",1)[-1]
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            rec=RECORDER.recording_data(rid)
            if not rec: return self._json({"error":"recording_not_found"},404)
            if "recording_name" in body:
                name=str(body.get("recording_name") or "").strip()[:160]
                if name: rec["recording_name"]=name
            if "notes" in body: rec["notes"]=str(body.get("notes") or "")[:4000]
            if "retention_days" in body:
                rec["retention_days"]=_bounded_retention_days(body.get("retention_days")); rec["retention_policy"]="hub-saved"; rec["retention_saved_at"]=datetime.now(timezone.utc).isoformat(); _ensure_recording_retention(rec,reset_from_now=True)
            persist_recording(rec)
            if RECORDER.recording and RECORDER.recording.get("id")==rid: RECORDER.recording.update(rec)
            # Keep the promoted Draft WebFlow aligned with a recording rename.
            fid=rec.get("flow_id")
            if fid and body.get("sync_flow_name",True):
                flows=read_json(FLOWS_FILE,[])
                changed=False
                for f in flows:
                    if f.get("id")==fid and rec.get("recording_name"):
                        f["name"]=rec.get("recording_name"); changed=True
                if changed: _write_json(FLOWS_FILE,flows)
            return self._json(rec)
        if path.startswith("/api/recordings/") and "/steps/" in path:
            parts=path.strip("/").split("/"); rid=parts[2]; sid=parts[4]
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            rec=RECORDER.recording_data(rid)
            if not rec: return self._json({"error":"recording_not_found"},404)
            step=next((x for x in rec.get("steps",[]) if x.get("id")==sid),None)
            if not step: return self._json({"error":"step_not_found"},404)
            allowed={"training_note","training_title","training_hidden"}
            for k,v in body.items():
                if k in allowed: step[k]=v
            if RECORDER.recording and RECORDER.recording.get("id")==rid:
                for x in RECORDER.recording.get("steps",[]):
                    if x.get("id")==sid: x.update({k:v for k,v in body.items() if k in allowed})
                RECORDER._persist()
            else: persist_recording(rec)
            return self._json(step)
        if path.startswith("/api/objects/"):
            oid=path.rsplit("/",1)[-1]
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            obj=OBJECTS.update(oid,body); return self._json(obj or {"error":"object_not_found"},200 if obj else 404)
        if path.startswith("/api/flows/"):
            flow_id=path.rsplit("/",1)[-1]
            if not isinstance(body,dict): return self._json({"error":"invalid_json"},400)
            flows=read_json(FLOWS_FILE,[])
            for i,flow in enumerate(flows):
                if flow.get("id")==flow_id:
                    allowed={"name","description","status","owner","starting_url"}; flows[i]={**flow,**{k:v for k,v in body.items() if k in allowed},"updated_at":datetime.now(timezone.utc).isoformat()}; _write_json(FLOWS_FILE,flows); return self._json(flows[i])
            return self._json({"error":"flow_not_found"},404)
        return self._json({"error":"not_found"},404)

    def do_DELETE(self):
        self._audit_written=False
        path=urlparse(self.path).path
        if path.startswith(("/api/flows/","/api/recordings/")):
            if not self._require_role("runner"): return
        elif path.startswith("/api/schedules/"):
            if not self._require_role("runner"): return
        if path.startswith("/api/schedules/"):
            sid=path.rsplit("/",1)[-1]; ok=SCHEDULER.delete_schedule(sid); return self._json({"deleted":sid} if ok else {"error":"schedule_not_found"},200 if ok else 404)
        if path.startswith("/api/recordings/"):
            rid=path.rsplit("/",1)[-1]
            rec=RECORDER.recording_data(rid)
            if not rec: return self._json({"error":"recording_not_found"},404)
            try: (DATA_DIR/"recordings"/f"{rid}.json").unlink(missing_ok=True)
            except Exception: pass
            # Remove screenshots owned by this recording only.
            shotdir=DATA_DIR/"recordings"/"screenshots"/rid
            try:
                if shotdir.exists(): shutil.rmtree(shotdir)
            except Exception: pass
            fid=rec.get("flow_id")
            if fid:
                flows=read_json(FLOWS_FILE,[]); _write_json(FLOWS_FILE,[f for f in flows if f.get("id")!=fid])
                try: (DATA_DIR/"designs"/f"{fid}.json").unlink(missing_ok=True)
                except Exception: pass
            try: OBJECTS.rebuild()
            except Exception: pass
            return self._json({"deleted":rid,"flow_id":fid})
        if path.startswith("/api/flows/"):
            flow_id=path.rsplit("/",1)[-1]; flows=read_json(FLOWS_FILE,[]); kept=[f for f in flows if f.get("id")!=flow_id]
            if len(kept)==len(flows): return self._json({"error":"flow_not_found"},404)
            _write_json(FLOWS_FILE,kept); return self._json({"deleted":flow_id})
        return self._json({"error":"not_found"},404)

def open_browser(): webbrowser.open(f"http://{HOST}:{PORT}")

if __name__=="__main__":
    print(f"ALM WebFlow Studio - Phase 14 Complete + Hybrid Runtime v5\nhttp://{HOST}:{PORT}\nEnvironment: {RUNTIME.environment} | Storage: {STORAGE.adapter}\nCtrl+C to stop")
    if not RECORDER.dependency_status():
        print("\nPlaywright is not installed yet. Run:\n  pip install -r requirements.txt\n  python -m playwright install chromium\n")
    STORAGE.start()
    if not RUNTIME.cloud_foundry and os.getenv("WEBFLOW_NO_BROWSER","0")!="1": threading.Timer(.6,open_browser).start()
    try: ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
    except KeyboardInterrupt:
        try: RECORDER.stop()
        except Exception: pass
        try: DATA.cancel_batch()
        except Exception: pass
        try: EXECUTOR.cancel()
        except Exception: pass
        try: PERFORMANCE.cancel()
        except Exception: pass
        try: SCHEDULER.shutdown()
        except Exception: pass
        try: STORAGE.stop()
        except Exception: pass
