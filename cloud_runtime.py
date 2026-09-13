from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
import base64, json, os, threading
try:
    from google.cloud import storage as gcs_storage
except Exception:
    gcs_storage=None

@dataclass
class RuntimeProfile:
    environment:str; cloud_foundry:bool; application_name:str; application_id:str|None; application_uris:list[str]; public_url:str|None; host:str; port:int; auth_adapter:str; hub_mode:bool

class CloudRuntime:
    def __init__(self,root:Path):
        self.root=Path(root); self.cloud_run=bool(os.getenv("K_SERVICE") or os.getenv("K_REVISION")); self.environment=os.getenv("WEBFLOW_ENV","cloud-run" if self.cloud_run else "local"); self.hub_mode=False; self.auth_adapter=os.getenv("WEBFLOW_AUTH_ADAPTER","cloud-run-iap" if self.cloud_run else "local-development")
    def profile(self):
        public=os.getenv("WEBFLOW_PUBLIC_URL"); return RuntimeProfile(self.environment,self.cloud_run,os.getenv("K_SERVICE") or "ALM WebFlow Studio",os.getenv("K_REVISION"),[public] if public else [],public,os.getenv("WEBFLOW_HOST","0.0.0.0" if self.cloud_run else "127.0.0.1"),int(os.getenv("PORT") or os.getenv("WEBFLOW_PORT") or "5678"),self.auth_adapter,False)
    def service_summary(self):
        b=os.getenv("WEBFLOW_GCS_BUCKET"); return [{"label":"google-cloud-storage","name":b,"plan":"bucket","tags":["gcs"]}] if b else []
    def _payload(self,authorization):
        if not authorization or not authorization.lower().startswith("bearer "): return {}
        try:
            p=authorization.split(None,1)[1].split('.')[1]; p+='='*(-len(p)%4); return json.loads(base64.urlsafe_b64decode(p).decode())
        except Exception:return {}
    def session(self,headers):
        if not self.cloud_run:
            n=os.getenv("WEBFLOW_LOCAL_USER","Local Developer"); e=os.getenv("WEBFLOW_LOCAL_EMAIL",""); return {"authenticated":True,"source":"local-development","display_name":n,"email":e,"user_name":e or n,"roles":["WebFlowAdmin","WebFlowRunner"],"trust_boundary":"standalone"}
        p=self._payload(headers.get("Authorization")); e=headers.get("X-Goog-Authenticated-User-Email") or p.get("email") or ""; e=e.split(':',1)[-1] if e.startswith('accounts.google.com:') else e; admins={x.strip().lower() for x in os.getenv("WEBFLOW_ADMIN_EMAILS","").split(',') if x.strip()}; roles=["WebFlowViewer","WebFlowRunner"]
        if e.lower() in admins or os.getenv("WEBFLOW_CLOUD_RUN_ADMIN_ALL","0")=="1": roles.insert(0,"WebFlowAdmin")
        return {"authenticated":bool(e or headers.get("Authorization")),"source":self.auth_adapter,"display_name":p.get("name") or e or "Authenticated User","email":e,"user_name":e or p.get("name") or "Authenticated User","roles":roles,"trust_boundary":"cloud-run"}
    def manifest(self,filename,fallback=None):
        try:return json.loads((self.root/'config'/filename).read_text())
        except Exception:return fallback or {}
    def readiness(self,storage_status,playwright_ready):
        p=self.profile(); checks=[{"id":"runtime","label":"Google Cloud Run runtime","status":"ready" if self.cloud_run else "local","detail":p.environment},{"id":"route","label":"Public application route","status":"ready" if p.public_url else ("local" if not self.cloud_run else "warning"),"detail":p.public_url or "Set WEBFLOW_PUBLIC_URL after deployment"},{"id":"storage","label":"Persistent storage","status":"ready" if storage_status.get("persistent") else ("local" if not self.cloud_run else "warning"),"detail":storage_status.get("adapter") or "local-json"},{"id":"playwright","label":"Playwright runtime","status":"ready" if playwright_ready else "warning","detail":"Supplied Dockerfile uses the Playwright image with Chromium"},{"id":"auth","label":"Authentication adapter","status":"ready","detail":p.auth_adapter}]; warnings=[c["label"]+": "+c["detail"] for c in checks if c["status"]=="warning"]; return {"ready":not warnings,"environment":p.environment,"cloud_foundry":self.cloud_run,"cloud_provider":"google-cloud-run","public_url":p.public_url,"checks":checks,"warnings":warnings,"time":datetime.now(timezone.utc).isoformat()}

