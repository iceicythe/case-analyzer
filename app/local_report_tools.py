"""Scoped local report completion and evidence-bound report archives."""
from __future__ import annotations

import io
import hashlib
import json
import math
import re
import threading
import zipfile
from datetime import datetime, timezone

from app.event_investigation import default_investigation_checks
from app.human_event_review import POLICY_ID
from app.offline_report import VERSION as VIEWER_VERSION, render_offline_report
from app.workbench_reviews import ReviewBusyError


READY = {'local_ready', 'model_validated', 'model_failed'}
VERSION = 'local-report-tools-1.0'


class ReportInputChanged(ValueError):
    pass


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _validate(payload, known_ids, exporting=False):
    if not isinstance(payload, dict) or set(payload) != {'items'}:
        raise ValueError('报告请求只接受 items 清单')
    items = payload['items']
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise ValueError('每次需选择 1～200 个确认候选')
    result, pairs = [], set()
    fields = {'case_id', 'candidate_id', 'input_fingerprint', 'confirmed_intervals', 'human_confirmation'}
    if exporting:
        fields.add('report_fingerprint')
    for item in items:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError('报告清单字段不完整或包含未知参数')
        case_id, candidate_id = item['case_id'], item['candidate_id']
        if (not isinstance(case_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', case_id)
                or case_id not in known_ids or not isinstance(candidate_id, str)
                or not re.fullmatch(r'C\d+', candidate_id)):
            raise ValueError('报告清单包含非法 Case 或候选编号')
        if (case_id, candidate_id) in pairs:
            raise ValueError('报告清单包含重复候选')
        pairs.add((case_id, candidate_id))
        if not _sha(item['input_fingerprint']) or (exporting and not _sha(item['report_fingerprint'])):
            raise ValueError('报告清单指纹无效，请刷新核对')
        intervals = item['confirmed_intervals']
        if not isinstance(intervals, list) or not 1 <= len(intervals) <= 100:
            raise ValueError('报告清单缺少确认区间')
        previous_end = -1
        for interval in intervals:
            if (not isinstance(interval, list) or len(interval) != 2
                    or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in interval)
                    or interval[0] < 0 or interval[1] <= interval[0] or interval[0] < previous_end):
                raise ValueError('确认区间格式无效')
            previous_end = interval[1]
        human = item['human_confirmation']
        if human is not None:
            if (not isinstance(human, dict) or set(human) != {'annotation_sha256', 'selected_event_ids'}
                    or not _sha(human['annotation_sha256']) or not isinstance(human['selected_event_ids'], list)
                    or not human['selected_event_ids']
                    or any(not isinstance(n, str) or not n or len(n) > 100 for n in human['selected_event_ids'])
                    or len(set(human['selected_event_ids'])) != len(human['selected_event_ids'])):
                raise ValueError('人工确认来源格式无效')
            human = dict(human, selected_event_ids=sorted(human['selected_event_ids']))
        result.append(dict(item, human_confirmation=human))
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


