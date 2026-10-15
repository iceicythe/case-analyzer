"""Local browser workbench for reviewing and labeling real driving cases."""
from __future__ import annotations
import argparse
import hashlib
import json
import mimetypes
import re
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
import pandas as pd
from app.annotation_assist import PROTOCOL_ID, build_annotation_assist
from app.paths import PROJECT_ROOT
from app.annotations import DEFAULT_ANNOTATIONS_ROOT, DEFAULT_REAL_CASES_ROOT, AnnotationValidationError, save_annotation
from app.workbench_reviews import WorkbenchReviewStore, ReviewBusyError
from app.human_event_review import HumanEventReviewStore, HumanConfirmationConflictError
from app.model_runner import ModelConfigurationError
from app.automatic_review import AutomaticReviewQueue, QueueStorageError
from app.event_investigation import default_investigation_checks
from app.investigation_tracking import InvestigationTrackingStore, TrackingConflictError
from app.workbench_overview import build_overview, build_review_queue, load_overview_index, review_task
from app.local_report_tools import LocalReportTools
from app.workspace import APP_VERSION, DEFAULT_WORKSPACE, load_workspace, workspace_identity, check_workspace, model_status
from app.evaluation import EvaluationStore, EvaluationConflict
DEFAULT_BATCH_DIR = PROJECT_ROOT / 'runtime/index'
DEFAULT_PORT = 8770
DEFAULT_EVALUATION_ROOT = PROJECT_ROOT / 'runtime/evaluations'
MAX_REQUEST_BYTES = 1000000
WORKBENCH_HTML = (PROJECT_ROOT / 'static').joinpath('annotation_workbench.html')
DEFAULT_REVIEW_SUMMARY = None
EVIDENCE_VIEWER_JS = (PROJECT_ROOT / 'static').joinpath('evidence_viewer.js')
TRACKING_JS = (PROJECT_ROOT / 'static').joinpath('investigation_tracking.js')
OVERVIEW_HTML = (PROJECT_ROOT / 'static').joinpath('workbench_overview.html')
SYSTEM_ASSETS = {'/assets/import.js': ('import.js', 'application/javascript'), '/assets/system.css': ('system.css', 'text/css'), '/assets/system-shell.js': ('system_shell.js', 'application/javascript'), '/assets/system-overview.js': ('system_overview.js', 'application/javascript'), '/assets/case-workflow.js': ('case_workflow.js', 'application/javascript'), '/assets/workbench.css': ('workbench.css', 'text/css'), '/assets/workbench.js': ('workbench.js', 'application/javascript'), '/assets/favicon.svg': ('favicon.svg', 'image/svg+xml')}
REQUIRED_BATCH_COLUMNS = {'case_id'}

def investigation_payload(record: dict) -> dict:
    """Keep the displayed checklist identical to the rendered report."""
    if not record.get('report'):
        return record
    model_events = {event['event_id']: event['prioritized_checks'] for event in (record.get('model_output') or {}).get('events', [])}
    return dict(record, display_checks={event['event_id']: model_events.get(event['event_id'], default_investigation_checks(event)) for event in record['bundle']['events']})

def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'无法读取 JSON：{path}') from error
    if not isinstance(value, dict):
        raise ValueError(f'JSON 顶层必须是对象：{path}')
    return value

def _case_label(annotation: dict[str, Any]) -> str:
    label = str(annotation.get('case_label', '')).strip()
    if label:
        return label
    if annotation.get('annotation_status') == 'completed':
        return 'positive' if annotation.get('events') else 'negative'
    return 'uncertain'

