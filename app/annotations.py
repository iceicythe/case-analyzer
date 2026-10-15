"""Human annotation templates, validation and atomic storage."""
from __future__ import annotations
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from app.paths import PROJECT_ROOT
from app.data_io import load_dataframe
from app.annotation_protocol import PROTOCOL_ID, LABEL_DEFINITION
DEFAULT_REAL_CASES_ROOT = PROJECT_ROOT / 'runtime/data'
DEFAULT_ANNOTATIONS_ROOT = PROJECT_ROOT / 'runtime/annotations'
ANNOTATION_SCHEMA_VERSION = '1.0'
CASE_LABELS = {'positive', 'negative', 'uncertain'}
class AnnotationValidationError(ValueError):
    pass

def _annotation_template(case_dir: Path, data_root: Path) -> dict[str, Any]:
    dataframe = load_dataframe(case_dir / "vehicle_state.csv")
    try:
        source_case = str(case_dir.relative_to(PROJECT_ROOT))
    except ValueError:
        source_case = str(case_dir)
    template = {
        "schema_version": ANNOTATION_SCHEMA_VERSION,
        "case_id": case_dir.name,
        "source_case_dir": source_case,
        "annotation_status": "pending",
        "case_label": "uncertain",
        "annotator": "",
        "annotated_at": "",
        "label_definition": (
            "人工确认的持续纵向减速事件；events 为空且状态为 completed "
            "表示已确认该 Case 没有目标事件"
        ),
        "case_time_range_seconds": {
            "start": float(dataframe["timestamp"].min()),
            "end": float(dataframe["timestamp"].max()),
        },
        "events": [],
        "notes": "",
    }
    metadata_path = case_dir / "source_metadata.json"
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("schema") == "case-import-1.0":
            template["annotation_protocol_id"] = PROTOCOL_ID
            template["label_definition"] = LABEL_DEFINITION + "；模板尚待人工审查"
    return template

def _validate_annotation(
    annotation: dict[str, Any],
    case_dir: Path,
    require_completed: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    status = annotation.get("annotation_status")
    case_label = annotation.get("case_label")
    if annotation.get("schema_version") != ANNOTATION_SCHEMA_VERSION:
        errors.append(f"schema_version 必须为 {ANNOTATION_SCHEMA_VERSION}")
    if annotation.get("case_id") != case_dir.name:
        errors.append("case_id 与 Case 文件夹名称不一致")
    if status not in {"pending", "completed"}:
        errors.append("annotation_status 必须为 pending 或 completed")
    if case_label not in {None, "", *CASE_LABELS}:
        errors.append("case_label 必须为 positive、negative 或 uncertain")
    if require_completed and status != "completed":
        errors.append("标注尚未完成")

    events = annotation.get("events")
    if not isinstance(events, list):
        errors.append("events 必须是数组")
        events = []
    if status == "completed":
        if not str(annotation.get("annotator", "")).strip():
            errors.append("completed 标注必须填写 annotator")
        if not str(annotation.get("annotated_at", "")).strip():
            errors.append("completed 标注必须填写 annotated_at")
        else:
            try:
                datetime.fromisoformat(
                    str(annotation["annotated_at"]).replace("Z", "+00:00")
                )
            except ValueError:
                errors.append("annotated_at 必须是 ISO 8601 时间")

    dataframe = load_dataframe(case_dir / "vehicle_state.csv")
    data_start = float(dataframe["timestamp"].min())
    data_end = float(dataframe["timestamp"].max())
    ids: set[str] = set()
    intervals: list[tuple[float, float, str]] = []
    cleaned_events: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        prefix = f"events[{index - 1}]"
        if not isinstance(event, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        event_id = str(event.get("event_id", "")).strip()
        if not event_id:
            errors.append(f"{prefix}.event_id 不能为空")
        elif event_id in ids:
            errors.append(f"事件编号重复：{event_id}")
        ids.add(event_id)
        try:
            start = float(event["start_time"])
            end = float(event["end_time"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{prefix} 的 start_time/end_time 必须是数字")
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            errors.append(f"{prefix} 的时间必须是有限数字")
            continue
        if end <= start:
            errors.append(f"{prefix} 的 end_time 必须大于 start_time")
        if start < data_start or end > data_end:
            errors.append(
                f"{prefix} 超出日志范围 {data_start:.3f}～{data_end:.3f}s"
            )
        intervals.append((start, end, event_id))
        cleaned_events.append(
            {
                **event,
                "event_id": event_id,
                "start_time": start,
                "end_time": end,
            }
        )

    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1]:
            errors.append(
                f"事件时间重叠：{previous[2]} 与 {current[2]}，请合并或修正"
            )
    if status == "completed" and case_label == "positive" and not cleaned_events:
        errors.append("positive 标注至少需要一个人工事件")
    if status == "completed" and case_label == "negative" and cleaned_events:
        errors.append("negative 标注的 events 必须为空")
    if status == "completed" and case_label == "uncertain":
        errors.append("uncertain 标注应保持 pending，不能标记为 completed")
    if errors:
        raise AnnotationValidationError(
            f"{case_dir.name} 标注校验失败：\n- " + "\n- ".join(errors)
        )
    return {**annotation, "events": cleaned_events}

def save_annotation(
    case_dir: Path,
    annotations_root: Path,
    updates: dict[str, Any],
    *,
    confirmed_protocol_id: str | None = None,
) -> dict[str, Any]:
    """Validate and atomically save the fields editable in the workbench."""
    annotations_root = annotations_root.resolve()
    annotation_path = annotations_root / f"{case_dir.name}.json"
    if not annotation_path.is_file():
        raise AnnotationValidationError(
            f"缺少标注模板：{annotation_path}"
        )
    try:
        current = json.loads(annotation_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise AnnotationValidationError(
            f"无法读取现有标注：{annotation_path}"
        ) from error

    editable_fields = {
        "annotation_status",
        "annotator",
        "annotated_at",
        "case_label",
        "events",
        "notes",
        "review_confirmation",
    }
    candidate = dict(current)
    for field in editable_fields:
        if field in updates:
            candidate[field] = updates[field]
    if confirmed_protocol_id is not None:
        confirmation = candidate.get("review_confirmation") or {}
        if (confirmed_protocol_id != PROTOCOL_ID or candidate.get("annotation_status") != "completed"
                or confirmation.get("protocol_id") != PROTOCOL_ID or confirmation.get("whole_case_reviewed") is not True):
            raise AnnotationValidationError("口径升级需要重新人工确认完成")
        candidate["annotation_protocol_id"] = confirmed_protocol_id
        candidate["label_definition"] = LABEL_DEFINITION
    validated = _validate_annotation(candidate, case_dir)

    temporary_path = annotation_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(validated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(annotation_path)
    return validated
