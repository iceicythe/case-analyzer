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
from app.model_runner import ModelConfigurationError
from app.server import WorkbenchContext
from app.workbench_overview import load_overview_index
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
        reviewed = failed = attempted = 0
        errors = []
        for candidate in case['candidates']:
            if candidate['disposition']['status'] != 'pending_ai':
                continue
            try:
                attempted += 1
                result = store.review(case_dir, candidate['candidate_id'], fingerprint)
                saved = next(c for c in result['candidates'] if c['candidate_id'] == candidate['candidate_id'])
                status = saved['review']['status']
                if status == 'validated':
                    reviewed += 1
                else:
                    failed += 1
                    errors.append({'candidate_id':candidate['candidate_id'], 'status':status})
            except (ValueError, OSError, ModelConfigurationError) as error:
                failed += 1
                errors.append({'candidate_id':candidate['candidate_id'], 'status':'exception', 'error_type':type(error).__name__})
        outcomes[cid] = {'attempted':attempted, 'reviewed':reviewed, 'failed':failed, 'errors':errors}
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
    for cid in store.plan()['case_ids']:
        try:
            label = json.loads((labels_dir / f'{cid}.json').read_text(encoding='utf-8'))
            current = store.label(cid)
            assist = build_annotation_assist(context.case_dir(cid))
            decision, events, notes = resolve_label_decision(
                label, assist['quality']['can_complete'], assist['quality']['issues'])
            body = {'expected_revision': current['revision'], 'decision': decision, 'events': events,
                    'reviewer': reviewer, 'notes': notes, 'whole_case_reviewed': True}
            store.save_label(cid, body)
            replayed.append({'case_id': cid, 'decision': decision, 'ok': True})
        except (ValueError, OSError, TypeError, AttributeError) as error:
            replayed.append({'case_id':cid, 'ok':False, 'error_type':type(error).__name__})
    return replayed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='scripts/run_benchmark',
        description='在带人工标签的数据集上端到端跑一次冻结评估，并输出指标')
    parser.add_argument('--dataset', type=Path, required=True, help='包含 Case 子目录的目录')
    parser.add_argument('--labels', type=Path, required=True, help='人工标签目录（每 Case 一个 JSON）')
    parser.add_argument('--runtime', type=Path, required=True, help='运行数据目录（建议用临时目录）')
    parser.add_argument('--reviewer', default='', help='标注复核人姓名（写入评估记录）')
    parser.add_argument('--source-type', choices=('recorded','synthetic','unknown'), default='synthetic', help='导入来源类型，默认 synthetic')
    parser.add_argument('--source-name', default='', help='批次名称，默认取数据集目录名')
    parser.add_argument('--output', type=Path, help='指标 JSON 输出路径，默认 <runtime>/metrics.json')
    parser.add_argument('--notify', default='', help='飞书机器人 webhook；默认读环境变量 FEISHU_WEBHOOK_URL，留空不推送')
    args = parser.parse_args(argv)
    webhook = resolve_webhook(args.notify)

    runtime = initialize(args.runtime)
    context = build_context(runtime)
    if (load_overview_index(context.batch_dir) or any(context.data_root.iterdir())
            or any(context.model_reviews_root.rglob('*.json')) or EvaluationStore(context).enabled):
        print('评测需要新的空运行目录，已有数据或冻结结果不会覆盖；请更换 --runtime。', file=sys.stderr)
        return 1
    if not args.labels.is_dir():
        print('标签目录不存在，请检查 --labels。', file=sys.stderr)
        return 1
    try:
        imported = run_import(args.dataset, context, source_type=args.source_type,
                              source_name=args.source_name or args.dataset.name)
    except (ValueError, OSError) as error:
        print('导入未开始：%s' % error, file=sys.stderr)
        return 1
    case_ids = [item['case_id'] for item in imported['cases'] if item['status'] == 'imported']
    print('导入：%(imported)d 个 Case，失败 %(failed)d' % imported['totals'], flush=True)
    if imported['totals']['failed'] or imported['totals']['conflict']:
        print('数据集导入不完整，已停止评测，未调用模型；修复后使用新的运行目录。', file=sys.stderr)
        return 1
    if not case_ids:
        print('没有可评估的 Case', file=sys.stderr)
        return 1
    label_ids = {p.stem for p in args.labels.glob('*.json') if not p.stem.startswith('_')}
    if label_ids != set(case_ids):
        print('标签文件与导入 Case 不一一对应，已停止评测，未调用模型。', file=sys.stderr)
        return 1

    outcomes = review_all(context, case_ids)
    failures = [v for v in outcomes.values() if isinstance(v, dict) and v['failed']]
    print('AI 复核：%d 个 Case，%d 个输出通过校验，%d 个 Case 有失败候选' % (
        len(case_ids),sum(v['reviewed'] for v in outcomes.values()),len(failures)), flush=True)

    snapshot = build_snapshot(context, case_ids)
    freeze(context, snapshot, seed=0, generator_hashes={'dataset':args.dataset.name, 'evaluation_scope':'event_review'})
    print('批次已冻结：%d 个 Case' % len(snapshot['cases']), flush=True)

    replayed = replay_labels(context, args.labels, args.reviewer or '未署名')
    bad = [item for item in replayed if not item['ok']]
    print('标签回放：%d 条，失败 %d' % (len(replayed), len(bad)), flush=True)
    for item in bad:
        print('  标签失败 %(case_id)s：%(error_type)s' % item, file=sys.stderr)

    store = EvaluationStore(context)
    try:
        summary = store.unblind()
    except ValueError as error:
        print('开启对照失败：%s' % error, file=sys.stderr)
        return 1
    metrics = summary['metrics']
    summary.update(evaluation_scope='event_review',
                   report_quality={'status':'not_evaluated','reason':'本脚本只评估候选与事件复核，未生成或审阅排查报告'},
                   execution={'review_outcomes':outcomes,'failed_candidates':sum(v['failed'] for v in outcomes.values())},
                   run_status='completed_with_failures' if failures else 'completed')
    output = args.output or (runtime / 'metrics.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print('完整评估记录：%s' % output)
    print('评估范围：候选筛选与事件复核；排查建议质量：未评测。')
    warning = send_card(webhook, *benchmark_card(summary))
    if warning:
        print(warning, file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
