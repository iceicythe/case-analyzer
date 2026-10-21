"""生成仿真行车场景数据集（四路日志同源，答案预先已知）。

来源属性：仿真（simulation）。

做法：先定义驾驶场景（初始车速、分段纵向加速度、前车行为），
再由同一个闭环模型同时产生车辆、规划、控制、目标四路日志。
车速是规划目标加速度经过控制请求、由车辆模型积分得到的，
不存在"从真实车速反推规划目标或控制请求"的情形。

时间基准：每个 Case 使用独立的时钟原点 t0（秒，各不相同），
四路日志共用同一原点；车辆采样为严格 0.1 秒网格；
规划/控制为 10 Hz 输出（个别 Case 故意使用其他周期或相位，见场景说明）。

口径依据：../workbench/docs/REAL_DATA_REQUIREMENTS.md
  第 2 节 交付组织；第 3 节 CSV 与时间要求；第 4 节 四类字段定义。

运行：python generate.py
输出：cases/<case_id>/{四路 CSV + delivery_metadata.json}
      _expected/<case_id>.json   场景设计意图与期望结论（供 verify.py 比对，不是最终标签）
"""

from __future__ import annotations

import csv
import json
import random
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES_DIR = ROOT / "cases"
EXPECTED_DIR = ROOT / "_expected"

DT = 0.1
ROUND_SPEED = 3          # 连续量记录小数位
TS_DIGITS = 2            # 时间戳记录小数位（容纳 0.05 相位偏移等非整格时钟）
RANDOM_SEED = 20261015

# 巡航段叠加的极小记录抖动，避免"完全匀速"的失真感。
# 幅度远低于事件门槛（0.5 秒降幅 1.0），不改变任何事件判定。
JITTER_MPS = 0.004
# 规划/控制输出自身的小幅波动，避免整段恒定的失真感。
# 同样远小于判定阈值（规划 -1.0 / 制动 0.5）。
PLAN_JITTER_MPS2 = 0.015
CTRL_JITTER = 0.008

# 规划/控制的请求相对车速变化的提前量：真实系统里决策先于执行。
PLANNING_LEAD_S = 0.3
CONTROL_LEAD_S = 0.2

# 控制侧把期望减速度映射成制动请求的比例（用于产生自洽的仿真日志）。
BRAKE_PER_MPS2 = 1.0 / 3.5
THROTTLE_CRUISE = 0.25

# 期望结论常量（与 workbench 口径一致）
PTR = "planning_trigger_needs_review"
UNEX = "vehicle_response_unexplained"
MEETS = "meets_numeric_rule"
BELOW = "below_numeric_rule"
UNVER = "source_unverified"
NONE_VERDICT = "none"


def sc(case_id, v0, segments, scene, expect_events, *, lead=None, jitter=True,
       verdict=MEETS, statuses=None, boundary=False, obj="normal", warn=False,
       decision="positive", note="", candidates=None, **extra):
    """场景定义的统一入口。

    segments: [(时长 s, 纵向加速度 m/s^2, 决策原因), ...]
    lead:     前车行为；None 表示本 Case 不提供目标日志
    expect_events: 设计事件区间（相对片段起点，秒）
    verdict:  预期候选判定；statuses: 每个事件预期的 observation_status
    obj:      预期目标证据形态 normal/ambiguous/partial/absent
    warn:     预期导入警告（车辆采样缺口）
    其余 extra 键：vehicle_gaps/planning_gaps/control_gaps/object_gaps/
      planning_offset/planning_period/adjacent/object_id_switch
    """
    return dict(case_id=case_id, v0=v0, segments=segments, scene=scene,
                expect_events=expect_events, lead=lead, jitter=jitter,
                expect_verdict=verdict,
                expect_statuses=statuses if statuses is not None else ([PTR] * len(expect_events) if expect_events else []),
                expect_boundary=boundary, expect_object=obj,
                expect_import_warning=warn, label_decision=decision, note=note,
                expect_candidates=(0 if verdict == NONE_VERDICT else len(expect_events)) if candidates is None else candidates,
                **extra)


