"""Push run summaries to a Feishu (Lark) custom bot webhook.

The webhook URL is a secret: it comes from --notify or the FEISHU_WEBHOOK_URL
environment variable and is never written into reports, logs or this file.
Sending is a side channel — any network failure returns an error string for
the caller to print as a warning, and never interrupts the run itself.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ENV_WEBHOOK = 'FEISHU_WEBHOOK_URL'
TIMEOUT_SECONDS = 5.0


def resolve_webhook(cli_value: str = '') -> str:
    """Command-line value wins over the environment; empty means disabled."""
    value = (cli_value or '').strip()
    return value or os.environ.get(ENV_WEBHOOK, '').strip()


def mask_webhook(webhook: str) -> str:
    """Host only: the path carries the token and must stay out of messages."""
    return urlsplit(webhook).hostname or 'webhook'


def send_card(webhook: str, title: str, lines: list[str], note: str = '',
              template: str = 'blue') -> str | None:
    """POST one interactive card. Returns None on success, an error string otherwise."""
    if not webhook:
        return None
    elements = [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': '\n'.join(lines)}}]
    if note:
        elements.append({'tag': 'note', 'elements': [{'tag': 'plain_text', 'content': note}]})
    payload = {'msg_type': 'interactive', 'card': {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': template},
        'elements': elements}}
    request = urllib.request.Request(
        webhook, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError) as error:
        return '飞书推送失败（%s）：%s' % (mask_webhook(webhook), error)
    if body.get('code') != 0 and body.get('StatusCode') != 0:
        return '飞书返回错误（%s）：%s' % (mask_webhook(webhook), body.get('msg') or body)
    return None


def import_card(summary: dict) -> tuple[str, list[str], str]:
    """Title, markdown lines and footnote for one batch-import summary."""
    totals, source = summary['totals'], summary.get('source', {})
    if summary.get('dry_run'):
        # 预检不写入：可导入数看 ready，不拿 imported=0 说“已导入 0”
        head = ('**可导入 %(ready)d** · 内容相同跳过 %(skipped)d · 编号冲突 %(conflict)d · 失败 %(error)d'
                '（预检，未写入）' % totals)
    else:
        head = ('**已导入 %(imported)d** · 内容相同跳过 %(skipped)d · 编号冲突 %(conflict)d · 失败 %(error)d'
                '（已写入）' % totals)
    title = '批量导入：%s' % (source.get('name') or Path(summary.get('root', '.')).name)
    lines = [head,
             '来源类型：%(type)s · Case 总数 %(total)d' % dict(source, total=totals['total'])]
    failed = [item for item in summary.get('cases', []) if item['status'] in ('error', 'invalid')]
    if failed:
        lines.append('失败明细（最多 10 条）：')
        lines += ['- %s：%s' % (item['case_id'] or '-', item['message']) for item in failed[:10]]
        if len(failed) > 10:
            lines.append('- …等 %d 条' % (len(failed) - 10))
    return title, lines, 'case-analyzer 批量导入 · %s' % summary.get('started_at', '')


def benchmark_card(summary: dict) -> tuple[str, list[str], str]:
    """Title, markdown lines and footnote for one unblinded metrics summary."""
    metrics = summary.get('metrics', {})
    title = '效果评估：%s' % summary.get('name', '未命名批次')
    lines = [
        '**候选覆盖率 %(candidate_recall)s**（%(candidate_covered_events)s/%(gold_event_count)s）' % metrics,
        '**自动确认 Precision %(automatic_confirmation_precision)s** · Recall %(automatic_event_recall)s'
        % metrics,
        'TP %(tp)s · FP %(fp)s · FN %(fn)s · 误拒绝 %(wrongly_rejected_gold_events)s' % metrics,
        '自动处理覆盖率 %(automatic_processing_coverage)s（%(automatic_candidates)s/%(total_candidates)s）' % metrics,
        '评估 Case %(evaluated_cases)s · 排除 %(excluded_cases)s' % metrics]
    return title, lines, 'case-analyzer 冻结评估 · 先冻结预测再回放标签'
