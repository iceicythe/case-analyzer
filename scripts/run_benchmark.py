"""Run a frozen evaluation benchmark end to end on a labeled dataset.

The script imports every case from a directory tree, asks the configured model
to review each screening candidate, freezes the batch, replays the human
labels, then unblinds and prints the metrics. Order matters: predictions are
frozen before labels are replayed, so a human answer can never steer the
model path. Labels that fail the data-quality gate are replayed as
`data_issue` instead of an event verdict, and the replay records why.

Usage:
    python scripts/run_benchmark.py --dataset <case 目录> --labels <标签目录> \
        --runtime <运行目录> [--reviewer 姓名] [--source-type synthetic]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.annotation_assist import build_annotation_assist
from app.cli_import import run_import
from app.evaluation import EvaluationStore, freeze
from app.human_event_review import HumanEventReviewStore
from app.notify import benchmark_card, resolve_webhook, send_card
from app.server import WorkbenchContext
from app.workspace import initialize


def build_context(runtime: Path) -> WorkbenchContext:
    return WorkbenchContext(batch_dir=runtime / 'index', data_root=runtime / 'data',
        annotations_root=runtime / 'annotations', evaluation_output=runtime / 'evaluations',
        model_reviews_root=runtime / 'model_reviews', automatic_review=False,
        workspace_id='local', workspace_name='脚本化效果评估')


def review_all(context: WorkbenchContext, case_ids: list[str]) -> dict:
    """Review every pending screening candidate with the configured model."""
    store = HumanEventReviewStore(context.review_summary, context.model_reviews_root,
                                  annotations_root=context.annotations_root, manual_mode=False)
    outcomes = {}
    for cid in case_ids:
        case_dir = context.case_dir(cid)
        case = store.get_case(case_dir)
        fingerprint = case['input_fingerprint']
        reviewed = failed = 0
        for candidate in case['candidates']:
            if candidate['disposition']['status'] != 'pending_ai':
                continue
            try:
                store.review(case_dir, candidate['candidate_id'], fingerprint)
                reviewed += 1
            except (ValueError, OSError) as error:
                failed += 1
                outcomes.setdefault('errors', []).append(f'{cid}/{candidate["candidate_id"]}: {error}')
        outcomes[cid] = {'reviewed': reviewed, 'failed': failed}
    return outcomes


def build_snapshot(context: WorkbenchContext, case_ids: list[str]) -> dict:
    """Freeze the tool's own predictions; human labels are not consulted here."""
    store = HumanEventReviewStore(context.review_summary, context.model_reviews_root,
                                  annotations_root=context.annotations_root, manual_mode=False)
    cases = {}
    for cid in case_ids:
        case = store.get_case(context.case_dir(cid))
        cases[cid] = {'candidates': [
            {'candidate_id': c['candidate_id'], 'interval': [c['start_time'], c['end_time']],
             'status': c['disposition']['status'],
             'confirmed_intervals': c['disposition'].get('confirmed_intervals', []),
             'review_status': c['review']['status']}
            for c in case['candidates']]}
    return {'cases': cases, 'reports': {}}


def resolve_label_decision(label: dict, can_complete: bool, issues: list[str]) -> tuple[str, list, str]:
    """Map a stored human label onto a replayable decision.

    An event verdict is only replayed when the tool's own quality gate passes;
    otherwise the protocol forbids the verdict and the label is replayed as
    `data_issue` with the gate's issues recorded in the notes.
    """
    decision, events = label.get('decision', ''), [list(e) for e in label.get('events', [])]
    notes = f'人工标注来自 {label.get("label_origin", "unknown")}'
    if decision in {'positive', 'negative'} and not can_complete:
        return 'data_issue', [], '数据质量门未过，不按事件结论标注：' + '；'.join(issues)
    return decision, events, notes


def replay_labels(context: WorkbenchContext, labels_dir: Path, reviewer: str) -> list[dict]:
    """Replay human labels through the store's own validation and audit path."""
    store = EvaluationStore(context)
    replayed = []
    for cid in sorted({p.stem for p in labels_dir.glob('*.json') if not p.stem.startswith('_')}):
        label = json.loads((labels_dir / f'{cid}.json').read_text(encoding='utf-8'))
        current = store.label(cid)
        assist = build_annotation_assist(context.case_dir(cid))
        decision, events, notes = resolve_label_decision(
            label, assist['quality']['can_complete'], assist['quality']['issues'])
        body = {'expected_revision': current['revision'], 'decision': decision, 'events': events,
                'reviewer': reviewer, 'notes': notes, 'whole_case_reviewed': True}
        try:
            store.save_label(cid, body)
            replayed.append({'case_id': cid, 'decision': decision, 'ok': True})
        except ValueError as error:
            replayed.append({'case_id': cid, 'decision': decision, 'ok': False, 'error': str(error)})
    return replayed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='scripts/run_benchmark',
        description='在带人工标签的数据集上端到端跑一次冻结评估，并输出指标')
    parser.add_argument('--dataset', type=Path, required=True, help='包含 Case 子目录的目录')
    parser.add_argument('--labels', type=Path, required=True, help='人工标签目录（每 Case 一个 JSON）')
    parser.add_argument('--runtime', type=Path, required=True, help='运行数据目录（建议用临时目录）')
    parser.add_argument('--reviewer', default='', help='标注复核人姓名（写入评估记录）')
    parser.add_argument('--source-type', default='synthetic', help='导入来源类型，默认 synthetic')
    parser.add_argument('--source-name', default='', help='批次名称，默认取数据集目录名')
    parser.add_argument('--output', type=Path, help='指标 JSON 输出路径，默认 <runtime>/metrics.json')
    parser.add_argument('--notify', default='', help='飞书机器人 webhook；默认读环境变量 FEISHU_WEBHOOK_URL，留空不推送')
    args = parser.parse_args(argv)
    webhook = resolve_webhook(args.notify)

    runtime = initialize(args.runtime)
    context = build_context(runtime)
    imported = run_import(args.dataset, context, source_type=args.source_type,
                          source_name=args.source_name or args.dataset.name)
    case_ids = [item['case_id'] for item in imported['cases'] if item['status'] == 'imported']
    print('导入：%(imported)d 个 Case，失败 %(error)d' % imported['totals'], flush=True)
    if not case_ids:
        print('没有可评估的 Case', file=sys.stderr)
        return 1

    outcomes = review_all(context, case_ids)
    failures = [v for v in outcomes.values() if isinstance(v, dict) and v['failed']]
    print('AI 复核完成：%d 个 Case，%d 个 Case 有失败候选' % (len(case_ids), len(failures)), flush=True)

    snapshot = build_snapshot(context, case_ids)
    freeze(context, snapshot, seed=0, generator_hashes={'dataset': args.dataset.name})
    print('批次已冻结：%d 个 Case' % len(snapshot['cases']), flush=True)

    replayed = replay_labels(context, args.labels, args.reviewer or '未署名')
    bad = [item for item in replayed if not item['ok']]
    print('标签回放：%d 条，失败 %d' % (len(replayed), len(bad)), flush=True)
    for item in bad:
        print('  标签失败 %(case_id)s：%(error)s' % item, file=sys.stderr)

    store = EvaluationStore(context)
    try:
        summary = store.unblind()
    except ValueError as error:
        print('开启对照失败：%s' % error, file=sys.stderr)
        return 1
    metrics = summary['metrics']
    output = args.output or (runtime / 'metrics.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print('完整评估记录：%s' % output)
    warning = send_card(webhook, *benchmark_card(summary))
    if warning:
        print(warning, file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
