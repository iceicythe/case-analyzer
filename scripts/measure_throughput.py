"""Measure import throughput on formula-generated synthetic batches.

Generates a temporary batch of synthetic cases (same formula family as
examples/), imports them through the normal import path, and reports rows and
bytes per second. The data is synthetic by construction; the numbers describe
pipeline throughput, not real road-test performance.

Usage:
    python scripts/measure_throughput.py [--cases 4] [--rows 100000] [--report out.json]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cli_import import run_import
from app.server import WorkbenchContext
from app.workspace import initialize

MAX_ROWS = 200000  # matches the per-file row limit in app.storage
SAMPLE = 0.1       # seconds, the fixed vehicle sampling grid


def _table(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


def synthetic_case(case_id: str, rows: int) -> dict:
    """One long cruise with a single hard-braking window in the middle."""
    vehicle, objects, planning, control = [], [], [], []
    distance = 60.0
    brake_start, brake_end = rows // 3, rows // 3 + 300
    for i in range(rows):
        t = i * SAMPLE
        braking = brake_start < i <= brake_end
        speed = 20.0 - 8.0 * (i - brake_start) / 300 if braking else 20.0
        acceleration = -8.0 / 30 if braking else 0.0
        brake = .6 if braking else 0
        vehicle.append([f'{t:.1f}', f'{speed:.3f}', f'{acceleration:.3f}', brake])
        # 简易跟车：间距收敛到目标车距，永不为负（快车速距大，减速自然接近）。
        desired = max(10.0, speed)
        relative = max(-12.0, min(6.0, (desired - distance) * 0.2))
        if i:
            step = relative * SAMPLE
            if distance + step < 5.0:
                step = 5.0 - distance
                relative = step / SAMPLE
            distance += step
        objects.append([f'{t:.1f}', 'target_a', f'{distance:.3f}', f'{relative:.3f}', .95])
        planning.append([f'{t:.1f}', f'{max(0, speed + acceleration * SAMPLE):.3f}', acceleration,
                         'FOLLOW_LEAD_VEHICLE' if braking else 'CRUISE'])
        control.append([f'{t:.1f}', brake, .15 if not braking else 0, acceleration])
    return {'case_id': case_id, 'source_type': 'synthetic',
            'source_name': '公式生成的吞吐测量数据；不是道路采集或闭环仿真',
            'acceleration_source': '由演示车速有限差分得到，不是独立传感器测量',
            'files': {
                'vehicle_state.csv': _table(['timestamp', 'speed_mps', 'acceleration_mps2', 'brake'], vehicle),
                'object_state.csv': _table(['timestamp', 'object_id', 'distance_m', 'relative_speed_mps', 'confidence'], objects),
                'planning_state.csv': _table(['timestamp', 'target_speed_mps', 'planned_acceleration_mps2', 'decision_reason'], planning),
                'control_state.csv': _table(['timestamp', 'brake_command', 'throttle_command', 'requested_acceleration_mps2'], control)}}


def write_batch(root: Path, cases: int, rows: int) -> int:
    total_bytes = 0
    for index in range(cases):
        case = synthetic_case(f'perf_{index:03d}', rows)
        directory = root / case['case_id']
        directory.mkdir(parents=True)
        for name, text in case['files'].items():
            path = directory / name
            path.write_text(text, encoding='utf-8', newline='\n')
            total_bytes += path.stat().st_size
    return total_bytes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Measure import throughput on synthetic batches')
    parser.add_argument('--cases', type=int, default=4, help='合成 Case 条数，默认 4')
    parser.add_argument('--rows', type=int, default=100000, help=f'每条 Case 的行数，默认 100000，上限 {MAX_ROWS}')
    parser.add_argument('--report', type=Path, help='把 JSON 报告写入指定文件')
    args = parser.parse_args(argv)
    if not 1 <= args.rows <= MAX_ROWS:
        print(f'行数必须在 1～{MAX_ROWS} 之间', file=sys.stderr)
        return 2
    if args.cases < 1:
        print('Case 条数至少为 1', file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix='case-perf-') as temporary:
        batch = Path(temporary) / 'batch'
        runtime = Path(temporary) / 'runtime'
        total_bytes = write_batch(batch, args.cases, args.rows)
        initialize(runtime)  # 建好 index/data 等目录，与 CLI 导入同一条路径
        context = WorkbenchContext(batch_dir=runtime / 'index', data_root=runtime / 'data',
                                   annotations_root=runtime / 'annotations',
                                   evaluation_output=runtime / 'evaluations',
                                   model_reviews_root=runtime / 'model_reviews', automatic_review=False)
        began = time.perf_counter()
        summary = run_import(batch, context, 'synthetic', '吞吐测量批次（公式合成）', '由车速差分得到')
        elapsed = time.perf_counter() - began
        failed = [item for item in summary['cases'] if item['status'] in ('error', 'invalid')]

    total_rows = args.cases * args.rows * 4
    imported = summary['totals']['imported']
    report = {'tool': 'measure-throughput-1.0', 'started_at': started,
              'cases': args.cases, 'rows_per_case': args.rows, 'files_per_case': 4,
              'imported': imported, 'failed': summary['totals']['failed'],
              'elapsed_seconds': round(elapsed, 3),
              'rows_per_second': int(total_rows / elapsed) if elapsed else None,
              'mb_per_second': round(total_bytes / 1048576 / elapsed, 2) if elapsed else None,
              'source': 'synthetic（公式生成，非道路采集）'}
    print(f'导入 {imported}/{args.cases} 条 Case（每条 {args.rows} 行 × 4 个文件，'
          f'共 {total_rows} 行 / {total_bytes / 1048576:.1f} MB）')
    print(f'耗时 {elapsed:.2f} s · {report["rows_per_second"]} 行/秒 · {report["mb_per_second"]} MB/秒')
    for item in failed[:5]:
        print(f"失败 {item['case_id']}: {item['message']}", file=sys.stderr)
    if failed:
        print('有用例失败，吞吐数字不代表正常路径', file=sys.stderr)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('报告已写入：%s' % args.report)
    return 1 if summary['totals']['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