def build_workbench_data(batch_dir: Path=DEFAULT_BATCH_DIR, data_root: Path=DEFAULT_REAL_CASES_ROOT, annotations_root: Path=DEFAULT_ANNOTATIONS_ROOT) -> dict[str, Any]:
    """Build the read-only payload consumed by the browser application."""
    summary_path = batch_dir / 'summary.csv'
    if not summary_path.is_file():
        raise ValueError(f'找不到批量分析汇总：{summary_path}')
    summary = pd.read_csv(summary_path, dtype={'case_id': str}, keep_default_na=False)
    missing = REQUIRED_BATCH_COLUMNS - set(summary.columns)
    if missing:
        raise ValueError(f'summary.csv 缺少字段：{sorted(missing)}')
    if summary['case_id'].duplicated().any():
        duplicates = summary.loc[summary['case_id'].duplicated(), 'case_id'].tolist()
        raise ValueError(f'summary.csv 存在重复 Case：{duplicates}')
    cases: list[dict[str, Any]] = []
    for raw in summary.to_dict(orient='records'):
        case_id = str(raw['case_id'])
        case_dir = data_root / case_id
        annotation_path = annotations_root / f'{case_id}.json'
        read_error = None
        try:
            if not (case_dir / 'vehicle_state.csv').is_file():
                raise ValueError('车辆日志无法读取，请检查此 Case 的数据文件')
            annotation = _read_json(annotation_path)
            if not isinstance(annotation.get('events'), list):
                raise ValueError('人工标注的 events 格式无效')
            if annotation.get('annotation_status') not in {'pending', 'completed'}:
                raise ValueError('人工标注状态无效')
        except (OSError, ValueError, TypeError):
            # Keep membership available so one damaged case cannot block the
            # browser bootstrap or saving an unrelated, healthy annotation.
            read_error = '此 Case 的数据或人工标注无法读取，请检查本地文件'
            annotation = {'annotation_status': 'pending', 'case_label': 'uncertain', 'events': []}
        completed = annotation.get('annotation_status') == 'completed'
        cases.append({'case_id': case_id, 'source_route': str(raw.get('source_route', '')), 'source_segment': str(raw.get('source_segment', '')), 'annotation_status': annotation.get('annotation_status', 'pending'), 'case_label': _case_label(annotation), 'annotated_event_count': len(annotation.get('events', [])), 'annotator': annotation.get('annotator', ''), 'completed': completed, 'annotation_protocol_id': annotation.get('annotation_protocol_id'), 'requires_protocol_review': bool(annotation.get('annotation_protocol_id') and annotation['annotation_protocol_id'] != PROTOCOL_ID), 'read_error': read_error})
    completed_cases = sum((case['completed'] for case in cases))
    positive_cases = sum((case['completed'] and case['case_label'] == 'positive' for case in cases))
    negative_cases = sum((case['completed'] and case['case_label'] == 'negative' for case in cases))
    return {'practice_case_ids': [], 'annotation_protocol_id': PROTOCOL_ID, 'summary': {'case_count': len(cases), 'completed_count': completed_cases, 'pending_count': len(cases) - completed_cases, 'positive_count': positive_cases, 'negative_count': negative_cases, 'previous_protocol_completed_count': sum((c['completed'] and c['requires_protocol_review'] for c in cases))}, 'cases': cases}

@dataclass(frozen=True)
class WorkbenchContext:
    batch_dir: Path
    data_root: Path
    annotations_root: Path
    evaluation_output: Path
    review_summary: Path | None = DEFAULT_REVIEW_SUMMARY
    model_reviews_root: Path | None = None
    automatic_review: bool = False
    workspace_id: str = 'custom'
    workspace_name: str = '本地工作空间'

    def case_dir(self, case_id: str) -> Path:
        if not case_id or Path(case_id).name != case_id:
            raise ValueError('Case ID 非法')
        if case_id not in {item['case_id'] for item in load_overview_index(self.batch_dir)}:
            raise ValueError('Case 不在当前工作空间')
        case_dir = (self.data_root / case_id).resolve()
        if case_dir.parent != self.data_root.resolve():
            raise ValueError('Case 路径越界')
        return case_dir


