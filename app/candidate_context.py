"""Vehicle evidence for event review, kept separate from cause investigation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.paths import PROJECT_ROOT

CONTEXT_PATH = PROJECT_ROOT / "configs/candidate_context.json"
VERSION = "candidate-context-1.0"


def enrich_case(case_dir: Path, case: dict, config_path: Path = CONTEXT_PATH) -> dict:
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    if config.get("profile_id") != VERSION or config.get("scope") != "event_review":
        raise ValueError("Invalid event review context configuration")
    if hashlib.sha256((case_dir / "vehicle_state.csv").read_bytes()).hexdigest() != case["vehicle_sha256"]:
        raise ValueError("车辆数据在构建证据期间变化，请重新打开 Case")
    metadata_path = case_dir / "source_metadata.json"
    metadata_bytes = metadata_path.read_bytes() if metadata_path.is_file() else None
    metadata_sha = hashlib.sha256(metadata_bytes).hexdigest() if metadata_bytes is not None else None
    expected_sha = case["quality"]["original_can"].get("metadata_sha256")
    if metadata_sha != expected_sha:
        raise ValueError("来源数据在构建证据期间变化，请重新打开 Case")
    try:
        metadata = json.loads(metadata_bytes) if metadata_bytes else {}
    except ValueError:
        metadata = {}
    conversion = metadata.get("conversion", {}) if isinstance(metadata, dict) else {}
    if not isinstance(conversion, dict):
        conversion = {}
    source = conversion.get("acceleration_source")
    source = source if isinstance(source, str) else None
    smoothing = conversion.get("speed_smoothing_window_samples")
    smoothing = smoothing if type(smoothing) is int and smoothing > 0 else None
    derived = any(token in (source or "").lower() for token in ("finite difference", "由车速", "差分", "求导"))
    synthetic = case['quality']['original_can'].get('source_type') == 'synthetic'
    if synthetic:
        source = metadata.get('signal_sources', {}).get('vehicle_state.csv', {}).get('acceleration_mps2')
        derived = isinstance(source, str) and any(token in source.lower() for token in ('finite difference', '由车速', '差分', '求导'))
    provenance = {
        "acceleration_source": source,
        "speed_smoothing_window_samples": smoothing,
        "acceleration_independent_measurement": False if derived else None,
        "interpretation": "速度求导是辅助证据，不是独立传感器测量" if derived else "来源未证实为独立传感器测量",
    }
    case["acceleration_provenance"] = provenance
    if synthetic:
        provenance.update(source_type='synthetic', source_label='合成数据',
                          interpretation='合成数据；加速度来源以提供方声明为准，不据此确认独立测量')
    case["context_source_hashes"] = {"candidate_context_config": hashlib.sha256(config_bytes).hexdigest()}
    case["context_version"] = VERSION
    for candidate in case["candidates"]:
        # Auxiliary module availability belongs to cause investigation. Removing it
        # also prevents unrelated planning/radar changes invalidating event reviews.
        candidate["module_presence"] = {}
        candidate["evidence"] = [e for e in candidate["evidence"] if e["evidence_id"] not in {"E005", "E007"}]
        points = candidate["points"]
        event_points = [p for p in points if candidate["start_time"] - 1e-6 <= p["timestamp"] <= candidate["end_time"] + 1e-6]
        values = [p["acceleration_mps2"] for p in event_points if p["acceleration_mps2"] is not None]
        candidate["supplemental_context"] = {
            "version": VERSION, "scope": "event_review", "source_file": "vehicle_state.csv",
            "provenance": provenance,
            "context_sample_count": len(points),
            "valid_acceleration_sample_count": sum(p["acceleration_mps2"] is not None for p in points),
            "missing_acceleration_sample_count": sum(p["acceleration_mps2"] is None for p in points),
            "candidate_acceleration_minimum_mps2": min(values) if values else None,
            "candidate_acceleration_maximum_mps2": max(values) if values else None,
            "truncated": False,
        }
        candidate["evidence"].append({"evidence_id": "E007", "source_file": "vehicle_state.csv",
            "source_rows": [p["source_row"] for p in event_points],
            "statement": f"已提供完整上下文逐点车速与加速度；候选内有效加速度 {len(values)} 点；加速度来源：{source or '未声明'}；{provenance['interpretation']}。缺失值不是零；事件仍按固定车速窗口规则判断。"})
    return case