# --------------------------------------------------------------------------
# 30 个场景：001-018 常规与数值边界；019-021 目标日志困难样本；
# 022-024 车辆采样缺口；025-026 模块日志丢失；027-029 时钟/速率；030 片段边界。
# --------------------------------------------------------------------------
SCENARIOS = [
    # ---- 常规场景 ----
    sc("seg_001", 22.40,
       [(12.0, 0.00, "CRUISE"), (3.0, -2.35, "FOLLOW_LEAD_VEHICLE"), (15.0, 0.00, "CRUISE")],
       "高速跟车，前车减速，ACC 跟随减速", [[12.0, 15.0]],
       lead=dict(t_start=11.5, t_end=14.4, decel=-2.50, d0=35.0)),
    sc("seg_002", 18.30,
       [(8.0, 0.00, "CRUISE"), (2.5, -3.10, "CUT_IN_EVASION"), (19.5, 0.00, "CRUISE")],
       "旁车道车辆切入本车道，紧急减速", [[8.0, 10.5]],
       lead=dict(t_start=7.6, t_end=9.6, decel=-4.00, d0=22.0)),
    sc("seg_003", 14.20,
       [(18.0, 0.00, "CRUISE"), (6.5, -2.15, "STOP_FOR_TRAFFIC_LIGHT"), (5.5, 0.00, "STOPPED")],
       "路口红灯，跟随前车减速至停止", [[18.0, 24.5]],
       lead=dict(t_start=17.5, t_end=24.0, decel=-2.15, d0=30.0)),
    sc("seg_004", 21.70,
       [(6.0, 0.00, "CRUISE"), (4.0, -2.20, "SPEED_LIMIT_CHANGE"), (20.0, 0.00, "CRUISE")],
       "限速由 80 降为 60，主动减速至新限速", [[6.0, 10.0]],
       lead=dict(t_start=999.0, t_end=999.1, decel=0.00, d0=80.0)),
    sc("seg_005", 16.10,
       [(10.0, 0.00, "CRUISE"), (3.0, -2.55, "FOLLOW_LEAD_VEHICLE"),
        (4.0, 1.20, "ACCELERATE_AFTER_CUT_OUT"), (13.0, 0.00, "CRUISE")],
       "跟车减速后前车切出，恢复巡航速度", [[10.0, 13.0]],
       lead=dict(t_start=9.7, t_end=12.7, decel=-2.55, d0=25.0, drop_after=13.0)),
    sc("seg_006", 24.80,
       [(30.0, 0.00, "CRUISE")],
       "高速巡航，全程无纵向减速事件", [],
       verdict=NONE_VERDICT, decision="negative",
       lead=dict(t_start=999.0, t_end=999.1, decel=0.00, d0=45.0)),
    # ---- 数值边界 / 行为边界 ----
    sc("seg_007", 19.60,
       [(5.0, 0.00, "CRUISE"), (20.0, -0.60, "CRUISE"), (5.0, 0.00, "CRUISE")],
       "长距离缓慢降速（滑行减速），单窗口降幅不足", [],
       verdict=NONE_VERDICT, decision="negative",
       lead=dict(t_start=999.0, t_end=999.1, decel=0.00, d0=50.0)),
    sc("seg_008", 20.30,
       [(6.0, 0.00, "CRUISE"), (3.0, -2.45, "FOLLOW_LEAD_VEHICLE"),
        (0.3, 0.50, "RESUME_CRUISE"), (3.0, -2.45, "FOLLOW_LEAD_VEHICLE"),
        (17.7, 0.00, "CRUISE")],
       "减速过程中短暂回升，回落处不满足连续性桥接", [[6.0, 9.0], [9.3, 12.3]],
       lead=dict(t_start=5.7, t_end=8.7, decel=-2.45, d0=35.0),
       candidates=1,
       note="回升段不足以隔开粗筛窗口：候选合并为一个，但严格参考区间仍为两个。"),
    sc("seg_009", 19.90,
       [(6.0, 0.00, "CRUISE"), (2.5, -2.45, "FOLLOW_LEAD_VEHICLE"), (4.5, 0.00, "CRUISE"),
        (2.5, -2.45, "FOLLOW_LEAD_VEHICLE"), (14.5, 0.00, "CRUISE")],
       "两次相隔较远的独立减速事件", [[6.0, 8.5], [13.0, 15.5]],
       lead=dict(t_start=999.0, t_end=999.1, decel=0.00, d0=40.0)),
    sc("seg_010", 18.00,
       [(8.0, 0.00, "CRUISE"), (4.0, -2.00, "FOLLOW_LEAD_VEHICLE"), (18.0, 0.00, "CRUISE")],
       "受控边界样本：半秒窗口降幅恰为 1.000 m/s，正好落在口径门槛上", [[8.0, 12.0]],
       jitter=False, note="关闭记录抖动以保证数值精确。",
       lead=dict(t_start=999.0, t_end=999.1, decel=0.00, d0=45.0)),
    sc("seg_011", 18.00,
       [(8.0, 0.00, "CRUISE"), (4.0, -1.996, "FOLLOW_LEAD_VEHICLE"), (18.0, 0.00, "CRUISE")],
       "受控边界样本：半秒窗口降幅 0.998 m/s，低于门槛但高于候选粗筛", [],
       jitter=False, verdict=BELOW, candidates=1, decision="negative",
       note="典型误报样本：程序会报候选，但按数值规则不该算事件。",
       lead=dict(t_start=999.0, t_end=999.1, decel=0.00, d0=45.0)),
    sc("seg_012", 13.80,
       [(5.0, 0.00, "CRUISE"), (1.5, -2.20, "FOLLOW_LEAD_VEHICLE"), (2.0, 0.00, "CRUISE"),
        (1.5, -2.20, "FOLLOW_LEAD_VEHICLE"), (2.0, 0.00, "CRUISE"),
        (1.5, -2.20, "FOLLOW_LEAD_VEHICLE"), (7.5, 0.00, "CRUISE")],
       "拥堵路段走走停停，出现三次短减速", [[5.0, 6.5], [8.5, 10.0], [12.0, 13.5]],
       lead=dict(t_start=999.0, t_end=999.1, decel=0.00, d0=35.0)),
    # ---- 常规场景补充 ----
    sc("seg_013", 13.60,
       [(8.0, 0.00, "CRUISE"), (2.0, -2.60, "FOLLOW_LEAD_VEHICLE"),
        (4.0, 0.55, "RESUME_CRUISE"), (15.5, 0.00, "CRUISE")],
       "城市道路跟车，中等强度减速后缓慢恢复", [[8.0, 10.0]],
       lead=dict(t_start=7.5, t_end=10.2, decel=-2.70, d0=26.0)),
    sc("seg_014", 19.80,
       [(10.0, 0.00, "CRUISE"), (2.5, -2.40, "YIELD_FOR_MERGE"),
        (3.0, 0.45, "MERGE_COMPLETE"), (14.0, 0.00, "CRUISE")],
       "匝道汇入车流，让行主路车辆减速", [[10.0, 12.5]],
       lead=dict(t_start=9.6, t_end=12.2, decel=-2.40, d0=30.0)),
    sc("seg_015", 17.90,
       [(9.0, 0.00, "CRUISE"), (1.2, -3.10, "CUT_IN_EVASION"),
        (1.8, -2.10, "FOLLOW_LEAD_VEHICLE"), (16.0, 0.00, "CRUISE")],
       "目标切入后两段式减速：先避让再跟随", [[9.0, 12.0]],
       lead=dict(t_start=8.7, t_end=11.8, decel=-3.20, d0=20.0)),
    sc("seg_016", 23.50,
       [(7.0, 0.00, "CRUISE"), (1.8, -3.90, "FOLLOW_LEAD_VEHICLE"), (16.2, 0.00, "CRUISE")],
       "前车急刹，本车重制动跟随", [[7.0, 8.8]],
       lead=dict(t_start=6.6, t_end=8.8, decel=-4.10, d0=28.0)),
    sc("seg_017", 21.20,
       [(6.0, 0.00, "CRUISE"), (9.0, -0.75, "CRUISE"), (15.0, 0.00, "CRUISE")],
       "长下坡松油门滑行，缓慢单调降速（阴性对照）", [],
       verdict=NONE_VERDICT, decision="negative", obj="absent"),
    sc("seg_018", 11.40,
       [(6.0, 0.00, "CRUISE"), (3.0, 1.15, "SPEED_UP"),
        (2.2, -2.30, "FOLLOW_LEAD_VEHICLE"), (16.8, 0.00, "CRUISE")],
       "车流波动：先加速跟进，随后跟随减速", [[9.0, 11.2]],
       lead=dict(t_start=8.8, t_end=11.4, decel=-2.30, d0=18.0)),
    # ---- 目标日志困难样本 ----
    sc("seg_019", 20.60,
       [(10.0, 0.00, "CRUISE"), (3.0, -2.50, "FOLLOW_LEAD_VEHICLE"), (16.0, 0.00, "CRUISE")],
       "本车道前车与相邻车道目标全程同帧输出（多目标）", [[10.0, 13.0]],
       lead=dict(t_start=9.7, t_end=13.0, decel=-2.60, d0=32.0),
       adjacent=dict(d0=14.0, rel_event=-0.45, rel_cruise=0.20, t_start=10.0, t_end=13.0),
       obj="ambiguous", note="同一时刻存在两条目标记录：目标证据应出现对齐歧义；规划/控制链路不受影响。"),
    sc("seg_020", 18.90,
       [(9.0, 0.00, "CRUISE"), (2.6, -2.60, "FOLLOW_LEAD_VEHICLE"), (16.4, 0.00, "CRUISE")],
       "事件中目标轨迹重联（跟踪标识切换）", [[9.0, 11.6]],
       lead=dict(t_start=8.7, t_end=11.6, decel=-2.70, d0=24.0),
       object_id_switch=dict(t=10.8, new_id="obj-7216"),
       note="同一前车的跟踪标识在事件中段切换；每时刻仅一条记录，目标证据应保持正常对齐。"),
    sc("seg_021", 17.80,
       [(8.0, 0.00, "CRUISE"), (3.0, -2.40, "FOLLOW_LEAD_VEHICLE"), (17.0, 0.00, "CRUISE")],
       "事件中目标日志短暂丢失", [[8.0, 11.0]],
       lead=dict(t_start=7.7, t_end=11.0, decel=-2.50, d0=28.0),
       object_gaps=[[9.5, 10.8]],
       obj="partial", note="目标日志在事件中段缺失 1.3 秒：事件处目标有效对齐应少于采样总数；链路不受影响。"),
    # ---- 车辆采样缺口 ----
    sc("seg_022", 21.60,
       [(10.0, 0.00, "CRUISE"), (3.0, -2.35, "FOLLOW_LEAD_VEHICLE"), (17.0, 0.00, "CRUISE")],
       "事件后巡航段存在 0.3 秒采样缺口", [[10.0, 13.0]],
       lead=dict(t_start=9.7, t_end=13.0, decel=-2.45, d0=34.0),
       vehicle_gaps=[[20.1, 20.4]],
       verdict=UNVER, warn=True,
       note="缺口不在事件内：事件仍可检出，但来源核查降级，候选应判 source_unverified。"),
    sc("seg_023", 21.40,
       [(8.0, 0.00, "CRUISE"), (8.0, -2.35, "FOLLOW_LEAD_VEHICLE"), (14.0, 0.00, "CRUISE")],
       "事件中段存在 0.3 秒采样缺口（单次连续减速被拆分）", [[8.0, 13.7], [14.1, 16.0]],
       lead=dict(t_start=7.7, t_end=16.0, decel=-2.35, d0=40.0),
       vehicle_gaps=[[13.8, 14.1]],
       verdict=UNVER, warn=True,
       note="缺口穿过事件内部：桥接合并被禁用，应拆分为两个区间且均判 source_unverified。"),
    sc("seg_024", 20.80,
       [(10.0, 0.00, "CRUISE"), (1.0, -2.20, "FOLLOW_LEAD_VEHICLE"), (17.0, 0.00, "CRUISE")],
       "事件内部 0.6 秒采样缺口，数据不足以确认事件", [],
       lead=dict(t_start=9.7, t_end=11.0, decel=-2.30, d0=30.0),
       vehicle_gaps=[[10.2, 10.8]],
       verdict=NONE_VERDICT, warn=True, decision="uncertain",
       note="设计上存在一次减速，但缺口吃掉全部完整窗口：程序应不报候选；真值记 uncertain，不计入漏报。"),
    # ---- 模块日志丢失 ----
    sc("seg_025", 19.70,
       [(10.0, 0.00, "CRUISE"), (3.0, -2.50, "FOLLOW_LEAD_VEHICLE"), (16.0, 0.00, "CRUISE")],
       "规划日志在事件中段丢失 1 秒", [[10.0, 13.0]],
       lead=dict(t_start=9.7, t_end=13.0, decel=-2.60, d0=30.0),
       planning_gaps=[[12.0, 13.0]],
       statuses=[UNEX],
       note="车辆侧事件正常检出；事件中段规划行缺失，链路证据不足应判 vehicle_response_unexplained。"),
    sc("seg_026", 20.10,
       [(10.0, 0.00, "CRUISE"), (3.0, -2.45, "FOLLOW_LEAD_VEHICLE"), (16.0, 0.00, "CRUISE")],
       "控制日志在事件中段丢失 1 秒", [[10.0, 13.0]],
       lead=dict(t_start=9.7, t_end=13.0, decel=-2.55, d0=31.0),
       control_gaps=[[12.0, 13.0]],
       statuses=[UNEX],
       note="控制行缺失与规划缺失同理：链路证据不足，不能判断请求来源。"),
    # ---- 时钟相位 / 输出速率 ----
    sc("seg_027", 20.20,
       [(10.0, 0.00, "CRUISE"), (3.0, -2.45, "FOLLOW_LEAD_VEHICLE"), (16.0, 0.00, "CRUISE")],
       "规划时钟相位差半格（+0.05 秒）", [[10.0, 13.0]],
       lead=dict(t_start=9.7, t_end=13.0, decel=-2.55, d0=32.0),
       planning_offset=0.05,
       statuses=[UNEX],
       note="规划时间戳整体偏移半个采样周期：每个车辆采样到最近规划行均为等距并列，全时段对齐歧义。"),
    sc("seg_028", 19.30,
       [(10.0, 0.00, "CRUISE"), (3.0, -2.40, "FOLLOW_LEAD_VEHICLE"), (16.0, 0.00, "CRUISE")],
       "规划时钟偏移 0.14 秒（在 0.15 秒对齐容差内）", [[10.0, 13.0]],
       lead=dict(t_start=9.7, t_end=13.0, decel=-2.50, d0=29.0),
       planning_offset=0.14,
       note="容差边界对照：偏移小于容差时每个采样仍可唯一对齐，链路应保持完整。"),
    sc("seg_029", 18.60,
       [(9.0, 0.00, "CRUISE"), (3.2, -2.40, "FOLLOW_LEAD_VEHICLE"), (16.8, 0.00, "CRUISE")],
       "规划以 5 Hz 输出（0.2 秒周期），车辆为 10 Hz 采样", [[9.0, 12.2]],
       lead=dict(t_start=8.7, t_end=12.2, decel=-2.50, d0=28.0),
       planning_period=0.2,
       statuses=[UNEX],
       note="模块输出速率不同：落在规划周期中点的车辆采样对齐歧义，链路应判证据不足。"),
    # ---- 片段边界 ----
    sc("seg_030", 20.40,
       [(12.0, 0.00, "CRUISE"), (3.3, -2.30, "FOLLOW_LEAD_VEHICLE")],
       "减速事件延伸至片段末尾", [[12.0, 15.3]],
       lead=dict(t_start=11.7, t_end=15.3, decel=-2.40, d0=30.0),
       boundary=True,
       note="事件一直持续到数据结束：应触发片段首尾边界核查提示。"),
]


