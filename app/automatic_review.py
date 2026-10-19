"""Single worker with durable progress; validated reviews are checkpoints."""
from __future__ import annotations

import json
import os
import re
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.candidate_decision import VERSION
from app.workbench_reviews import ReviewBusyError


class QueueStorageError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


class AutomaticReviewQueue:
    def __init__(self, reviews, state_path=None, resolve_case=None, scope='', autostart=True, investigate=False, on_result=None):
        self.reviews = reviews
        self.investigate = investigate
        self.on_result = on_result
        self.notification_error = None
        self.discovery, self.discovery_errors = deque(), {}
        self.discovering = None
        self.state_path = Path(state_path) if state_path else None
        self.resolve_case, self.scope = resolve_case, scope
        self.condition = threading.Condition()
        self.pending, self.jobs, self.directories = deque(), {}, {}
        self.paused = self.closed = self.invalid_state = False
        self.pause_reason = None
        self.revision = 0
        self.instance_id = uuid4().hex
        self.storage_error = self.owner = self.worker = None
        self._claim()
        self._load()
        if autostart:
            self.start()

    def _claim(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        owner = self.state_path.with_suffix('.lock').open('a+b')
        try:
            if owner.seek(0, os.SEEK_END) == 0:
                owner.write(b'0'); owner.flush()
            owner.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner.close()
            raise QueueStorageError('已有服务持有 AI 队列，请先关闭该服务') from None
        self.owner = owner

    def _load(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            value = json.loads(self.state_path.read_text(encoding='utf-8'))
            if (value['version'] != 1 or value['scope'] != self.scope or type(value['paused']) is not bool
                    or type(value['revision']) is not int or value['revision'] < 0 or not isinstance(value['jobs'], list)):
                raise ValueError('InvalidQueue')
            jobs = {}
            for job in value['jobs']:
                if (not isinstance(job, dict) or not isinstance(job['job_id'], str)
                        or not re.fullmatch(r'[a-f0-9]{32}', job['job_id']) or job['job_id'] in jobs
                        or not isinstance(job['case_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]+', job['case_id'])
                        or not re.fullmatch(r'[a-f0-9]{64}', job['input_fingerprint'])
                        or not isinstance(job['prompt_version'], str)
                        or job['status'] not in {'queued','running','complete','failed','changed'}
                        or type(job['retry']) is not bool or not isinstance(job['candidate_ids'], list)
                        or any(not isinstance(cid, str) or not re.fullmatch(r'C\d+', cid) for cid in job['candidate_ids'])
                        or len(set(job['candidate_ids'])) != len(job['candidate_ids'])
                        or type(job['completed_count']) is not int or not 0 <= job['completed_count'] <= len(job['candidate_ids'])
                        or job['candidate_id'] is not None and job['candidate_id'] not in job['candidate_ids']
                        or type(job['in_flight']) is not bool or job['in_flight'] and job['candidate_id'] is None
                        or not isinstance(job['created_at'], str) or not isinstance(job['updated_at'], str)
                        or job.get('error_type') is not None and not re.fullmatch(r'[A-Za-z0-9_]{1,80}', job['error_type'])):
                    raise ValueError('InvalidJob')
                jobs[job['job_id']] = dict(job)
                if job.get('stage', 'review') not in {'review', 'investigation', 'done'}:
                    raise ValueError('InvalidStage')
            pending = [j['job_id'] for j in jobs.values() if j['status'] == 'running']
            if len(pending) > 1:
                raise ValueError('InvalidRunningJobs')
            pending += [j['job_id'] for j in jobs.values() if j['status'] == 'queued']
            for key in pending:
                jobs[key].update(status='queued', recovered=True)
            self.jobs, self.pending = jobs, deque(pending)
            self.paused, self.revision = value['paused'], value['revision']
            self.pause_reason = 'ProviderAccessError' if value.get('pause_reason') == 'ProviderAccessError' else None
        except (OSError, ValueError, KeyError, TypeError):
            # Preserve the original file; unreadable state cannot authorize calls.
            self.storage_error, self.invalid_state, self.paused = 'QueueStateUnreadable', True, True

    def _persist(self):
        if self.invalid_state:
            raise QueueStorageError('队列记录无法读取，已暂停；请检查本地队列文件')
        self.revision += 1
        if not self.state_path:
            return
        temporary = self.state_path.with_name(self.state_path.name+'.'+uuid4().hex+'.tmp')
        try:
            payload = {'version':1, 'scope':self.scope, 'paused':self.paused, 'pause_reason':self.pause_reason,
                       'revision':self.revision, 'jobs':list(self.jobs.values())}
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False, indent=2)
                stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            temporary.replace(self.state_path)
            self.storage_error = None
        except OSError:
            self.paused, self.storage_error = True, 'QueueWriteFailed'
            raise QueueStorageError('队列保存失败，已暂停；检查磁盘后可继续') from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def start(self):
        with self.condition:
            if self.worker or self.closed:
                return
            self.worker = threading.Thread(target=self._run, name='automatic-case-review', daemon=True)
            self.worker.start()

    def enqueue(self, case_dir: Path, case: dict, retry=False):
        with self.condition:
            if self.closed or self.storage_error:
                raise QueueStorageError('队列不可用，请先检查服务或队列存储状态')
            current = next((j for j in reversed(list(self.jobs.values())) if j['case_id'] == case_dir.name
                            and j['input_fingerprint'] == case['input_fingerprint'] and j['prompt_version'] == VERSION), None)
            ids = [c['candidate_id'] for c in case['candidates'] if c['disposition']['status'] == 'pending_ai']
            if self.investigate:
                ids += [c['candidate_id'] for c in case['candidates'] if self._confirmed(c)
                        and self.reviews.get_investigation(case_dir, c['candidate_id']).get('status') != 'model_validated']
            if not ids and current and current['status'] in {'failed','changed'}:
                current.update(status='complete', completed_count=len(current['candidate_ids']),
                               error_type=None, candidate_id=None, in_flight=False, updated_at=_now())
                self._persist()
            if ids and (not current or current['status'] in {'complete','changed'} or retry and current['status'] == 'failed'):
                key, stamp = uuid4().hex, _now()
                self.jobs[key] = {'job_id':key, 'case_id':case_dir.name, 'input_fingerprint':case['input_fingerprint'],
                    'prompt_version':VERSION, 'status':'queued', 'retry':bool(retry), 'candidate_ids':ids,
                    'candidate_id':None, 'in_flight':False, 'completed_count':0, 'error_type':None,
                    'stage':'review', 'investigation_count':0,
                    'created_at':stamp, 'updated_at':stamp, 'recovered':False}
                self.directories[key] = case_dir
                self.pending.append(key)
                self._persist()  # Persist authorization before waking the worker.
                self.condition.notify_all()
        return self.snapshot(case_dir.name)

    @staticmethod
    def _confirmed(candidate):
        disposition = candidate['disposition']
        return (disposition['status'] == 'ai_confirmed_event' or
                disposition['status'] == 'human_reviewed' and disposition.get('human_decision') == 'confirmed_event')

    def schedule(self, case_id):
        """Discover work off the HTTP thread. The import index survives restarts."""
        with self.condition:
            if self.closed or self.storage_error:
                raise QueueStorageError('自动队列不可用，请检查服务状态')
            if case_id not in self.discovery:
                self.discovery.append(case_id)
            self.discovery_errors.pop(case_id, None)
            self.condition.notify_all()

    def snapshot(self, case_id=None):
        with self.condition:
            latest = {}
            for job in self.jobs.values():
                latest[job['case_id']] = job
            jobs = [dict(j, total_count=len(j['candidate_ids'])) for j in latest.values()
                    if case_id is None or j['case_id'] == case_id]
            counts = {s:sum(j['status'] == s for j in latest.values()) for s in ('queued','running','complete','failed','changed')}
            return {'enabled':True, 'persistent':self.state_path is not None, 'paused':self.paused,
                    'pause_reason':self.pause_reason,
                    'jobs':jobs, 'running':any(j['status']=='running' for j in self.jobs.values()),
                    'queued_count':len(self.pending), 'discovery_count':len(self.discovery),
                    'discovering':self.discovering, 'discovery_errors':dict(self.discovery_errors),
                    'counts':counts, 'revision':self.revision, 'instance_id':self.instance_id,
                    'notification_error':self.notification_error,
                    'storage_error':self.storage_error, 'recovered_count':sum(bool(j.get('recovered')) for j in latest.values())}

    def pause(self, paused, reason=None):
        with self.condition:
            if self.invalid_state:
                raise QueueStorageError('队列记录无法读取，请先检查本地队列文件')
            self.paused = paused
            self.pause_reason = reason if paused else None
            self._persist()
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        if self.worker:
            self.worker.join(timeout=2)
        elif self.owner:
            self.owner.close(); self.owner = None
        # An in-flight worker retains the file lock until it actually exits.

    def _update(self, key, **values):
        with self.condition:
            self.jobs[key].update(values, updated_at=_now())
            self._persist()

    def _ready(self):
        with self.condition:
            self.condition.wait_for(lambda: self.closed or not self.paused and not self.storage_error)
            return not self.closed

    def _process(self, key):
        job = self.jobs[key]
        case_dir = self.resolve_case(job['case_id']) if self.resolve_case else self.directories[key]
        case = self.reviews.get_case(case_dir)
        if job['prompt_version'] != VERSION or case['input_fingerprint'] != job['input_fingerprint']:
            self._update(key, status='changed', candidate_id=None, in_flight=False, error_type='InputChanged')
            return
        candidates = {c['candidate_id']:c for c in case['candidates']}
        if any(cid not in candidates for cid in job['candidate_ids']):
            self._update(key, status='changed', candidate_id=None, in_flight=False, error_type='CandidatesChanged')
            return
        interrupted = job['in_flight'] and (self.reviews.get_investigation(case_dir, job['candidate_id']).get('status') != 'model_validated'
            if job.get('stage') == 'investigation' else candidates[job['candidate_id']]['disposition']['status'] == 'pending_ai')
        if interrupted:
            # A crash may happen after the provider accepted a request. Require
            # explicit retry when there is no usable saved result to resume from.
            self._update(key, status='failed', error_type='InterruptedRequest', in_flight=False)
            return
        self._update(key, in_flight=False, candidate_id=None, stage='review')
        for candidate_id in job['candidate_ids']:
            if not self._ready():
                return
            case = self.reviews.get_case(case_dir)
            if case['input_fingerprint'] != job['input_fingerprint']:
                self._update(key, status='changed', candidate_id=None, in_flight=False, error_type='InputChanged')
                return
            current = next(c for c in case['candidates'] if c['candidate_id'] == candidate_id)
            completed = sum(c['candidate_id'] in job['candidate_ids'] and c['disposition']['status'] != 'pending_ai' for c in case['candidates'])
            self._update(key, completed_count=completed)
            if current['disposition']['status'] != 'pending_ai':
                continue
            if current['review']['status'] == 'call_failed' and not job['retry']:
                self._update(key, status='failed', error_type='ModelCallFailed', candidate_id=None)
                return
            self._update(key, candidate_id=candidate_id, in_flight=True)
            while self._ready():
                try:
                    result = self.reviews.review(case_dir, candidate_id, job['input_fingerprint'], only_pending=True)
                    break
                except ReviewBusyError:
                    with self.condition:
                        self.condition.wait(timeout=.3)
            else:
                return
            reviewed = next(c for c in result['candidates'] if c['candidate_id'] == candidate_id)
            if reviewed['review']['status'] in {'call_failed','stale'}:
                self._pause_provider_failure(reviewed['review'])
                self._update(key, status='failed', in_flight=False, candidate_id=None,
                             error_type='InputChanged' if reviewed['review']['status']=='stale' else 'ModelCallFailed')
                return
            completed = sum(c['candidate_id'] in job['candidate_ids'] and c['disposition']['status'] != 'pending_ai' for c in result['candidates'])
            self._update(key, completed_count=completed, in_flight=False, candidate_id=None)
        if self.investigate and not self._investigate(key, case_dir):
            return
        self._update(key, status='complete', candidate_id=None, in_flight=False, error_type=None, stage='done')

    def _investigate(self, key, case_dir):
        job = self.jobs[key]
        self._update(key, stage='investigation')
        completed = 0
        for cid in job['candidate_ids']:
            if not self._ready():
                return False
            case = self.reviews.get_case(case_dir)
            if case['input_fingerprint'] != job['input_fingerprint']:
                self._update(key, status='changed', error_type='InputChanged')
                return False
            candidate = next(c for c in case['candidates'] if c['candidate_id'] == cid)
            if not self._confirmed(candidate):
                continue
            existing = self.reviews.get_investigation(case_dir, cid)
            if existing.get('status') == 'model_validated':
                completed += 1
                self._update(key, investigation_count=completed)
                continue
            if existing.get('status') == 'model_failed' and not job['retry']:
                self._update(key, status='failed', error_type='InvestigationFailed')
                return False
            disposition = candidate['disposition']
            confirmation = ({'annotation_sha256':disposition['annotation_sha256'],
                             'selected_event_ids':disposition['selected_event_ids']}
                            if disposition['status'] == 'human_reviewed' else None)
            arguments = (case_dir, cid, job['input_fingerprint'])
            # Save deterministic evidence first, so provider failures retain a usable report.
            while self._ready():
                try:
                    self.reviews.investigate(*arguments, expected_confirmation=confirmation)
                    break
                except ReviewBusyError:
                    with self.condition:
                        self.condition.wait(timeout=.3)
            else:
                return False
            self._update(key, candidate_id=cid, in_flight=True)
            while self._ready():
                try:
                    record = self.reviews.investigate(*arguments, use_model=True, expected_confirmation=confirmation)
                    break
                except ReviewBusyError:
                    with self.condition:
                        self.condition.wait(timeout=.3)
            else:
                return False
            if record.get('status') != 'model_validated':
                self._pause_provider_failure(record)
                self._update(key, status='changed' if record.get('status') == 'stale' else 'failed',
                             candidate_id=None, in_flight=False, error_type='InvestigationFailed')
                return False
            completed += 1
            self._update(key, investigation_count=completed, candidate_id=None, in_flight=False)
        return True

    def _pause_provider_failure(self, record):
        if (record.get('model_call_metrics') or record.get('call_metrics') or {}).get('http_status') in {401, 402, 403}:
            self.pause(True, reason='ProviderAccessError')

    def _notify_result(self, case_id, job=None):
        if self.on_result:
            try:
                self.on_result(case_id, dict(job, provider_paused=self.pause_reason == 'ProviderAccessError') if job else None)
            except Exception:
                self.notification_error = '通知记录失败，分析结果已保留'

    def _run(self):
        key = None
        try:
            while True:
                case_id = None
                with self.condition:
                    self.condition.wait_for(lambda: self.closed or (self.pending or self.discovery) and not self.paused and not self.storage_error)
                    if self.closed:
                        return
                    if not self.pending:
                        self.discovering = self.discovery.popleft()
                        case_id = self.discovering
                    else:
                        key = self.pending.popleft()
                if case_id is not None:
                    try:
                        directory = self.resolve_case(case_id)
                        result = self.enqueue(directory, self.reviews.get_case(directory))
                        job = result['jobs'][0] if result['jobs'] else None
                        if not job or job['status'] in {'complete','failed','changed'}:
                            self._notify_result(case_id, job)
                    except Exception as error:
                        with self.condition:
                            self.discovery_errors[case_id] = type(error).__name__[:80]
                        self._notify_result(case_id)
                    finally:
                        with self.condition:
                            self.discovering = None
                    continue
                try:
                    self._update(key, status='running')
                    self._process(key)
                    if self.jobs[key]['status'] == 'complete' and not self.closed:
                        self.schedule(self.jobs[key]['case_id'])
                except QueueStorageError:
                    with self.condition:
                        self.jobs[key]['status'] = 'queued'; self.pending.appendleft(key)
                except Exception as error:
                    try:
                        # Do not persist provider messages, URLs or credentials.
                        self._update(key, status='failed', in_flight=False, candidate_id=None, error_type=type(error).__name__[:80])
                    except QueueStorageError:
                        with self.condition:
                            self.jobs[key]['status'] = 'queued'; self.pending.appendleft(key)
                if self.jobs[key]['status'] in {'complete','failed','changed'}:
                    self._notify_result(self.jobs[key]['case_id'], self.jobs[key])
                if self.closed:
                    return
                key = None
        finally:
            with self.condition:
                if key and self.jobs[key]['status'] == 'running':
                    self.jobs[key]['status'] = 'queued'
                    try:
                        self._persist()
                    except QueueStorageError:
                        pass
            if self.owner:
                self.owner.close(); self.owner = None
