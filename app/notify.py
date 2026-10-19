"""Push run summaries to a Feishu (Lark) custom bot webhook.

The webhook URL is a secret: it comes from --notify or the FEISHU_WEBHOOK_URL
environment variable and is never written into reports, logs or this file.
Sending is a side channel — any network failure returns an error string for
the caller to print as a warning, and never interrupts the run itself.
"""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import http.client
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from app.paths import PROJECT_ROOT

ENV_WEBHOOK = 'FEISHU_WEBHOOK_URL'
TIMEOUT_SECONDS = 5.0
CONFIG_PATH = PROJECT_ROOT / 'configs/feishu.local.json'
DEFAULT_CONFIG = {'enabled':False, 'webhook_url':'', 'signing_secret':'',
                  'keyword':'case-analyzer', 'workbench_url':'', 'summary_window_seconds':5}


def load_config(cli_value='', path=None):
    """Local file plus environment overrides. Never return parser/secret text in errors."""
    config = dict(DEFAULT_CONFIG, error=None)
    try:
        source = Path(path) if path is not None else CONFIG_PATH
        if source.exists():
            value = json.loads(source.read_text(encoding='utf-8-sig'))
            if not isinstance(value, dict) or set(value) - set(DEFAULT_CONFIG):
                raise ValueError()
            config.update(value)
        if type(config['enabled']) is not bool:
            raise ValueError()
        for key in ('webhook_url','signing_secret','keyword','workbench_url'):
            if not isinstance(config[key], str):
                raise ValueError()
            config[key] = config[key].strip()
        if (type(config['summary_window_seconds']) is not int
                or not 1 <= config['summary_window_seconds'] <= 60 or len(config['keyword']) > 60):
            raise ValueError()
        override = cli_value.strip() or os.environ.get(ENV_WEBHOOK, '').strip()
        if override:
            if override != config['webhook_url']:
                config['signing_secret'] = ''
            config.update(webhook_url=override, enabled=True)
        if 'FEISHU_SIGNING_SECRET' in os.environ:
            config['signing_secret'] = os.environ['FEISHU_SIGNING_SECRET'].strip()
        if config['enabled']:
            url = urlsplit(config['webhook_url'])
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
                raise ValueError()
        if config['workbench_url']:
            url = urlsplit(config['workbench_url'])
            if (url.scheme not in {'http','https'} or not url.hostname or url.username or url.password
                    or url.query or url.fragment or any(c in config['workbench_url'] for c in '\r\n<>')):
                raise ValueError()
    except (OSError, ValueError, TypeError):
        return dict(DEFAULT_CONFIG, error='飞书配置无效，请检查 configs/feishu.local.json 与环境变量')
    return config


def resolve_webhook(cli_value: str = '') -> str:
    """Command-line value wins over the environment; empty means disabled."""
    config = load_config(cli_value)
    if config['error']:
        print(config['error'], file=sys.stderr)
    return config['webhook_url'] if config['enabled'] else ''


def mask_webhook(webhook: str) -> str:
    """Host only: the path carries the token and must stay out of messages."""
    try:
        return urlsplit(webhook).hostname or 'webhook'
    except ValueError:
        return 'webhook'


def send_card(webhook: str, title: str, lines: list[str], note: str = '',
              template: str = 'blue', *, signing_secret=None, keyword=None) -> str | None:
    """POST one interactive card. Returns None on success, an error string otherwise."""
    if not webhook:
        return None
    if signing_secret is None or keyword is None:
        config = load_config(webhook)
        if config['error']:
            return config['error']
        if signing_secret is None:
            signing_secret = config['signing_secret']
        if keyword is None:
            keyword = config['keyword']
    if keyword and not any(keyword in text for text in (title, note, *lines)):
        note = (note + ' · ' + keyword).strip(' ·')
    elements = [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': '\n'.join(lines)}}]
    if note:
        elements.append({'tag': 'note', 'elements': [{'tag': 'plain_text', 'content': note}]})
    payload = {'msg_type': 'interactive', 'card': {
        'config': {'wide_screen_mode': True},
        'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': template},
        'elements': elements}}
    if signing_secret:
        stamp = str(int(time.time()))
        digest = hmac.new((stamp+'\n'+signing_secret).encode('utf-8'), digestmod=hashlib.sha256).digest()
        payload.update(timestamp=stamp, sign=base64.b64encode(digest).decode('ascii'))
    try:
        parsed = urlsplit(webhook)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            return '飞书推送失败：webhook 必须是有效的 HTTPS 地址'
        request = urllib.request.Request(
            webhook, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            content = response.read(65537)
            if len(content) > 65536:
                return '飞书推送失败：响应超过大小限制'
            body = json.loads(content.decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError, TypeError, http.client.HTTPException) as error:
        # Provider text and exception messages can echo the complete secret URL.
        return '飞书推送失败（%s）：%s' % (mask_webhook(webhook), type(error).__name__)
    if not isinstance(body, dict):
        return '飞书推送失败：响应格式无效'
    code = body.get('code') if 'code' in body else body.get('StatusCode')
    if type(code) is not int:
        return '飞书推送失败：响应缺少有效状态码'
    if code != 0:
        return '飞书返回错误（%s）：状态码 %d' % (mask_webhook(webhook), code)
    return None


def import_card(summary: dict) -> tuple[str, list[str], str]:
    """Title, markdown lines and footnote for one batch-import summary."""
    totals, source = dict(summary['totals']), summary.get('source', {})
    totals['failed'] = totals['error'] + totals['invalid']
    if summary.get('dry_run'):
        # 预检不写入：可导入数看 ready，不拿 imported=0 说“已导入 0”
        head = ('**可导入 %(ready)d** · 内容相同跳过 %(skipped)d · 编号冲突 %(conflict)d · 失败 %(failed)d'
                '（预检，未写入）' % totals)
    else:
        head = ('**已导入 %(imported)d** · 内容相同跳过 %(skipped)d · 编号冲突 %(conflict)d · 失败 %(failed)d'
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
    if summary.get('evaluation_scope') == 'event_review':
        lines.append('评估范围：事件复核；排查建议质量未评测')
        lines.append('复核失败候选：%s' % summary['execution']['failed_candidates'])
    return title, lines, 'case-analyzer 冻结评估 · 先冻结预测再回放标签'