# --------------------------------------------------------------------------
# 场景求值
# --------------------------------------------------------------------------
def total_duration(segments):
    return round(sum(s[0] for s in segments), 6)


def decel_segments(segments):
    """把所有减速段解析为 (起, 止, 加速度, 决策原因)。"""
    out = []
    t = 0.0
    for dur, accel, reason in segments:
        if accel < 0:
            out.append((round(t, 6), round(t + dur, 6), accel, reason))
        t += dur
    return out


def ego_speed(t, v0, segments):
    """自车车速：分段恒定加速度积分（解析式，无递推累积误差）。"""
    v = v0
    elapsed = 0.0
    for dur, accel, _ in segments:
        if t <= elapsed:
            break
        span = min(t, elapsed + dur) - elapsed
        v += accel * span
        elapsed += dur
    return v


def lead_speed(t, v0, lead):
    if t < lead["t_start"]:
        return v0
    if t < lead["t_end"]:
        return v0 + lead["decel"] * (t - lead["t_start"])
    return v0 + lead["decel"] * (lead["t_end"] - lead["t_start"])


def in_gap(rel, gaps):
    if not gaps:
        return False
    return any(a - 1e-9 <= rel < b - 1e-9 for a, b in gaps)


def conf_draw(rng):
    """目标置信度：多数 0.87~0.97，偶发 0.78~0.88 的低置信帧（均高于可信门槛 0.5）。"""
    if rng.random() < 0.08:
        return round(rng.uniform(0.78, 0.88), 3)
    return round(rng.uniform(0.87, 0.97), 3)


