"""Read-only daily workbench progress; never queues reviews or generates reports."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd

from app.event_investigation import default_investigation_checks
from app.human_event_review import POLICY_ID


VERSION = 'workbench-overview-1.0'
READY_REPORTS = {'local_ready', 'model_validated', 'model_failed'}
STATUSES = {'pending_ai', 'needs_human', 'ai_confirmed_event', 'ai_rejected_candidate', 'human_reviewed'}


def load_overview_index(batch_dir):
    """Load membership without requiring every case/template to be readable.

    A missing or damaged case belongs in the overview's error queue rather than
    preventing all the other cases from being inspected.
    """
    path = batch_dir / 'summary.csv'
    if not path.is_file():
        raise ValueError('找不到批量分析汇总')
    index = pd.read_csv(path, dtype=str, keep_default_na=False)
    if 'case_id' not in index.columns:
        raise ValueError('summary.csv 缺少 case_id')
    if index['case_id'].duplicated().any() or index['case_id'].str.strip().eq('').any():
        raise ValueError('summary.csv 存在重复或空白 Case ID')
    return [{key: item.get(key, '') for key in ('case_id', 'source_route', 'source_segment')}
            for item in index.to_dict(orient='records')]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _human_confirmed(disposition):
    return (disposition.get('policy_id') == POLICY_ID
            and disposition.get('human_decision') == 'confirmed_event'
            and bool(disposition.get('confirmed_intervals')))


def _needs_human(disposition):
    return (disposition['status'] == 'needs_human'
            or (disposition['status'] == 'human_reviewed'
                and (disposition.get('policy_id') != POLICY_ID
                     or disposition.get('human_decision') not in {'confirmed_event', 'no_event'}
                     or (disposition.get('human_decision') == 'confirmed_event'
                         and not _human_confirmed(disposition)))))


def review_task(case):
    """One review-state projection for the sidebar, overview and live responses.

    Consume current, validated dispositions, never annotation completion or the
    presence of a model file. Completing review does not complete investigation.
    """
    candidates = case['candidates']
    if any(c['disposition']['status'] not in STATUSES for c in candidates):
        raise ValueError('候选处理状态无效，请检查已保存记录')
    counts = {key: sum(c['disposition']['status'] == key for c in candidates)
              for key in ('pending_ai', 'ai_confirmed_event', 'ai_rejected_candidate', 'human_reviewed')}
    counts['needs_human'] = sum(_needs_human(c['disposition']) for c in candidates)
    status = ('data_issue' if not case['quality']['can_complete'] else
              'needs_human' if counts['needs_human'] else
              'pending_ai' if counts['pending_ai'] else
              'no_candidates' if not candidates else 'reviewed')
    return {'status': status, 'counts': counts, 'candidate_count': len(candidates),
            'input_fingerprint': case['input_fingerprint'],
            'data_quality_confirmed': case['quality']['can_complete'],
            'quality_issues': case['quality']['issues'], 'error': None}


def build_review_queue(items, case_dir, reviews):
    """Read-only case states, without opening investigation reports or notes."""
    cases = []
    for item in items:
        try:
            task = review_task(reviews.get_case(case_dir(item['case_id'])))
        except (OSError, ValueError, KeyError, TypeError) as error:
            task = {'status': 'read_error', 'counts': None, 'candidate_count': None,
                    'input_fingerprint': None, 'data_quality_confirmed': False,
                    'quality_issues': [], 'error': str(error)}
        cases.append({'case_id': item['case_id'], **task})
    return {'cases': cases, 'completed_at': _now(), 'new_model_calls': 0}


def _link(case_id, candidate_id=None, investigation=False):
    value = '/workbench/?case=' + quote(case_id, safe='')
    if candidate_id:
        value += '&candidate=' + quote(candidate_id, safe='')
    return value + ('#investigationPanel' if investigation else '#aiPanel')


def _investigation(case_dir, case, candidate, reviews, tracking):
    disposition = candidate['disposition']
    confirmed = (disposition['status'] == 'ai_confirmed_event'
                 or (disposition['status'] == 'human_reviewed' and _human_confirmed(disposition)))
    result = {'confirmed': confirmed, 'confirmation_source': (
        'human' if confirmed and disposition['status'] == 'human_reviewed'
        else 'ai' if confirmed else None), 'report_status': 'not_applicable',
        'report_fingerprint': None, 'tracking_status': 'not_applicable',
        'tracking_summary': None, 'tracking_updated_at': None, 'report_error': None}
    if not confirmed:
        return result
    result['report_status'] = 'not_requested'
    result['tracking_status'] = 'no_report'
    if not candidate.get('investigation_available'):
        return result
    try:
        record = reviews.get_investigation(case_dir, candidate['candidate_id'])
        result['report_status'] = record['status']
        if not record.get('report') or record['status'] not in READY_REPORTS:
            return result
        bundle = record['bundle']
        # A report read after a concurrent confirmation must not belong to the
        # earlier candidate snapshot, even if both records are individually valid.
        source = bundle['acceleration_provenance'].get('human_event_confirmation')
        expected = disposition if result['confirmation_source'] == 'human' else None
        if (bundle['review_fingerprint'] != case['input_fingerprint']
                or [event['confirmed_interval'] for event in bundle['events']] != disposition['confirmed_intervals']
                or (expected is None and source is not None)
                or (expected is not None and (not source
                    or source['annotation_sha256'] != expected['annotation_sha256']
                    or source['selected_event_ids'] != expected['selected_event_ids']))):
            result['report_status'] = 'stale'
            return result
        result['report_fingerprint'] = bundle['input_fingerprint']
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(report_status='read_error', report_error=str(error))
        return result
    try:
        model_events = {event['event_id']: event['prioritized_checks']
                        for event in (record.get('model_output') or {}).get('events', [])}
        payload = dict(record, display_checks={event['event_id']: model_events.get(
            event['event_id'], default_investigation_checks(event)) for event in bundle['events']})
        saved = tracking.get(payload)
        summary = saved['summary']
        state = ('blocked' if summary['blocked'] else
                 'checked' if summary['total'] and summary['checked'] == summary['total'] else 'pending')
        result.update(tracking_status=state, tracking_summary=summary,
                      tracking_updated_at=saved['updated_at'], tracking_persisted=saved['persisted'])
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(tracking_status='read_error', tracking_error=str(error))
    return result


def _case(item, case_dir, reviews, tracking):
    case = reviews.get_case(case_dir)
    task = review_task(case)
    candidates = []
    for candidate in case['candidates']:
        disposition = candidate['disposition']
        if disposition['status'] not in STATUSES:
            raise ValueError('候选处理状态无效，请检查已保存记录')
        investigation = _investigation(case_dir, case, candidate, reviews, tracking)
        candidates.append({
            'candidate_id': candidate['candidate_id'],
            'start_time': candidate['start_time'], 'end_time': candidate['end_time'],
            'status': disposition['status'], 'reason': disposition.get('reason', ''),
            'review_status': candidate['review']['status'],
            'needs_human': _needs_human(disposition),
            'human_decision': disposition.get('human_decision'),
            'confirmed_intervals': disposition.get('confirmed_intervals', []),
            'selected_event_ids': disposition.get('selected_event_ids', []),
            'annotation_sha256': disposition.get('annotation_sha256'),
            'workbench_url': _link(item['case_id'], candidate['candidate_id']),
            'investigation_url': _link(item['case_id'], candidate['candidate_id'], True),
            **investigation,
        })
    counts = dict(task['counts'])
    counts.update(needs_human=sum(c['needs_human'] for c in candidates),
                  confirmed=sum(c['confirmed'] for c in candidates),
                  reports_ready=sum(c['confirmed'] and c['report_status'] in READY_REPORTS for c in candidates),
                  reports_pending=sum(c['confirmed'] and c['report_status'] not in READY_REPORTS for c in candidates),
                  tracking_checked=sum(c['tracking_status'] == 'checked' for c in candidates),
                  tracking_blocked=sum(c['tracking_status'] == 'blocked' for c in candidates))
    return {**item, 'status': task['status'], 'observed_at': _now(), 'error': None,
            'input_fingerprint': case['input_fingerprint'], 'candidate_count': len(candidates),
            'counts': counts, 'quality_issues': case['quality']['issues'],
            'data_quality_confirmed': case['quality']['can_complete'], 'candidates': candidates}


def build_overview(items, case_dir, reviews, tracking):
    """Read each indexed case and validate cached decisions/reports against inputs.

    Cases are observed sequentially. Nothing is written, no model is called, and
    these counts are workflow state rather than event accuracy or root-cause proof.
    """
    started_at = _now()
    cases = []
    for item in items:
        base = {key: item.get(key, '') for key in ('case_id', 'source_route', 'source_segment')}
        base['workbench_url'] = _link(base['case_id'])
        try:
            cases.append(_case(base, case_dir(base['case_id']), reviews, tracking))
        except (OSError, ValueError, KeyError, TypeError) as error:
            cases.append({**base, 'status': 'read_error', 'observed_at': _now(),
                          'error': str(error), 'input_fingerprint': None, 'candidate_count': None,
                          'counts': None, 'quality_issues': [], 'data_quality_confirmed': False, 'candidates': []})
    valid = [case for case in cases if case['status'] != 'read_error']
    totals = {key: sum(case['counts'][key] for case in valid) for key in (
        'pending_ai', 'needs_human', 'ai_confirmed_event', 'ai_rejected_candidate', 'human_reviewed',
        'confirmed', 'reports_ready', 'reports_pending', 'tracking_checked', 'tracking_blocked')}
    totals.update(total_cases=len(cases), scanned_cases=len(valid), error_cases=len(cases) - len(valid),
                  candidate_count=sum(case['candidate_count'] for case in valid),
                  no_candidate_cases=sum(case['candidate_count'] == 0 for case in valid),
                  reviewed_cases=sum(case['status'] == 'reviewed' for case in valid),
                  data_issue_cases=sum(not case['data_quality_confirmed'] for case in valid),
                  cases_with_errors=len(cases) - len(valid) + sum(
                      any(c['report_status'] == 'read_error' or c['tracking_status'] == 'read_error'
                          for c in case['candidates']) for case in valid),
                  report_error_candidates=sum(c['report_status'] == 'read_error' or c['tracking_status'] == 'read_error'
                                              for case in valid for c in case['candidates']))
    return {'version': VERSION, 'scope': 'daily_workbench', 'operation': 'read_only_local_scan',
            'started_at': started_at, 'completed_at': _now(), 'new_model_calls': 0,
            'interpretation': 'Sequential workflow snapshot. No candidates is not an AI negative decision; '
                'candidate counts are not unique event counts. Checked notes do not confirm root cause. '
                'This is not an accuracy evaluation or a productivity measurement.',
            'summary': totals, 'cases': cases}
