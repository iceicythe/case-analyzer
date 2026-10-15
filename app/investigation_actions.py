"""Bind investigation procedures to observable fields and explicit data gaps."""
from __future__ import annotations
from collections import Counter

VERSION = 'action-checklist-2.0'


def describe_context(event, raw):
    """Summarize source rows and matching limits, never infer a selected target."""
    start,end = event['confirmed_interval']
    objects = [r for r in raw['object_state.csv'] if start-1e-6 <= r['timestamp'] <= end+1e-6]
    times = Counter(r['timestamp'] for r in objects)
    ids = sorted({r['object_id'] for r in objects if r.get('object_id')})
    single = [r for r in objects if times[r['timestamp']]==1 and r.get('object_id')]
    switches = [{'time':b['timestamp'],'before':a['object_id'],'after':b['object_id'],'source_rows':[a['csv_row'],b['csv_row']]}
                for a,b in zip(single,single[1:]) if a['object_id']!=b['object_id']]
    planning = {r['csv_row']:r for r in raw['planning_state.csv']}
    matched = {}; missing = ambiguous = 0; gaps = []; active = None
    for sample in event['aligned_event_samples']:
        p = sample['matches'].get('planning_state.csv')
        if p and not p['ambiguous'] and p['csv_row'] in planning:
            matched[p['csv_row']] = planning[p['csv_row']]
        obj = sample['matches'].get('object_state.csv')
        missing += obj is None
        ambiguous += bool(obj and obj['ambiguous'])
        kind = '无近邻记录' if obj is None else '匹配歧义' if obj['ambiguous'] else None
        if kind:
            if active and active['reason']==kind:
                active['end']=sample['time'];active['samples']+=1
            else:
                active={'start':sample['time'],'end':sample['time'],'samples':1,'reason':kind};gaps.append(active)
        else: active=None
    reasons = Counter(r['decision_reason'] for r in matched.values() if r.get('decision_reason'))
    sequence = sorted(matched.values(),key=lambda r:(r['timestamp'],r['csv_row']))
    transitions = [{'time':b['timestamp'],'before':a['decision_reason'],'after':b['decision_reason'],
                    'source_rows':[a['csv_row'],b['csv_row']]}
                   for a,b in zip(sequence,sequence[1:]) if a.get('decision_reason')!=b.get('decision_reason')]
    return {'planning_reasons':dict(reasons),'planning_reason_rows':sorted(r['csv_row'] for r in matched.values() if r.get('decision_reason')),
            'reason_transitions':transitions[:12], 'reason_transition_count':len(transitions),
            'object_rows':len(objects),'object_timestamps':len(times),'object_ids':ids[:20],'object_id_count':len(ids),
            'multi_object_timestamps':sum(n>1 for n in times.values()),'object_switches':switches[:12],
            'object_switch_count':len(switches),'object_missing_samples':missing,'object_ambiguous_samples':ambiguous,
            'object_gaps':gaps[:12],'object_gap_count':len(gaps),
            'object_reference_rows':[r['csv_row'] for r in objects[:4]],
            'interpretation':'原因枚举是日志自述；多目标不等于坏数据，ID变化不证明物理对象变化；近邻匹配不提供规划目标关联。'}


def signal(event, filename, field):
    module = event['module_evidence'][filename]
    rows = {row['csv_row']: row for row in module['rows']}
    values, references, missing, ambiguous, omitted = [], [], 0, 0, 0
    for sample in event['aligned_event_samples']:
        match = ({'csv_row': sample['vehicle_csv_row'], 'ambiguous': False,
                  'timestamp': sample['time'], 'time_offset_seconds': 0}
                 if filename == 'vehicle_state.csv' else sample['matches'].get(filename))
        if match and match['ambiguous']:
            ambiguous += 1
            continue
        row = rows.get(match['csv_row']) if match else None
        if match and row is None:
            omitted += 1
        if not row or row.get(field) is None:
            missing += 1
            continue
        values.append(row[field])
        references.append({'csv_row': row['csv_row'], 'timestamp': row['timestamp'],
                           'time_offset_seconds': match['time_offset_seconds']})
    numeric = [v for v in values if isinstance(v, (float, int))]
    count = len(event['aligned_event_samples'])
    return {'source_file': filename, 'field': field, 'valid_samples': len(values),
            'total_samples': count, 'missing_samples': missing, 'ambiguous_samples': ambiguous,
            'omitted_samples': omitted, 'minimum': min(numeric) if numeric else None,
            'maximum': max(numeric) if numeric else None,
            'source_rows': sorted({r['csv_row'] for r in references}),
            'maximum_time_offset_seconds': max((abs(r['time_offset_seconds']) for r in references), default=None),
            'ready': len(values) == count and count > 0}


