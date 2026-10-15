"""Initialize an empty workspace and describe local model availability."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from app.paths import PROJECT_ROOT, DEFAULT_RUNTIME

APP_VERSION = '0.1.0'
DEFAULT_WORKSPACE = PROJECT_ROOT / 'configs/workspace.json'


def load_workspace(path=DEFAULT_WORKSPACE):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or set(value) != {'version', 'name', 'port', 'automatic_review'}:
        raise ValueError('工作空间配置字段不正确')
    if value['version'] != 1 or not isinstance(value['name'], str) or not 1 <= len(value['name'].strip()) <= 60:
        raise ValueError('工作空间名称或版本无效')
    if type(value['port']) is not int or not 0 <= value['port'] <= 65535 or type(value['automatic_review']) is not bool:
        raise ValueError('端口或自动复核设置无效')
    return value


def initialize(runtime=DEFAULT_RUNTIME):
    runtime = Path(runtime).resolve()
    for name in ('data', 'index', 'annotations', 'model_reviews', 'evaluations'):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    index = runtime / 'index/summary.csv'
    if not index.exists():
        try:
            with index.open('x', encoding='utf-8') as stream:
                stream.write('case_id,source_route,source_segment\n')
        except FileExistsError:
            pass
    return runtime


def workspace_identity(context):
    root = str(context.data_root.parent.resolve())
    scope = hashlib.sha256(os.path.normcase(root).encode('utf-8')).hexdigest()[:24]
    return {'id': 'local', 'name': context.workspace_name, 'storage_scope': scope, 'allow_legacy_drafts': False}


def model_status():
    """Return readiness only; credentials are never included in HTTP responses."""
    from app.candidate_review import create_candidate_adapter
    from app.model_runner import ModelConfigurationError
    try:
        adapter = create_candidate_adapter()
        close = getattr(adapter.client, 'close', None)
        if close:
            close()
        return {'ready': True, 'message': 'AI 已配置'}
    except (ModelConfigurationError, OSError, ValueError):
        return {'ready': False, 'message': 'AI 未配置，可先导入数据并人工复核；配置后重启启用自动复核。'}


def check_workspace(context):
    from app.workbench_overview import load_overview_index
    checks = []
    try:
        items = load_overview_index(context.batch_dir)
        checks.append({'check': 'index', 'ok': True, 'detail': f'{len(items)} 个 Case；空工作空间可正常使用'})
    except (OSError, ValueError) as error:
        checks.append({'check': 'index', 'ok': False, 'detail': str(error)})
    for name in ('annotation_workbench.html', 'workbench_overview.html', 'system.css', 'import.html'):
        checks.append({'check': name, 'ok': (PROJECT_ROOT / 'static' / name).is_file()})
    return {'ok': all(item['ok'] for item in checks), 'workspace': workspace_identity(context), 'checks': checks}
