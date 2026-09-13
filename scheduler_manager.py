from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone, timedelta
from threading import Thread, RLock, Event
from concurrent.futures import ThreadPoolExecutor
import json, time, uuid

from execution_engine import ExecutionManager


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    if not value:
        return None
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class SchedulerManager:
    """Local-first scheduler + queue + worker pool.

    Phase 10 intentionally exposes a provider-neutral contract: jobs are queued as
    JSON documents and dispatched through a worker adapter. The current adapter is
    a local thread pool. Phase 11 can replace the adapter/storage without changing
    the queue/schedule API or execution payload contract.
    """
    def __init__(self, root: Path, write_json, recording_provider, design_provider, object_provider=None, finalize_run=None, job_guard=None, external_dispatcher=None, external_status_provider=None, local_execution_enabled=True):
        self.root=root
        self.write_json=write_json
        self.recording_provider=recording_provider
        self.design_provider=design_provider
        self.object_provider=object_provider
        self.finalize_run=finalize_run
        # Optional Phase 14 policy callback. Keeping it injected avoids coupling the
        # queue implementation to domain/risk policy and lets a future BTP worker
        # adapter reuse the same queue schema.
        self.job_guard=job_guard
        # Optional hybrid adapter. When supplied, jobs with a client_id are
        # dispatched as signed Local Client tasks instead of launching Chromium
        # in this server process. This keeps the queue/schedule contract stable.
        self.external_dispatcher=external_dispatcher
        self.external_status_provider=external_status_provider
        self.local_execution_enabled=bool(local_execution_enabled)
        self.data_dir=root/'data'
        self.queue_file=self.data_dir/'execution_queue.json'
        self.schedules_file=self.data_dir/'schedules.json'
        self.settings_file=self.data_dir/'worker_settings.json'
        self.lock=RLock()
        self.stop_event=Event()
        self.active_runners={}
        self.queue=self._read(self.queue_file,[])
        self.schedules=self._read(self.schedules_file,[])
        self.settings=self._read(self.settings_file,{})
        self._normalize_settings()
        self._recover_state()
        self.thread=Thread(target=self._loop,daemon=True,name='WebFlowScheduler')
        self.thread.start()

    def _read(self,path,fallback):
        try: return json.loads(path.read_text(encoding='utf-8'))
        except Exception: return fallback

    def _normalize_settings(self):
        self.settings={
            'schema_version':'webflow-worker-settings/1',
            'adapter':'local-thread-pool',
            'max_workers':max(1,min(4,int(self.settings.get('max_workers',2) or 2))),
            'queue_limit':max(10,min(500,int(self.settings.get('queue_limit',100) or 100))),
            'retention_days':max(1,min(90,int(self.settings.get('retention_days',14) or 14))),
            'poll_seconds':max(2,min(60,int(self.settings.get('poll_seconds',5) or 5))),
            'updated_at':utc_now(),
        }
        self.write_json(self.settings_file,self.settings)

    def _recover_state(self):
        changed=False
        for j in self.queue:
            if j.get('state') in {'assigned','running','cancelling'}:
                j['state']='queued'; j['worker_id']=None; j['message']='Recovered after runtime restart'; changed=True
        if changed: self._persist_queue()

    def _persist_queue(self): self.write_json(self.queue_file,self.queue)
    def _persist_schedules(self): self.write_json(self.schedules_file,self.schedules)

    def status(self):
        with self.lock:
            counts={s:sum(1 for j in self.queue if j.get('state')==s) for s in ('queued','running','completed','failed','cancelled')}
            return {'schema_version':'webflow-worker-status/1','adapter':('hybrid-local-client' if self.external_dispatcher else self.settings['adapter']),'max_workers':self.settings['max_workers'],'active_workers':len(self.active_runners),'counts':counts,'queue_depth':counts['queued'],'schedules_enabled':sum(1 for s in self.schedules if s.get('enabled')),'settings':dict(self.settings)}

    def update_settings(self,patch):
        with self.lock:
            for k in ('max_workers','queue_limit','retention_days','poll_seconds'):
                if k in patch: self.settings[k]=patch[k]
            self._normalize_settings()
            return dict(self.settings)

    def list_jobs(self,limit=100):
        with self.lock:
            return list(reversed(self.queue[-max(1,min(500,int(limit or 100))):]))

    def get_job(self,jid):
        with self.lock:
            j=next((x for x in self.queue if x.get('id')==jid),None)
            return json.loads(json.dumps(j)) if j else None

    def enqueue(self,*,flow_id,recording_id=None,headless=True,timeout_ms=12000,retries=1,failure_policy='stop',variables=None,use_designer=True,source='manual',schedule_id=None,not_before=None,governance_approved=False,client_id=None,requested_by=None):
        with self.lock:
            live=sum(1 for j in self.queue if j.get('state') in {'queued','assigned','running','cancelling'})
            if live>=self.settings['queue_limit']:
                return False,{'error':'queue_full','message':'Execution queue reached its configured limit.'}
            if not recording_id:
                return False,{'error':'recording_required','message':'A saved recording is required.'}
            if self.job_guard:
                policy_ok,policy_issue=self.job_guard(flow_id,recording_id,bool(governance_approved),source)
                if not policy_ok:
                    return False,policy_issue or {'error':'governance_blocked'}
            job={'schema_version':'webflow-queue-job/1','id':str(uuid.uuid4()),'flow_id':flow_id,'recording_id':recording_id,'state':'queued','source':source,'schedule_id':schedule_id,'created_at':utc_now(),'not_before':not_before,'started_at':None,'ended_at':None,'worker_id':None,'run_id':None,'duration_ms':None,'message':'Waiting for worker','error':None,'headless':bool(headless),'timeout_ms':max(1000,int(timeout_ms or 12000)),'retries':max(0,min(3,int(retries or 0))),'failure_policy':failure_policy if failure_policy in {'stop','continue'} else 'stop','variables':variables or {},'use_designer':bool(use_designer),'governance_approved':bool(governance_approved),'client_id':client_id or None,'requested_by':requested_by or None,'external_task_id':None}
            self.queue.append(job); self._persist_queue(); return True,json.loads(json.dumps(job))

    def cancel_job(self,jid):
        with self.lock:
            job=next((j for j in self.queue if j.get('id')==jid),None)
            if not job: return False,{'error':'job_not_found'}
            if job.get('state') in {'completed','failed','cancelled'}: return False,{'error':'job_finished','message':'This queue job is already finished.'}
            runner=self.active_runners.get(jid)
            if job.get('external_task_id') and self.external_dispatcher:
                job['state']='cancelling';job['message']='Cancellation requested for Local Client';self._persist_queue()
                try:self.external_dispatcher({'cancel_target_task_id':job.get('external_task_id'),'client_id':job.get('client_id'),'requested_by':job.get('requested_by')},None,None)
                except Exception:pass
            elif runner:
                job['state']='cancelling'; job['message']='Cancellation requested'; self._persist_queue()
                try: runner.cancel()
                except Exception: pass
            else:
                job['state']='cancelled'; job['ended_at']=utc_now(); job['message']='Cancelled before worker assignment'; self._persist_queue()
            return True,json.loads(json.dumps(job))

    def retry_job(self,jid):
        old=self.get_job(jid)
        if not old: return False,{'error':'job_not_found'}
        return self.enqueue(flow_id=old.get('flow_id'),recording_id=old.get('recording_id'),headless=old.get('headless',True),timeout_ms=old.get('timeout_ms',12000),retries=old.get('retries',1),failure_policy=old.get('failure_policy','stop'),variables=old.get('variables') or {},use_designer=old.get('use_designer',True),source='retry',governance_approved=bool(old.get('governance_approved',False)),client_id=old.get('client_id'),requested_by=old.get('requested_by'))

    def list_schedules(self):
        with self.lock: return json.loads(json.dumps(self.schedules))

    def save_schedule(self,payload):
        with self.lock:
            sid=str(payload.get('id') or uuid.uuid4())
            existing=next((s for s in self.schedules if s.get('id')==sid),None)
            kind=str(payload.get('kind') or 'interval')
            if kind not in {'interval','daily','once'}: kind='interval'
            schedule={
                'schema_version':'webflow-schedule/1','id':sid,'name':str(payload.get('name') or 'Scheduled WebFlow').strip(),
                'flow_id':str(payload.get('flow_id') or ''),'recording_id':str(payload.get('recording_id') or ''),
                'enabled':bool(payload.get('enabled',True)),'kind':kind,
                'interval_minutes':max(1,min(10080,int(payload.get('interval_minutes',60) or 60))),
                'daily_time':str(payload.get('daily_time') or '08:00')[:5],
                'run_at':payload.get('run_at'),'headless':bool(payload.get('headless',True)),
                'timeout_ms':max(1000,int(payload.get('timeout_ms',12000) or 12000)),'retries':max(0,min(3,int(payload.get('retries',1) or 0))),
                'failure_policy':str(payload.get('failure_policy') or 'stop'),'use_designer':bool(payload.get('use_designer',True)),'governance_approved':bool(payload.get('confirmed') or payload.get('governance_approved',False)),'client_id':payload.get('client_id') or None,'requested_by':payload.get('requested_by') or (existing or {}).get('requested_by'),
                'created_at':(existing or {}).get('created_at') or utc_now(),'updated_at':utc_now(),
                'last_enqueued_at':(existing or {}).get('last_enqueued_at'),'next_run_at':None,
            }
            schedule['next_run_at']=self._next_due(schedule,datetime.now(timezone.utc),initial=True)
            if existing: self.schedules[self.schedules.index(existing)]=schedule
            else: self.schedules.append(schedule)
            self._persist_schedules(); return json.loads(json.dumps(schedule))

    def delete_schedule(self,sid):
        with self.lock:
            before=len(self.schedules); self.schedules=[s for s in self.schedules if s.get('id')!=sid]
            if len(self.schedules)==before: return False
            self._persist_schedules(); return True

    def toggle_schedule(self,sid,enabled):
        with self.lock:
            s=next((x for x in self.schedules if x.get('id')==sid),None)
            if not s: return None
            s['enabled']=bool(enabled); s['updated_at']=utc_now(); s['next_run_at']=self._next_due(s,datetime.now(timezone.utc),initial=True) if s['enabled'] else None
            self._persist_schedules(); return json.loads(json.dumps(s))

    def _next_due(self,s,now,initial=False):
        if not s.get('enabled'): return None
        kind=s.get('kind')
        if kind=='once':
            dt=parse_iso(s.get('run_at'))
            return dt.astimezone(timezone.utc).isoformat() if dt and dt>now else (None if not initial else s.get('run_at'))
        if kind=='daily':
            try: hh,mm=[int(x) for x in str(s.get('daily_time','08:00')).split(':')[:2]]
            except Exception: hh,mm=8,0
            dt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
            if dt<=now: dt+=timedelta(days=1)
            return dt.isoformat()
        minutes=max(1,int(s.get('interval_minutes',60) or 60))
        base=parse_iso(s.get('last_enqueued_at')) or now
        return (base+timedelta(minutes=minutes)).isoformat()

    def _schedule_tick(self):
        now=datetime.now(timezone.utc); changed=False
        for s in list(self.schedules):
            if not s.get('enabled'): continue
            due=parse_iso(s.get('next_run_at'))
            if not due:
                s['next_run_at']=self._next_due(s,now,initial=True); changed=True; continue
            if due<=now:
                ok,result=self.enqueue(flow_id=s.get('flow_id'),recording_id=s.get('recording_id'),headless=s.get('headless',True),timeout_ms=s.get('timeout_ms',12000),retries=s.get('retries',1),failure_policy=s.get('failure_policy','stop'),variables={},use_designer=s.get('use_designer',True),source='schedule',schedule_id=s.get('id'),governance_approved=bool(s.get('governance_approved',False)),client_id=s.get('client_id'),requested_by=s.get('requested_by'))
                if ok:
                    s['last_enqueued_at']=utc_now(); s['last_error']=None
                    if s.get('kind')=='once': s['enabled']=False; s['next_run_at']=None
                    else: s['next_run_at']=self._next_due(s,now)
                    s['updated_at']=utc_now(); changed=True
                else:
                    # A schedule created before Phase 14 may not carry an approval
                    # marker. Disable it on a policy failure instead of retrying the
                    # same blocked request every scheduler poll.
                    s['enabled']=False; s['next_run_at']=None; s['last_error']=(result or {}).get('message') or (result or {}).get('error') or 'Schedule blocked by policy'; s['updated_at']=utc_now(); changed=True
        if changed: self._persist_schedules()

    def _eligible_job(self):
        now=datetime.now(timezone.utc)
        for j in self.queue:
            if j.get('state')!='queued': continue
            nb=parse_iso(j.get('not_before'))
            if nb and nb>now: continue
            return j
        return None

    def _dispatch_tick(self):
        while True:
            with self.lock:
                if len(self.active_runners)>=self.settings['max_workers']: return
                job=self._eligible_job()
                if not job: return
                jid=job['id']
                if not job.get('client_id') and not self.local_execution_enabled:
                    job['state']='failed';job['ended_at']=utc_now();job['error']='local_client_required';job['message']='Browser execution is disabled in the hosted runtime; select a Local Client';self._persist_queue();continue
                if self.external_dispatcher and job.get('client_id'):
                    job['state']='assigned'; job['worker_id']='local-client:'+str(job.get('client_id'))[:8]; job['message']='Dispatching signed task to Local Client'; self._persist_queue()
                    external=True
                else:
                    job['state']='assigned'; job['worker_id']=f"local-{len(self.active_runners)+1}"; job['message']='Assigned to local worker'; self._persist_queue(); external=False
            if external:
                self._dispatch_external(jid)
            else:
                Thread(target=self._run_job,args=(jid,),daemon=True,name=f'WebFlowWorker-{jid[:6]}').start()
            time.sleep(.05)

    def _dispatch_external(self,jid):
        with self.lock:
            job=next((j for j in self.queue if j.get('id')==jid),None)
            if not job: return
        rec=self.recording_provider(job.get('recording_id'))
        design=self.design_provider(job.get('flow_id')) if job.get('use_designer') else None
        if not rec:
            with self.lock:
                job['state']='failed';job['ended_at']=utc_now();job['error']='recording_not_found';job['message']='Recording unavailable';self._persist_queue()
            return
        try:
            ok,payload=self.external_dispatcher(job,rec,design)
        except Exception as exc:
            ok,payload=False,{'error':f'{type(exc).__name__}: {exc}'}
        with self.lock:
            if ok:
                job['state']='running';job['started_at']=utc_now();job['external_task_id']=payload.get('id') or payload.get('task_id');job['message']='Signed task claimed by Local Client queue'
            else:
                job['state']='failed';job['ended_at']=utc_now();job['error']=payload.get('message') or payload.get('error') or 'external_dispatch_failed';job['message']=job['error']
            self._persist_queue()

    def _sync_external_jobs(self):
        if not self.external_status_provider:return
        changed=False
        with self.lock:
            jobs=[j for j in self.queue if j.get('external_task_id') and j.get('state') in {'running','cancelling'}]
        for job in jobs:
            try: task=self.external_status_provider(job.get('external_task_id'))
            except Exception: task=None
            if not task: continue
            result=task.get('result') or {}; run=result.get('run') if isinstance(result,dict) else None
            state=task.get('state')
            with self.lock:
                if run:
                    job['run_id']=run.get('id');job['duration_ms']=run.get('duration_ms');job['error']=run.get('last_error')
                if state in {'completed','failed'}:
                    run_state=(run or {}).get('state')
                    job['state']='completed' if run_state=='completed' else ('cancelled' if run_state=='cancelled' else 'failed')
                    job['ended_at']=utc_now();job['message']='Local Client execution completed' if job['state']=='completed' else (job.get('error') or 'Local Client execution '+job['state'])
                    changed=True
        if changed:
            with self.lock:self._persist_queue()

    def _run_job(self,jid):
        with self.lock:
            job=next((j for j in self.queue if j.get('id')==jid),None)
            if not job or job.get('state')=='cancelled': return
            job['state']='running'; job['started_at']=utc_now(); job['message']='Launching browser worker'; self._persist_queue()
        rec=self.recording_provider(job.get('recording_id'))
        if not rec:
            with self.lock: job['state']='failed'; job['ended_at']=utc_now(); job['error']='recording_not_found'; job['message']='Recording unavailable'; self._persist_queue()
            return
        design=self.design_provider(job.get('flow_id')) if job.get('use_designer') else None
        runner=ExecutionManager(self.root,self.write_json,self.finalize_run,object_provider=self.object_provider)
        with self.lock: self.active_runners[jid]=runner
        try:
            ok,payload=runner.start(rec,flow_id=job.get('flow_id'),headless=job.get('headless',True),timeout_ms=job.get('timeout_ms',12000),retries=job.get('retries',1),failure_policy=job.get('failure_policy','stop'),variables=job.get('variables') or {},logic_steps=(design or {}).get('steps') if design else None,subflows=(design or {}).get('subflows') if design else None,batch_context={'queue_job_id':jid,'schedule_id':job.get('schedule_id'),'source':job.get('source')})
            if not ok: raise RuntimeError(payload.get('message') or payload.get('error') or 'worker_start_failed')
            while True:
                st=runner.status()
                with self.lock:
                    job['run_id']=st.get('id'); job['message']=st.get('current_message') or f"{st.get('completed_steps',0)} / {st.get('total_steps',0)} steps"; self._persist_queue()
                if st.get('state') not in {'starting','running','cancelling'}: break
                time.sleep(.25)
            run=runner.status()
            with self.lock:
                job['run_id']=run.get('id'); job['duration_ms']=run.get('duration_ms'); job['ended_at']=utc_now(); job['error']=run.get('last_error')
                job['state']='completed' if run.get('state')=='completed' else ('cancelled' if run.get('state')=='cancelled' else 'failed')
                job['message']='Execution completed' if job['state']=='completed' else (job['error'] or f"Execution {job['state']}")
                self._persist_queue()
        except Exception as exc:
            with self.lock:
                job['state']='cancelled' if job.get('state')=='cancelling' else 'failed'; job['ended_at']=utc_now(); job['error']=f'{type(exc).__name__}: {exc}'; job['message']=job['error']; self._persist_queue()
        finally:
            with self.lock: self.active_runners.pop(jid,None)

    def cleanup(self):
        cutoff=datetime.now(timezone.utc)-timedelta(days=self.settings['retention_days'])
        with self.lock:
            before=len(self.queue)
            self.queue=[j for j in self.queue if j.get('state') in {'queued','assigned','running','cancelling'} or not parse_iso(j.get('ended_at')) or parse_iso(j.get('ended_at'))>=cutoff]
            removed=before-len(self.queue)
            if removed: self._persist_queue()
            return {'removed_jobs':removed,'retention_days':self.settings['retention_days']}

    def _loop(self):
        last_cleanup=0
        while not self.stop_event.is_set():
            try:
                with self.lock: self._schedule_tick(); self._dispatch_tick()
                if time.time()-last_cleanup>3600:
                    self.cleanup(); last_cleanup=time.time()
            except Exception as exc:
                print('[WebFlow] scheduler loop:',exc)
            self.stop_event.wait(self.settings.get('poll_seconds',5))

    def shutdown(self):
        self.stop_event.set()
        with self.lock: runners=list(self.active_runners.values())
        for r in runners:
            try: r.cancel()
            except Exception: pass
