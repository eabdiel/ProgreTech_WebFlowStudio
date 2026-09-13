from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from queue import Queue, Empty
import json
import os
import re
import time
import uuid

try:
    import psutil
except Exception:
    psutil = None

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except Exception:  # dependency is reported cleanly through /api/recorder/status
    sync_playwright = None
    PlaywrightTimeoutError = Exception


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_file_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or ""))
    return value.strip("-.")[:80] or "item"


RECORDER_JS = r"""
(() => {
  if (window.__webflowRecorderInstalled) return;
  window.__webflowRecorderInstalled = true;
  const timers = new WeakMap();

  const clip = (value, n=180) => String(value == null ? '' : value).replace(/\s+/g,' ').trim().slice(0,n);
  const cssEscape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');

  function stableId(id){
    if(!id) return false;
    if(id.length > 80) return false;
    const digits=(id.match(/\d/g)||[]).length;
    return digits < Math.max(5, id.length * .45);
  }

  function labelFor(el){
    try{
      if(el.labels && el.labels.length) return clip(el.labels[0].innerText || el.labels[0].textContent);
      if(el.id){
        const label=document.querySelector(`label[for="${cssEscape(el.id)}"]`);
        if(label) return clip(label.innerText || label.textContent);
      }
      const parent=el.closest('label');
      if(parent) return clip(parent.innerText || parent.textContent);
    }catch(e){}
    return '';
  }

  function selectors(el){
    const out=[];
    const push=(kind,value,score)=>{ if(value && !out.some(x=>x.value===value)) out.push({kind,value,score}); };
    const testid=el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa');
    if(testid) push('test-id', `[data-testid="${testid}"]`, 100);
    const aria=el.getAttribute('aria-label');
    if(aria) push('aria-label', `[aria-label="${aria.replace(/"/g,'\\"')}"]`, 96);
    const lbl=labelFor(el);
    if(lbl) push('label', lbl, 95);
    const name=el.getAttribute('name');
    if(name) push('name', `[name="${name.replace(/"/g,'\\"')}"]`, 91);
    if(stableId(el.id)) push('id', `#${cssEscape(el.id)}`, 90);
    const role=el.getAttribute('role');
    if(role) push('role', role, 86);
    const type=el.getAttribute('type');
    if(type) push('css', `${tag}[type="${type}"]`, 65);
    if(el.classList && el.classList.length){
      const classes=[...el.classList].filter(x=>x.length<40).slice(0,3);
      if(classes.length) push('css', `${tag}.${classes.map(cssEscape).join('.')}`, 55);
    }
    if(!out.length) push('css', tag, 30);
    return out.slice(0,6);
  }

  function payload(action, el, extra={}){
    if(!el || !el.tagName) return;
    const tag=el.tagName.toLowerCase();
    const type=(el.getAttribute('type')||'').toLowerCase();
    const isSecret=type==='password';
    let value='';
    if('value' in el) value=isSecret ? '••••••••' : clip(el.value, 500);
    const rect=el.getBoundingClientRect();
    const p={
      action,
      page_url: location.href,
      page_title: document.title,
      target:{
        tag,
        type,
        id: el.id || '',
        name: el.getAttribute('name') || '',
        role: el.getAttribute('role') || '',
        aria_label: el.getAttribute('aria-label') || '',
        href: el.getAttribute('href') || '',
        title: el.getAttribute('title') || '',
        label: labelFor(el),
        text: clip(el.innerText || el.textContent || el.getAttribute('value') || ''),
        classes: clip(el.className || '', 200),
        selectors: selectors(el),
        bounds:{x:Math.round(rect.x),y:Math.round(rect.y),width:Math.round(rect.width),height:Math.round(rect.height)}
      },
      value,
      secret:isSecret,
      ...extra
    };
    try{ window.__webflow_record_event(p); }catch(e){}
  }

  document.addEventListener('click', e => {
    const el=e.target && e.target.closest ? e.target.closest('a,button,input,select,textarea,[role="button"],[role="link"],[contenteditable="true"]') || e.target : e.target;
    payload('click',el,{button:e.button});
  }, true);

  document.addEventListener('input', e => {
    const el=e.target;
    if(!el || !('value' in el)) return;
    clearTimeout(timers.get(el));
    timers.set(el,setTimeout(()=>payload('input',el),420));
  }, true);

  document.addEventListener('change', e => {
    const el=e.target;
    const tag=el && el.tagName ? el.tagName.toLowerCase() : '';
    const type=(el && el.getAttribute ? el.getAttribute('type') : '') || '';
    let action='change';
    if(tag==='select') action='select';
    if(type==='checkbox' || type==='radio') action='check';
    payload(action,el,{checked: !!el.checked});
  }, true);

  document.addEventListener('keydown', e => {
    if(['Enter','Tab','Escape'].includes(e.key)) payload('key', e.target, {key:e.key});
  }, true);
})();
"""