class CloudStorageMirror:
    def __init__(self,root:Path,interval_seconds=20):
        self.root=Path(root); self.data_dir=self.root/'data'; self.interval_seconds=max(10,int(os.getenv("WEBFLOW_STORAGE_SYNC_SECONDS",interval_seconds))); self.prefix=os.getenv("WEBFLOW_OBJECT_PREFIX","webflow-studio/data/").strip('/')+'/'; self.bucket_name=os.getenv("WEBFLOW_GCS_BUCKET","").strip(); self.client=None; self.bucket=None; self.adapter="local-json"; self.last_sync=None; self.last_error=None; self.stop_event=threading.Event(); self.thread=None; self._fingerprints={}
        if self.bucket_name and gcs_storage:
            try:self.client=gcs_storage.Client(); self.bucket=self.client.bucket(self.bucket_name); self.adapter="google-cloud-storage-mirror"
            except Exception as exc:self.last_error=str(exc)
        elif self.bucket_name:self.last_error="google-cloud-storage is not installed"
    @property
    def enabled(self):return self.bucket is not None
    def status(self):return {"adapter":self.adapter,"persistent":self.enabled,"bucket_bound":bool(self.bucket_name),"prefix":self.prefix if self.enabled else None,"sync_interval_seconds":self.interval_seconds if self.enabled else None,"last_sync":self.last_sync,"last_error":self.last_error,"instance_model":"single-active-instance" if self.enabled else "local-filesystem"}
    def hydrate(self):
        if not self.enabled:return
        self.data_dir.mkdir(parents=True,exist_ok=True)
        try:
            for blob in self.client.list_blobs(self.bucket_name,prefix=self.prefix):
                if blob.name.endswith('/'):continue
                rel=blob.name[len(self.prefix):]
                if not rel or '..' in Path(rel).parts:continue
                t=self.data_dir/rel; t.parent.mkdir(parents=True,exist_ok=True); blob.download_to_filename(str(t))
            self.last_sync=datetime.now(timezone.utc).isoformat(); self.last_error=None
        except Exception as exc:self.last_error="hydrate: "+str(exc)
    def _files(self):return [p for p in self.data_dir.rglob('*') if p.is_file() and not p.name.endswith('.tmp')] if self.data_dir.exists() else []
    def sync_once(self):
        if not self.enabled:return
        try:
            for p in self._files():
                st=p.stat(); rel=p.relative_to(self.data_dir).as_posix(); fp=(int(st.st_mtime_ns),int(st.st_size))
                if self._fingerprints.get(rel)==fp:continue
                self.bucket.blob(self.prefix+rel).upload_from_filename(str(p)); self._fingerprints[rel]=fp
            self.last_sync=datetime.now(timezone.utc).isoformat(); self.last_error=None
        except Exception as exc:self.last_error="sync: "+str(exc)
    def delete_relative(self,rel):
        if not self.enabled:return False
        rel=str(rel).lstrip('/').replace('\\','/');
        if '..' in Path(rel).parts:return False
        try:self.bucket.blob(self.prefix+rel).delete(); self._fingerprints.pop(rel,None); return True
        except Exception:return False
    def delete_prefix_relative(self,rel_prefix):
        if not self.enabled:return 0
        rel_prefix=str(rel_prefix).lstrip('/').replace('\\','/').rstrip('/')+'/'; count=0
        if '..' in Path(rel_prefix).parts:return 0
        try:
            for blob in self.client.list_blobs(self.bucket_name,prefix=self.prefix+rel_prefix):blob.delete(); count+=1
        except Exception:pass
        return count
    def start(self):
        if not self.enabled or self.thread:return
        def loop():
            while not self.stop_event.wait(self.interval_seconds):self.sync_once()
        self.thread=threading.Thread(target=loop,name="webflow-gcs-mirror",daemon=True); self.thread.start()
    def stop(self):
        if not self.enabled:return
        self.stop_event.set(); self.sync_once()
        if self.thread and self.thread.is_alive():self.thread.join(timeout=3)

BTPRuntime=CloudRuntime
BTPStorageMirror=CloudStorageMirror
