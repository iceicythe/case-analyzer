"""Shared active event definition and continuous-deceleration interval merging."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from app.paths import PROJECT_ROOT

PROTOCOL_ID = "SD-WINDOW-1.1"
WINDOW_PATH = PROJECT_ROOT / "configs/annotation_protocol_v1.json"
MERGE_PATH = PROJECT_ROOT / "configs/annotation_protocol_v1_1.json"
LABEL_DEFINITION = PROTOCOL_ID + "：持续明显纵向减速区间，规则见 docs/ANNOTATION_PROTOCOL_1_1.md；正常制动也可能达标，不代表故障"


def load_annotation_protocol() -> tuple[dict, str]:
    """Fingerprint both inherited window rules and active merge rules."""
    window_bytes, merge_bytes = WINDOW_PATH.read_bytes(), MERGE_PATH.read_bytes()
    window, merge = json.loads(window_bytes), json.loads(merge_bytes)
    if (window.get("protocol_id") != "SD-WINDOW-1.0" or merge.get("protocol_id") != PROTOCOL_ID
            or merge.get("inherits_window_and_quality_rules_from") != window["protocol_id"]):
        raise ValueError("标注口径版本不匹配")
    for key in ("maximum_bridge_gap_seconds", "maximum_per_step_speed_increase_mps",
                "minimum_bridge_speed_drop_mps", "comparison_epsilon"):
        value = merge[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("合并规则参数无效：" + key)
    protocol = {**window, "protocol_id": PROTOCOL_ID, "window_protocol_id": window["protocol_id"],
                "merge_rule": merge, "boundary_rule": merge["bridge_rule"],
                "documentation": merge["documentation"], "scope": "active human and AI event review",
                "config_sha256": {"window": hashlib.sha256(window_bytes).hexdigest(),
                                  "merge": hashlib.sha256(merge_bytes).hexdigest()}}
    return protocol, hashlib.sha256(window_bytes + b"\0" + merge_bytes).hexdigest()


def merge_intervals(intervals: list[tuple[float, float]], points: list[dict], rule: dict,
                    source_verified: bool, sample_period: float = .1) -> list[list[float]]:
    """Union overlaps; bridge only a complete, verified non-increasing gap."""
    eps = rule["comparison_epsilon"]
    merged: list[list[float]] = []
    timed_points = [p for p in points if p.get("timestamp") is not None and np.isfinite(p["timestamp"])]
    for start, end in sorted(intervals):
        bridge = False
        if merged:
            previous = merged[-1][1]
            if start <= previous + eps:
                bridge = True
            elif source_verified and start - previous <= rule["maximum_bridge_gap_seconds"] + eps:
                gap = [p for p in timed_points if previous - eps <= p["timestamp"] <= start + eps]
                speed = np.array([p["speed_mps"] for p in gap], dtype=float)
                time = np.array([p["timestamp"] for p in gap], dtype=float)
                bridge = (len(speed) >= 2 and np.isfinite(speed).all()
                          and abs(time[0] - previous) <= eps and abs(time[-1] - start) <= eps
                          and np.all(np.abs(np.diff(time) - sample_period) <= eps)
                          and np.all(np.diff(speed) <= rule["maximum_per_step_speed_increase_mps"] + eps)
                          and speed[0] - speed[-1] >= rule["minimum_bridge_speed_drop_mps"] - eps)
        if bridge:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