# --------------------------------------------------------------------------
# 四路日志构造
# --------------------------------------------------------------------------
def build_case(scn, rng):
    cid = scn["case_id"]
    segments = scn["segments"]
    dur = total_duration(segments)
    v0 = scn["v0"]
    jitter = scn["jitter"]
    decels = decel_segments(segments)

    t0 = round(rng.uniform(600.0, 64800.0), 1)
    lead_oid = f"obj-{rng.randrange(1100, 9899)}"
    adj_oid = f"obj-{rng.randrange(1100, 9899)}"

    n = int(round(dur / DT)) + 1
    rel_times = [round(i * DT, 1) for i in range(n)]

    # ---- 车辆：完整物理速度序列（含记录抖动），再按缺口过滤 ----
    speeds = []
    for rt in rel_times:
        v = ego_speed(rt, v0, segments)
        if jitter:
            v += rng.uniform(-JITTER_MPS, JITTER_MPS)
        speeds.append(round(max(v, 0.0), ROUND_SPEED))

    vehicle = [(round(t0 + rt, TS_DIGITS), speeds[i])
               for i, rt in enumerate(rel_times)
               if not in_gap(rt, scn.get("vehicle_gaps"))]

    # 加速度列：由相邻记录行车速差分派生（缺口处按实际行间隔，不声称独立传感器）
    accel_col = []
    for j, (_ts, v) in enumerate(vehicle):
        if j == 0:
            a = (vehicle[1][1] - v) / DT
        else:
            dt = (vehicle[j][0] - vehicle[j - 1][0])
            a = (v - vehicle[j - 1][1]) / dt
        accel_col.append(round(a, ROUND_SPEED))

    # ---- 规划：独立周期与相位偏移，支持行丢失 ----
    p_period = scn.get("planning_period", DT)
    p_offset = scn.get("planning_offset", 0.0)
    p_rel = []
    k = 0
    while True:
        rt = round(k * p_period, 6)
        if rt > dur + 1e-9:
            break
        p_rel.append(rt)
        k += 1

    planning = []
    for rt in p_rel:
        if in_gap(rt, scn.get("planning_gaps")):
            continue
        hit = None
        for (ta, tb, a, reason) in decels:
            if ta - PLANNING_LEAD_S <= rt < tb:
                hit = (a, reason, tb)
                break
        ts = round(t0 + p_offset + rt, TS_DIGITS)
        if hit:
            a, reason, tb = hit
            target = max(ego_speed(tb, v0, segments), 0.0)
            a_out = a + (rng.uniform(-PLAN_JITTER_MPS2, PLAN_JITTER_MPS2) if jitter else 0.0)
            planning.append((ts, round(target, ROUND_SPEED), round(a_out, ROUND_SPEED), reason))
        else:
            v_now = ego_speed(min(rt + PLANNING_LEAD_S, dur), v0, segments)
            a_out = rng.uniform(-PLAN_JITTER_MPS2, PLAN_JITTER_MPS2) if jitter else 0.0
            planning.append((ts, round(max(v_now, 0.0), ROUND_SPEED), round(a_out, ROUND_SPEED), "CRUISE"))

    # ---- 控制：全网格，支持行丢失 ----
    control = []
    for i, rt in enumerate(rel_times):
        if in_gap(rt, scn.get("control_gaps")):
            continue
        hit = None
        for (ta, tb, a, reason) in decels:
            if ta - CONTROL_LEAD_S <= rt < tb:
                hit = (a, reason)
                break
        ts = round(t0 + rt, TS_DIGITS)
        if hit:
            a = hit[0]
            brake = min(1.0, abs(a) * BRAKE_PER_MPS2)
            if jitter:
                brake += rng.uniform(-CTRL_JITTER, CTRL_JITTER)
            control.append((ts, round(min(max(brake, 0.0), 1.0), ROUND_SPEED), 0.0, round(a, ROUND_SPEED)))
        else:
            th = THROTTLE_CRUISE
            if jitter:
                th += rng.uniform(-CTRL_JITTER, CTRL_JITTER)
            control.append((ts, 0.0, round(max(th, 0.0), ROUND_SPEED), 0.0))

    # ---- 目标：前车 + 可选相邻目标 / 标识切换 / 行丢失 ----
    objects = []          # (ts, object_id, distance, rel_speed, confidence)
    lead = scn.get("lead")
    adjacent = scn.get("adjacent")
    switch = scn.get("object_id_switch")
    if lead is not None:
        # 前车距离按物理时间连续积分（与是否落盘无关）
        dist = lead["d0"]
        prev_rel_v = None
        adj_dist = adjacent["d0"] if adjacent else None
        prev_adj_v = None
        for i, rt in enumerate(rel_times):
            lv = lead_speed(rt, v0, lead)
            ev = speeds[i]
            rel_v = lv - ev
            if prev_rel_v is not None:
                dist += (rel_v + prev_rel_v) * 0.5 * DT
            prev_rel_v = rel_v
            if not in_gap(rt, scn.get("object_gaps")):
                oid = lead_oid
                if switch is not None and rt >= switch["t"]:
                    oid = switch["new_id"]
                objects.append((round(t0 + rt, TS_DIGITS), oid,
                                round(max(dist, 0.0), ROUND_SPEED),
                                round(rel_v, ROUND_SPEED), conf_draw(rng)))
            if adjacent is not None:
                av = (adjacent["rel_event"] if adjacent["t_start"] <= rt < adjacent["t_end"]
                      else adjacent["rel_cruise"])
                if prev_adj_v is not None:
                    adj_dist += (av + prev_adj_v) * 0.5 * DT
                prev_adj_v = av
                objects.append((round(t0 + rt, TS_DIGITS), adj_oid,
                                round(max(adj_dist, 0.0), ROUND_SPEED),
                                round(av, ROUND_SPEED), conf_draw(rng)))

    return dict(case_id=cid, duration=dur, t0=t0, vehicle=vehicle, accel=accel_col,
                planning=planning, control=control, objects=objects,
                scene=scn["scene"])


