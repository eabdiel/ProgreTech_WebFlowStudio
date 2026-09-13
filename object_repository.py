from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import json, re, uuid


def utc_now(): return datetime.now(timezone.utc).isoformat()

def _norm(v): return re.sub(r"\s+", " ", str(v or "")).strip()

def _friendly(step):
    t=step.get("target") or {}
    base=_norm(t.get("label") or t.get("aria_label") or t.get("text") or t.get("name") or t.get("id") or t.get("tag") or "Page object")
    base=base[:70] or "Page object"
    tag=(t.get("tag") or "").lower(); typ=(t.get("type") or "").lower(); action=(step.get("action") or "").lower()
    if tag in {"input","textarea"} and "input" not in base.lower(): base += " Input"
    elif tag=="button" and "button" not in base.lower(): base += " Button"
    elif tag=="select" and "select" not in base.lower(): base += " Select"
    elif action=="navigate": base = step.get("page_title") or "Page"
    return base

def _kind(step):
    t=step.get("target") or {}; tag=(t.get("tag") or "").lower(); typ=(t.get("type") or "").lower(); role=(t.get("role") or "").lower()
    if typ in {"checkbox","radio"}: return typ.title()
    if tag in {"input","textarea"}: return "Textbox"
    if tag=="button" or role=="button": return "Button"
    if tag=="a" or role=="link": return "Link"
    if tag=="select": return "Select"
    if tag=="document": return "Page"
    return (role or tag or "Element").title()

def _score(selectors):
    vals=[int(x.get("score") or 0) for x in selectors or [] if isinstance(x,dict)]
    if not vals: return 0
    # Primary stability is strongest candidate, slightly boosted by fallback depth.
    return min(100, max(vals) + min(4, max(0,len(vals)-1)))

def _fingerprint(step):
    t=step.get("target") or {}; sels=t.get("selectors") or []
    if sels: return f"{step.get('page_url','').split('#')[0]}|{sels[0].get('kind')}|{sels[0].get('value')}"
    return f"{step.get('page_url','').split('#')[0]}|{t.get('tag')}|{t.get('label')}|{t.get('text')}"

class ObjectRepository:
    def __init__(self, root:Path, write_json):
        self.root=root; self.write_json=write_json
        self.path=root/"data"/"objects.json"
        self.recordings=root/"data"/"recordings"
        if not self.path.exists(): self.write_json(self.path,[])

    def all(self):
        try: return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception: return []

    def save_all(self, objects): self.write_json(self.path,objects)

    def rebuild(self):
        existing={x.get("fingerprint"):x for x in self.all() if x.get("fingerprint")}
        found={}
        counts={}
        if self.recordings.exists():
            for p in self.recordings.glob("*.json"):
                if p.name=="objects.json": continue
                try: rec=json.loads(p.read_text(encoding="utf-8"))
                except Exception: continue
                for step in rec.get("steps",[]):
                    t=step.get("target") or {}; sels=t.get("selectors") or []
                    if not sels or t.get("tag")=="document": continue
                    fp=_fingerprint(step); counts[fp]=counts.get(fp,0)+1; prev=found.get(fp) or existing.get(fp)
                    obj={
                        "id": (prev or {}).get("id") or str(uuid.uuid4()),
                        "fingerprint":fp,
                        "name":(prev or {}).get("name") or _friendly(step),
                        "type":_kind(step),
                        "page_url":step.get("page_url") or "",
                        "page_title":step.get("page_title") or "",
                        "accessible_name":t.get("label") or t.get("aria_label") or "",
                        "selectors":sels,
                        "primary_locator":sels[0].get("value") if sels else "",
                        "stability":_score(sels),
                        "capture_count":counts[fp],
                        "last_seen":step.get("timestamp") or utc_now(),
                        "source_recording_id":rec.get("id"),
                        "source_step_id":step.get("id"),
                        "source_screenshot":step.get("screenshot"),
                        "bounds":t.get("bounds") or {},
                        "classification":step.get("classification"),
                        "notes":(prev or {}).get("notes") or "",
                        "group_label":(prev or {}).get("group_label") or "",
                    }
                    # Preserve user-chosen selector order where possible if it still exists.
                    if prev and prev.get("primary_locator"):
                        values=[s.get("value") for s in sels]
                        if prev["primary_locator"] in values: obj["primary_locator"]=prev["primary_locator"]
                    found[fp]=obj
        objects=sorted(found.values(), key=lambda x:(-int(x.get("stability") or 0), x.get("name") or ""))
        self.save_all(objects); return objects

    def get(self, oid): return next((x for x in self.all() if x.get("id")==oid),None)

    def update(self, oid, patch):
        objs=self.all(); allowed={"name","primary_locator","notes","classification","group_label"}
        for i,o in enumerate(objs):
            if o.get("id")==oid:
                for k,v in patch.items():
                    if k in allowed: o[k]=v
                o["updated_at"]=utc_now(); objs[i]=o; self.save_all(objs); return o
        return None

    def diagnostics(self, oid):
        o=self.get(oid)
        if not o: return None
        sels=o.get("selectors") or []
        ranked=sorted(sels,key=lambda x:int(x.get("score") or 0),reverse=True)
        warnings=[]
        if not ranked: warnings.append("No locator candidates were captured.")
        elif int(ranked[0].get("score") or 0)<70: warnings.append("Primary locator is relatively brittle; capture a stable label, test-id, name, or ID if possible.")
        if ranked and ranked[0].get("kind")=="css" and int(ranked[0].get("score") or 0)<70: warnings.append("CSS-only object: page layout/class changes may break this locator.")
        return {"object_id":oid,"stability":_score(ranked),"ranked_selectors":ranked,"warnings":warnings,"recommended": ranked[0] if ranked else None}
