"""Self-contained, read-only viewer for an exported report snapshot."""
from __future__ import annotations

import json
from pathlib import Path


VERSION = 'offline-report-viewer-1.0'
TEMPLATE = (Path(__file__).resolve().parents[1] / 'static').joinpath('offline_report.html')
EVIDENCE_VIEWER = (Path(__file__).resolve().parents[1] / 'static').joinpath('evidence_viewer.js')


def render_offline_report(manifest, reports):
    if not reports or len(reports) != len(manifest['included']):
        raise ValueError('离线报告清单与文档数量不一致')
    payload = json.dumps({'version': VERSION, 'manifest': manifest, 'reports': reports},
                         ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    # The data is inert JSON; closing tags and JS line separators must not escape it.
    payload = payload.replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    template = TEMPLATE.read_text(encoding='utf-8')
    for marker in ('__REPORT_DATA__', '__EVIDENCE_VIEWER__'):
        if template.count(marker) != 1:
            raise ValueError('离线报告模板占位符无效')
    # Insert user data last, so text resembling a template marker stays literal.
    return template.replace('__EVIDENCE_VIEWER__', EVIDENCE_VIEWER.read_text(encoding='utf-8')).replace('__REPORT_DATA__', payload)