def build_actions(event, context_seconds):
    eid = event['event_id']
    start, end = event['confirmed_interval']
    vehicle_rows = event['module_evidence']['vehicle_state.csv']['rows']
    context = [max(vehicle_rows[0]['timestamp'], start-context_seconds),
               min(vehicle_rows[-1]['timestamp'], end+context_seconds)]
    def make(index, title, fields, procedure, branches, letters, required=(), caution=''):
        signals = [signal(event, name, field) for name, field in fields]
        gaps = [f"{s['source_file']}.{s['field']}：有效对齐 {s['valid_samples']}/{s['total_samples']} 点"
                f"，缺失 {s['missing_samples']} 点、歧义 {s['ambiguous_samples']} 点"
                for s in signals if not s['ready']]
        gaps += list(required)
        return {'check_id': f'{eid}-K{index}', 'step': title, 'knowledge_id': None,
                'evidence_ids': [f'{eid}-{letter}' for letter in letters],
                'readiness': 'needs_data' if gaps else 'ready',
                'event_interval': [start, end], 'context_interval': context,
                'signals': signals, 'procedure': procedure,
                'result_branches': [{'condition': condition, 'next_action': action} for condition, action in branches],
                'required_data': gaps, 'caution': caution}

    checks = [make(0, '核对事件覆盖与时间基准', [
        ('vehicle_state.csv', 'speed_mps'), ('planning_state.csv', 'planned_acceleration_mps2'),
        ('control_state.csv', 'requested_acceleration_mps2'), ('control_state.csv', 'brake_command')], [
        '按确认区间检查逐点匹配记录、有效字段、重复时间戳与缺失采样，再查看前后上下文。',
        '核对日志 timestamp 的单位、时钟来源和采集说明；近邻匹配偏差只能用来发现疑点，不直接修正时钟。'], [
        ('字段缺失、匹配歧义或事件覆盖不足', '先补同一事件的日志或采集说明；将依赖这些字段的检查记为待数据。'),
        ('覆盖完整且时间基准经核实', '继续比较规划请求、控制请求和车辆响应；执行时延仍需另行核实。')], 'VPCTA',
        caution='有效对齐表示当前日志可比较，不代表时钟已校准或消息传输无延迟。'),
        make(1, '比较规划减速请求与控制目标', [
            ('planning_state.csv', 'planned_acceleration_mps2'),
            ('control_state.csv', 'requested_acceleration_mps2')], [
            '在事件及上下文内叠加两条加速度请求曲线，查看每个有效匹配点的方向、变化起点和变化趋势。',
            '记录差异发生的时间和 CSV 行号；先核实两字段语义、控制模式与允许时延，再判断偏差是否超出系统规格。'], [
            ('两条请求趋势一致', '继续检查制动/油门请求与车辆速度响应，不能据此认定整个链路正常。'),
            ('存在不一致且时间基准已核实', '补查规划到控制的接口、仲裁及限幅日志，验证是否存在合法覆盖或转换。'),
            ('时序、字段语义或允许时延未知', '记录差异，补接口与时延规格；暂不判定丢包或控制故障。')], 'PCTA',
            caution='项目未配置可用于故障判断的请求差值或时延容差；不自行设定通过/失败阈值。'),
        make(2, '核对控制请求与车辆减速响应', [
            ('control_state.csv', 'requested_acceleration_mps2'), ('control_state.csv', 'brake_command'),
            ('control_state.csv', 'throttle_command'), ('vehicle_state.csv', 'speed_mps')], [
            '查看控制目标、制动/油门请求及速度在事件前后的变化顺序；加速度仅作辅助，先核实其来源。',
            '记录“请求变化—车速响应”的时间范围，结合执行器反馈和车辆标定说明检查；CSV 请求值不是执行器实测值。'], [
            ('制动值未达筛查阈值，但仍有非零制动或油门下降', '继续核查减速策略与执行器反馈；不能写成没有制动或控制未响应。'),
            ('请求与速度变化有一致趋势', '保留为链路观测，再检查请求触发条件，不据此断言控制模块无故障。'),
            ('请求与响应不一致', '补制动压力/扭矩、驱动扭矩、坡度、控制模式与标定容差，验证车辆响应。')], 'VCPA',
            caution='brake_command 与 vehicle.brake 的物理含义、单位和归一化规则需核实；0.5 只是项目筛查阈值。'),
        make(3, '核对目标变化与规划触发依据', [
            ('object_state.csv', 'object_id'), ('object_state.csv', 'distance_m'),
            ('object_state.csv', 'relative_speed_mps'), ('object_state.csv', 'confidence'),
            ('planning_state.csv', 'decision_reason'), ('planning_state.csv', 'target_speed_mps')], [
            '在事件及上下文中核对目标 ID、距离、相对速度和置信度是否连续，记录规划理由与目标速度的变化。',
            '若使用 TTC，逐点核对目标 ID 和输入有效性；多目标或匹配歧义时先补目标关联记录。'], [
            ('目标与规划变化同时出现', '作为待验证的触发假设；补场景回放、车道/路径关系和规划触发记录验证。'),
            ('目标跳变、低可信或关联不清', '补目标跟踪与关联信息，不能直接判断感知误检。'),
            ('日志未显示明确触发', '补规划决策状态和约束来源，不能据此认定无障碍物或不合理制动。')], 'OPVA',
            caution='TTC 是恒速运动学近似；没有路径关系、场景回放和策略约束，不能单独判断制动必要性。'),
        make(4, '补查请求来源、仲裁与接口传输', [], [
            '收集覆盖同一事件及上下文的控制输入来源、自动/人工驾驶状态、安全模块介入及仲裁记录。',
            '需要判断传输问题时，补发送/接收时间戳、消息序号、接口周期和时钟说明，再逐条核对。'], [
            ('有合法人工或安全模块覆盖', '核对覆盖触发条件及策略；当前 CSV 不能直接判定其合理性。'),
            ('补齐记录后发现异常丢包或延迟', '按接口周期与时延规格验证，再提交相关模块排查。'),
            ('仍无来源和接口证据', '保持待数据和根因未知，不推测实际触发模块。')], 'PCA', required=(
            '当前导入格式不含控制模式、指令来源与仲裁记录，需补原始日志。',
            '当前导入格式不含消息序号、发送/接收时间戳及接口时延规格，需补原始日志。'))]
    detail = event['context_summary']
    reasons = detail['planning_reasons']
    target = checks[3]
    target['evidence_ids'] += [eid+'-R',eid+'-J']
    observed = '、'.join(f'{name} {count}条' for name,count in reasons.items()) or '没有唯一有效对齐的原因字段'
    target['procedure'].insert(0, f'本事件规划原因记录：{observed}；先核对这些记录及切换时点，枚举不是已验证的实际原因。')
    if detail['multi_object_timestamps'] or detail['object_ambiguous_samples']:
        target['step'] = '先核对多目标与匹配歧义，再关联规划选中对象'
        target['procedure'].insert(0,'按 object_id 分开保留轨迹，核对同一时刻多目标记录；不可把非唯一匹配当成目标不存在，也不能选择最近目标冒充规划选中目标。')
    elif detail['object_missing_samples']:
        target['step'] = '先补目标覆盖缺口，再核对规划触发依据'
        target['procedure'].insert(0,'优先核查事实摘要列出的目标匹配缺口区间；日志缺口不能解释为目标消失。')
    elif detail['object_switch_count']:
        target['step'] = '先核对目标 ID 切换与规划关联'
        target['procedure'].insert(0,'按摘要中的 ID 切换时点核对跟踪重关联、目标替换和规划选中对象；不把跨 ID 距离统计当同一物体轨迹。')
    elif reasons:
        target['step'] = '核对已有规划原因及对应输入'
    focus = []
    if 'FOLLOW_LEAD_VEHICLE' in reasons:
        focus += ['核对规划跟车对象与 object_state.object_id 的关联；关联成立后比较该对象 distance_m、relative_speed_mps 与规划 target_speed_mps 的变化。']
    if any(r in reasons for r in ('CUT_IN_EVASION','YIELD_FOR_MERGE')):
        focus += ['对照切入/汇入原因的变化时点，核对目标横向轨迹、车道/路径关系和规划选中目标；现有纵向距离不足以验证切入或让行。']
        target['required_data'].append('需补目标横向轨迹、车道/路径关系与切入/汇入触发记录；当前 CSV 不含这些证据。')
    if 'STOP_FOR_TRAFFIC_LIGHT' in reasons:
        focus += ['以原因切换时点为线索，核对灯色、停止线与规划停车约束是否一致；枚举本身不能证明红灯存在。']
        target['required_data'].append('需补交通灯状态、停止线与规划停车约束记录；当前 CSV 不含这些证据。')
    if 'SPEED_LIMIT_CHANGE' in reasons:
        focus += ['核对限速来源、限速变化时点与 target_speed_mps 的变化；按道路策略规格验证，不能用目标速度反推真实限速。']
        target['required_data'].append('需补地图/感知限速、来源版本及限速策略规格；当前 CSV 不含这些证据。')
    target['procedure'][1:1] = focus
    if detail['object_id_count'] or any(r in reasons for r in ('FOLLOW_LEAD_VEHICLE','CUT_IN_EVASION','YIELD_FOR_MERGE')):
        target['required_data'].append('需补规划选中目标 ID 与跟踪目标的关联记录；当前格式未提供，不能仅凭距离认定目标。')
    target['readiness'] = 'needs_data' if target['required_data'] else 'ready'
    return checks


def default_actions(event):
    by_id = {int(c['check_id'].rsplit('K', 1)[1]): c for c in event['checks']}
    status = event['observation_status']
    order = ([0, 1, 2] if by_id[0]['readiness'] != 'ready' else
             [2, 4, 1] if status == 'control_without_plan' else
             [3, 1, 2] if status == 'planning_trigger_needs_review' else [1, 2, 4])
    required = required_actions(event)
    order = required + [i for i in order if i not in required]
    return [by_id[i] for i in order[:3]]


def required_actions(event):
    summary = event['context_summary']
    result = [0] if not event['chain_coverage_complete'] else []
    if (summary['object_missing_samples'] or summary['object_ambiguous_samples'] or summary['object_switch_count']
            or event['observation_status']=='planning_trigger_needs_review'):
        result.append(3)
    return result