def _make_handler(context: WorkbenchContext) -> type[BaseHTTPRequestHandler]:
    evaluation = EvaluationStore(context)
    reviews = HumanEventReviewStore(context.review_summary, context.model_reviews_root or context.evaluation_output.parent / 'workbench_model_reviews', annotations_root=context.annotations_root, manual_mode=not model_status()['ready'])
    indexed_ids = {item['case_id'] for item in load_overview_index(context.batch_dir)}

    def queue_case_dir(case_id):
        if case_id not in indexed_ids:
            raise ValueError('Case 不在当前任务清单')
        return context.case_dir(case_id)
    queue_scope = hashlib.sha256(str((context.batch_dir.resolve(), context.data_root.resolve(), context.annotations_root.resolve())).encode()).hexdigest()
    automatic = AutomaticReviewQueue(reviews, reviews.output_root / 'automatic_queue.json', queue_case_dir, queue_scope, autostart=False) if context.automatic_review else None
    tracking = InvestigationTrackingStore(reviews.output_root)
    overview_lock = threading.Lock()
    queue_lock = threading.Lock()
    annotation_lock = threading.Lock()

    def annotation_payload(case_id):
        content = (context.annotations_root / f'{case_id}.json').read_bytes()
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError('人工标注格式无效')
        value['_revision'] = hashlib.sha256(content).hexdigest()
        return value
    report_tools = LocalReportTools(reviews, tracking, context.case_dir)

    def with_tracking(record):
        payload = investigation_payload(record)
        if payload.get('report'):
            try:
                payload['tracking'] = tracking.get(payload)
            except (ValueError, OSError):
                payload['tracking'] = None
                payload['tracking_error'] = '排查记录无法读取，原文件未覆盖；仍可查看事件证据。'
        return payload

    def workbench_data():
        data = build_workbench_data(context.batch_dir, context.data_root, context.annotations_root)
        data['workspace'] = workspace_identity(context)
        data['automatic_review'] = context.automatic_review
        data['model'] = model_status()
        data['blind_annotation'] = False
        known = reviews.saved_case_ids()
        for case in data['cases']:
            case['has_model_record'] = case['case_id'] in known
        return data

    class WorkbenchHandler(BaseHTTPRequestHandler):
        server_version = 'CaseAnalyzer/' + APP_VERSION

        def log_message(self, format_string: str, *args: Any) -> None:
            print(f'[标注工作台] {self.address_string()} - {format_string % args}')

        def _json(self, value: Any, status: int=HTTPStatus.OK) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

        def _error(self, status: int, message: str) -> None:
            self._json({'ok': False, 'error': message}, status)

        def _mutation_allowed(self):
            host = self.headers.get('Host', '')
            allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            origin = self.headers.get('Origin')
            if host not in allowed or (origin and origin != 'http://' + host):
                self._error(HTTPStatus.FORBIDDEN, '仅接受本机工作台页面请求')
                return False
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, '请求需要 application/json')
                return False
            return True

        def _body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError as error:
                raise ValueError('Content-Length 非法') from error
            limit = 32 * 1024 * 1024 if urlparse(self.path).path == '/api/import' else MAX_REQUEST_BYTES
            if length <= 0 or length > limit:
                raise ValueError('请求体为空或超过大小限制')
            try:
                value = json.loads(self.rfile.read(length).decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError('请求体不是有效 JSON') from error
            if not isinstance(value, dict):
                raise ValueError('请求 JSON 顶层必须是对象')
            return value

        def do_GET(self) -> None:
            if self.headers.get('Host', '') not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}:
                self._error(HTTPStatus.FORBIDDEN, '仅接受本机工作台请求')
                return
            path = urlparse(self.path).path
            indexed_ids.update((item['case_id'] for item in load_overview_index(context.batch_dir)))
            try:
                if path == '/evaluation/' or (path=='/' and evaluation.enabled):
                    payload=(PROJECT_ROOT/'static/evaluation.html').read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type','text/html; charset=utf-8')
                    self.send_header('Content-Length',str(len(payload)))
                    self.send_header('Cache-Control','no-store')
                    self.end_headers(); self.wfile.write(payload)
                    return
                if path=='/assets/evaluation.js':
                    payload=(PROJECT_ROOT/'static/evaluation.js').read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type','application/javascript; charset=utf-8')
                    self.send_header('Content-Length',str(len(payload)))
                    self.send_header('Cache-Control','no-store')
                    self.end_headers(); self.wfile.write(payload)
                    return
                if path=='/api/evaluation':
                    self._json(evaluation.summary()); return
                if path.startswith('/api/evaluation/case/'):
                    self._json(evaluation.case(unquote(path.removeprefix('/api/evaluation/case/')))); return
                if path.startswith('/api/evaluation/report/'):
                    self._json(evaluation.report(unquote(path.removeprefix('/api/evaluation/report/')))); return
                if (evaluation.enabled and not evaluation.state()['unblinded'] and path.startswith('/api/')
                        and path not in {'/api/health','/api/system'}):
                    self._error(HTTPStatus.FORBIDDEN,'请先完成独立事件核对，再查看系统结果'); return
                if path in {'/workbench', '/reports', '/overview'}:
                    self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
                    query = urlparse(self.path).query
                    self.send_header('Location', path + '/' + ('?' + query if query else ''))
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if path == '/api/health':
                    self._json({'status': 'ok', 'service': 'case-analyzer', 'version': APP_VERSION})
                    return
                if path == '/api/system':
                    self._json({'version': APP_VERSION, 'workspace': workspace_identity(context), 'protocol': PROTOCOL_ID, 'case_count': len(indexed_ids), 'automatic_review': automatic is not None, 'model': model_status(), 'evaluation': {'enabled':evaluation.enabled,'unblinded':evaluation.state()['unblinded'] if evaluation.enabled else False}})
                    return
                if path == '/' or path == '/workbench/':
                    legacy_case = bool(parse_qs(urlparse(self.path).query).get('case'))
                    page = WORKBENCH_HTML if path == '/workbench/' or legacy_case else OVERVIEW_HTML
                    if path == '/workbench/' and (not indexed_ids):
                        page = PROJECT_ROOT / 'static/empty_workbench.html'
                    payload = page.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if path == '/import/':
                    payload = (PROJECT_ROOT / 'static/import.html').read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if path == '/api/data':
                    self._json(workbench_data())
                    return
                if path in {'/overview/', '/reports/'}:
                    payload = OVERVIEW_HTML.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if path in SYSTEM_ASSETS:
                    filename, content_type = SYSTEM_ASSETS[path]
                    payload = (PROJECT_ROOT / 'static').joinpath(filename).read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', content_type + '; charset=utf-8')
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if path == '/api/overview':
                    if not overview_lock.acquire(blocking=False):
                        self._error(HTTPStatus.CONFLICT, '正在核对处理概览，请稍后刷新')
                        return
                    try:
                        items = load_overview_index(context.batch_dir)
                        self._json(build_overview(items, context.case_dir, reviews, tracking))
                    finally:
                        overview_lock.release()
                    return
                if path == '/api/review-queue':
                    if not queue_lock.acquire(blocking=False):
                        self._error(HTTPStatus.CONFLICT, '任务状态正在刷新，请稍后重试')
                        return
                    try:
                        self._json(build_review_queue(load_overview_index(context.batch_dir), context.case_dir, reviews))
                    finally:
                        queue_lock.release()
                    return
                if path in {'/assets/evidence-viewer.js', '/assets/investigation-tracking.js'}:
                    payload = (EVIDENCE_VIEWER_JS if path.endswith('evidence-viewer.js') else TRACKING_JS).read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'application/javascript; charset=utf-8')
                    self.send_header('Content-Length', str(len(payload)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if path == '/api/automatic-review':
                    self._json(automatic.snapshot() if automatic else {'enabled': False})
                    return
                if path.startswith('/api/investigation/'):
                    parts = unquote(path.removeprefix('/api/investigation/')).split('/')
                    if len(parts) != 2:
                        raise ValueError('事件排查路径非法')
                    self._json(with_tracking(reviews.get_investigation(context.case_dir(parts[0]), parts[1])))
                    return
                if path.startswith('/api/investigation-tracking/'):
                    parts = unquote(path.removeprefix('/api/investigation-tracking/')).split('/')
                    if len(parts) != 2:
                        raise ValueError('排查记录路径非法')
                    self._json(tracking.get(investigation_payload(reviews.get_investigation(context.case_dir(parts[0]), parts[1]))))
                    return
                if path.startswith('/api/candidates/'):
                    case_id = unquote(path.removeprefix('/api/candidates/'))
                    result = reviews.get_case(context.case_dir(case_id))
                    result['workflow'] = review_task(result)
                    result['automatic_review'] = automatic.snapshot(case_id) if automatic else None
                    self._json(result)
                    return
                if path.startswith('/api/annotation/'):
                    case_id = unquote(path.removeprefix('/api/annotation/'))
                    context.case_dir(case_id)
                    self._json(annotation_payload(case_id))
                    return
                if path.startswith('/api/assist/'):
                    case_id = unquote(path.removeprefix('/api/assist/'))
                    assist = build_annotation_assist(context.case_dir(case_id))
                    self._json(assist)
                    return
                self._error(HTTPStatus.NOT_FOUND, '接口不存在')
            except FileNotFoundError:
                self._error(HTTPStatus.NOT_FOUND, '文件不存在')
            except (TrackingConflictError, EvaluationConflict) as error:
                self._error(HTTPStatus.CONFLICT, str(error))
            except (AnnotationValidationError, ValueError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except OSError as error:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def do_PUT(self) -> None:
            if not self._mutation_allowed():
                return
            if evaluation.enabled:
                self._error(HTTPStatus.CONFLICT,'评估空间请使用独立核对页面，日常标注不参与本批次评分'); return
            with annotation_lock:
                self._put_annotation()

        def _put_annotation(self) -> None:
            path = urlparse(self.path).path
            if not path.startswith('/api/annotation/'):
                self._error(HTTPStatus.NOT_FOUND, '接口不存在')
                return
            try:
                case_id = unquote(path.removeprefix('/api/annotation/'))
                case_dir = context.case_dir(case_id)
                updates = self._body()
                current = annotation_payload(case_id)
                if '_expected_revision' in updates and updates['_expected_revision'] != current['_revision']:
                    self._json({'error': '人工标注已在其他页面更新，本页内容已保留，请先读取最新标注', 'code': 'annotation_conflict'}, HTTPStatus.CONFLICT)
                    return
                protocol_case = str(current.get('annotation_protocol_id', '')).startswith('SD-WINDOW-')
                metadata_path = case_dir / 'source_metadata.json'
                if metadata_path.is_file():
                    protocol_case = protocol_case or _read_json(metadata_path).get('schema') == 'case-import-1.0'
                confirmed_protocol_id = None
                if updates.get('annotation_status') == 'completed' and protocol_case:
                    confirmation = updates.get('review_confirmation') or {}
                    if not isinstance(confirmation, dict) or confirmation.get('whole_case_reviewed') is not True:
                        raise AnnotationValidationError('请先确认已查看整段数据并核对参考区间')
                    if confirmation.get('protocol_id') != PROTOCOL_ID:
                        raise AnnotationValidationError('口径已变化，请按当前 ' + PROTOCOL_ID + ' 重新核查并确认')
                    assist = build_annotation_assist(case_dir)
                    if not assist['quality']['can_complete']:
                        raise AnnotationValidationError('数据质量尚未确认，请保存为待标注：' + '；'.join(assist['quality']['issues']))
                    source = assist['quality']['original_can']
                    expected = {'vehicle_sha256': assist['vehicle_sha256'], 'protocol_sha256': assist['protocol_sha256'], 'metadata_sha256': source['metadata_sha256'], 'original_can_sha256': source['original_can_sha256']}
                    if any((confirmation.get(key) != value for key, value in expected.items())):
                        raise AnnotationValidationError('数据或口径已变化，请重新打开 Case 核对后确认')
                    if assist['quality']['boundary_review_required'] and confirmation.get('boundary_reviewed') is not True:
                        raise AnnotationValidationError('片段边界存在持续减速，请结合相邻片段确认后再完成')
                    confirmation = {'protocol_id': PROTOCOL_ID, 'whole_case_reviewed': True, 'boundary_reviewed': confirmation.get('boundary_reviewed') is True, 'vehicle_sha256': assist['vehicle_sha256'], 'protocol_sha256': assist['protocol_sha256'], 'metadata_sha256': source['metadata_sha256'], 'original_can_sha256': source['original_can_sha256'], 'confirmed_at': updates.get('annotated_at', '')}
                    updates['review_confirmation'] = confirmation
                    confirmed_protocol_id = PROTOCOL_ID
                annotation = save_annotation(case_dir, context.annotations_root, updates, confirmed_protocol_id=confirmed_protocol_id)
                annotation = annotation_payload(case_id)
                payload = build_workbench_data(context.batch_dir, context.data_root, context.annotations_root)
                self._json({'ok': True, 'annotation': annotation, 'summary': payload['summary']})
            except (AnnotationValidationError, ValueError) as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except OSError as error:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def do_POST(self) -> None:
            if not self._mutation_allowed():
                return
            indexed_ids.update((item['case_id'] for item in load_overview_index(context.batch_dir)))
            path = urlparse(self.path).path
            if path.startswith('/api/evaluation/'):
                try:
                    body=self._body()
                    if path=='/api/evaluation/unblind':
                        if body.get('confirm') is not True:
                            raise ValueError('请明确开启结果对照')
                        self._json(evaluation.unblind())
                    elif path.startswith('/api/evaluation/label/'):
                        self._json(evaluation.save_label(unquote(path.removeprefix('/api/evaluation/label/')),body))
                    elif path.startswith('/api/evaluation/rating/'):
                        self._json(evaluation.save_rating(unquote(path.removeprefix('/api/evaluation/rating/')),body))
                    else:
                        self._error(HTTPStatus.NOT_FOUND,'评估接口不存在')
                except EvaluationConflict as error:
                    self._error(HTTPStatus.CONFLICT,str(error))
                except ValueError as error:
                    self._error(HTTPStatus.BAD_REQUEST,str(error))
                except OSError:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR,'评估记录无法保存，请检查本地目录')
                return
            if evaluation.enabled and path!='/api/local-reports/archive':
                self._error(HTTPStatus.CONFLICT,'评估模型结果已冻结，请在日常工作空间运行新任务'); return
            if evaluation.enabled and not evaluation.state()['unblinded']:
                self._error(HTTPStatus.FORBIDDEN,'先完成独立核对，再查看报告'); return
            if path == '/api/import':
                try:
                    from app.storage import import_case
                    result = import_case(context, self._body())
                    indexed_ids.add(result['case_id'])
                    self._json(result, HTTPStatus.CREATED)
                except ValueError as error:
                    self._error(HTTPStatus.BAD_REQUEST, str(error))
                except OSError:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, '导入未完成，请检查磁盘和目录权限')
                return
            if path in {'/api/local-reports/complete', '/api/local-reports/archive'}:
                try:
                    body = self._body()
                    known = {item['case_id'] for item in load_overview_index(context.batch_dir)}
                    if path.endswith('/complete'):
                        self._json(report_tools.generate(body, known))
                    else:
                        payload, manifest = report_tools.archive(body, known)
                        filename = 'case-reports-' + manifest['created_at'][:19].replace(':', '-').replace('T', '-') + '.zip'
                        self.send_response(HTTPStatus.OK)
                        self.send_header('Content-Type', 'application/zip')
                        self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
                        self.send_header('Content-Length', str(len(payload)))
                        self.send_header('X-Report-Included', str(len(manifest['included'])))
                        self.send_header('X-Report-Skipped', str(len(manifest['skipped'])))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        self.wfile.write(payload)
                except ReviewBusyError as error:
                    self._error(HTTPStatus.CONFLICT, str(error))
                except ValueError as error:
                    self._error(HTTPStatus.BAD_REQUEST, str(error))
                except OSError:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, '报告无法读取或保存，请检查本地目录')
                return
            if path.startswith('/api/investigation-tracking/'):
                locked = False
                try:
                    parts = unquote(path.removeprefix('/api/investigation-tracking/')).split('/')
                    if len(parts) != 2:
                        raise ValueError('排查记录路径非法')
                    case_dir = context.case_dir(parts[0])
                    body = self._body()
                    locked = reviews.lock.acquire(blocking=False)
                    if not locked:
                        raise ReviewBusyError('已有请求正在运行，请稍后保存')

                    def current_record():
                        return investigation_payload(reviews.get_investigation(case_dir, parts[1]))
                    self._json(tracking.save(current_record(), body, current_record))
                except (TrackingConflictError, ReviewBusyError) as error:
                    self._error(HTTPStatus.CONFLICT, str(error))
                except ValueError as error:
                    self._error(HTTPStatus.BAD_REQUEST, str(error))
                except OSError:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, '排查记录无法读取或保存，请检查本地目录')
                finally:
                    if locked:
                        reviews.lock.release()
                return
            if path == '/api/automatic-review' or path.startswith('/api/automatic-review/'):
                try:
                    if automatic is None:
                        raise ValueError('自动复核未启用')
                    body = self._body()
                    if path == '/api/automatic-review/batch':
                        items, retry = (body.get('items'), body.get('retry', False))
                        if not isinstance(items, list) or not 1 <= len(items) <= 1000 or type(retry) is not bool or any((not isinstance(item, dict) or not isinstance(item.get('case_id'), str) or item['case_id'] not in indexed_ids or (not isinstance(item.get('input_fingerprint'), str)) or (not re.fullmatch('[a-f0-9]{64}', item['input_fingerprint'])) for item in items)) or (len({item['case_id'] for item in items}) != len(items)):
                            raise ValueError('批量任务清单无效')
                        results = []
                        for item in items:
                            try:
                                directory = queue_case_dir(item['case_id'])
                                case = reviews.get_case(directory)
                                if case['input_fingerprint'] != item['input_fingerprint']:
                                    results.append({'case_id': item['case_id'], 'status': 'changed'})
                                    continue
                                current = automatic.enqueue(directory, case, retry)
                                job = current['jobs'][0] if current['jobs'] else None
                                results.append({'case_id': item['case_id'], 'status': job['status'] if job else 'no_pending'})
                            except QueueStorageError:
                                results.append({'case_id': item['case_id'], 'status': 'storage_error'})
                            except (OSError, ValueError, KeyError, TypeError):
                                results.append({'case_id': item['case_id'], 'status': 'read_error'})
                        self._json({'results': results, 'queue': automatic.snapshot()})
                    elif path == '/api/automatic-review':
                        if type(body.get('paused')) is not bool:
                            raise ValueError('paused 必须为布尔值')
                        automatic.pause(body['paused'])
                        self._json(automatic.snapshot())
                    else:
                        case_id = unquote(path.removeprefix('/api/automatic-review/'))
                        case_dir = queue_case_dir(case_id)
                        if not (context.annotations_root / (case_id + '.json')).is_file():
                            raise ValueError('Case 不在标注目录')
                        retry = body.get('retry', False)
                        if type(retry) is not bool:
                            raise ValueError('retry 必须为布尔值')
                        self._json(automatic.enqueue(case_dir, reviews.get_case(case_dir), retry))
                except QueueStorageError as error:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(error))
                except (ValueError, OSError):
                    self._error(HTTPStatus.BAD_REQUEST, '自动复核请求无效，请检查 Case 和参数')
                return
            if path.startswith('/api/investigation/'):
                try:
                    parts = unquote(path.removeprefix('/api/investigation/')).split('/')
                    if len(parts) != 2:
                        raise ValueError('事件排查路径非法')
                    body = self._body()
                    record = reviews.investigate(context.case_dir(parts[0]), parts[1], body.get('input_fingerprint', ''), body.get('use_model', False), body.get('human_confirmation'))
                    self._json({'ok': True, 'investigation': with_tracking(record)})
                except ReviewBusyError as error:
                    self._error(HTTPStatus.CONFLICT, str(error))
                except ModelConfigurationError:
                    self._error(HTTPStatus.BAD_REQUEST, '请检查本地模型配置')
                except ValueError as error:
                    self._error(HTTPStatus.BAD_REQUEST, str(error))
                except OSError:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, '排查记录无法读取或保存')
                return
            if path.startswith('/api/model-review/') or path.startswith('/api/resolve-candidate/'):
                try:
                    resolving = path.startswith('/api/resolve-candidate/')
                    parts = unquote(path.removeprefix('/api/resolve-candidate/' if resolving else '/api/model-review/')).split('/')
                    if len(parts) != 2:
                        raise ValueError('模型复核路径非法')
                    case_id, candidate_id = parts
                    body = self._body()
                    if resolving:
                        result = reviews.resolve_human(context.case_dir(case_id), candidate_id, body.get('input_fingerprint', ''), body.get('human_decision'), body.get('selected_event_ids'), body.get('annotation_sha256'), body.get('expected_record_fingerprint'))
                    else:
                        result = reviews.review(context.case_dir(case_id), candidate_id, body.get('input_fingerprint', ''))
                    result['workflow'] = review_task(result)
                    self._json({'ok': True, 'case': result})
                except HumanConfirmationConflictError as error:
                    self._json({'ok': False, 'error': str(error), 'code': 'human_confirmation_conflict'}, HTTPStatus.CONFLICT)
                except ReviewBusyError as error:
                    self._error(HTTPStatus.CONFLICT, str(error))
                except ModelConfigurationError:
                    self._error(HTTPStatus.BAD_REQUEST, '请检查 configs/deepseek.local.json 中的模型配置')
                except ValueError as error:
                    self._error(HTTPStatus.BAD_REQUEST, str(error))
                except OSError:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, '模型记录无法保存，请检查本地目录')
                return
            self._error(HTTPStatus.NOT_FOUND, '接口不存在')
    WorkbenchHandler.automatic_queue = automatic
    return WorkbenchHandler

def create_server(context: WorkbenchContext, port: int=DEFAULT_PORT) -> ThreadingHTTPServer:

    class WorkbenchServer(ThreadingHTTPServer):
        request_queue_size = 64

        def server_close(self):
            if self.RequestHandlerClass.automatic_queue:
                self.RequestHandlerClass.automatic_queue.close()
            super().server_close()
    handler = _make_handler(context)
    try:
        server = WorkbenchServer(('127.0.0.1', port), handler)
    except Exception:
        if handler.automatic_queue:
            handler.automatic_queue.close()
        raise
    if handler.automatic_queue:
        handler.automatic_queue.start()
    return server
