"""Small, deterministic briefing built from the same validated local evidence."""
from app.investigation_actions import required_actions
from app.knowledge_notes import case_notes


def summarize_event(event, checks):
    detail = event['context_summary']
    primary = checks[0]
    kind = int(primary['check_id'].rsplit('K', 1)[1])
    gaps = []
    if 0 in required_actions(event):
        incomplete = [f"{s['source_file']}.{s['field']}（{s['valid_samples']}/{s['total_samples']} 点可用）"
                      for s in event['checks'][0]['signals'] if not s['ready']]
        gaps.append('链路覆盖不足：' + ('、'.join(incomplete) or '规划与控制未完整共同对齐。'))
    if 3 in required_actions(event):
        if detail['object_ambiguous_samples'] or detail['multi_object_timestamps']:
            gaps.append('目标匹配存在歧义，当前日志不能确定规划选中了哪个目标。')
        elif detail['object_missing_samples']:
            gaps.append('目标记录覆盖有缺口，无法完整关联规划触发依据。')
        elif detail['object_switch_count']:
            gaps.append('目标 ID 有切换，尚未核实跟踪对象与规划对象的关联。')
        else:
            gaps.append('规划触发条件尚未验证，需要结合原因记录及对应场景输入。')
    actions = {
        0:'先补齐所列缺失信号在确认区间及前后上下文的日志，并核对各模块时间基准。',
        1:'叠加规划与控制的加速度请求，记录变化起点、差异时刻及对应 CSV 行号。',
        2:'对照控制目标、制动/油门请求与车速响应，记录请求变化到车辆响应的时间范围。',
        3:('先补规划选中目标 ID 及其与 object_id 的关联，再逐目标核对规划触发时点。'
           if detail['object_id_count'] or detail['object_ambiguous_samples'] else
           '对照规划原因切换时点，补充对应场景输入，核查目标速度与触发条件。'),
        4:'补充指令来源、控制模式与仲裁记录，核对该区间是否存在合法指令覆盖。',
    }
    reasons = {
        0:'关键请求信号未完整对齐，先补证据才能比较模块行为。',
        1:'需要先验证规划请求与控制目标是否一致，再缩小排查范围。',
        2:'现有观测需要验证控制请求到车辆响应的关系。',
        3:'现有目标或规划原因记录只能提供线索，尚不足以验证减速触发依据。',
        4:'当前观测不能单独确定实际指令来源。',
    }
    if kind == 3:
        planning = detail['planning_reasons']
        if 'STOP_FOR_TRAFFIC_LIGHT' in planning:
            actions[3] = '核对原因切换时点，补充灯色、停止线和规划停车约束，验证停车触发依据。'
        elif 'SPEED_LIMIT_CHANGE' in planning:
            actions[3] = '核对限速来源及版本，比较限速变化时点与规划目标速度。'
    return {'event_id':event['event_id'], 'observation':event['observation'],
            'gaps':gaps, 'primary_check_id':primary['check_id'], 'title':primary['step'],
            'next_action':actions[kind], 'reason':reasons[kind],
            'evidence_ids':primary['evidence_ids'], 'other_check_ids':[c['check_id'] for c in checks[1:]],
            'required_data':list(dict.fromkeys(item for c in checks for item in c.get('required_data', []))),
            'knowledge_notes':case_notes(event)}


def briefings(bundle, display_checks):
    return {e['event_id']:summarize_event(e, display_checks[e['event_id']]) for e in bundle['events']}