# --------------------------------------------------------------------------
# 落盘
# --------------------------------------------------------------------------
def write_csv(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def fmt(value, digits=ROUND_SPEED):
    return f"{value:.{digits}f}"


def write_case(bundle):
    cid = bundle["case_id"]
    target = CASES_DIR / cid
    target.mkdir(parents=True, exist_ok=True)

    write_csv(target / "vehicle_state.csv",
              ["timestamp", "speed_mps", "acceleration_mps2"],
              [(f"{t:.2f}", fmt(v), fmt(a)) for (t, v), a in zip(bundle["vehicle"], bundle["accel"])])

    if bundle["planning"]:
        write_csv(target / "planning_state.csv",
                  ["timestamp", "target_speed_mps", "planned_acceleration_mps2", "decision_reason"],
                  [(f"{t:.2f}", fmt(v), fmt(a), reason) for (t, v, a, reason) in bundle["planning"]])

    if bundle["control"]:
        write_csv(target / "control_state.csv",
                  ["timestamp", "brake_command", "throttle_command", "requested_acceleration_mps2"],
                  [(f"{t:.2f}", fmt(b), fmt(th), fmt(a)) for (t, b, th, a) in bundle["control"]])

    if bundle["objects"]:
        write_csv(target / "object_state.csv",
                  ["timestamp", "object_id", "distance_m", "relative_speed_mps", "confidence"],
                  [(f"{t:.2f}", oid, fmt(d), fmt(rel), fmt(c)) for (t, oid, d, rel, c) in bundle["objects"]])

    meta = {
        "case_id": cid,
        "schema": "delivery-metadata-1.0",
        "source_kind": "simulation",
        "platform": "closed-loop scenario replay",
        "driving_state": "automated",
        "scene": bundle["scene"],
        "generated_at": "2026-10-15",
        "t0_seconds": bundle["t0"],
        "time_base": "四路日志共用同一时间原点；片段起点对应时钟 t0 秒（各 Case 不同）",
        "sampling": "车辆 0.1 秒网格；规划/控制 10 Hz 输出（个别 Case 按场景说明使用其他周期或相位）",
        "units": {"speed_mps": "m/s", "acceleration_mps2": "m/s^2 纵向，负值为减速",
                  "distance_m": "m，纵向距离", "relative_speed_mps": "m/s，负值表示接近"},
        "missing": ["无原始 CAN 报文与 DBC", "无执行器反馈", "无控制模式与仲裁日志", "无同步视频"],
        "notes": "车速为规划目标加速度经控制请求、由车辆模型积分得到；四路日志同源，非从车速反推。",
    }
    (target / "delivery_metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    if CASES_DIR.exists():
        shutil.rmtree(CASES_DIR)
    CASES_DIR.mkdir(parents=True)
    if EXPECTED_DIR.exists():
        shutil.rmtree(EXPECTED_DIR)
    EXPECTED_DIR.mkdir(parents=True)

    rng = random.Random(RANDOM_SEED)
    for scenario in SCENARIOS:
        bundle = build_case(scenario, rng)
        write_case(bundle)
        expected = {
            "case_id": scenario["case_id"],
            "scene": scenario["scene"],
            "t0": bundle["t0"],
            "duration": bundle["duration"],
            "design_events": scenario["expect_events"],
            "expect_candidates": scenario["expect_candidates"],
            "expect_verdict": scenario["expect_verdict"],
            "expect_statuses": scenario["expect_statuses"],
            "expect_boundary": scenario["expect_boundary"],
            "expect_object": scenario["expect_object"],
            "expect_import_warning": scenario["expect_import_warning"],
            "label_decision": scenario["label_decision"],
            "note": scenario["note"],
            "has_object_log": bool(bundle["objects"]),
            "jitter": scenario["jitter"],
            "vehicle_gaps": scenario.get("vehicle_gaps", []),
            "planning_gaps": scenario.get("planning_gaps", []),
            "control_gaps": scenario.get("control_gaps", []),
            "object_gaps": scenario.get("object_gaps", []),
            "planning_offset": scenario.get("planning_offset", 0.0),
            "planning_period": scenario.get("planning_period", 0.1),
        }
        (EXPECTED_DIR / (scenario["case_id"] + ".json")).write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  {scenario['case_id']}  t0={bundle['t0']:>9.1f}  {bundle['duration']:5.1f}s  "
              f"行数 v/p/c/o = {len(bundle['vehicle'])}/{len(bundle['planning'])}/"
              f"{len(bundle['control'])}/{len(bundle['objects'])}  {scenario['scene']}")

    print(f"\n共生成 {len(SCENARIOS)} 个 Case → {CASES_DIR}")


if __name__ == "__main__":
    main()
