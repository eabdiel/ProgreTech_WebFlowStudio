from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import ast
import base64
import hashlib
import io
import json
import re
import zipfile


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def slug(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-.")
    return value[:100] or "flow"


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class ScriptManager:
    """Phase 8 script export/import manager.

    The visual WebFlow document remains the source of truth while a flow is
    Visual Managed. Generated projects carry a manifest marker with the visual
    design hash. Re-uploaded code is syntax checked and classified as:
      - visual-managed: byte-for-byte generated source
      - code-managed-compatible: edited code still references current design hash
      - code-managed-drifted: edited code references an older visual design
      - code-managed-unlinked: valid Python without a WebFlow manifest marker
    Imported code is stored but never executed automatically by this manager.
    """

    MARKER = "# WEBFLOW-MANIFEST:"

    def __init__(self, root: Path, write_json, flow_provider, design_provider, recording_provider, object_provider):
        self.root = root
        self.write_json = write_json
        self.flow_provider = flow_provider
        self.design_provider = design_provider
        self.recording_provider = recording_provider
        self.object_provider = object_provider
        self.base = root / "data" / "scripts"
        self.base.mkdir(parents=True, exist_ok=True)

    def _dir(self, flow_id):
        p = self.base / slug(flow_id)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _state_path(self, flow_id):
        return self._dir(flow_id) / "state.json"

    def _source_path(self, flow_id):
        return self._dir(flow_id) / "automation.py"

    def _manifest_path(self, flow_id):
        return self._dir(flow_id) / "webflow_manifest.json"

    def _flow(self, flow_id):
        return next((f for f in (self.flow_provider() or []) if f.get("id") == flow_id), None)

    def _recording_for_flow(self, flow_id):
        flow = self._flow(flow_id) or {}
        rid = flow.get("recording_id")
        return self.recording_provider(rid) if rid else None

    def _design_payload(self, flow_id):
        design = self.design_provider(flow_id)
        recording = self._recording_for_flow(flow_id)
        if design and design.get("steps"):
            return design, recording
        # Keep export available before a user explicitly imports the recording
        # into Designer by synthesizing browser nodes from the latest recording.
        steps = []
        for st in (recording or {}).get("steps", []):
            if st.get("action") not in {"navigate", "click", "input", "select", "check", "key", "change"}:
                continue
            steps.append({
                "id": st.get("id"),
                "type": "browser",
                "name": str(st.get("action") or "Action").title(),
                "source_recording_id": (recording or {}).get("id"),
                "source_step_id": st.get("id"),
                "browser_step": st,
                "enabled": True,
            })
        return {
            "schema_version": "webflow-flow/1",
            "flow_id": flow_id,
            "name": (self._flow(flow_id) or {}).get("name") or "WebFlow Automation",
            "steps": steps,
            "subflows": {},
        }, recording

    def _objects_for_flow(self, flow_id):
        recording = self._recording_for_flow(flow_id) or {}
        rid = recording.get("id")
        objects = {}
        for obj in self.object_provider() or []:
            if rid and obj.get("source_recording_id") == rid:
                objects[str(obj.get("source_step_id"))] = obj
        return objects

    def _design_hash(self, design, objects):
        semantic = {
            "schema_version": design.get("schema_version"),
            "flow_id": design.get("flow_id"),
            "name": design.get("name"),
            "steps": design.get("steps") or [],
            "subflows": design.get("subflows") or {},
            "objects": objects,
        }
        return sha256_text(stable_json(semantic))

    def _manifest(self, flow_id, design, recording, objects):
        flow = self._flow(flow_id) or {}
        design_hash = self._design_hash(design, objects)
        return {
            "schema_version": "webflow-script-manifest/1",
            "flow_id": flow_id,
            "flow_name": flow.get("name") or design.get("name") or flow_id,
            "recording_id": (recording or {}).get("id"),
            "design_schema": design.get("schema_version") or "webflow-flow/1",
            "design_hash": design_hash,
            "generated_at": utc_now(),
            "generator": "ALM WebFlow Studio Phase 8",
            "runtime": "playwright-python",
        }

    def _header_marker(self, manifest):
        # Keep generated source deterministic: volatile timestamps belong in the
        # sidecar manifest, not in the source-link marker/hash.
        linked = {k: v for k, v in manifest.items() if k != "generated_at"}
        payload = base64.urlsafe_b64encode(stable_json(linked).encode("utf-8")).decode("ascii")
        return f"{self.MARKER} {payload}"

    def _generated_source(self, flow_id):
        design, recording = self._design_payload(flow_id)
        if not design.get("steps"):
            raise ValueError("This flow has no Designer steps or recorded browser steps to export.")
        objects = self._objects_for_flow(flow_id)
        manifest = self._manifest(flow_id, design, recording, objects)
        marker = self._header_marker(manifest)
        model_json = json.dumps({"steps": design.get("steps") or [], "subflows": design.get("subflows") or {}}, ensure_ascii=False, indent=2)
        objects_json = json.dumps(objects, ensure_ascii=False, indent=2)
        source = f'''{marker}
"""Generated by ALM WebFlow Studio.

This file is intentionally readable and editable. The manifest marker above
links this script to the visual WebFlow design that generated it.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

FLOW_MODEL = {model_json}
OBJECTS = {objects_json}


def apply_template(value, variables):
    if value is None or not isinstance(value, str):
        return value
    return re.sub(r"\\{{\\{{\\s*([A-Za-z0-9_]+)\\s*\\}}\\}}", lambda m: "" if variables.get(m.group(1)) is None else str(variables.get(m.group(1), m.group(0))), value)


def safe_eval(expression, variables, outputs):
    tree = ast.parse(str(expression or "None"), mode="eval")
    blocked = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Await, ast.Yield, ast.NamedExpr)
    for node in ast.walk(tree):
        if isinstance(node, blocked):
            raise ValueError("Expression contains a blocked construct")
        if isinstance(node, ast.Attribute) and str(node.attr).startswith("_"):
            raise ValueError("Private attributes are blocked")
    safe = {{"len":len,"str":str,"int":int,"float":float,"bool":bool,"min":min,"max":max,"sum":sum,"any":any,"all":all,"round":round,"range":range,"list":list,"dict":dict,"set":set,"sorted":sorted,"abs":abs}}
    env = dict(variables); env["variables"] = variables; env["outputs"] = outputs
    return eval(compile(tree, "<webflow-expression>", "eval"), {{"__builtins__": safe}}, env)


def governed_python(code, variables, outputs):
    tree = ast.parse(str(code or ""), mode="exec")
    blocked = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.Global, ast.Nonlocal, ast.Lambda)
    for node in ast.walk(tree):
        if isinstance(node, blocked):
            raise ValueError(f"Blocked Python construct: {{type(node).__name__}}")
        if isinstance(node, ast.Attribute) and str(node.attr).startswith("_"):
            raise ValueError("Private attributes are blocked")
        if isinstance(node, ast.Name) and str(node.id).startswith("__"):
            raise ValueError("Private names are blocked")
    safe = {{"len":len,"str":str,"int":int,"float":float,"bool":bool,"min":min,"max":max,"sum":sum,"any":any,"all":all,"round":round,"range":range,"enumerate":enumerate,"list":list,"dict":dict,"set":set,"sorted":sorted,"abs":abs}}
    env = dict(variables); env["variables"] = variables; env["outputs"] = outputs
    exec(compile(tree, "<webflow-python>", "exec"), {{"__builtins__": safe}}, env)
    for key, value in env.items():
        if key not in {{"variables", "outputs"}} and not key.startswith("_") and key not in safe:
            variables[key] = value
    return env.get("result")


def candidates(step):
    target = step.get("target") or {{}}
    selectors = list(target.get("selectors") or [])
    obj = OBJECTS.get(str(step.get("id"))) or {{}}
    primary = obj.get("primary_locator")
    if primary:
        match = next((s for s in obj.get("selectors", []) if s.get("value") == primary), None)
        if match:
            selectors = [match] + [s for s in selectors if s.get("value") != primary]
    if target.get("label"):
        selectors.append({{"kind":"label","value":target["label"]}})
    if target.get("aria_label"):
        selectors.append({{"kind":"aria-name","value":target["aria_label"]}})
    if target.get("text") and (target.get("tag") in {{"a","button"}} or target.get("role") in {{"link","button"}}):
        selectors.append({{"kind":"text","value":str(target["text"])[:120]}})
    seen=set(); out=[]
    for s in selectors:
        key=(s.get("kind"),s.get("value"))
        if s.get("value") and key not in seen:
            seen.add(key); out.append(s)
    return out


def make_locator(page, candidate, step):
    kind, value = candidate.get("kind"), candidate.get("value")
    target = step.get("target") or {{}}
    if kind == "label": return page.get_by_label(value, exact=True)
    if kind == "aria-name":
        role = target.get("role") or ("button" if target.get("tag") == "button" else "link" if target.get("tag") == "a" else None)
        return page.get_by_role(role, name=value, exact=True) if role else page.get_by_label(value, exact=True)
    if kind == "role":
        name = target.get("aria_label") or target.get("label") or target.get("text") or None
        return page.get_by_role(value, name=name, exact=False) if name else page.get_by_role(value)
    if kind == "text":
        role = target.get("role") or ("button" if target.get("tag") == "button" else "link" if target.get("tag") == "a" else None)
        return page.get_by_role(role, name=value, exact=False) if role else page.get_by_text(value, exact=False)
    return page.locator(value)


def resolve(page, step, timeout_ms):
    errors=[]
    for c in candidates(step):
        try:
            loc=make_locator(page,c,step).first
            loc.wait_for(state="attached", timeout=min(timeout_ms,3500))
            return loc,c
        except Exception as exc:
            errors.append(str(exc).splitlines()[0][:160])
    raise RuntimeError("Object could not be resolved: " + " | ".join(errors[-3:]))


def read_value(loc, step):
    tag=(step.get("target") or {{}}).get("tag")
    if tag in {{"input","textarea","select"}}:
        try: return loc.input_value(timeout=2500)
        except Exception: pass
    try: return (loc.inner_text(timeout=2500) or "").strip()
    except Exception:
        return loc.get_attribute("value", timeout=1500)


def variable_key(step, fallback="value"):
    target=step.get("target") or {{}}
    raw=step.get("variable_key") or step.get("training_title") or target.get("label") or target.get("aria_label") or target.get("name") or target.get("text") or fallback
    key=re.sub(r"[^A-Za-z0-9_]+","_",str(raw).strip()).strip("_").lower() or fallback
    return ("v_"+key) if key[0].isdigit() else key


def browser_step(page, step, variables, outputs, timeout_ms=12000):
    action=step.get("action")
    if action == "navigate":
        page.goto(apply_template(step.get("page_url") or "", variables), wait_until="domcontentloaded", timeout=max(timeout_ms,15000)); return
    loc,_=resolve(page,step,timeout_ms)
    cls=step.get("classification")
    key=variable_key(step)
    if action == "click": loc.click(timeout=timeout_ms)
    elif action == "input":
        value=variables.get(key) if cls in {{"Input","Secret"}} and key in variables else apply_template(step.get("value"),variables)
        if cls == "Secret" and key not in variables: raise RuntimeError(f"Required secret variable '{{key}}' was not supplied")
        loc.fill("" if value is None else str(value), timeout=timeout_ms)
    elif action == "select":
        value=variables.get(key) if cls in {{"Input","Secret"}} and key in variables else apply_template(step.get("value"),variables)
        if cls == "Secret" and key not in variables: raise RuntimeError(f"Required secret variable '{{key}}' was not supplied")
        try: loc.select_option(value="" if value is None else str(value), timeout=timeout_ms)
        except Exception: loc.select_option(label="" if value is None else str(value), timeout=timeout_ms)
    elif action == "check":
        loc.check(timeout=timeout_ms) if bool(step.get("checked")) else loc.uncheck(timeout=timeout_ms)
    elif action == "key": loc.press(str(step.get("key") or "Enter"), timeout=timeout_ms)
    elif action == "change": pass
    if cls == "Output":
        outputs[key]=read_value(loc,step); variables[key]=outputs[key]
    elif cls == "Assertion":
        actual=read_value(loc,step); expected=variables.get(key,apply_template(step.get("value"),variables))
        if expected not in (None,"") and str(expected).strip() not in str(actual or ""):
            raise AssertionError(f"Expected '{{expected}}', got '{{actual}}'")


def execute_nodes(page, nodes, subflows, variables, outputs, timeout_ms):
    for node in nodes or []:
        if not node.get("enabled",True): continue
        typ=node.get("type")
        if typ == "browser": browser_step(page,node.get("browser_step") or {{}},variables,outputs,timeout_ms)
        elif typ == "if": execute_nodes(page,node.get("then") if safe_eval(node.get("expression") or "False",variables,outputs) else node.get("else"),subflows,variables,outputs,timeout_ms)
        elif typ == "foreach":
            source=safe_eval(node.get("source") or "[]",variables,outputs)
            if isinstance(source,str): source=[x.strip() for x in source.split(",") if x.strip()]
            for i,item in enumerate(list(source or [])):
                variables[node.get("item_var") or "item"]=item; variables[node.get("index_var") or "index"]=i
                execute_nodes(page,node.get("body"),subflows,variables,outputs,timeout_ms)
        elif typ == "set": variables[str(node.get("variable") or "value")]=safe_eval(node.get("expression") or "None",variables,outputs)
        elif typ == "wait": time.sleep(max(0,min(60000,int(node.get("milliseconds") or 500)))/1000)
        elif typ == "python": governed_python(node.get("code") or "",variables,outputs)
        elif typ == "subflow":
            name=str(node.get("subflow") or "")
            if name not in subflows: raise ValueError(f"Subflow '{{name}}' does not exist")
            execute_nodes(page,subflows[name],subflows,variables,outputs,timeout_ms)


def run(context=None, *, headless=True, timeout_ms=12000):
    variables=dict(context or {{}}); outputs={{}}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=headless)
        try:
            ctx=browser.new_context(viewport={{"width":1360,"height":820}}); page=ctx.new_page()
            execute_nodes(page,FLOW_MODEL.get("steps") or [],FLOW_MODEL.get("subflows") or {{}},variables,outputs,timeout_ms)
        finally:
            browser.close()
    return {{"outputs":outputs,"variables":variables}}


def main():
    ap=argparse.ArgumentParser(description="Run exported ALM WebFlow Studio automation")
    ap.add_argument("--context", help="JSON file containing runtime variables")
    ap.add_argument("--headed", action="store_true", help="Show Chromium while running")
    ap.add_argument("--timeout-ms", type=int, default=12000)
    args=ap.parse_args()
    context={{}}
    if args.context: context=json.loads(Path(args.context).read_text(encoding="utf-8"))
    print(json.dumps(run(context,headless=not args.headed,timeout_ms=args.timeout_ms),indent=2,default=str))


if __name__ == "__main__": main()
'''
        return source, manifest, design

    def generated(self, flow_id):
        source, manifest, design = self._generated_source(flow_id)
        return {
            "flow_id": flow_id,
            "mode": "visual-managed",
            "source": source,
            "manifest": manifest,
            "design_hash": manifest["design_hash"],
            "generated_source_hash": sha256_text(source),
            "designer_steps": len(design.get("steps") or []),
        }

    def regenerate(self, flow_id):
        data = self.generated(flow_id)
        self._source_path(flow_id).write_text(data["source"], encoding="utf-8")
        self.write_json(self._manifest_path(flow_id), data["manifest"])
        state = {
            "schema_version": "webflow-script-state/1",
            "flow_id": flow_id,
            "mode": "visual-managed",
            "source_name": "automation.py",
            "design_hash": data["design_hash"],
            "generated_source_hash": data["generated_source_hash"],
            "source_hash": data["generated_source_hash"],
            "validation": {"ok": True, "message": "Generated from current visual flow."},
            "updated_at": utc_now(),
        }
        self.write_json(self._state_path(flow_id), state)
        return self.get(flow_id)

    def _decode_marker(self, source):
        for line in source.splitlines()[:8]:
            if line.startswith(self.MARKER):
                raw = line[len(self.MARKER):].strip()
                try:
                    return json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
                except Exception:
                    return None
        return None

    def _validate_python(self, source):
        try:
            ast.parse(source)
            return {"ok": True, "message": "Python syntax is valid."}
        except SyntaxError as exc:
            return {"ok": False, "message": f"SyntaxError line {exc.lineno}: {exc.msg}", "line": exc.lineno, "offset": exc.offset}

    def get(self, flow_id):
        generated = self.generated(flow_id)
        state_path = self._state_path(flow_id)
        source_path = self._source_path(flow_id)
        if not state_path.exists() or not source_path.exists():
            # Preview without silently persisting anything.
            return {
                **{k: generated[k] for k in ("flow_id","mode","source","manifest","design_hash","generated_source_hash","designer_steps")},
                "source_hash": generated["generated_source_hash"],
                "validation": {"ok": True, "message": "Preview generated from current visual flow."},
                "persisted": False,
                "drift": False,
            }
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        source = source_path.read_text(encoding="utf-8")
        current_design_hash = generated["design_hash"]
        state_design_hash = state.get("design_hash")
        drift = bool(state_design_hash and state_design_hash != current_design_hash)
        if state.get("mode") == "visual-managed" and sha256_text(source) != generated["generated_source_hash"]:
            state["mode"] = "code-managed-compatible" if state_design_hash == current_design_hash else "code-managed-drifted"
        return {
            **state,
            "flow_id": flow_id,
            "source": source,
            "manifest": self._decode_marker(source) or generated["manifest"],
            "current_design_hash": current_design_hash,
            "current_generated_source_hash": generated["generated_source_hash"],
            "drift": drift,
            "persisted": True,
            "designer_steps": generated["designer_steps"],
        }

    def import_upload(self, flow_id, name, content_base64):
        try:
            raw = base64.b64decode(content_base64)
        except Exception as exc:
            raise ValueError("Uploaded file could not be decoded.") from exc
        name = str(name or "automation.py")
        manifest_file = None
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                    py_names = [n for n in zf.namelist() if Path(n).name == "automation.py"] or [n for n in zf.namelist() if n.lower().endswith(".py")]
                    if not py_names:
                        raise ValueError("ZIP does not contain automation.py or another Python file.")
                    source = zf.read(py_names[0]).decode("utf-8")
                    mnames = [n for n in zf.namelist() if Path(n).name == "webflow_manifest.json"]
                    if mnames:
                        manifest_file = json.loads(zf.read(mnames[0]).decode("utf-8"))
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"ZIP could not be read: {exc}") from exc
        elif name.lower().endswith(".py"):
            try: source = raw.decode("utf-8")
            except UnicodeDecodeError as exc: raise ValueError("Python file must be UTF-8 text.") from exc
        else:
            raise ValueError("Upload a .py file or exported .zip project.")

        validation = self._validate_python(source)
        if not validation["ok"]:
            return {"accepted": False, "validation": validation, "mode": "invalid"}

        generated = self.generated(flow_id)
        marker = self._decode_marker(source) or manifest_file
        source_hash = sha256_text(source)
        if source_hash == generated["generated_source_hash"]:
            mode = "visual-managed"
        elif marker and marker.get("flow_id") == flow_id and marker.get("design_hash") == generated["design_hash"]:
            mode = "code-managed-compatible"
        elif marker and marker.get("flow_id") == flow_id:
            mode = "code-managed-drifted"
        else:
            mode = "code-managed-unlinked"

        self._source_path(flow_id).write_text(source, encoding="utf-8")
        if marker:
            self.write_json(self._manifest_path(flow_id), marker)
        state = {
            "schema_version": "webflow-script-state/1",
            "flow_id": flow_id,
            "mode": mode,
            "source_name": Path(name).name,
            "design_hash": (marker or {}).get("design_hash"),
            "generated_source_hash": generated["generated_source_hash"],
            "source_hash": source_hash,
            "validation": validation,
            "updated_at": utc_now(),
        }
        self.write_json(self._state_path(flow_id), state)
        return {"accepted": True, **self.get(flow_id)}

    def validate_current(self, flow_id):
        data = self.get(flow_id)
        validation = self._validate_python(data.get("source") or "")
        data["validation"] = validation
        if data.get("persisted"):
            state = json.loads(self._state_path(flow_id).read_text(encoding="utf-8"))
            state["validation"] = validation; state["updated_at"] = utc_now()
            self.write_json(self._state_path(flow_id), state)
        return data

    def project_zip(self, flow_id):
        data = self.get(flow_id)
        source = data.get("source") or self.generated(flow_id)["source"]
        manifest = self._decode_marker(source) or data.get("manifest") or self.generated(flow_id)["manifest"]
        design, _ = self._design_payload(flow_id)
        flow = self._flow(flow_id) or {}
        sample_context = {"example_input": "replace-me"}
        readme = f'''# {flow.get("name") or flow_id} — Exported WebFlow Project

Generated by ALM WebFlow Studio Phase 8.

## Run
```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python automation.py --headed --context sample_context.json
```

- `automation.py` is intentionally editable.
- `flow.json` is the visual WebFlow model used at export time.
- `webflow_manifest.json` links the code to the visual design hash.
- Re-upload `automation.py` or this ZIP into WebFlow Studio for validation.

Current management mode at export: **{data.get("mode") or "visual-managed"}**.
'''
        buff = io.BytesIO()
        with zipfile.ZipFile(buff, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("automation.py", source)
            zf.writestr("webflow_manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("flow.json", json.dumps(design, indent=2))
            zf.writestr("requirements.txt", "playwright>=1.55,<2\n")
            zf.writestr("sample_context.json", json.dumps(sample_context, indent=2))
            zf.writestr("README.md", readme)
        return buff.getvalue(), f"webflow-{slug(flow_id)}-project.zip"