class LocalReportTools:
    def __init__(self, reviews, tracking, case_dir):
        self.reviews, self.tracking, self.case_dir = reviews, tracking, case_dir
        self.lock = threading.Lock()

    def _current(self, item):
        directory = self.case_dir(item['case_id'])
        case = self.reviews.get_case(directory)
        candidate = next((c for c in case['candidates'] if c['candidate_id'] == item['candidate_id']), None)
        if case['input_fingerprint'] != item['input_fingerprint'] or not candidate:
            raise ReportInputChanged('数据或候选已变化，请刷新核对')
        d = candidate['disposition']
        human = (d['status'] == 'human_reviewed' and d.get('policy_id') == POLICY_ID
                 and d.get('human_decision') == 'confirmed_event' and bool(d.get('confirmed_intervals')))
        expected = {'annotation_sha256': d['annotation_sha256'], 'selected_event_ids': d['selected_event_ids']} if human else None
        if ((d['status'] != 'ai_confirmed_event' and not human)
                or d.get('confirmed_intervals') != item['confirmed_intervals']
                or expected != item['human_confirmation']):
            raise ReportInputChanged('事件确认或人工标注已变化，请刷新核对')
        return directory

    def _record(self, item, directory):
        record = self.reviews.get_investigation(directory, item['candidate_id'])
        if record['status'] not in READY or not record.get('report'):
            if record['status'] not in {'not_requested', 'stale'}:
                raise ValueError('已保存排查报告状态无效，未覆盖原文件')
            return record
        b = record['bundle']
        source = b['acceleration_provenance'].get('human_event_confirmation')
        human = item['human_confirmation']
        if (b['case_id'] != item['case_id'] or b['candidate_id'] != item['candidate_id']
                or b['review_fingerprint'] != item['input_fingerprint']
                or [e['confirmed_interval'] for e in b['events']] != item['confirmed_intervals']
                or (human is None and source is not None)
                or (human is not None and (not source or source.get('annotation_sha256') != human['annotation_sha256']
                    or source.get('selected_event_ids') != human['selected_event_ids']))):
            raise ReportInputChanged('报告确认来源已变化，请刷新核对')
        return record

    def generate(self, payload, known_ids):
        items = _validate(payload, known_ids)
        if not self.lock.acquire(blocking=False):
            raise ReviewBusyError('正在补齐本地报告，请稍后刷新')
        results = []
        try:
            for item in items:
                result = {key: item[key] for key in ('case_id', 'candidate_id')}
                try:
                    directory = self._current(item)
                    record = self._record(item, directory)
                    if record.get('report') and record['status'] in READY:
                        result.update(status='reused', message='已有有效报告，直接复用')
                    else:
                        # This path is deliberately local-only; request fields
                        # cannot select a model or replace a valid cached report.
                        record = self.reviews.investigate(directory, item['candidate_id'], item['input_fingerprint'],
                                                          False, item['human_confirmation'])
                        self._current(item)
                        if record.get('report') and record['status'] in READY:
                            record = self._record(item, directory)
                        if not record.get('report') or record['status'] not in READY:
                            raise ReportInputChanged('生成期间证据已变化，请刷新核对')
                        result.update(status='ready', message='报告已就绪')
                    result['report_status'] = record['status']
                    result['report_fingerprint'] = record['bundle']['input_fingerprint']
                except ReportInputChanged as error:
                    result.update(status='changed', message=str(error))
                except ReviewBusyError:
                    result.update(status='busy', message='已有复核或保存请求运行，稍后重试')
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                    result.update(status='error', message=str(error))
                results.append(result)
        finally:
            self.lock.release()
        return {'version': VERSION, 'new_model_calls': 0, 'results': results,
                'summary': {status: sum(r['status'] == status for r in results)
                            for status in ('ready', 'reused', 'changed', 'busy', 'error')}}

    def archive(self, payload, known_ids):
        items = _validate(payload, known_ids, exporting=True)
        manifest = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
                    'scope': 'daily_workbench_saved_reports', 'new_model_calls': 0,
                    'requested': len(items), 'included': [], 'skipped': [],
                    'interpretation': 'Current valid reports and explicitly saved human notes only. '
                        'No model calls or report generation. Case-by-case snapshot, not root-cause proof or accuracy evaluation.'}
        manifest['viewer'] = {'path': 'index.html', 'version': VIEWER_VERSION, 'mode': 'read_only_snapshot'}
        viewer_reports = []
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for item in items:
                identity = {key: item[key] for key in ('case_id', 'candidate_id')}
                try:
                    directory = self._current(item)
                    record = self._record(item, directory)
                    if not record.get('report') or record['status'] not in READY:
                        raise ReportInputChanged('报告缺失或失效，请刷新核对')
                    if record['bundle']['input_fingerprint'] != item['report_fingerprint']:
                        raise ReportInputChanged('报告证据已更新，请刷新核对')
                    model_events = {e['event_id']: e['prioritized_checks']
                                    for e in (record.get('model_output') or {}).get('events', [])}
                    context = dict(record, display_checks={e['event_id']: model_events.get(
                        e['event_id'], default_investigation_checks(e)) for e in record['bundle']['events']})
                    from app.investigation_summary import briefings
                    from app.event_investigation import build_findings
                    context['briefings'] = briefings(record['bundle'], context['display_checks'])
                    context['findings'] = {e['event_id']: build_findings(e) for e in record['bundle']['events']}
                    tracking, tracking_error = None, None
                    try:
                        tracking = self.tracking.get(context)
                    except (ValueError, OSError, KeyError, TypeError) as error:
                        tracking_error = str(error)
                    # Serialize both documents before adding either ZIP member.
                    markdown = record['report'] + (tracking['report_appendix'] if tracking else '')
                    data = _json({'status': record['status'], 'created_at': record.get('created_at'),
                                  'bundle': record['bundle'], 'model_output': record.get('model_output'),
                                  'human_tracking': tracking, 'tracking_error': tracking_error,
                                  'briefings':context['briefings'], 'findings':context['findings']})
                    self._current(item)
                    current = self._record(item, directory)
                    if (not current.get('report') or current['status'] not in READY
                            or current['bundle']['input_fingerprint'] != item['report_fingerprint']):
                        raise ReportInputChanged('打包期间报告证据已变化，请刷新核对')
                    prefix = 'reports/' + item['case_id'] + '/' + item['candidate_id']
                    archive.writestr(prefix + '.md', markdown)
                    archive.writestr(prefix + '.json', data)
                    included = {**identity, 'input_fingerprint': item['input_fingerprint'],
                        'report_fingerprint': item['report_fingerprint'], 'human_confirmation': item['human_confirmation'],
                        'report_status': record['status'], 'tracking_saved': bool(tracking and tracking['persisted']),
                        'tracking_updated_at': tracking['updated_at'] if tracking else None,
                        'tracking_error': tracking_error, 'markdown': prefix + '.md', 'data': prefix + '.json',
                        'markdown_sha256': hashlib.sha256(markdown.encode('utf-8')).hexdigest(),
                        'data_sha256': hashlib.sha256(data.encode('utf-8')).hexdigest()}
                    manifest['included'].append(included)
                    viewer_reports.append({'metadata': included, 'markdown': markdown, 'data_json': data,
                                           'display_checks': context['display_checks']})
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                    manifest['skipped'].append({**identity, 'reason': str(error)})
            if not manifest['included']:
                raise ValueError('没有可打包的有效报告，请刷新核对后重试')
            archive.writestr('index.html', render_offline_report(manifest, viewer_reports))
            archive.writestr('manifest.json', _json(manifest))
            archive.writestr('README.md', '# 日常排查报告包\n\n'
                '解压后用浏览器打开 index.html，即可离线查看报告、曲线、引用行和已保存核查记录，无需启动工作台。\n\n'
                'index.html 自带数据与绘图脚本，单独复制也可查看和下载文档；此页仅展示导出时的只读快照。\n\n'
                '每个候选含 Markdown 报告和 JSON 证据。manifest.json 记录纳入/跳过项目、确认来源、指纹和文档 SHA256。\n\n'
                '仅包含已保存的人工核查记录，不包含浏览器草稿。损坏核查记录保留异常说明，不覆盖原文件。\n\n'
                '缺失日志仍表示未知，检查建议和人工核查结果不能直接认定根因。打包没有调用模型。\n')
        return buffer.getvalue(), manifest
