from __future__ import annotations
from datetime import datetime, timezone
import re


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class EngenieBridge:
    """Deterministic tool bridge used by ALM Engenie and by the local bridge tester.

    Natural-language understanding belongs to Engenie (or another approved agent).
    This bridge owns discovery, parameter contracts, confirmation gates, queue
    invocation, and compact status/result summaries.
    """
    DESTRUCTIVE_WORDS = {
        "submit", "approve", "approval", "sign", "delete", "remove", "post",
        "publish", "release", "create user", "provision", "commit", "save changes"
    }

    def __init__(self, flow_provider, recording_provider, design_provider, scheduler,
                 run_provider, variable_provider=None, tutorial_provider=None, policy_guard=None, execution_normalizer=None):
        self.flow_provider = flow_provider
        self.recording_provider = recording_provider
        self.design_provider = design_provider
        self.scheduler = scheduler
        self.run_provider = run_provider
        self.variable_provider = variable_provider
        self.tutorial_provider = tutorial_provider
        # Phase 14 policy hooks remain optional so the bridge can still be unit-tested
        # independently. In the application they enforce domain/risk/resource policy
        # before the shared scheduler receives an Engenie/AI-triggered job.
        self.policy_guard = policy_guard
        self.execution_normalizer = execution_normalizer

    def tools(self):
        return {
            "schema": "engenie-webflow-tools/1",
            "app_id": "alm-webflow-studio",
            "tools": [
                {"name":"webflow.list","method":"GET","path":"/api/engenie/flows","description":"Discover available WebFlow automations."},
                {"name":"webflow.describe","method":"GET","path":"/api/engenie/flows/{flow_id}","description":"Explain a flow, required parameters, safety classification, and tutorial availability."},
                {"name":"webflow.prepare","method":"POST","path":"/api/engenie/prepare","description":"Resolve a flow and determine missing parameters or confirmation requirements before execution."},
                {"name":"webflow.run","method":"POST","path":"/api/engenie/invoke","description":"Queue an approved WebFlow execution using the shared worker contract."},
                {"name":"webflow.status","method":"GET","path":"/api/engenie/jobs/{job_id}","description":"Return a compact job/run summary suitable for conversational explanation."},
                {"name":"webflow.tutorial","method":"GET","path":"/api/engenie/tutorial","description":"Return the guided WebFlow Studio tutorial."}
            ],
            "safety": {
                "confirmation_required_for_destructive": True,
                "restricted_context_fields": ["secret_values","passwords","session_tokens","raw_auth_headers"],
                "secret_values_persisted": False
            }
        }

    def _flows(self):
        try: return list(self.flow_provider() or [])
        except Exception: return []

    def _find_flow(self, flow_id=None, flow_name=None, query=None):
        flows=self._flows()
        if flow_id:
            return next((f for f in flows if str(f.get("id"))==str(flow_id)),None)
        needle=str(flow_name or query or "").strip().lower()
        if not needle: return None
        exact=next((f for f in flows if str(f.get("name","")).lower()==needle),None)
        if exact: return exact
        matches=[f for f in flows if needle in (str(f.get("name","")).lower()+" "+str(f.get("description","")).lower())]
        return matches[0] if len(matches)==1 else None

    def _recording(self, flow):
        rid=str((flow or {}).get("recording_id") or "")
        if not rid: return None
        try: return self.recording_provider(rid)
        except Exception: return None

    def _variables(self, rec):
        if not rec or not self.variable_provider: return []
        try:
            raw=self.variable_provider(rec) or []
            if isinstance(raw,dict): raw=raw.get("variables") or raw.get("items") or []
            out=[]
            for v in raw:
                if not isinstance(v,dict): continue
                name=str(v.get("key") or v.get("name") or v.get("variable") or "").strip()
                if not name: continue
                out.append({
                    "name":name,
                    "kind":v.get("kind") or v.get("classification") or "Input",
                    "required":bool(v.get("required",True)),
                    "secret":bool(v.get("secret") or str(v.get("classification","")).lower()=="secret"),
                    "step_id":v.get("step_id") or v.get("source_step_id")
                })
            return out
        except Exception:
            return []

    def _destructive(self, flow, rec):
        if bool((flow or {}).get("destructive")):
            return True,["flow metadata"]
        texts=[]
        for s in (rec or {}).get("steps",[]):
            t=s.get("target") or {}
            # Safety classification is action/target based. A read-only flow named
            # "Approval Validation" should not become destructive merely by title.
            texts += [str(s.get("action", "")),str(s.get("training_title", "")),str(t.get("label", "")),str(t.get("text", "")),str(t.get("aria_label", ""))]
        joined=" ".join(texts).lower()
        hits=sorted({w for w in self.DESTRUCTIVE_WORDS if w in joined})
        return bool(hits), hits

    def describe(self, flow):
        if not flow: return None
        rec=self._recording(flow)
        vars_=self._variables(rec)
        destructive,hits=self._destructive(flow,rec)
        design=None
        try: design=self.design_provider(str(flow.get("id")))
        except Exception: pass
        return {
            "schema":"engenie-webflow-description/1",
            "flow_id":flow.get("id"),"name":flow.get("name"),"description":flow.get("description"),
            "status":flow.get("status"),"owner":flow.get("owner"),"success_rate":flow.get("success_rate"),
            "recording_id":flow.get("recording_id"),"recorded_steps":len((rec or {}).get("steps",[])),
            "designer_steps":len((design or {}).get("steps",[])) if isinstance(design,dict) else 0,
            "parameters":vars_,"destructive":destructive,"destructive_signals":hits,
            "confirmation_required":destructive,
            "runnable":bool(rec and (rec.get("steps") or [])),
            "training_available":bool(rec),
            "training_path":f"/api/training/export/{rec.get('id')}" if rec else None,
            "safe_context":{"flow_id":flow.get("id"),"flow_name":flow.get("name"),"status":flow.get("status"),"success_rate":flow.get("success_rate")}
        }

    def list_flows(self, query=None, runnable_only=False):
        q=str(query or "").strip().lower()
        items=[]
        for f in self._flows():
            if q and q not in (str(f.get("name","")).lower()+" "+str(f.get("description","")).lower()): continue
            d=self.describe(f)
            if runnable_only and not d.get("runnable"): continue
            items.append({k:d.get(k) for k in ("flow_id","name","description","status","owner","success_rate","runnable","destructive","confirmation_required","recorded_steps")})
        return {"schema":"engenie-webflow-discovery/1","count":len(items),"flows":items}

    def prepare(self, payload):
        payload=payload or {}
        flow=self._find_flow(payload.get("flow_id"),payload.get("flow_name"),payload.get("query"))
        if not flow:
            return False,{"error":"flow_not_resolved","message":"I could not resolve that request to one WebFlow. Ask for available automations or provide a flow_id."}
        d=self.describe(flow)
        if not d.get("runnable"):
            return False,{"error":"flow_not_runnable","message":"This WebFlow does not yet have executable recorded steps.","flow":d}
        supplied=payload.get("variables") or {}
        missing=[]
        for v in d.get("parameters") or []:
            if v.get("required") and v.get("name") not in supplied:
                missing.append({"name":v.get("name"),"kind":v.get("kind"),"secret":v.get("secret",False)})
        return True,{"schema":"engenie-webflow-preparation/1","flow":d,"missing_parameters":missing,"ready_to_run":not missing,"confirmation_required":d.get("confirmation_required",False),"confirmation_text":f"Run '{d.get('name')}'? This flow may perform: {', '.join(d.get('destructive_signals') or ['a state-changing action'])}." if d.get("confirmation_required") else None}

    def invoke(self, payload):
        ok,prep=self.prepare(payload)
        if not ok: return False,prep,404 if prep.get("error")=="flow_not_resolved" else 409
        if prep.get("missing_parameters"):
            return False,{**prep,"error":"parameters_required","message":"Collect the required parameters before running this WebFlow."},409
        if prep.get("confirmation_required") and not bool((payload or {}).get("confirmed")):
            return False,{**prep,"error":"confirmation_required","message":prep.get("confirmation_text")},409
        d=prep["flow"]
        normalized=dict(payload or {})
        if self.execution_normalizer:
            normalized=self.execution_normalizer(normalized)
        if self.policy_guard:
            policy_ok,policy_issue=self.policy_guard(d, bool(normalized.get("confirmed",False)), "engenie")
            if not policy_ok:
                status=409 if (policy_issue or {}).get("error") in {"confirmation_required","destructive_performance_blocked"} else 403
                return False,policy_issue or {"error":"governance_blocked"},status
        variables=dict(normalized.get("variables") or {})
        ok,job=self.scheduler.enqueue(
            flow_id=d.get("flow_id"),recording_id=d.get("recording_id"),headless=bool(normalized.get("headless",True)),
            timeout_ms=normalized.get("timeout_ms",12000),retries=normalized.get("retries",1),
            failure_policy=str(normalized.get("failure_policy") or "stop"),variables=variables,
            use_designer=bool(normalized.get("use_designer",True)),source="engenie",governance_approved=bool(normalized.get("confirmed",False))
        )
        if not ok: return False,job,409
        # Never echo runtime variables back to the agent after submission.
        return True,{"schema":"engenie-webflow-invocation/1","accepted":True,"flow_id":d.get("flow_id"),"flow_name":d.get("name"),"job_id":job.get("id"),"state":job.get("state"),"status_path":f"/api/engenie/jobs/{job.get('id')}","submitted_at":utc_now()},201

    def job_status(self, job_id):
        job=self.scheduler.get_job(job_id)
        if not job: return None
        run=self.run_provider(job.get("run_id")) if job.get("run_id") else None
        result={
            "schema":"engenie-webflow-status/1","job_id":job.get("id"),"flow_id":job.get("flow_id"),
            "state":job.get("state"),"message":job.get("message"),"created_at":job.get("created_at"),
            "started_at":job.get("started_at"),"ended_at":job.get("ended_at"),"duration_ms":job.get("duration_ms"),
            "run_id":job.get("run_id"),"error":job.get("error")
        }
        if run:
            result["run"]={"state":run.get("state"),"duration_ms":run.get("duration_ms"),"counts":run.get("counts"),"outputs":run.get("outputs") or {},"last_error":run.get("last_error")}
        return result

    def message(self, text):
        """Small deterministic bridge tester; not an LLM replacement."""
        text=str(text or "").strip(); low=text.lower()
        if not text: return {"reply":"Ask me to list flows, explain a flow, run a flow, check a job, or show the tutorial.","action":"help"}
        if "tutorial" in low or "teach" in low or "how do i use" in low:
            return {"reply":"I can start the WebFlow Studio guided tutorial.","action":"tutorial","tutorial":self.tutorial_provider() if self.tutorial_provider else None}
        m=re.search(r"(?:job|status)\s+([0-9a-f-]{8,})",low)
        if m:
            st=self.job_status(m.group(1)); return {"reply":("That job is currently "+str(st.get("state"))+".") if st else "I could not find that job.","action":"status","status":st}
        if any(x in low for x in ("list", "available", "show flows", "automations")):
            data=self.list_flows(runnable_only=False); return {"reply":f"I found {data['count']} WebFlow automations.","action":"list","data":data}
        intent="run" if any(x in low for x in ("run ","execute ","start ")) else "explain"
        cleaned=re.sub(r"\b(run|execute|start|explain|describe|what does|flow|webflow|automation)\b"," ",low)
        cleaned=re.sub(r"\s+"," ",cleaned).strip(" ?.")
        flow=self._find_flow(query=cleaned)
        if not flow:
            # fuzzy token overlap fallback for tester only
            toks=set(cleaned.split()); ranked=[]
            for f in self._flows():
                hay=set((str(f.get("name","")+" "+f.get("description","")).lower()).split())
                ranked.append((len(toks & hay),f))
            ranked.sort(key=lambda x:x[0],reverse=True)
            flow=ranked[0][1] if ranked and ranked[0][0]>0 else None
        if not flow: return {"reply":"I could not resolve that to one WebFlow. Try asking me to list the available automations.","action":"clarify"}
        d=self.describe(flow)
        if intent=="explain": return {"reply":f"{d['name']}: {d.get('description') or 'No description.'}","action":"describe","flow":d}
        ok,prep=self.prepare({"flow_id":d["flow_id"],"variables":{}})
        return {"reply":("I need parameters or confirmation before I can run this flow." if (prep.get("missing_parameters") or prep.get("confirmation_required")) else "This flow is ready to run."),"action":"prepare","preparation":prep}
