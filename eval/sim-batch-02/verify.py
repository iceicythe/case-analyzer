"""独立校验：按 workbench 口径从最终 CSV 重算全部结论，并与设计意图比对。

刻意不引用 workbench 的任何代码：窗口判定、合并桥接、候选判定、
片段边界核查、四路对齐（最近匹配 + 并列歧义 + 容差）全部按
../workbench/configs/*.json 里记录的数字独立重写，
避免生成器与工具共用同一处 off-by-one 而互相掩盖。

覆盖的期望结论（与工具实际行为一一对应）：
  1. 严格参考事件（SD-WINDOW-1.1：合并桥接受来源核查门控）
  2. 候选区间与判定（meets / below / source_unverified）
  3. 片段首尾边界核查提示
  4. 事件处规划/控制对齐 → observation_status（四类）
  5. 事件处目标对齐证据（normal / ambiguous / partial / absent）
  6. 导入采样缺口警告

运行：python verify.py                    # 默认只读校验，不写任何标签
      python verify.py --labels-out DIR   # 需要重算标签时显式写到独立目录
输出：verify_report.txt（--report 可改路径）
      --labels-out 指定时：DIR/<case_id>.json（构造真值，非人工标注）

只读是默认行为：reference_labels/ 存放人工复核生效标签，任何自动
流程都不得删除或改写它。历史版本曾无条件清空该目录重写，导致人工
复核记录被预言机产物覆盖——现在重算结果只进调用方指定的目录。
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES = ROOT / "cases"
EXPECTED = ROOT / "_expected"
# reference_labels/ 存放人工复核生效标签（human_verified）。本脚本的任何
# 自动流程都不得写入该目录——重算标签一律走 --labels-out 指定目录。
# 这里刻意不再定义指向它的常量，让“写人工标签目录”在代码里无路可走。

DT = 0.1
EPS = 1e-6                     # comparison_epsilon（configs/annotation_protocol_v1_1.json）

# --- SD-WINDOW-1.0 窗口规则（1.1 继承） ---
WINDOW_POINTS = 6              # 0.5 s @ 0.1 s -> 6 点 5 间隔
W_MIN_DROP = 1.0
W_MIN_DEC_INTERVALS = 4
W_MIN_STEP_DROP = 0.01
W_MAX_CUM_RISE = 0.1

# --- SD-WINDOW-1.1 连续性桥接 ---
B_MAX_GAP_S = 0.5
B_MAX_STEP_RISE = 0.0
B_MIN_BRIDGE_DROP = 0.01

# --- 候选粗筛（configs/candidate_screening.json） ---
C_MIN_DROP = 0.6
C_MIN_DEC_INTERVALS = 3
C_MAX_CUM_RISE = 0.2

# --- 证据链（configs/chain.json + configs/evidence.json） ---
PLAN_DECEL_THRESHOLD = -1.0
CONTROL_BRAKE_THRESHOLD = 0.5
ALIGN_TOL_S = 0.15
CONTEXT_S = 3.0
MAX_MODULE_ROWS = 256

MAX_ROWS = 200000
MAX_BYTES = 24 * 1024 * 1024
EDGE_TOL = 0.1 + EPS           # 记录抖动允许的窗口边界位移

STATUS_TEXT = {
    "vehicle_response_unexplained": "规划/控制链路证据不足",
    "control_without_plan": "控制有制动、规划无请求",
    "control_not_following_plan": "规划有请求、控制未制动",
    "planning_trigger_needs_review": "规划与控制请求共同出现",
}


# ------------------------------------------------------------------
# 读取与契约
# ------------------------------------------------------------------
def read_rows(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def parse_case(case_dir):
    """返回 {文件名: (times, 提取值列表)} 与契约问题、车辆网格偏差数。"""
    series, problems, grid_dev = {}, [], 0
    names = ("vehicle_state.csv", "planning_state.csv", "control_state.csv", "object_state.csv")
    for name in names:
        path = case_dir / name
        if not path.is_file():
            series[name] = None
            continue
        rows = read_rows(path)
        if len(rows) > MAX_ROWS:
            problems.append(f"{name} 超过 {MAX_ROWS} 行")
        ts, vals, prev = [], [], None
        bad = False
        for idx, row in enumerate(rows):
            if None in row or any(v is None for v in row.values()):
                problems.append(f"{name} 第 {idx+2} 行列数与表头不一致")
                bad = True
                break
            try:
                t = float(row["timestamp"])
                if not math.isfinite(t):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                problems.append(f"{name} 第 {idx+2} 行时间无效")
                bad = True
                break
            if prev is not None and (t < prev or (name != "object_state.csv" and t == prev)):
                problems.append(f"{name} 第 {idx+2} 行时间未严格递增")
                bad = True
                break
            if name == "vehicle_state.csv" and prev is not None and abs(t - prev - DT) > EPS:
                grid_dev += 1
            prev = t
            ts.append(t)
            if name == "vehicle_state.csv":
                v = float(row["speed_mps"])
                if not math.isfinite(v) or v < 0:
                    problems.append(f"{name} 第 {idx+2} 行车速无效")
                    bad = True
                    break
                vals.append(v)
            elif name == "planning_state.csv":
                vals.append(float(row["planned_acceleration_mps2"]))
            elif name == "control_state.csv":
                b = float(row["brake_command"])
                if not 0 <= b <= 1:
                    problems.append(f"{name} 第 {idx+2} 行制动值越界")
                    bad = True
                    break
                vals.append(b)
            else:
                d = float(row["distance_m"])
                if d < 0:
                    problems.append(f"{name} 第 {idx+2} 行距离越界")
                    bad = True
                    break
                vals.append(d)
        if bad:
            series[name] = None
        else:
            series[name] = (ts, vals)
    if series["vehicle_state.csv"] is None:
        problems.append("缺少可用的 vehicle_state.csv")
    elif len(series["vehicle_state.csv"][0]) < 6:
        problems.append("车辆日志少于 6 个采样点")
    total = sum((case_dir / n).stat().st_size for n in names if (case_dir / n).is_file())
    if total > MAX_BYTES:
        problems.append(f"总大小 {total} 超过 24 MB")
    return series, problems, grid_dev


# ------------------------------------------------------------------
# SD-WINDOW-1.1 独立实现（含网格有效性）
# ------------------------------------------------------------------
def build_windows(ts, vs):
    steps = WINDOW_POINTS - 1
    out = []
    for i in range(max(0, len(vs) - steps)):
        wt, wv = ts[i:i + WINDOW_POINTS], vs[i:i + WINDOW_POINTS]
        valid = all(abs(wt[k + 1] - wt[k] - DT) <= EPS for k in range(steps))
        if valid:
            drop = wv[0] - wv[-1]
            dec = sum(1 for k in range(steps) if wv[k] - wv[k + 1] >= W_MIN_STEP_DROP - EPS)
            rise = sum(max(0.0, wv[k + 1] - wv[k]) for k in range(steps))
        else:
            drop = dec = rise = None
        out.append({"start": wt[0], "end": wt[-1], "valid": valid,
                    "drop": drop, "dec": dec, "rise": rise})
    return out


def merge(intervals, vts, vvs, verified):
    """并集重叠合并；桥接仅在来源核查通过且缺口区间网格干净、单调不升时允许。"""
    merged = []
    for start, end in sorted(intervals):
        bridge = False
        if merged:
            prev = merged[-1][1]
            if start <= prev + EPS:
                bridge = True
            elif verified and start - prev <= B_MAX_GAP_S + EPS:
                pts = [(t, v) for t, v in zip(vts, vvs) if prev - EPS <= t <= start + EPS]
                if len(pts) >= 2 and abs(pts[0][0] - prev) <= EPS and abs(pts[-1][0] - start) <= EPS:
                    ta = [p[0] for p in pts]
                    sa = [p[1] for p in pts]
                    bridge = (all(abs(ta[k + 1] - ta[k] - DT) <= EPS for k in range(len(ta) - 1))
                              and all(sa[k + 1] - sa[k] <= B_MAX_STEP_RISE + EPS for k in range(len(sa) - 1))
                              and sa[0] - sa[-1] >= B_MIN_BRIDGE_DROP - EPS)
        if bridge:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def boundary_review(vs):
    edge_steps = WINDOW_POINTS - 2
    if len(vs) < edge_steps + 1:
        return False
    for a, b in ((0, edge_steps), (len(vs) - edge_steps - 1, len(vs) - 1)):
        seg = vs[a:b + 1]
        drop = seg[0] - seg[-1]
        dec = sum(1 for k in range(len(seg) - 1) if seg[k] - seg[k + 1] >= W_MIN_STEP_DROP - EPS)
        if drop >= W_MIN_DROP * edge_steps / (WINDOW_POINTS - 1) - EPS and dec >= W_MIN_DEC_INTERVALS - 1:
            return True
    return False


# ------------------------------------------------------------------
# 四路对齐（复刻工具 nearest：最近匹配 + 并列歧义 + 容差）
# ------------------------------------------------------------------
def nearest(times, t, tol=ALIGN_TOL_S):
    if not times:
        return None
    i = bisect.bisect_left(times, t)
    indices = [j for j in (i - 1, i) if 0 <= j < len(times)]
    if not indices:
        return None
    best = min(indices, key=lambda j: (abs(times[j] - t), times[j]))
    delta = times[best] - t
    if abs(delta) > tol + 1e-6:
        return None
    same = bisect.bisect_right(times, times[best]) - bisect.bisect_left(times, times[best])
    tied = len(indices) == 2 and abs(abs(times[indices[0]] - t) - abs(times[indices[1]] - t)) < 1e-9
    return {"i": best, "ambiguous": same > 1 or tied}


def event_status(series, a, b, vts):
    """按工具逻辑推导单个事件区间的 observation_status 与目标有效对齐数。"""
    samples = [t for t in vts if a - 1e-6 <= t <= b + 1e-6]
    if not samples:
        return None, 0, 0
    p = series["planning_state.csv"]
    c = series["control_state.csv"]
    o = series["object_state.csv"]
    p_times, p_vals = p if p else ([], [])
    c_times, c_vals = c if c else ([], [])
    o_times, _o_vals = o if o else ([], [])

    pairs = []
    obj_valid = 0
    for t in samples:
        mp = nearest(p_times, t)
        mc = nearest(c_times, t)
        if mp and mc and not mp["ambiguous"] and not mc["ambiguous"]:
            pv, cv = p_vals[mp["i"]], c_vals[mc["i"]]
            pairs.append((pv, cv))
        if o:
            mo = nearest(o_times, t)
            if mo and not mo["ambiguous"]:
                obj_valid += 1

    # 上下文行数上限（截断即不完整）
    truncated = False
    for times in (p_times, c_times):
        ctx = [x for x in times if a - CONTEXT_S - ALIGN_TOL_S - 1e-6 <= x <= b + CONTEXT_S + ALIGN_TOL_S + 1e-6]
        if len(ctx) > MAX_MODULE_ROWS:
            truncated = True

    complete = (p and c and len(pairs) == len(samples) and not truncated
                and any(a - CONTEXT_S - ALIGN_TOL_S <= x <= b + CONTEXT_S + ALIGN_TOL_S for x in p_times)
                and any(a - CONTEXT_S - ALIGN_TOL_S <= x <= b + CONTEXT_S + ALIGN_TOL_S for x in c_times))
    if not complete:
        status = "vehicle_response_unexplained"
    else:
        planned = any(pv <= PLAN_DECEL_THRESHOLD for pv, _cv in pairs)
        braked = any(cv >= CONTROL_BRAKE_THRESHOLD for _pv, cv in pairs)
        if not planned and braked:
            status = "control_without_plan"
        elif planned and not braked:
            status = "control_not_following_plan"
        elif planned and braked:
            status = "planning_trigger_needs_review"
        else:
            status = "vehicle_response_unexplained"
    return status, obj_valid, len(samples)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="独立校验：从四路 CSV 重算全部结论并与设计意图比对（默认只读）")
    parser.add_argument("--labels-out", type=Path, default=None, metavar="DIR",
                        help="重算标签（构造真值）的输出目录；不指定则不写任何标签，"
                             "reference_labels/ 中的人工复核标签不会被读取以外的动作触碰")
    parser.add_argument("--report", type=Path, default=None, metavar="PATH",
                        help="校验报告输出路径，默认 ROOT/verify_report.txt")
    args = parser.parse_args(argv)

    report_path = args.report or (ROOT / "verify_report.txt")
    labels_out = args.labels_out
    if labels_out is not None:
        labels_out.mkdir(parents=True, exist_ok=True)
        print(f"重算标签将写入 {labels_out}（不触碰 reference_labels/）")

    report, ok_all, summary = [], True, []

    for case_dir in sorted(p for p in CASES.iterdir() if p.is_dir()):
        cid = case_dir.name
        expected = json.loads((EXPECTED / (cid + ".json")).read_text(encoding="utf-8"))
        t0 = expected["t0"]

        series, problems, grid_dev = parse_case(case_dir)
        vts, vvs = series["vehicle_state.csv"] if series["vehicle_state.csv"] else ([], [])

        can_complete = (not problems and grid_dev == 0
                        and all(math.isfinite(v) and v >= 0 for v in vvs))
        windows = build_windows(vts, vvs) if vts else []

        qualifying = [(w["start"], w["end"]) for w in windows
                      if w["valid"] and w["drop"] >= W_MIN_DROP - EPS
                      and w["dec"] >= W_MIN_DEC_INTERVALS and w["rise"] <= W_MAX_CUM_RISE + EPS]
        broad = [(w["start"], w["end"]) for w in windows
                 if w["valid"] and w["drop"] >= C_MIN_DROP - EPS
                 and w["dec"] >= C_MIN_DEC_INTERVALS and w["rise"] <= C_MAX_CUM_RISE + EPS]

        events = merge(qualifying, vts, vvs, can_complete)
        cand = merge(broad, vts, vvs, can_complete)
        verdicts = []
        for a, b in cand:
            if not can_complete:
                verdicts.append("source_unverified")
            elif any(qs >= a - EPS and qe <= b + EPS for qs, qe in qualifying):
                verdicts.append("meets_numeric_rule")
            else:
                verdicts.append("below_numeric_rule")

        boundary = boundary_review(vvs) if vvs else False

        statuses, obj_valid_total, sample_total = [], 0, 0
        for a, b in events:
            status, ov, ns = event_status(series, a, b, vts)
            statuses.append(status)
            obj_valid_total += ov
            sample_total += ns

        # ---- 与设计意图比对 ----
        design = [tuple(e) for e in expected["design_events"]]
        rel_events = [(round(a - t0, 2), round(b - t0, 2)) for a, b in events]
        same_events = len(design) == len(rel_events) and all(
            abs(x[0] - y[0]) <= EDGE_TOL and abs(x[1] - y[1]) <= EDGE_TOL
            for x, y in zip(design, rel_events))
        shift = ""
        if design and len(design) == len(rel_events):
            moved = [(round(y[0] - x[0], 1), round(y[1] - x[1], 1)) for x, y in zip(design, rel_events)]
            if any(m != (0.0, 0.0) for m in moved):
                shift = f"        边界位移 {moved}（记录抖动引起，口径内正常）\n"

        diffs = []
        if not same_events:
            diffs.append(f"事件不一致：设计 {design} vs 重算 {rel_events}")
        if problems:
            diffs.append("契约：" + "；".join(problems))
        if len(cand) != expected["expect_candidates"]:
            diffs.append(f"候选数 {len(cand)} != 预期 {expected['expect_candidates']}")
        if verdicts and (len(set(verdicts)) > 1 or verdicts[0] != expected["expect_verdict"]):
            diffs.append(f"判定 {verdicts} != 预期 {expected['expect_verdict']}")
        if statuses != expected["expect_statuses"]:
            diffs.append(f"观察状态 {statuses} != 预期 {expected['expect_statuses']}")
        if boundary != expected["expect_boundary"]:
            diffs.append(f"边界核查 {boundary} != 预期 {expected['expect_boundary']}")
        if expected["expect_object"] != "absent":
            if series["object_state.csv"] is None:
                diffs.append("缺少目标日志")
            elif sample_total == 0:
                pass                      # 无事件：目标对齐为真空情形，不参与判定
            else:
                kind = ("ambiguous" if obj_valid_total == 0
                        else "normal" if obj_valid_total == sample_total else "partial")
                if kind != expected["expect_object"]:
                    diffs.append(f"目标证据 {kind}({obj_valid_total}/{sample_total}) != 预期 {expected['expect_object']}")
        elif series["object_state.csv"] is not None:
            diffs.append("预期无目标日志但文件存在")
        warn_pred = grid_dev > 0
        if warn_pred != expected["expect_import_warning"]:
            diffs.append(f"采样缺口警告 {warn_pred} != 预期 {expected['expect_import_warning']}")

        status_line = "OK " if not diffs else "DIFF"
        if diffs:
            ok_all = False

        # ---- 构造真值标签 ----
        label = {
            "case_id": cid,
            "completed": True,
            "decision": expected["label_decision"],
            "events": [[round(a, 2), round(b, 2)] for a, b in events],
            "events_relative": [[a, b] for a, b in rel_events],
            "t0_seconds": t0,
            "expected_candidate_verdict": expected["expect_verdict"],
            "expected_observation_status": expected["expect_statuses"],
            "label_origin": "constructive_truth",
            "derived_by": "verify.py 独立实现 SD-WINDOW-1.1 与四路对齐，从四路 CSV 重算",
            "scene": expected["scene"],
            "notes": (expected["note"] + " " if expected["note"] else "")
                     + "构造真值（场景设计已知）；非人工标注，不得记为人工审查结果。",
        }
        if labels_out is not None:
            (labels_out / (cid + ".json")).write_text(
                json.dumps(label, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        report.append(
            f"[{status_line}] {cid}  t0={t0:.1f}  场景={expected['scene']}\n"
            f"        设计事件 {design or '无'}\n"
            f"        重算事件 {rel_events or '无'}   候选 {len(cand)} 个 → {verdicts or '无'}\n"
            + shift +
            f"        观察状态 {[STATUS_TEXT.get(s, s) for s in statuses] or '无'}\n"
            f"        目标对齐 {obj_valid_total}/{sample_total}   网格缺口 {grid_dev} 处"
            f"   边界核查 {'是' if boundary else '否'}\n"
            f"        契约 {'通过' if not problems else '；'.join(problems)}"
            + ("" if not diffs else "\n        !! " + "\n           ".join(diffs)))
        summary.append((cid, len(events), len(cand), len(vvs)))

    header = [
        "生成数据集独立校验报告（完整口径预言机）",
        f"Case 数 {len(summary)}",
        f"SD-WINDOW-1.1 独立实现；容差 1e-6；桥接受来源核查门控；对齐容差 0.15 s",
        "=" * 78, "",
    ]
    tail = ["", "=" * 78,
            f"{'case':<10}{'事件数':>6}{'候选数':>8}{'车速点':>8}",
            "-" * 78]
    for cid, ne, nc, nv in summary:
        tail.append(f"{cid:<10}{ne:>6}{nc:>8}{nv:>8}")
    tail += ["", "结论：" + ("全部一致，可导入。" if ok_all else "存在差异，见上方 DIFF 行。")]

    text = "\n".join(header + report + tail) + "\n"
    report_path.write_text(text, encoding="utf-8")
    print(text)
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
