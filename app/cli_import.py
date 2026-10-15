"""Batch import cases from a directory tree through the same path as the browser.

The browser import sends one case per HTTP request; this module walks a
directory tree once and feeds each case to the same storage functions
(app.storage.inspect_import / app.storage.import_case), so batch and browser
imports share one set of write rules: identical content is skipped, conflicting
ids are never overwritten, and every case reports its own outcome.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.notify import import_card, resolve_webhook, send_card
from app.paths import DEFAULT_RUNTIME
from app.server import WorkbenchContext
from app.storage import FILES, MAX_BYTES, import_case, inspect_import
from app.workspace import initialize

VERSION = 'cli-import-1.0'
SOURCE_TYPES = ('recorded', 'synthetic', 'unknown')


def read_case(directory: Path) -> dict:
    """Read the normalized CSV files of one case directory as text."""
    files = {}
    for name in FILES:
        path = directory / name
        if not path.is_file():
            continue
        files[name] = path.read_text(encoding='utf-8-sig')
    return files


def collect_cases(root: Path) -> tuple[list[tuple[Path, str]], list[dict]]:
    """Walk one level of subdirectories; each directory is one case.

    Returns (candidates, problems). A directory without vehicle_state.csv is
    reported as a problem instead of being silently dropped, and a case whose
    files exceed the size limit is reported before any read is attempted.
    """
    root = Path(root)
    candidates: list[tuple[Path, str]] = []
    problems: list[dict] = []
    if not root.is_dir():
        raise ValueError(f'目录不存在：{root}')
    directories = [root] if any((root / name).is_file() for name in FILES) else sorted(
        item for item in root.iterdir() if item.is_dir())
    for directory in directories:
        case_id = directory.name
        missing = [name for name in FILES if not (directory / name).is_file()]
        if 'vehicle_state.csv' in missing:
            problems.append({'case_id': case_id, 'status': 'invalid',
                             'message': '缺少 vehicle_state.csv，未导入（目录：%s）' % directory})
            continue
        if missing:
            problems.append({'case_id': case_id, 'status': 'partial',
                             'message': '可选日志缺失：%s；不影响导入' % ', '.join(missing)})
        total = sum((directory / name).stat().st_size for name in FILES if (directory / name).is_file())
        if total > MAX_BYTES:
            problems.append({'case_id': case_id, 'status': 'error',
                             'message': '文件总计 %.1f MB，超过单 Case %.0f MB 上限，请拆分后重试'
                                        % (total / 1048576, MAX_BYTES / 1048576)})
            continue
        candidates.append((directory, case_id))
    if not directories:
        problems.append({'case_id': '', 'status': 'invalid', 'message': f'目录为空：{root}'})
    return candidates, problems


def run_import(root: Path, context: WorkbenchContext, source_type: str = 'recorded',
               source_name: str = '', acceleration_source: str = '',
               dry_run: bool = False) -> dict:
    """Validate and import every case directory; one failure never stops the batch."""
    started = datetime.now(timezone.utc).isoformat()
    candidates, results = collect_cases(root)
    source_name = source_name or Path(root).name
    for directory, case_id in candidates:
        payload = {'case_id': case_id, 'source_type': source_type, 'source_name': source_name,
                   'acceleration_source': acceleration_source, 'files': read_case(directory)}
        try:
            checked = inspect_import(context, payload)
            if dry_run or checked['status'] == 'skipped':
                results.append({'case_id': case_id, 'status': checked['status'],
                                'message': checked['message'] + ('（预检，未写入）' if dry_run else ''),
                                'files': checked['files'], 'missing_files': checked['missing_files']})
                continue
            if checked['status'] == 'conflict':
                results.append({'case_id': case_id, 'status': 'conflict', 'message': checked['message']})
                continue
            imported = import_case(context, payload)
            results.append({'case_id': case_id, 'status': 'imported',
                            'message': '已导入', 'files': imported.get('files', {})})
        except (ValueError, OSError) as error:
            results.append({'case_id': case_id, 'status': 'error', 'message': str(error)})
    totals = {status: sum(1 for item in results if item['status'] == status)
              for status in ('imported', 'skipped', 'conflict', 'error', 'invalid', 'partial', 'ready')}
    return {'tool': VERSION, 'started_at': started, 'root': str(Path(root).resolve()),
            'dry_run': dry_run, 'source': {'type': source_type, 'name': source_name,
                                           'acceleration_source': acceleration_source},
            'cases': results, 'totals': dict(totals, total=len(results))}


def format_report(summary: dict) -> str:
    """One line per case plus a final tally; errors stay visible in plain text."""
    lines = []
    for item in summary['cases']:
        files = item.get('files') or {}
        detail = ' · '.join('%s %s 行' % (name.replace('_state.csv', ''), stats.get('rows'))
                            for name, stats in files.items() if isinstance(stats, dict))
        lines.append('%-16s %-9s %s%s' % (item['case_id'] or '-', item['status'],
                                          item['message'], ('（' + detail + '）') if detail else ''))
    totals = summary['totals']
    if summary['dry_run']:
        lines.append('-' * 60)
        lines.append('可导入 %(ready)d · 内容相同跳过 %(skipped)d · 编号冲突 %(conflict)d · 失败 %(error)d · 预检模式未写入'
                     % totals)
    else:
        lines.append('-' * 60)
        lines.append('已导入 %(imported)d · 内容相同跳过 %(skipped)d · 编号冲突 %(conflict)d · 失败 %(error)d'
                     % totals)
    return '\n'.join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m app import', description='从目录树批量导入 Case，与浏览器导入同规则')
    parser.add_argument('directory', type=Path, help='包含 Case 子目录的目录（或单个 Case 目录）')
    parser.add_argument('--runtime', type=Path, default=DEFAULT_RUNTIME, help='运行数据目录，默认 runtime/')
    parser.add_argument('--source-type', choices=SOURCE_TYPES, default='recorded',
                        help='数据来源类型，默认 recorded（道路/仿真平台采集）')
    parser.add_argument('--source-name', default='', help='路线/批次名称，默认取目录名')
    parser.add_argument('--acceleration-source', default='', help='加速度来源说明（选填）')
    parser.add_argument('--dry-run', action='store_true', help='只校验不写入')
    parser.add_argument('--report', type=Path, help='把 JSON 汇总写入指定文件')
    parser.add_argument('--notify', default='', help='飞书机器人 webhook；默认读环境变量 FEISHU_WEBHOOK_URL，留空不推送')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    runtime = initialize(args.runtime)
    context = WorkbenchContext(batch_dir=runtime / 'index', data_root=runtime / 'data',
                               annotations_root=runtime / 'annotations',
                               evaluation_output=runtime / 'evaluations',
                               model_reviews_root=runtime / 'model_reviews', automatic_review=False)
    webhook = resolve_webhook(args.notify)
    try:
        summary = run_import(args.directory, context, args.source_type, args.source_name,
                             args.acceleration_source, args.dry_run)
    except (ValueError, OSError) as error:
        print('导入未开始：%s' % error, file=sys.stderr)
        warning = send_card(webhook, '批量导入未开始', ['错误：%s' % error], 'case-analyzer 批量导入', 'red')
        if warning:
            print(warning, file=sys.stderr)
        return 2
    print(format_report(summary))
    if args.report:
        args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('汇总已写入：%s' % args.report)
    warning = send_card(webhook, *import_card(summary))
    if warning:
        print(warning, file=sys.stderr)
    return 1 if summary['totals']['error'] or summary['totals']['invalid'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
