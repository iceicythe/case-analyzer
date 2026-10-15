"""Read-only calculations using the shared active human and AI protocol."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.paths import PROJECT_ROOT
from app.annotation_protocol import PROTOCOL_ID, load_annotation_protocol, merge_intervals


def _source_quality(case_dir, start, end, protocol, grid, csv_speed):
    from app.storage import source_quality
    return source_quality(case_dir, protocol, grid, csv_speed)


def build_annotation_assist(case_dir: Path) -> dict[str, Any]:
    """Calculate every window and reference interval without writing any labels."""
    protocol, protocol_sha256 = load_annotation_protocol()
    data_bytes = (case_dir / "vehicle_state.csv").read_bytes()
    data = pd.read_csv(BytesIO(data_bytes))
    if not {"timestamp", "speed_mps"}.issubset(data.columns):
        raise ValueError("车辆 CSV 缺少时间或速度列")
    t = pd.to_numeric(data["timestamp"], errors="coerce").to_numpy(dtype=float)
    speed = pd.to_numeric(data["speed_mps"], errors="coerce").to_numpy(dtype=float)
    eps = protocol["comparison_epsilon"]
    period = protocol["sample_period_seconds"]
    steps = round(protocol["window_seconds"] / period)
    issues = []
    sample_ok = np.isfinite(t) & np.isfinite(speed) & (speed >= 0)
    grid_ok = np.isfinite(np.diff(t)) & (np.abs(np.diff(t) - period) <= eps)
    if not sample_ok.all():
        issues.append("CSV 存在缺失、无效或负速度，请保持待标注")
    if not grid_ok.all():
        issues.append("CSV 时间不是连续的 0.1 秒网格，请保持待标注")
    if len(data) < steps + 1:
        issues.append("数据不足一个完整的 0.5 秒窗口")
    finite_t = t[np.isfinite(t)]
    source = _source_quality(case_dir, float(finite_t.min()), float(finite_t.max()), protocol, t, speed) if len(finite_t) else {"verified": False, "issues": ["没有有效时间"], "gaps": []}
    issues.extend(source["issues"])
    precision = 12 if source.get('source_type') == 'synthetic' else 6
    windows = []
    for i in range(max(0, len(data) - steps)):
        valid = bool(sample_ok[i:i + steps + 1].all() and grid_ok[i:i + steps].all())
        delta = np.diff(speed[i:i + steps + 1])
        drop = float(speed[i] - speed[i + steps]) if valid else None
        decreases = int((delta <= -protocol["decreasing_interval_minimum_drop_mps"] + eps).sum()) if valid else 0
        recovery = float(np.maximum(delta, 0).sum()) if valid else None
        start = float(t[i]) if np.isfinite(t[i]) else None
        end = float(t[i + steps]) if np.isfinite(t[i + steps]) else None
        intersects_gap = bool(start is not None and end is not None and any(g["start_time"] < end and g["end_time"] > start for g in source["gaps"]))
        checks = {"speed_drop": bool(valid and drop >= protocol["minimum_speed_drop_mps"] - eps),
                  "sustained": bool(valid and decreases >= protocol["minimum_decreasing_intervals"]),
                  "recovery": bool(valid and recovery <= protocol["maximum_cumulative_speed_increase_mps"] + eps)}
        windows.append({"index": i, "start_time": start, "end_time": end,
                        "speed_drop_mps": round(drop, precision) if drop is not None else None,
                        "average_acceleration_mps2": round(-drop / protocol["window_seconds"], 6) if valid else None,
                        "decreasing_intervals": decreases,
                        "cumulative_recovery_mps": round(recovery, precision) if recovery is not None else None,
                        "checks": checks, "data_valid": valid and not intersects_gap,
                        "qualifies": valid and not intersects_gap and all(checks.values())})
    acceleration = pd.to_numeric(data.get("acceleration_mps2", pd.Series(np.nan, index=data.index)), errors="coerce").to_numpy(dtype=float)
    points = [{"timestamp": float(x) if np.isfinite(x) else None,
               "speed_mps": float(y) if np.isfinite(y) else None,
               "acceleration_mps2": float(z) if np.isfinite(z) else None,
               "source_row": i + 2} for i, (x, y, z) in enumerate(zip(t, speed, acceleration))]
    qualifying = [w for w in windows if w["qualifies"]]
    intervals = merge_intervals([(w["start_time"], w["end_time"]) for w in qualifying],
                                points, protocol["merge_rule"], not issues, period)
    events: list[dict[str, Any]] = []
    for start, end in intervals:
        contained = [w for w in qualifying if start - eps <= w["start_time"] and w["end_time"] <= end + eps]
        events.append({"start_time": start, "end_time": end, "window_count": len(contained),
                       "representative_window_index": contained[0]["index"]})
    boundary_review = False
    if sample_ok.all() and len(data) >= steps + 1:
        edge_steps = steps - 1
        for a, b in ((0, edge_steps), (len(t) - edge_steps - 1, len(t) - 1)):
            edge_delta = np.diff(speed[a:b + 1])
            minimum_edge_drop = protocol["minimum_speed_drop_mps"] * edge_steps / steps
            if (speed[a] - speed[b] >= minimum_edge_drop - eps and
                    (edge_delta <= -protocol["decreasing_interval_minimum_drop_mps"] + eps).sum() >= protocol["minimum_decreasing_intervals"] - 1):
                boundary_review = True
    for event in events:
        w = windows[event["representative_window_index"]]
        event["notes"] = (f"{PROTOCOL_ID}；工具参考窗口 {w['start_time']:.1f}～{w['end_time']:.1f}s，"
                          f"速度下降 {w['speed_drop_mps']:.3f}m/s，平均加速度 {w['average_acceleration_mps2']:.3f}m/s²，"
                          f"{w['decreasing_intervals']}/5间隔下降，累计回升 {w['cumulative_recovery_mps']:.3f}m/s；"
                          f"合并 {event['window_count']} 个合格窗口。")
    return {"protocol": protocol, "case_id": case_dir.name,
            "protocol_sha256": protocol_sha256,
            "vehicle_sha256": hashlib.sha256(data_bytes).hexdigest(),
            "points": points, "windows": windows, "reference_events": events,
            "summary": {"window_count": len(windows), "qualifying_window_count": sum(w["qualifies"] for w in windows), "reference_event_count": len(events)},
            "quality": {"can_complete": not issues, "issues": issues, "original_can": source,
                        "boundary_review_required": boundary_review},
            "negative_notes": f"{PROTOCOL_ID}；工具检查 {len(windows)} 个窗口，未发现满足全部条件的窗口。" if not events else ""}
