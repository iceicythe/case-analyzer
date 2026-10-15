"""Explicit human candidate decisions and their investigation confirmation source.

The daily workbench extends the frozen AI review store. Archived experiments keep
their original review implementation and labels.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.annotation_protocol import PROTOCOL_ID
from app.annotations import _validate_annotation
from app.workbench_reviews import WorkbenchReviewStore, ReviewBusyError, _digest, _read


POLICY_ID = 'human-candidate-confirmation-1.0'
EMPTY_RECORD_FINGERPRINT = hashlib.sha256(b'').hexdigest()


class HumanConfirmationConflictError(ValueError):
    """The displayed confirmation no longer matches the saved record."""


def _record_fingerprint(path):
    try:
        return _digest(path)
    except FileNotFoundError:
        return EMPTY_RECORD_FINGERPRINT


def _matching_events(annotation, candidate):
    return [event for event in annotation['events']
            if min(event['end_time'], candidate['end_time'])
            - max(event['start_time'], candidate['start_time']) > 1e-6]


def _selection(annotation, candidate, decision, event_ids):
    if not isinstance(decision, str) or decision not in {'confirmed_event', 'no_event'}:
        raise ValueError('请选择本候选的人工结论')
    if not isinstance(event_ids, list) or any(not isinstance(value, str) for value in event_ids) or len(set(event_ids)) != len(event_ids):
        raise ValueError('人工事件编号格式无效')
    matching = _matching_events(annotation, candidate)
    allowed = {event['event_id']: event for event in matching}
    if decision == 'no_event':
        if event_ids:
            raise ValueError('排除候选时不能选择事件区间')
        if matching:
            raise ValueError('候选范围内仍有已标注事件，请核对人工标注后再排除')
        return []
    if annotation['case_label'] != 'positive' or not event_ids or set(event_ids) - set(allowed):
        raise ValueError('请选择与本候选相交的已保存人工事件')
    # Keep the complete manual interval, including boundaries beyond the candidate.
    return sorted([[allowed[value]['start_time'], allowed[value]['end_time']] for value in event_ids])


class HumanEventReviewStore(WorkbenchReviewStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.investigation_request = threading.local()

    def _annotation(self, case_dir, case, candidate):
        if not self.annotations_root:
            raise ValueError('未配置人工标注目录')
        path = self.annotations_root / (case_dir.name + '.json')
        before = _digest(path)
        annotation = _validate_annotation(_read(path), case_dir, require_completed=True)
        source = case['quality']['original_can']
        confirmation = annotation.get('review_confirmation') or {}
        expected = {'vehicle_sha256': case['vehicle_sha256'],
                    'protocol_sha256': case['annotation_protocol_sha256'],
                    'metadata_sha256': source.get('metadata_sha256'),
                    'original_can_sha256': source.get('original_can_sha256')}
        if (annotation.get('annotation_protocol_id') != PROTOCOL_ID or confirmation.get('protocol_id') != PROTOCOL_ID
                or not case['quality']['can_complete'] or confirmation.get('whole_case_reviewed') is not True
                or any(confirmation.get(key) != value for key, value in expected.items())):
            raise ValueError('请按当前口径核查整段数据，并保存已完成的人工标注')
        if candidate['boundary_review_required'] and confirmation.get('boundary_reviewed') is not True:
            raise ValueError('请先核查相邻片段并保存边界确认')
        if _digest(path) != before:
            raise ValueError('人工标注已变化，请重新读取')
        return annotation, before

    def get_case(self, case_dir: Path):
        directory = self.output_root / case_dir.name / 'human'
        before = {path.stem: _record_fingerprint(path) for path in directory.glob('C*.json')}
        case = super().get_case(case_dir)
        for candidate in case['candidates']:
            record_sha = _record_fingerprint(directory / (candidate['candidate_id'] + '.json'))
            if record_sha != before.get(candidate['candidate_id'], EMPTY_RECORD_FINGERPRINT):
                raise ReviewBusyError('人工结论正在更新，请稍后读取')
            disposition = candidate['disposition']
            if disposition['status'] not in {'needs_human', 'human_reviewed'}:
                continue
            try:
                annotation, annotation_sha = self._annotation(case_dir, case, candidate)
                matching = _matching_events(annotation, candidate)
                candidate['human_resolution'] = {
                    'available': True, 'annotation_sha256': annotation_sha,
                    'record_fingerprint': record_sha,
                    'events': [{key: event[key] for key in ('event_id', 'start_time', 'end_time')} for event in matching],
                }
                if disposition.get('policy_id') == POLICY_ID:
                    intervals = _selection(annotation, candidate, disposition.get('human_decision'), disposition.get('selected_event_ids'))
                    if (disposition.get('annotation_sha256') != annotation_sha
                            or disposition.get('confirmed_intervals') != intervals):
                        raise ValueError('人工确认与当前标注不一致，请重新确认')
            except (OSError, ValueError) as error:
                candidate['human_resolution'] = {'available': False, 'events': [], 'reason': str(error),
                                                 'record_fingerprint': record_sha}
                if disposition.get('policy_id') == POLICY_ID:
                    candidate['disposition'] = {'status': 'needs_human', 'reason': '人工确认已失效，请重新核查并保存',
                                                'confirmed_intervals': [], 'event_protocol_id': PROTOCOL_ID}
        case['triage_summary'] = {status: sum(c['disposition']['status'] == status for c in case['candidates'])
                                 for status in case['triage_summary']}
        return case

    def resolve_human(self, case_dir, candidate_id, expected_fingerprint, decision=None, event_ids=None,
                      annotation_sha=None, expected_record_fingerprint=None):
        if not isinstance(expected_record_fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_record_fingerprint):
            raise ValueError('缺少当前人工结论版本，请刷新后再保存')
        if not self.lock.acquire(blocking=False):
            raise ReviewBusyError('已有请求正在运行，请稍后再试')
        try:
            case = self.get_case(case_dir)
            if case['input_fingerprint'] != expected_fingerprint:
                raise ValueError('数据或口径已变化，请重新打开 Case')
            candidate = next((c for c in case['candidates'] if c['candidate_id'] == candidate_id), None)
            if candidate is None or candidate['disposition']['status'] not in {'needs_human', 'human_reviewed'}:
                raise ValueError('候选不在人工处理队列')
            directory = self.output_root / case_dir.name / 'human'
            target = directory / (candidate_id + '.json')
            if candidate['human_resolution']['record_fingerprint'] != expected_record_fingerprint:
                raise HumanConfirmationConflictError('人工结论已在其他页面更新，请读取最新结论后再保存')
            annotation, current_sha = self._annotation(case_dir, case, candidate)
            if not isinstance(annotation_sha, str) or annotation_sha != current_sha:
                raise ValueError('人工标注已变化，请重新读取并选择区间')
            intervals = _selection(annotation, candidate, decision, event_ids)
            disposition = {'status': 'human_reviewed', 'policy_id': POLICY_ID,
                           'reason_code': 'explicit_human_confirmation', 'human_decision': decision,
                           'reason': '人工确认减速事件' if decision == 'confirmed_event' else '人工排除本候选',
                           'confirmed_intervals': intervals, 'selected_event_ids': sorted(event_ids),
                           'event_protocol_id': PROTOCOL_ID, 'annotation_sha256': current_sha}
            # Re-read before commit; labels can be edited independently of model requests.
            fresh = self.get_case(case_dir)
            fresh_candidate = next(c for c in fresh['candidates'] if c['candidate_id'] == candidate_id)
            if fresh_candidate['disposition']['status'] not in {'needs_human', 'human_reviewed'}:
                raise HumanConfirmationConflictError('候选复核结果已变化，请读取最新结论')
            _, fresh_sha = self._annotation(case_dir, fresh, fresh_candidate)
            if fresh['input_fingerprint'] != expected_fingerprint or fresh_sha != current_sha:
                raise ValueError('保存期间数据或人工标注已变化，请重新读取')
            if _record_fingerprint(target) != expected_record_fingerprint:
                raise HumanConfirmationConflictError('保存期间人工结论已变化，请读取最新结论')
            record = {'input_fingerprint': expected_fingerprint, 'annotation_sha256': current_sha,
                      'case_id': case_dir.name, 'candidate_id': candidate_id, 'disposition': disposition,
                      'annotation_case_label': annotation['case_label'], 'annotator': annotation.get('annotator'),
                      'resolved_at': datetime.now(timezone.utc).isoformat()}
            if target.is_file() and _read(target).get('disposition') == disposition:
                return fresh
            directory.mkdir(parents=True, exist_ok=True)
            history = directory / 'history'; history.mkdir(exist_ok=True)
            content = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
            temporary = target.with_name(target.name + '.' + uuid4().hex + '.tmp')
            try:
                temporary.write_text(content, encoding='utf-8')
                if _record_fingerprint(target) != expected_record_fingerprint:
                    raise HumanConfirmationConflictError('保存期间人工结论已变化，请读取最新结论')
                (history / (candidate_id + '_' + uuid4().hex + '.json')).write_text(content, encoding='utf-8')
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            return self.get_case(case_dir)
        finally:
            self.lock.release()

    def _confirmed_candidate(self, case_dir, candidate_id, expected_fingerprint=None):
        case = self.get_case(case_dir)
        if expected_fingerprint is not None and case['input_fingerprint'] != expected_fingerprint:
            raise ValueError('数据或口径已变化，请重新打开 Case')
        candidate = next((c for c in case['candidates'] if c['candidate_id'] == candidate_id), None)
        if candidate is None:
            raise ValueError('候选编号不存在')
        disposition = candidate['disposition']
        human = (disposition['status'] == 'human_reviewed' and disposition.get('policy_id') == POLICY_ID
                 and disposition.get('human_decision') == 'confirmed_event' and bool(disposition.get('confirmed_intervals')))
        if disposition['status'] != 'ai_confirmed_event' and not human:
            raise ValueError('仅有效的 AI 或人工确认事件可排查')
        confirmation = ({'annotation_sha256': disposition['annotation_sha256'],
                         'selected_event_ids': disposition['selected_event_ids']} if human else None)
        if (hasattr(self.investigation_request, 'confirmation')
                and self.investigation_request.confirmation != confirmation):
            raise ValueError('事件确认已变化，请重新读取报告')
        if human:
            candidate['supplemental_context']['provenance'] = copy.deepcopy(candidate['supplemental_context']['provenance'])
            candidate['supplemental_context']['provenance']['human_event_confirmation'] = {
                'source': 'human', 'policy_id': POLICY_ID, 'annotation_sha256': disposition['annotation_sha256'],
                'selected_event_ids': disposition['selected_event_ids'], 'confirmed_intervals': disposition['confirmed_intervals'],
            }
        return case, candidate

    def get_investigation(self, case_dir, candidate_id):
        _, candidate = self._confirmed_candidate(case_dir, candidate_id)
        record = super().get_investigation(case_dir, candidate_id)
        if record.get('report'):
            expected = candidate['supplemental_context']['provenance'].get('human_event_confirmation')
            actual = record['bundle']['acceleration_provenance'].get('human_event_confirmation')
            if actual != expected:
                return {'status': 'stale', 'report': None}
            if actual:
                record = dict(record, report=record['report'] + '\n\n## 事件确认来源\n\n'
                              + '人工确认；区间直接采用已保存的人工标注。\n\n'
                              + '人工事件：' + '、'.join(actual['selected_event_ids']) + '\n\n'
                              + '标注 SHA256：' + actual['annotation_sha256'] + '\n')
        return record

    def investigate(self, case_dir, candidate_id, expected_fingerprint, use_model=False, expected_confirmation=None):
        self.investigation_request.confirmation = expected_confirmation
        try:
            record = super().investigate(case_dir, candidate_id, expected_fingerprint, use_model)
            if record.get('report'):
                try:
                    current = self.get_investigation(case_dir, candidate_id)
                    if not current.get('report'):
                        return dict(record, status='stale', report=None)
                    record = dict(record, report=current['report'])
                except (OSError, ValueError):
                    return dict(record, status='stale', report=None)
            return record
        finally:
            del self.investigation_request.confirmation
