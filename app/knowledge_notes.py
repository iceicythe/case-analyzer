"""Render knowledge-base entries with the current case's own numbers.

A KB entry is a static troubleshooting manual; quoting it verbatim next to a
concrete event tells the reader nothing they could not read in the manual
itself. Each catalog entry may therefore carry a `case_note_template` with
`{placeholders}` that this module fills from the already validated event
summary. Templates only restate observed counts and intervals -- they never
assert a cause, so a template stays truthful even if retrieval routes the
entry to an event it was not written for.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.retriever import DEFAULT_CATALOG

_PLACEHOLDER = re.compile(r'\{(\w+)\}')


def case_placeholders(event) -> dict:
    """Values a template may reference; every number comes from the event."""
    detail = event['context_summary']
    start, end = event['confirmed_interval']
    coverage = event['alignment_summary']
    total = coverage['vehicle_event_sample_count']
    reasons = detail['planning_reasons']
    reasons_text = '、'.join(f'{name} {count} 条' for name, count in reasons.items()) or '无唯一有效对齐记录'
    switch = detail['reason_transitions'][0] if detail['reason_transitions'] else None
    return {
        'interval': f'{start:.1f}～{end:.1f} 秒',
        'planning_reasons': reasons_text,
        'reason_switch': (f"{switch['time']:.2f} 秒 {switch['before']} → {switch['after']}"
                          if switch else '未见相邻有效记录的原因变化'),
        'planning_coverage': f"规划 {coverage['planning_state.csv']['valid_event_samples']}/{total} 点",
        'control_coverage': f"控制 {coverage['control_state.csv']['valid_event_samples']}/{total} 点",
        'object_missing': str(detail['object_missing_samples']),
        'object_ambiguous': str(detail['object_ambiguous_samples']),
        'object_switches': str(detail['object_switch_count']),
        'object_ids': str(detail['object_id_count']),
        'object_rows': str(detail['object_rows']),
    }


def render_case_note(template: str, event) -> str:
    """Fill a template with this case's numbers; unknown slots stay explicit."""
    values = case_placeholders(event)
    return _PLACEHOLDER.sub(lambda match: values.get(match.group(1), '未提供'), template)


def case_notes(event, catalog_path: Path = DEFAULT_CATALOG) -> list[dict]:
    """Per-referenced-KB note for one event, template read from the catalog."""
    catalog = json.loads(Path(catalog_path).read_text(encoding='utf-8'))
    templates = {entry['knowledge_id']: entry for entry in catalog}
    notes = []
    for reference in event.get('knowledge', []):
        template = (templates.get(reference['knowledge_id']) or {}).get('case_note_template', '')
        notes.append({'knowledge_id': reference['knowledge_id'],
                      'title': reference['title'],
                      'source_file': reference['source_file'],
                      'note': render_case_note(template, event) if template else ''})
    return notes
