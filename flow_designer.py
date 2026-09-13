from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import json, re, uuid


def utc_now(): return datetime.now(timezone.utc).isoformat()

def slug(v):
    v=re.sub(r'[^A-Za-z0-9_.-]+','-',str(v or '')).strip('-')
    return v[:100] or 'flow'

class FlowDesignerManager:
    """Phase 7 normalized visual-flow persistence.

    Designer documents are deliberately independent from recorder JSON and
    generated Python. Browser nodes retain a copy of their source recorded step,
    while logic nodes use a small structured control-flow model.
    """
    def __init__(self, root: Path, write_json, recording_provider):
        self.root=root; self.write_json=write_json; self.recording_provider=recording_provider
        self.dir=root/'data'/'designs'; self.dir.mkdir(parents=True,exist_ok=True)

    def path(self, flow_id): return self.dir/f'{slug(flow_id)}.json'
    def get(self, flow_id):
        p=self.path(flow_id)
        if not p.exists(): return None
        try: return json.loads(p.read_text(encoding='utf-8'))
        except Exception: return None

    def list(self):
        out=[]
        for p in sorted(self.dir.glob('*.json'),key=lambda x:x.stat().st_mtime,reverse=True):
            try:
                d=json.loads(p.read_text(encoding='utf-8'))
                out.append({k:d.get(k) for k in ('flow_id','name','schema_version','updated_at') }|{'nodes':len(d.get('steps') or [])})
            except Exception: pass
        return out

    def from_recording(self, flow_id, recording_id=None):
        rec=self.recording_provider(recording_id) if recording_id else None
        if not rec:
            # provider may expose current only by id; caller resolves flow recording when needed
            return self.empty(flow_id)
        steps=[]
        for s in rec.get('steps',[]):
            if s.get('action') not in {'navigate','click','input','select','check','key','change'}: continue
            steps.append({
                'id':str(uuid.uuid4()),'type':'browser','name':self._step_name(s),
                'source_recording_id':rec.get('id'),'source_step_id':s.get('id'),'browser_step':s,
                'enabled':True
            })
        return self.save(flow_id,{'flow_id':flow_id,'name':rec.get('recording_name') or 'Recorded Flow','steps':steps,'subflows':{}})

    def empty(self, flow_id):
        return {'schema_version':'webflow-flow/1','flow_id':flow_id,'name':'Untitled Flow','steps':[],'subflows':{},'created_at':utc_now(),'updated_at':utc_now()}

    def save(self, flow_id, payload):
        current=self.get(flow_id) or self.empty(flow_id)
        doc={**current,**{k:v for k,v in (payload or {}).items() if k in {'name','steps','subflows'}},'schema_version':'webflow-flow/1','flow_id':flow_id,'updated_at':utc_now()}
        if not doc.get('created_at'): doc['created_at']=utc_now()
        doc['steps']=self._normalize_nodes(doc.get('steps') or [])
        doc['subflows']={str(k):self._normalize_nodes(v or []) for k,v in (doc.get('subflows') or {}).items()}
        self.write_json(self.path(flow_id),doc); return doc

    def _normalize_nodes(self,nodes):
        out=[]
        allowed={'browser','if','foreach','set','python','wait','subflow'}
        for raw in nodes:
            if not isinstance(raw,dict): continue
            n=dict(raw); n['id']=str(n.get('id') or uuid.uuid4()); n['type']=n.get('type') if n.get('type') in allowed else 'set'; n['enabled']=bool(n.get('enabled',True))
            if n['type']=='if':
                n['then']=self._normalize_nodes(n.get('then') or []); n['else']=self._normalize_nodes(n.get('else') or [])
            if n['type']=='foreach': n['body']=self._normalize_nodes(n.get('body') or [])
            out.append(n)
        return out

    @staticmethod
    def _step_name(s):
        t=s.get('target') or {}
        obj=t.get('label') or t.get('aria_label') or t.get('text') or t.get('name') or t.get('tag') or s.get('page_title') or 'Browser step'
        return f"{str(s.get('action') or 'Action').title()} · {str(obj)[:70]}"
