"""Human investigation notes, kept separate from model output and event labels."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


VERSION = 'investigation-tracking-1.0'
STATUSES = {'pending', 'blocked', 'checked'}


class TrackingConflictError(ValueError):
    pass


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _text(value, maximum, label):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f'{label}必须为不超过 {maximum} 字的文字')
    return value.strip()


def _context(record):
    if not record.get('report') or record.get('status') not in {'local_ready', 'model_validated', 'model_failed'}:
        raise TrackingConflictError('排查证据已失效，请重新生成报告后记录')
    bundle = record['bundle']
    case_id, candidate_id = bundle['case_id'], bundle['candidate_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]+', case_id) or not re.fullmatch(r'C\d+', candidate_id):
        raise ValueError('排查记录路径非法')
    checks = {check['check_id']: check for event in bundle['events'] for check in event['checks']}
    fingerprint = bundle['input_fingerprint']
    if not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('排查证据指纹非法')
    return case_id, candidate_id, fingerprint, checks


def _entries(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError('排查记录包含未知检查项')
    result = {}
    for check_id, entry in value.items():
        if not isinstance(entry, dict) or set(entry) != {'status', 'note'}:
            raise ValueError('检查项记录格式无效')
        status = entry['status']
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError('检查项状态无效')
        note = _text(entry['note'], 2000, '检查结果')
        if status != 'pending' and not note:
            raise ValueError('已核查或待数据的检查项需要填写结果')
        result[check_id] = {'status': status, 'note': note}
    return result


class InvestigationTrackingStore:
    def __init__(self, review_root: Path):
        self.root = review_root
        self.lock = threading.RLock()

    def _path(self, case_id, candidate_id):
        path = (self.root / case_id / 'investigation_notes' / (candidate_id + '.json')).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError('排查记录路径越界')
        return path

    def _read(self, path, case_id, candidate_id):
        if not path.is_file():
            return {'version': VERSION, 'case_id': case_id, 'candidate_id': candidate_id, 'records': {}}
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(document, dict) or document.get('version') != VERSION
                    or document.get('case_id') != case_id or document.get('candidate_id') != candidate_id
                    or not isinstance(document.get('records'), dict)):
                raise ValueError()
            for fingerprint, record in document['records'].items():
                if not re.fullmatch(r'[0-9a-f]{64}', fingerprint) or not isinstance(record, dict):
                    raise ValueError()
                labels = record.get('check_labels')
                if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
                    raise ValueError()
                _entries(record.get('entries'), labels)
                _text(record.get('notes'), 4000, '排查备注')
                timestamp = datetime.fromisoformat(record['updated_at'])
                if timestamp.tzinfo is None:
                    raise ValueError()
                if not isinstance(record.get('confirmed_intervals'), dict):
                    raise ValueError()
            return document
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('已有排查记录格式无效，未覆盖原文件') from error

    def _state(self, record, document):
        case_id, candidate_id, fingerprint, checks = _context(record)
        saved = document['records'].get(fingerprint)
        entries = saved['entries'] if saved else {}
        if set(entries) - set(checks):
            raise ValueError('已有记录与当前检查项不一致')
        displayed = {check['check_id'] for group in record['display_checks'].values() for check in group}
        statuses = [entries.get(check_id, {}).get('status', 'pending') for check_id in displayed]
        state = {
            'version': VERSION, 'case_id': case_id, 'candidate_id': candidate_id,
            'investigation_fingerprint': fingerprint, 'record_fingerprint': _fingerprint(document),
            'persisted': saved is not None, 'previous_record_count': len(document['records']) - int(saved is not None),
            'entries': entries, 'notes': saved['notes'] if saved else '',
            'updated_at': saved['updated_at'] if saved else None,
            'summary': {'total': len(displayed), 'checked': statuses.count('checked'),
                        'blocked': statuses.count('blocked'), 'pending': statuses.count('pending')},
        }
        state['report_appendix'] = render_tracking(record, state)
        return state

    def get(self, record):
        case_id, candidate_id, _, _ = _context(record)
        with self.lock:
            return self._state(record, self._read(self._path(case_id, candidate_id), case_id, candidate_id))

    def save(self, record, payload, current_record):
        case_id, candidate_id, fingerprint, checks = _context(record)
        if payload.get('investigation_fingerprint') != fingerprint:
            raise TrackingConflictError('排查证据已变化，请重新读取报告')
        entries = _entries(payload.get('entries'), checks)
        notes = _text(payload.get('notes'), 4000, '排查备注')
        path = self._path(case_id, candidate_id)
        with self.lock:
            document = self._read(path, case_id, candidate_id)
            if payload.get('expected_record_fingerprint') != _fingerprint(document):
                raise TrackingConflictError('排查记录已在其他页面更新，请重新读取后合并')
            _, _, current_fingerprint, _ = _context(current_record())
            if current_fingerprint != fingerprint:
                raise TrackingConflictError('保存期间排查证据已变化，请重新读取报告')
            saved = document['records'].get(fingerprint)
            # Repeated explicit save with identical content keeps its timestamp.
            if saved and saved['entries'] == entries and saved['notes'] == notes:
                return self._state(record, document)
            document['records'][fingerprint] = {
                'entries': entries, 'notes': notes,
                'check_labels': {key: value['step'] for key, value in checks.items()},
                'confirmed_intervals': {event['event_id']: event['confirmed_interval'] for event in record['bundle']['events']},
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return self._state(record, document)


def render_tracking(record, tracking):
    """Append explicitly saved human notes; model facts remain unchanged."""
    if not tracking or not tracking.get('persisted'):
        return ''
    status_text = {'pending': '待核查', 'blocked': '待数据', 'checked': '已核查'}
    lines = ['', '', '## 人工排查记录', '', '以下为人工填写的检查结果，不代表已确认根因。', '',
             f"保存时间：{tracking['updated_at']}", '']
    checks = {check['check_id']: check for event in record['bundle']['events'] for check in event['checks']}
    for check_id, entry in tracking['entries'].items():
        if entry['status'] == 'pending' and not entry['note']:
            continue
        lines += [f"### {check_id} · {status_text[entry['status']]}", '',
                  '> ' + checks[check_id]['step'].replace('\n', '\n> '), '',
                  '> ' + entry['note'].replace('\n', '\n> '), '']
    if tracking['notes']:
        lines += ['### 排查备注', '', '> ' + tracking['notes'].replace('\n', '\n> '), '']
    return '\n'.join(lines)