@dataclass
class RecorderResult:
    ok: bool
    payload: dict


class RecorderManager:
    """Owns one local headed Playwright session for the Phase 2 MVP."""

    def __init__(self, root: Path, write_json_callback, flow_update_callback=None):
        self.root = root
        self.data_dir = root / "data" / "recordings"
        self.shot_dir = self.data_dir / "screenshots"
        self.write_json = write_json_callback
        self.flow_update_callback = flow_update_callback
        self.lock = Lock()
        self.thread: Thread | None = None
        self.stop_event = Event()
        self.ready_event = Event()
        self.paused = False
        self.status_data = {
            "state": "idle",
            "dependency_ready": sync_playwright is not None,
            "recording_id": None,
            "flow_id": None,
            "url": None,
            "steps": 0,
            "error": None,
            "latest_step": None,
            "latest_screenshot": None,
        }
        self.recording: dict | None = None
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        # PIDs are captured only for Chromium descendants created by this
        # recorder session.  They are used as a last-resort cleanup path if a
        # browser/driver shutdown hangs after the user closes Chromium.
        self.browser_pids: set[int] = set()
        # Browser binding callbacks must return quickly.  Playwright can deadlock
        # if a binding callback performs another synchronous browser command
        # (for example page.screenshot()).  Events are therefore queued here
        # and drained by the recorder worker thread.
        self.event_queue: Queue = Queue()

    def dependency_status(self):
        return sync_playwright is not None

    def status(self):
        with self.lock:
            return dict(self.status_data)

    def _recording_path(self):
        if not self.recording:
            return None
        return self.data_dir / f"{safe_file_part(self.recording['id'])}.json"

    def _persist(self):
        path = self._recording_path()
        if path and self.recording is not None:
            self.write_json(path, self.recording)

    def start(self, flow_id: str | None, url: str, recording_name: str | None = None):
        if sync_playwright is None:
            return RecorderResult(False, {
                "error": "playwright_not_installed",
                "message": "Install Phase 2 dependencies with: pip install -r requirements.txt && python -m playwright install chromium"
            })
        url = (url or "").strip()
        if not re.match(r"^https?://", url, re.I):
            return RecorderResult(False, {"error":"invalid_url","message":"Starting URL must begin with http:// or https://"})
        with self.lock:
            if self.thread and self.thread.is_alive():
                return RecorderResult(False, {"error":"recorder_busy","message":"Stop the active recording first."})
            rid = str(uuid.uuid4())
            self.recording = {
                "schema_version": "webflow-recording/1",
                "id": rid,
                "flow_id": flow_id or None,
                "recording_name": (recording_name or "New Recording").strip()[:160],
                "starting_url": url,
                "status": "starting",
                "started_at": utc_now(),
                "ended_at": None,
                # Hub evidence is intentionally short-lived. A recording starts
                # with 24-hour retention; the user may explicitly extend it to
                # at most 30 days when saving from Studio.
                "retention_days": 1,
                "retention_until": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                "retention_policy": "hub-temporary",
                "steps": [],
            }
            self.status_data.update({
                "state":"starting","dependency_ready":True,"recording_id":rid,"flow_id":flow_id,
                "url":url,"steps":0,"error":None,"latest_step":None,"latest_screenshot":None
            })
            self.stop_event.clear(); self.ready_event.clear(); self.paused=False
            while True:
                try: self.event_queue.get_nowait()
                except Empty: break
            self._persist()
            self.thread = Thread(target=self._run, args=(url,), daemon=True, name="WebFlowRecorder")
            self.thread.start()
        self.ready_event.wait(timeout=25)
        st=self.status()
        return RecorderResult(st.get("state") in {"recording","paused"}, st)

    def pause(self):
        with self.lock:
            if self.status_data["state"] != "recording":
                return RecorderResult(False,{"error":"not_recording"})
            self.paused=True
            self.status_data["state"]="paused"
            if self.recording: self.recording["status"]="paused"
            self._persist()
            return RecorderResult(True,self.status())

    def resume(self):
        with self.lock:
            if self.status_data["state"] != "paused":
                return RecorderResult(False,{"error":"not_paused"})
            self.paused=False
            self.status_data["state"]="recording"
            if self.recording: self.recording["status"]="recording"
            self._persist()
            return RecorderResult(True,self.status())

    def stop(self):
        """Stop recording and make browser cleanup deterministic.

        The UI must never remain in ``stopping`` forever.  We first request a
        normal Playwright shutdown.  If the recorder thread does not finish in
        a few seconds, only the Chromium descendants captured for *this*
        recording session are terminated.  This is deliberately scoped so the
        recorder cannot kill unrelated user browsers.
        """
        with self.lock:
            alive = bool(self.thread and self.thread.is_alive())
            active = self.status_data.get("state") in {"starting","recording","paused","stopping"}
            if not alive and self.status_data.get("state") == "stopped":
                # Idempotent Stop: closing the Chromium window already stops and
                # finalizes the recording, so pressing Stop afterwards should
                # simply confirm success rather than surface an error.
                return RecorderResult(True,dict(self.status_data))
            if not alive and not active:
                return RecorderResult(False,{"error":"not_recording"})
            self.status_data["state"]="stopping"
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=4)
        forced = bool(self.thread and self.thread.is_alive())
        if forced:
            self._terminate_browser_tree()
            self.thread.join(timeout=4)
        promote=None
        with self.lock:
            # Even if the Playwright driver is unhealthy, return control to the
            # user.  A lingering daemon thread cannot keep the app process alive.
            if self.status_data.get("state") == "stopping":
                self._finalize_recording_locked()
                self.status_data["state"] = "stopped"
                promote=json.loads(json.dumps(self.recording)) if self.recording else None
            self.status_data["forced_cleanup"] = forced
        if promote and self.flow_update_callback:
            try: self.flow_update_callback(promote)
            except Exception: pass
        return RecorderResult(True,self.status())

    def _finalize_recording_locked(self):
        """Idempotently finalize the recording. Caller must hold ``self.lock``."""
        if not self.recording:
            return
        if not self.recording.get("ended_at"):
            self.recording["ended_at"] = utc_now()
        if self.recording.get("status") != "error":
            self.recording["status"] = "stopped"
        self._persist()

    def _capture_browser_processes(self, before: set[int]):
        if psutil is None:
            return
        try:
            parent = psutil.Process(os.getpid())
            rows = parent.children(recursive=True)
            self.browser_pids = {
                proc.pid for proc in rows
                if proc.pid not in before and any(k in ' '.join(proc.cmdline()).lower() for k in ('chromium','chrome','playwright'))
            }
        except Exception:
            self.browser_pids = set()

    def _terminate_browser_tree(self):
        """Best-effort cleanup of Chromium processes owned by this recorder only."""
        if psutil is None:
            return
        procs=[]
        for pid in list(self.browser_pids):
            try:
                procs.append(psutil.Process(pid))
            except Exception:
                pass
        # Children first avoids leaving renderer/helper processes behind.
        for proc in sorted(procs,key=lambda x:x.pid,reverse=True):
            try: proc.terminate()
            except Exception: pass
        try:
            _,alive=psutil.wait_procs(procs,timeout=1.5)
        except Exception:
            alive=[]
        for proc in alive:
            try: proc.kill()
            except Exception: pass

    def recording_data(self, recording_id=None):
        with self.lock:
            if self.recording and (not recording_id or self.recording.get("id") == recording_id):
                return json.loads(json.dumps(self.recording))
        if recording_id:
            path=self.data_dir/f"{safe_file_part(recording_id)}.json"
            if path.exists():
                try: return json.loads(path.read_text(encoding="utf-8"))
                except Exception: pass
        return None

    def _add_navigation(self, frame):
        # Playwright event callbacks follow the same rule as browser bindings:
        # do not issue nested synchronous browser commands here. Queue the
        # navigation and let the recorder worker persist it after the callback.
        try:
            if frame != self.page.main_frame: return
            url=frame.url
            if not url or url.startswith("about:"): return
            self.event_queue.put_nowait({"action":"navigate","page_url":url,"page_title":"","target":{"tag":"document","selectors":[]}})
        except Exception:
            pass

    def _binding(self, source, payload):
        """Receive a browser event without issuing nested Playwright commands.

        The old implementation captured screenshots directly inside this binding
        callback.  Because the page is waiting for the Python binding to return,
        calling another synchronous Playwright operation from the callback can
        stall the connection after the first captured click.  Queueing the event
        fixes that class of recorder freeze and also gives us a clean seam for the
        future local-client transport.
        """
        if self.paused or self.stop_event.is_set():
            return
        try:
            self.event_queue.put_nowait(payload if isinstance(payload,dict) else {"action":"event"})
        except Exception as exc:
            with self.lock:
                self.status_data["error"] = f"event_queue: {exc}"

    def _drain_events(self):
        while True:
            try:
                payload = self.event_queue.get_nowait()
            except Empty:
                return
            try:
                self._capture_event(payload, page=self.page, screenshot=payload.get("action") in {"navigate","click","select","check"})
            except Exception as exc:
                with self.lock:
                    self.status_data["error"] = f"event_capture: {exc}"

    def _capture_event(self, payload: dict, page=None, screenshot=False):
        with self.lock:
            if not self.recording or self.paused: return
            seq=len(self.recording["steps"])+1
            step={
                "id":str(uuid.uuid4()),
                "sequence":seq,
                "timestamp":utc_now(),
                "action":str(payload.get("action") or "event"),
                "page_url":str(payload.get("page_url") or self.status_data.get("url") or ""),
                "page_title":str(payload.get("page_title") or ""),
                "target":payload.get("target") or {},
                "value":payload.get("value"),
                "secret":bool(payload.get("secret",False)),
                "key":payload.get("key"),
                "checked":payload.get("checked"),
                "classification":None,
                "screenshot":None,
            }
            self.recording["steps"].append(step)
            self.status_data["steps"]=seq
            self.status_data["url"]=step["page_url"]
            self.status_data["latest_step"]=step
        if screenshot and page:
            try:
                self.shot_dir.mkdir(parents=True,exist_ok=True)
                rel=Path("data")/"recordings"/"screenshots"/f"{safe_file_part(self.recording['id'])}-{seq:04d}.png"
                page.screenshot(path=str(self.root/rel), full_page=False)
                with self.lock:
                    step["screenshot"]="/"+rel.as_posix()
                    self.status_data["latest_screenshot"]=step["screenshot"]
            except Exception:
                pass
        with self.lock:
            self._persist()

    def _run(self, url):
        try:
            self.data_dir.mkdir(parents=True,exist_ok=True)
            self.shot_dir.mkdir(parents=True,exist_ok=True)
            before=set()
            if psutil is not None:
                try: before={p.pid for p in psutil.Process(os.getpid()).children(recursive=True)}
                except Exception: before=set()
            self.playwright=sync_playwright().start()
            self.browser=self.playwright.chromium.launch(headless=False)
            self._capture_browser_processes(before)
            self.browser.on("disconnected", lambda: self.stop_event.set())
            self.context=self.browser.new_context(viewport={"width":1360,"height":820})
            self.context.expose_binding("__webflow_record_event", self._binding)
            self.context.add_init_script(RECORDER_JS)
            self.page=self.context.new_page()
            self.page.on("framenavigated", self._add_navigation)
            self.page.on("close", lambda: self.stop_event.set())
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except PlaywrightTimeoutError:
                pass
            with self.lock:
                self.status_data["state"]="recording"
                if self.recording: self.recording["status"]="recording"
                self._persist()
            self.ready_event.set()

            # IMPORTANT: sync Playwright dispatches browser events while its
            # message loop is being pumped.  A plain ``Event.wait()`` leaves the
            # connection idle and exposed bindings can appear to record only the
            # first event. ``page.wait_for_timeout`` is intentionally used here
            # as a tiny Playwright call so click/input/navigation callbacks keep
            # flowing while the user interacts with Chromium.
            while not self.stop_event.is_set():
                try:
                    self.page.wait_for_timeout(100)
                except Exception as exc:
                    text=str(exc).lower()
                    if self.stop_event.is_set() or 'closed' in text or 'disconnected' in text:
                        self.stop_event.set(); break
                    raise
                self._drain_events()
            self._drain_events()
        except Exception as exc:
            # Closing the user-visible browser is a normal way to finish a
            # recording, not an application error.
            text=str(exc).lower()
            normal_close=self.stop_event.is_set() or 'closed' in text or 'disconnected' in text
            if not normal_close:
                with self.lock:
                    self.status_data["state"]="error"
                    self.status_data["error"]=str(exc)
                    if self.recording:
                        self.recording["status"]="error"; self.recording["error"]=str(exc); self.recording["ended_at"]=utc_now(); self._persist()
            self.ready_event.set()
        finally:
            # If the user already closed Chromium, avoid making another browser
            # command against a disconnected transport.
            try:
                if self.context and self.browser and self.browser.is_connected(): self.context.close()
            except Exception: pass
            try:
                if self.browser and self.browser.is_connected(): self.browser.close()
            except Exception: pass
            try:
                if self.playwright: self.playwright.stop()
            except Exception: pass
            with self.lock:
                if self.recording and self.status_data.get("state") != "error":
                    self._finalize_recording_locked()
                    if self.flow_update_callback:
                        try: self.flow_update_callback(self.recording)
                        except Exception: pass
                    self.status_data["state"]="stopped"
                self.page=None; self.context=None; self.browser=None; self.playwright=None; self.paused=False
                self.browser_pids=set()
