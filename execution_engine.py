from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
import json
import re
import ast
import time
import uuid
import os

try:
    import psutil
except Exception:
    psutil = None

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except Exception:
    sync_playwright = None
    PlaywrightTimeoutError = Exception


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_file_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or ""))
    return value.strip("-.")[:80] or "item"


def derive_variable_key(step: dict, fallback_index=1):
    target = step.get("target") or {}
    raw = step.get("variable_key") or step.get("training_title") or target.get("label") or target.get("aria_label") or target.get("name") or target.get("text") or f"{str(step.get('classification') or 'value').lower()}_{str(step.get('id') or fallback_index)[:8]}"
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", str(raw).strip()).strip("_").lower()
    if not key:
        key = f"value_{fallback_index}"
    if key[0].isdigit():
        key = "v_" + key
    return key[:80]


def apply_template(value, variables):
    if value is None or not isinstance(value, str):
        return value
    def repl(match):
        key=match.group(1).strip()
        v=variables.get(key, match.group(0))
        return "" if v is None else str(v)
    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, value)


class ExecutionManager:
    """Phase 5 local Playwright runner.

    The runner consumes the same normalized recording produced by the recorder.
    Every meaningful executable step produces a structured result and a fresh
    execution screenshot, giving Replay/Training and later diagnostics a
    consistent run-artifact source.
    """

    EXECUTABLE_ACTIONS = {"navigate", "click", "input", "select", "check", "key", "change"}

    def __init__(self, root: Path, write_json, flow_update_callback=None, object_provider=None):
        self.root = root
        self.write_json = write_json
        self.flow_update_callback = flow_update_callback
        self.object_provider = object_provider
        self.runs_dir = root / "data" / "runs"
        self.lock = Lock()
        self.thread: Thread | None = None
        self.cancel_event = Event()
        self.active_run: dict | None = None
        self.page = None
        self.browser = None
        self.playwright = None
        self.browser_pids: set[int] = set()
        self.browser_closed = Event()

    def dependency_status(self):
        return sync_playwright is not None

    def _run_path(self, run_id: str) -> Path:
        return self.runs_dir / safe_file_part(run_id) / "run.json"

    def _persist(self, run: dict | None = None):
        run = run or self.active_run
        if not run:
            return
        self.write_json(self._run_path(run["id"]), run)

    def status(self):
        with self.lock:
            return json.loads(json.dumps(self.active_run)) if self.active_run else {"state": "idle"}

    def get(self, run_id: str):
        with self.lock:
            if self.active_run and self.active_run.get("id") == run_id:
                return json.loads(json.dumps(self.active_run))
        path = self._run_path(run_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_runs(self, limit=50):
        items = []
        if not self.runs_dir.exists():
            return items
        paths = sorted(self.runs_dir.glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in paths[:limit]:
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
                items.append({
                    "id": r.get("id"), "flow_id": r.get("flow_id"), "recording_id": r.get("recording_id"),
                    "state": r.get("state"), "started_at": r.get("started_at"), "ended_at": r.get("ended_at"),
                    "duration_ms": r.get("duration_ms"), "total_steps": r.get("total_steps", 0),
                    "completed_steps": r.get("completed_steps", 0), "failed_steps": r.get("failed_steps", 0),
                    "headless": r.get("headless", False), "last_error": r.get("last_error"),
                })
            except Exception:
                continue
        return items

    def start(self, recording: dict, *, flow_id: str | None = None, headless=True, timeout_ms=12000,
              retries=1, failure_policy="stop", start_step=0, variables=None, batch_context=None, logic_steps=None, subflows=None):
        if sync_playwright is None:
            return False, {"error": "playwright_not_installed", "message": "Install Playwright and Chromium first."}
        if not recording or not recording.get("steps"):
            return False, {"error": "recording_required", "message": "This flow has no saved recording to execute."}
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False, {"error": "runner_busy", "message": "Another execution is still active."}
            run_id = str(uuid.uuid4())
            executable = [s for s in recording.get("steps", []) if s.get("action") in self.EXECUTABLE_ACTIONS]
            self.active_run = {
                "schema_version": "webflow-run/1",
                "id": run_id,
                "flow_id": flow_id or recording.get("flow_id"),
                "recording_id": recording.get("id"),
                "state": "starting",
                "started_at": utc_now(),
                "ended_at": None,
                "duration_ms": None,
                "headless": bool(headless),
                "timeout_ms": max(1000, int(timeout_ms or 12000)),
                "retries": max(0, min(5, int(retries or 0))),
                "failure_policy": failure_policy if failure_policy in {"stop", "continue"} else "stop",
                "start_step": max(0, int(start_step or 0)),
                "total_steps": len(executable),
                "completed_steps": 0,
                "failed_steps": 0,
                "current_step": None,
                "last_error": None,
                "outputs": {},
                "variable_keys": sorted(list((variables or {}).keys())),
                "batch_context": batch_context or None,
                "logic_enabled": bool(logic_steps),
                "console_errors": [],
                "network_errors": [],
                "steps": [],
            }
            self.cancel_event.clear()
            self._persist()
            self.thread = Thread(target=self._run, args=(recording, dict(variables or {}), logic_steps, subflows or {}), daemon=True, name="WebFlowExecution")
            self.thread.start()
            return True, json.loads(json.dumps(self.active_run))

    def cancel(self):
        with self.lock:
            if not (self.thread and self.thread.is_alive()) or not self.active_run:
                return False, {"error": "no_active_run"}
            self.active_run["state"] = "cancelling"
            self._persist()
        self.cancel_event.set()
        return True, self.status()

    def retry_failed(self, run_id: str, recording: dict):
        old = self.get(run_id)
        if not old:
            return False, {"error": "run_not_found"}
        failed = [x for x in old.get("steps", []) if x.get("status") == "failed"]
        if not failed:
            return False, {"error": "no_failed_step", "message": "The selected run has no failed step."}
        index = int(failed[0].get("source_index", 0))
        return self.start(recording, flow_id=old.get("flow_id"), headless=old.get("headless", True),
                          timeout_ms=old.get("timeout_ms", 12000), retries=old.get("retries", 1),
                          failure_policy=old.get("failure_policy", "stop"), start_step=index)

    def _capture_browser_processes(self, before: set[int]):
        """Track only Chromium descendants created by this execution."""
        if psutil is None:
            return
        try:
            parent=psutil.Process(os.getpid())
            self.browser_pids={p.pid for p in parent.children(recursive=True) if p.pid not in before and any(k in ' '.join(p.cmdline()).lower() for k in ('chromium','chrome','playwright'))}
        except Exception:
            self.browser_pids=set()

    def _terminate_browser_tree(self):
        if psutil is None:
            return
        procs=[]
        for pid in list(self.browser_pids):
            try: procs.append(psutil.Process(pid))
            except Exception: pass
        for proc in sorted(procs,key=lambda x:x.pid,reverse=True):
            try: proc.terminate()
            except Exception: pass
        try: _,alive=psutil.wait_procs(procs,timeout=1.5)
        except Exception: alive=[]
        for proc in alive:
            try: proc.kill()
            except Exception: pass

    def _locator_matches_target(self, loc, step):
        """Reject a selector that resolves to the wrong DOM element.

        Recorded pages can contain deceptively generic IDs/classes.  A locator is
        accepted only when its tag and at least one recorded semantic identity
        (text/aria/name/id/href/title) still agree with the recorded target.
        """
        t=step.get('target') or {}
        try:
            info=loc.evaluate("""el => ({tag:(el.tagName||'').toLowerCase(),id:el.id||'',name:el.getAttribute('name')||'',aria:el.getAttribute('aria-label')||'',href:el.getAttribute('href')||'',title:el.getAttribute('title')||'',text:String(el.innerText||el.textContent||'').replace(/\\s+/g,' ').trim().slice(0,220)})""")
        except Exception:
            return False
        expected_tag=str(t.get('tag') or '').lower()
        if expected_tag and info.get('tag') and expected_tag != info.get('tag'):
            return False
        checks=[]
        for a,b in [('id','id'),('name','name'),('aria_label','aria'),('href','href'),('title','title')]:
            v=str(t.get(a) or '').strip()
            if v: checks.append(v==str(info.get(b) or '').strip())
        text=str(t.get('text') or t.get('label') or '').strip()
        if text:
            actual=str(info.get('text') or '').strip()
            checks.append(text==actual or text in actual or actual in text)
        return True if not checks else any(checks)

    def _object_for_step(self, recording_id, step_id):
        if not self.object_provider:
            return None
        try:
            objects = self.object_provider() or []
            return next((o for o in objects if o.get("source_recording_id") == recording_id and o.get("source_step_id") == step_id), None)
        except Exception:
            return None

    def _candidate_locators(self, step: dict, recording_id: str):
        target = step.get("target") or {}
        selectors = list(target.get("selectors") or [])
        obj = self._object_for_step(recording_id, step.get("id"))
        if obj and obj.get("primary_locator"):
            primary = obj["primary_locator"]
            match = next((s for s in obj.get("selectors", []) if s.get("value") == primary), None)
            if match:
                selectors = [match] + [s for s in selectors if s.get("value") != primary]
        # Derived semantic selectors are intentionally strong for links/buttons.
        semantic=[]
        if target.get("text") and (target.get("tag") in {"a", "button"} or target.get("role") in {"link", "button"}):
            semantic.append({"kind":"text","value":str(target["text"])[:120],"score":94})
        if target.get("href") and target.get("tag")=="a":
            href=str(target.get("href")).replace('"','\\"')
            semantic.append({"kind":"href","value":f'a[href="{href}"]',"score":89})
        selectors = semantic + selectors
        if target.get("label"):
            selectors.append({"kind": "label", "value": target["label"], "score": 50})
        if target.get("aria_label"):
            selectors.append({"kind": "aria-name", "value": target["aria_label"], "score": 48})
        if target.get("text") and (target.get("tag") in {"a", "button"} or target.get("role") in {"link", "button"}):
            selectors.append({"kind": "text", "value": str(target["text"])[:120], "score": 35})
        seen = set(); result = []
        for s in selectors:
            key = (s.get("kind"), s.get("value"))
            if not s.get("value") or key in seen:
                continue
            seen.add(key); result.append(s)
        return result

    def _locator(self, page, candidate: dict, step: dict):
        kind = candidate.get("kind")
        value = candidate.get("value")
        target = step.get("target") or {}
        if kind == "label":
            return page.get_by_label(value, exact=True)
        if kind == "aria-name":
            role = target.get("role") or ("button" if target.get("tag") == "button" else "link" if target.get("tag") == "a" else None)
            return page.get_by_role(role, name=value, exact=True) if role else page.get_by_label(value, exact=True)
        if kind == "role":
            name = target.get("aria_label") or target.get("label") or target.get("text") or None
            return page.get_by_role(value, name=name, exact=False) if name else page.get_by_role(value)
        if kind == "text":
            role = target.get("role") or ("button" if target.get("tag") == "button" else "link" if target.get("tag") == "a" else None)
            return page.get_by_role(role, name=value, exact=False) if role else page.get_by_text(value, exact=False)
        if kind == "href":
            return page.locator(value)
        return page.locator(value)

    def _resolve_locator(self, page, step, recording_id, timeout_ms):
        errors = []
        for candidate in self._candidate_locators(step, recording_id):
            try:
                loc = self._locator(page, candidate, step).first
                loc.wait_for(state="attached", timeout=min(timeout_ms, 3500))
                if not self._locator_matches_target(loc, step):
                    errors.append(f"{candidate.get('kind')}={candidate.get('value')}: resolved to a different element")
                    continue
                return loc, candidate, errors
            except Exception as exc:
                errors.append(f"{candidate.get('kind')}={candidate.get('value')}: {str(exc).splitlines()[0][:140]}")
        return None, None, errors

    def _screenshot(self, run_id, seq, suffix="ok"):
        if not self.page:
            return None
        rel = Path("data") / "runs" / safe_file_part(run_id) / "screenshots" / f"{seq:04d}-{safe_file_part(suffix)}.png"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(path), full_page=False)
            return "/" + rel.as_posix()
        except Exception:
            return None

    def _read_value(self, loc, step):
        target = step.get("target") or {}
        try:
            if target.get("tag") in {"input", "textarea", "select"}:
                return loc.input_value(timeout=2500)
        except Exception:
            pass
        try:
            return (loc.inner_text(timeout=2500) or "").strip()
        except Exception:
            try:
                return loc.get_attribute("value", timeout=1500)
            except Exception:
                return None

    def _execute_action(self, step, recording_id, timeout_ms, variables=None, variable_index=1):
        variables = variables or {}
        action = step.get("action")
        if action == "navigate":
            url = apply_template(step.get("page_url") or "", variables)
            self.page.goto(url, wait_until="domcontentloaded", timeout=max(timeout_ms, 15000))
            return None, {"kind": "url", "value": url}, None

        loc, used, locator_errors = self._resolve_locator(self.page, step, recording_id, timeout_ms)
        if not loc:
            raise RuntimeError("Object could not be resolved. " + " | ".join(locator_errors[-3:]))

        # Optional pre-action assertion/extraction target value.
        before_value = self._read_value(loc, step) if step.get("classification") in {"Output", "Assertion"} else None

        if action == "click":
            loc.click(timeout=timeout_ms)
        elif action == "input":
            cls = step.get("classification")
            key = derive_variable_key(step, variable_index)
            if cls in {"Input", "Secret"}:
                if key in variables:
                    value = variables.get(key)
                elif cls == "Secret" or step.get("secret"):
                    raise RuntimeError(f"Required secret variable '{key}' was not supplied.")
                else:
                    value = step.get("value")
            else:
                value = apply_template(step.get("value"), variables)
            loc.fill("" if value is None else str(value), timeout=timeout_ms)
        elif action == "select":
            cls = step.get("classification")
            key = derive_variable_key(step, variable_index)
            value = variables.get(key) if cls in {"Input", "Secret"} and key in variables else apply_template(step.get("value"), variables)
            if cls == "Secret" and key not in variables:
                raise RuntimeError(f"Required secret variable '{key}' was not supplied.")
            value = "" if value is None else str(value)
            try:
                loc.select_option(value=value, timeout=timeout_ms)
            except Exception:
                loc.select_option(label=value, timeout=timeout_ms)
        elif action == "check":
            if bool(step.get("checked")):
                loc.check(timeout=timeout_ms)
            else:
                loc.uncheck(timeout=timeout_ms)
        elif action == "key":
            loc.press(str(step.get("key") or "Enter"), timeout=timeout_ms)
        elif action == "change":
            # Recorder may emit a generic change after an input. Avoid duplicate mutation;
            # keep it as a verification/checkpoint event.
            pass

        output = None
        if step.get("classification") == "Output":
            output = self._read_value(loc, step)
        elif step.get("classification") == "Assertion":
            actual = self._read_value(loc, step)
            assertion_key = derive_variable_key(step, variable_index)
            expected = variables.get(assertion_key) if assertion_key in variables else apply_template(step.get("value"), variables)
            if expected in (None, ""):
                expected = (step.get("target") or {}).get("text") or before_value
            if expected not in (None, "") and str(expected).strip() not in str(actual or ""):
                raise AssertionError(f"Assertion failed: expected '{expected}', got '{actual}'")
            output = actual
        return loc, used, output

    def _safe_eval(self, expression, variables):
        expression=str(expression or '').strip()
        if not expression: return False
        tree=ast.parse(expression,mode='eval')
        bad=(ast.Import,ast.ImportFrom,ast.Lambda,ast.FunctionDef,ast.ClassDef,ast.Global,ast.Nonlocal)
        for n in ast.walk(tree):
            if isinstance(n,bad): raise ValueError('Unsupported expression')
            if isinstance(n,ast.Attribute) and str(n.attr).startswith('__'): raise ValueError('Private attributes are blocked')
            if isinstance(n,ast.Name) and str(n.id).startswith('__'): raise ValueError('Private names are blocked')
        safe={'len':len,'str':str,'int':int,'float':float,'bool':bool,'min':min,'max':max,'sum':sum,'any':any,'all':all,'round':round,'list':list,'dict':dict,'sorted':sorted}
        return eval(compile(tree,'<webflow-expression>','eval'),{'__builtins__':safe},dict(variables))

    def _safe_python(self, code, variables):
        code=str(code or '')
        tree=ast.parse(code,mode='exec')
        blocked=(ast.Import,ast.ImportFrom,ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Global,ast.Nonlocal,ast.With,ast.AsyncWith,ast.Try,ast.Raise)
        for n in ast.walk(tree):
            if isinstance(n,blocked): raise ValueError(f'{type(n).__name__} is not allowed in governed Python nodes')
            if isinstance(n,ast.Attribute) and str(n.attr).startswith('__'): raise ValueError('Private attributes are blocked')
            if isinstance(n,ast.Name) and str(n.id).startswith('__'): raise ValueError('Private names are blocked')
        safe={'len':len,'str':str,'int':int,'float':float,'bool':bool,'min':min,'max':max,'sum':sum,'any':any,'all':all,'round':round,'range':range,'enumerate':enumerate,'list':list,'dict':dict,'set':set,'sorted':sorted,'abs':abs}
        env=dict(variables); env['variables']=variables; env['outputs']=dict(self.active_run.get('outputs') or {})
        exec(compile(tree,'<webflow-python>','exec'),{'__builtins__':safe},env)
        # Explicit variables dict is the supported mutation contract; simple assignments are also copied back.
        for k,v in env.items():
            if k not in {'variables','outputs'} and not k.startswith('_') and k not in safe: variables[k]=v
        return env.get('result')

    def _logic_label(self,node):
        return node.get('name') or {'if':'IF','foreach':'FOR EACH','set':'Set Variable','python':'Python','wait':'Wait','subflow':'Subflow'}.get(node.get('type'),'Logic')

    def _append_logic_result(self,node,status='passed',output=None,error=None):
        seq=len(self.active_run.get('steps') or [])+1
        r={'id':str(uuid.uuid4()),'sequence':seq,'source_index':-1,'source_step_id':node.get('id'),'action':node.get('type'),'label':self._logic_label(node),'classification':'Logic','status':status,'started_at':utc_now(),'ended_at':utc_now(),'duration_ms':0,'attempts':1,'locator_used':None,'screenshot':self._screenshot(self.active_run['id'],seq,status) if self.page else None,'output':output,'error':error}
        self.active_run['steps'].append(r)
        if status=='passed': self.active_run['completed_steps']+=1
        else: self.active_run['failed_steps']+=1; self.active_run['last_error']=error
        self._persist(); return r

    def _execute_browser_result(self, step, recording_id, variables, source_index=-1):
        seq=len(self.active_run.get('steps') or [])+1
        result={'id':str(uuid.uuid4()),'sequence':seq,'source_index':source_index,'source_step_id':step.get('id'),'action':step.get('action'),'label':((step.get('target') or {}).get('label') or (step.get('target') or {}).get('aria_label') or (step.get('target') or {}).get('text') or (step.get('target') or {}).get('tag') or step.get('page_title') or 'Step'),'classification':step.get('classification'),'status':'running','started_at':utc_now(),'ended_at':None,'duration_ms':None,'attempts':0,'locator_used':None,'screenshot':None,'output':None,'error':None}
        self.active_run['current_step']=result; self.active_run['steps'].append(result); self._persist()
        st=time.perf_counter(); error=None
        for attempt in range(self.active_run.get('retries',1)+1):
            result['attempts']=attempt+1
            try:
                _,used,output=self._execute_action(step,recording_id,self.active_run.get('timeout_ms',12000),variables,seq)
                result['locator_used']=used; result['output']=output
                if output is not None:
                    key=derive_variable_key(step,seq); self.active_run['outputs'][key]=output; variables[key]=output
                error=None; break
            except Exception as exc:
                error=exc
                if attempt < self.active_run.get('retries',1): time.sleep(.35*(attempt+1))
        result['ended_at']=utc_now(); result['duration_ms']=round((time.perf_counter()-st)*1000,1)
        if error is None:
            result['status']='passed'; result['screenshot']=self._screenshot(self.active_run['id'],seq,'passed'); self.active_run['completed_steps']+=1
        else:
            result['status']='failed'; result['error']=f'{type(error).__name__}: {error}'; result['screenshot']=self._screenshot(self.active_run['id'],seq,'failed'); self.active_run['failed_steps']+=1; self.active_run['last_error']=result['error']
        self._persist(); return error is None

    def _execute_logic_nodes(self,nodes,recording,variables,subflows):
        for node in nodes or []:
            if self.cancel_event.is_set(): return False
            if not node.get('enabled',True): continue
            typ=node.get('type')
            try:
                if typ=='browser':
                    step=node.get('browser_step') or {}
                    rid=node.get('source_recording_id') or recording.get('id')
                    if not self._execute_browser_result(step,rid,variables):
                        if self.active_run.get('failure_policy')=='stop': return False
                elif typ=='if':
                    passed=bool(self._safe_eval(node.get('expression') or 'False',variables)); self._append_logic_result(node,output=passed)
                    if not self._execute_logic_nodes(node.get('then') if passed else node.get('else'),recording,variables,subflows): return False
                elif typ=='foreach':
                    source=self._safe_eval(node.get('source') or '[]',variables)
                    if isinstance(source,str): source=[x.strip() for x in source.split(',') if x.strip()]
                    items=list(source or []); self._append_logic_result(node,output=f'{len(items)} iteration(s)')
                    item_var=node.get('item_var') or 'item'; index_var=node.get('index_var') or 'index'
                    for i,item in enumerate(items):
                        variables[item_var]=item; variables[index_var]=i
                        if not self._execute_logic_nodes(node.get('body'),recording,variables,subflows): return False
                elif typ=='set':
                    key=str(node.get('variable') or 'value'); val=self._safe_eval(node.get('expression') or 'None',variables); variables[key]=val; self._append_logic_result(node,output=f'{key} = {val}')
                elif typ=='wait':
                    ms=max(0,min(60000,int(node.get('milliseconds') or 500))); time.sleep(ms/1000); self._append_logic_result(node,output=f'{ms} ms')
                elif typ=='python':
                    val=self._safe_python(node.get('code') or '',variables); self._append_logic_result(node,output=val)
                elif typ=='subflow':
                    name=str(node.get('subflow') or ''); self._append_logic_result(node,output=name)
                    if name not in subflows: raise ValueError(f"Subflow '{name}' does not exist")
                    if not self._execute_logic_nodes(subflows.get(name),recording,variables,subflows): return False
            except Exception as exc:
                self._append_logic_result(node,status='failed',error=f'{type(exc).__name__}: {exc}')
                if self.active_run.get('failure_policy')=='stop': return False
        return True

    def _run(self, recording, variables=None, logic_steps=None, subflows=None):
        variables = variables or {}
        started_perf = time.perf_counter()
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            before=set()
            if psutil is not None:
                try: before={p.pid for p in psutil.Process(os.getpid()).children(recursive=True)}
                except Exception: before=set()
            self.browser_closed.clear()
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=bool(self.active_run.get("headless", True)))
            self._capture_browser_processes(before)
            self.browser.on("disconnected", lambda: self.browser_closed.set())
            context = self.browser.new_context(viewport={"width": 1360, "height": 820})
            self.page = context.new_page()

            # Phase 9 telemetry: capture browser console/page errors and failed HTTP/network activity
            # without collecting response bodies or sensitive request payloads.
            def _console(msg):
                try:
                    if getattr(msg, "type", "") == "error":
                        self.active_run.setdefault("console_errors", []).append({"at": utc_now(), "type": "console", "text": str(getattr(msg, "text", ""))[:1200]})
                        self._persist()
                except Exception:
                    pass
            def _page_error(exc):
                try:
                    self.active_run.setdefault("console_errors", []).append({"at": utc_now(), "type": "pageerror", "text": str(exc)[:1200]})
                    self._persist()
                except Exception:
                    pass
            def _request_failed(req):
                try:
                    failure = getattr(req, "failure", None)
                    self.active_run.setdefault("network_errors", []).append({"at": utc_now(), "type": "requestfailed", "method": str(getattr(req, "method", "")), "url": str(getattr(req, "url", ""))[:1600], "detail": str(failure or "Request failed")[:500]})
                    self._persist()
                except Exception:
                    pass
            def _response(resp):
                try:
                    status = int(getattr(resp, "status", 0) or 0)
                    if status >= 400:
                        req = getattr(resp, "request", None)
                        self.active_run.setdefault("network_errors", []).append({"at": utc_now(), "type": "http", "status": status, "method": str(getattr(req, "method", "")) if req else "", "url": str(getattr(resp, "url", ""))[:1600]})
                        self._persist()
                except Exception:
                    pass
            self.page.on("console", _console)
            self.page.on("pageerror", _page_error)
            self.page.on("requestfailed", _request_failed)
            self.page.on("response", _response)
            with self.lock:
                self.active_run["state"] = "running"
                self._persist()

            if logic_steps:
                # Designer runtime: structured IF / FOR EACH / Set / Python / Subflow nodes.
                self.active_run["total_steps"] = len(logic_steps)
                ok = self._execute_logic_nodes(logic_steps, recording, variables, subflows or {})
                if not ok and self.active_run.get("state") == "running":
                    self.active_run["state"] = "failed"
            else:
                executable = [(i, st) for i, st in enumerate(recording.get("steps", [])) if st.get("action") in self.EXECUTABLE_ACTIONS]
                start_index = int(self.active_run.get("start_step", 0))
                for display_seq, (source_index, step) in enumerate(executable, 1):
                    if source_index < start_index: continue
                    if self.cancel_event.is_set() or self.browser_closed.is_set():
                        with self.lock:
                            self.active_run["state"] = "cancelled" if self.cancel_event.is_set() else "failed"
                            if self.browser_closed.is_set(): self.active_run["last_error"]="Browser window was closed before execution completed."
                        break
                    if not self._execute_browser_result(step, recording.get("id"), variables, source_index):
                        if self.active_run.get("failure_policy") == "stop":
                            with self.lock: self.active_run["state"] = "failed"
                            break
            with self.lock:
                if self.active_run.get("state") == "running":
                    self.active_run["state"] = "failed" if self.active_run.get("failed_steps") else "completed"
        except Exception as exc:
            with self.lock:
                if self.active_run:
                    self.active_run["state"] = "failed"
                    self.active_run["last_error"] = f"runner: {exc}"
        finally:
            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                pass
            try:
                if self.playwright:
                    self.playwright.stop()
            except Exception:
                pass
            self._terminate_browser_tree()
            with self.lock:
                if self.active_run:
                    self.active_run["ended_at"] = utc_now()
                    self.active_run["duration_ms"] = round((time.perf_counter() - started_perf) * 1000, 1)
                    self.active_run["current_step"] = None
                    self._persist()
                    final = json.loads(json.dumps(self.active_run))
                else:
                    final = None
                self.page = None; self.browser = None; self.playwright = None
            if final and self.flow_update_callback:
                try:
                    self.flow_update_callback(final)
                except Exception:
                    pass
