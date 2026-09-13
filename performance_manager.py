from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
import json
import math
import statistics
import time
import uuid

from execution_engine import ExecutionManager


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def percentile(values, pct):
    vals=sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals)==1:
        return round(vals[0],1)
    rank=(len(vals)-1)*(pct/100.0)
    lo=math.floor(rank); hi=math.ceil(rank)
    if lo==hi:
        return round(vals[lo],1)
    value=vals[lo]+(vals[hi]-vals[lo])*(rank-lo)
    return round(value,1)


class PerformanceManager:
    """Phase 9 synthetic transaction orchestrator.

    Reuses the production ExecutionManager so performance runs exercise the same
    browser actions, selector fallbacks, assertions, Designer logic and screenshot
    pipeline as ordinary runs. Warm-ups are persisted but excluded from statistics.
    """
    def __init__(self, root: Path, write_json, object_provider=None):
        self.root=root
        self.write_json=write_json
        self.object_provider=object_provider
        self.tests_dir=root/'data'/'performance_tests'
        self.lock=Lock()
        self.cancel_event=Event()
        self.thread: Thread|None=None
        self.active_test: dict|None=None
        self.active_runners=[]

    def _path(self,test_id):
        return self.tests_dir/f'{test_id}.json'

    def _persist(self,test=None):
        test=test or self.active_test
        if test:
            self.write_json(self._path(test['id']),test)

    def status(self):
        with self.lock:
            return json.loads(json.dumps(self.active_test)) if self.active_test else {'state':'idle'}

    def get(self,test_id):
        with self.lock:
            if self.active_test and self.active_test.get('id')==test_id:
                return json.loads(json.dumps(self.active_test))
        path=self._path(test_id)
        if not path.exists(): return None
        try: return json.loads(path.read_text(encoding='utf-8'))
        except Exception: return None

    def list_tests(self,limit=30):
        if not self.tests_dir.exists(): return []
        paths=sorted(self.tests_dir.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        out=[]
        for p in paths[:limit]:
            try:
                t=json.loads(p.read_text(encoding='utf-8'))
                out.append({k:t.get(k) for k in ('id','flow_id','recording_id','state','started_at','ended_at','runs','warmups','concurrency','headless','completed_samples','failed_samples','metrics')})
            except Exception: pass
        return out

    def start(self,recording,*,flow_id=None,logic_steps=None,subflows=None,runs=10,warmups=1,concurrency=1,headless=True,timeout_ms=12000,retries=0):
        if not recording or not recording.get('steps'):
            return False,{'error':'recording_required','message':'Select a flow with a saved recording.'}
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False,{'error':'performance_busy','message':'A performance test is already running.'}
            runs=max(1,min(100,int(runs or 10)))
            warmups=max(0,min(10,int(warmups or 0)))
            concurrency=max(1,min(4,int(concurrency or 1)))
            test_id=str(uuid.uuid4())
            self.active_test={
                'schema_version':'webflow-performance-test/1','id':test_id,
                'flow_id':flow_id or recording.get('flow_id'),'recording_id':recording.get('id'),
                'state':'starting','started_at':utc_now(),'ended_at':None,
                'runs':runs,'warmups':warmups,'concurrency':concurrency,'headless':bool(headless),
                'timeout_ms':max(1000,int(timeout_ms or 12000)),'retries':max(0,min(3,int(retries or 0))),
                'total_executions':runs+warmups,'completed_executions':0,'completed_samples':0,'failed_samples':0,
                'current_message':'Preparing synthetic transaction…','samples':[],'warmup_samples':[],
                'metrics':{},'step_metrics':[],'console_errors':[],'network_errors':[],
            }
            self.cancel_event.clear(); self.active_runners=[]; self._persist()
            self.thread=Thread(target=self._run,args=(recording,logic_steps,subflows or {}),daemon=True,name='WebFlowPerformance')
            self.thread.start()
            return True,json.loads(json.dumps(self.active_test))

    def cancel(self):
        with self.lock:
            if not (self.thread and self.thread.is_alive()): return False,{'error':'no_active_test'}
            self.active_test['state']='cancelling'; self.active_test['current_message']='Cancelling active synthetic runs…'; self._persist()
            runners=list(self.active_runners)
        self.cancel_event.set()
        for runner in runners:
            try: runner.cancel()
            except Exception: pass
        return True,self.status()

    def _one(self,recording,logic_steps,subflows,sample_no,warmup=False):
        if self.cancel_event.is_set(): return {'cancelled':True,'sample_no':sample_no,'warmup':warmup}
        runner=ExecutionManager(self.root,self.write_json,None,object_provider=self.object_provider)
        with self.lock: self.active_runners.append(runner)
        try:
            ok,payload=runner.start(recording,flow_id=self.active_test.get('flow_id'),headless=self.active_test.get('headless',True),timeout_ms=self.active_test.get('timeout_ms',12000),retries=self.active_test.get('retries',0),failure_policy='stop',variables={},batch_context={'performance_test_id':self.active_test['id'],'sample_no':sample_no,'warmup':warmup},logic_steps=logic_steps,subflows=subflows)
            if not ok:
                return {'sample_no':sample_no,'warmup':warmup,'state':'failed','duration_ms':None,'error':payload.get('message') or payload.get('error'),'run_id':None,'steps':[],'console_errors':[],'network_errors':[]}
            while True:
                if self.cancel_event.is_set(): runner.cancel()
                st=runner.status()
                if st.get('state') not in {'starting','running','cancelling'}: break
                time.sleep(.15)
            run=runner.status()
            return {
                'sample_no':sample_no,'warmup':warmup,'state':run.get('state'),'duration_ms':run.get('duration_ms'),
                'run_id':run.get('id'),'error':run.get('last_error'),'steps':run.get('steps') or [],
                'console_errors':run.get('console_errors') or [],'network_errors':run.get('network_errors') or [],
                'started_at':run.get('started_at'),'ended_at':run.get('ended_at')
            }
        finally:
            with self.lock:
                if runner in self.active_runners: self.active_runners.remove(runner)

    def _update_metrics(self):
        samples=self.active_test.get('samples') or []
        durations=[s.get('duration_ms') for s in samples if s.get('duration_ms') is not None]
        completed=[s for s in samples if s.get('state')=='completed']
        failed=[s for s in samples if s.get('state') not in {'completed'}]
        self.active_test['completed_samples']=len(samples)
        self.active_test['failed_samples']=len(failed)
        self.active_test['metrics']={
            'count':len(samples),'successes':len(completed),'failures':len(failed),
            'success_rate':round((len(completed)/len(samples))*100,1) if samples else None,
            'average_ms':round(statistics.mean(durations),1) if durations else None,
            'median_ms':round(statistics.median(durations),1) if durations else None,
            'p95_ms':percentile(durations,95),'p99_ms':percentile(durations,99),
            'min_ms':round(min(durations),1) if durations else None,'max_ms':round(max(durations),1) if durations else None,
        }
        buckets={}
        for sample in samples:
            for step in sample.get('steps') or []:
                key=f"{step.get('sequence')}|{step.get('action')}|{step.get('label')}"
                b=buckets.setdefault(key,{'sequence':step.get('sequence'),'action':step.get('action'),'label':step.get('label'),'durations':[],'failures':0,'count':0})
                b['count']+=1
                if step.get('duration_ms') is not None: b['durations'].append(step.get('duration_ms'))
                if step.get('status')=='failed': b['failures']+=1
        metrics=[]
        for b in sorted(buckets.values(),key=lambda x:(x.get('sequence') or 9999,str(x.get('label')))):
            vals=b.pop('durations')
            b.update({'average_ms':round(statistics.mean(vals),1) if vals else None,'median_ms':round(statistics.median(vals),1) if vals else None,'p95_ms':percentile(vals,95),'min_ms':round(min(vals),1) if vals else None,'max_ms':round(max(vals),1) if vals else None})
            metrics.append(b)
        self.active_test['step_metrics']=metrics
        self.active_test['console_errors']=[{'sample_no':s.get('sample_no'),**e} for s in samples for e in (s.get('console_errors') or [])][-200:]
        self.active_test['network_errors']=[{'sample_no':s.get('sample_no'),**e} for s in samples for e in (s.get('network_errors') or [])][-200:]

    def _record_sample(self,result,warmup=False):
        with self.lock:
            target='warmup_samples' if warmup else 'samples'
            self.active_test[target].append(result)
            self.active_test['completed_executions']+=1
            self._update_metrics()
            kind='Warm-up' if warmup else 'Run'
            self.active_test['current_message']=f"{kind} {result.get('sample_no')} finished · {result.get('state','unknown')}"
            self._persist()

    def _run(self,recording,logic_steps,subflows):
        try:
            with self.lock:
                self.active_test['state']='running'; self.active_test['current_message']='Running warm-up transactions…' if self.active_test['warmups'] else 'Running synthetic transactions…'; self._persist()
            for i in range(1,self.active_test['warmups']+1):
                if self.cancel_event.is_set(): break
                self._record_sample(self._one(recording,logic_steps,subflows,i,True),True)
            if not self.cancel_event.is_set():
                total=self.active_test['runs']; concurrency=self.active_test['concurrency']
                with ThreadPoolExecutor(max_workers=concurrency,thread_name_prefix='WebFlowPerfSample') as pool:
                    futures={pool.submit(self._one,recording,logic_steps,subflows,i,False):i for i in range(1,total+1)}
                    for future in as_completed(futures):
                        if self.cancel_event.is_set():
                            for f in futures: f.cancel()
                        try: result=future.result()
                        except Exception as exc: result={'sample_no':futures[future],'warmup':False,'state':'failed','duration_ms':None,'error':f'{type(exc).__name__}: {exc}','run_id':None,'steps':[],'console_errors':[],'network_errors':[]}
                        self._record_sample(result,False)
            with self.lock:
                self._update_metrics()
                self.active_test['state']='cancelled' if self.cancel_event.is_set() else 'completed'
                self.active_test['ended_at']=utc_now(); self.active_test['current_message']='Performance test cancelled.' if self.cancel_event.is_set() else 'Performance test completed.'; self._persist()
        except Exception as exc:
            with self.lock:
                if self.active_test:
                    self.active_test['state']='failed'; self.active_test['ended_at']=utc_now(); self.active_test['current_message']=f'{type(exc).__name__}: {exc}'; self._persist()
        finally:
            with self.lock: self.active_runners=[]
