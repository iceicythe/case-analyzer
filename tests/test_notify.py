"""Feishu webhook side channel: payload shape, failure isolation, no secrets."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.__main__ import main as app_main
from app.notify import (ENV_WEBHOOK, benchmark_card, import_card, mask_webhook,
                        resolve_webhook, send_card)
from app.workspace import initialize

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
from generate_demo import demo_cases


def ok_response(body=b'{"code":0,"msg":"success"}'):
    response = MagicMock()
    response.read.return_value = body
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.webhook = 'https://open.feishu.cn/open-apis/bot/v2/hook/secret-token-123'

    def test_send_card_posts_interactive_card_without_token_in_errors(self):
        with patch('app.notify.urllib.request.urlopen', return_value=ok_response()) as urlopen:
            error = send_card(self.webhook, '标题', ['**一行**', '两行'], note='脚注')
        self.assertIsNone(error)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, self.webhook)
        self.assertEqual(request.get_method(), 'POST')
        payload = json.loads(request.data.decode('utf-8'))
        self.assertEqual(payload['msg_type'], 'interactive')
        self.assertEqual(payload['card']['header']['title']['content'], '标题')
        self.assertEqual(payload['card']['header']['template'], 'blue')
        self.assertEqual(payload['card']['elements'][0]['text']['content'], '**一行**\n两行')
        self.assertEqual(payload['card']['elements'][1]['tag'], 'note')

    def test_network_and_protocol_failures_never_raise(self):
        import urllib.error
        cases = [
            urllib.error.URLError('connection refused'),
            TimeoutError('timed out'),
            ValueError('unknown url type'),
        ]
        for error in cases:
            with patch('app.notify.urllib.request.urlopen', side_effect=error):
                message = send_card(self.webhook, '标题', ['内容'])
            self.assertIsInstance(message, str)
            self.assertIn('飞书', message)
            self.assertNotIn('secret-token', message)  # token 不出现在任何消息里
        with patch('app.notify.urllib.request.urlopen', return_value=ok_response(b'{"code":19021,"msg":"sign match err"}')):
            message = send_card(self.webhook, '标题', ['内容'])
        self.assertIn('sign match err', message)
        with patch('app.notify.urllib.request.urlopen', return_value=ok_response(b'not json')):
            self.assertIsInstance(send_card(self.webhook, '标题', ['内容']), str)

    def test_empty_webhook_is_a_noop(self):
        with patch('app.notify.urllib.request.urlopen') as urlopen:
            self.assertIsNone(send_card('', '标题', ['内容']))
        urlopen.assert_not_called()

    def test_resolve_webhook_prefers_cli_then_environment(self):
        import os
        with patch.dict(os.environ, {ENV_WEBHOOK: 'https://env/hook'}):
            self.assertEqual(resolve_webhook('https://cli/hook'), 'https://cli/hook')
            self.assertEqual(resolve_webhook(), 'https://env/hook')
            self.assertEqual(resolve_webhook('  '), 'https://env/hook')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_webhook(), '')

    def test_mask_keeps_host_only(self):
        self.assertEqual(mask_webhook(self.webhook), 'open.feishu.cn')
        self.assertEqual(mask_webhook('not a url'), 'webhook')

    def test_import_card_reports_totals_and_truncates_failures(self):
        summary = {'tool': 'cli-import-1.0', 'started_at': 'now', 'root': '/data/batch-0915',
                   'dry_run': False, 'source': {'type': 'recorded', 'name': '西安路测 1015'},
                   'totals': {'imported': 28, 'skipped': 2, 'conflict': 1, 'error': 12,
                              'invalid': 0, 'partial': 0, 'ready': 0, 'total': 43},
                   'cases': [{'case_id': 'seg_%03d' % i, 'status': 'error',
                              'message': '文件过大' } for i in range(12)]}
        title, lines, note = import_card(summary)
        self.assertIn('西安路测 1015', title)
        self.assertIn('**已导入 28**', lines[0])
        self.assertIn('失败 12', lines[0])
        self.assertEqual(sum([1 for line in lines if line.startswith('- seg_')]), 10)
        self.assertIn('…等 2 条', lines[-1])
        self.assertIn('recorded', lines[1])

    def test_benchmark_card_renders_metrics(self):
        metrics = {'candidate_recall': '100%', 'candidate_covered_events': 27, 'gold_event_count': 27,
                   'automatic_confirmation_precision': '100%', 'automatic_event_recall': '96.3%',
                   'tp': 26, 'fp': 0, 'fn': 1, 'wrongly_rejected_gold_events': 0,
                   'automatic_processing_coverage': '86.7%', 'automatic_candidates': 26,
                   'total_candidates': 30, 'evaluated_cases': 27, 'excluded_cases': 3}
        title, lines, note = benchmark_card({'name': 'sim-batch-02', 'metrics': metrics})
        self.assertIn('sim-batch-02', title)
        self.assertIn('**候选覆盖率 100%**（27/27）', lines[0])
        self.assertIn('Precision 100%', lines[1])
        self.assertIn('TP 26 · FP 0 · FN 1', lines[2])
        self.assertIn('先冻结预测再回放标签', note)

    def test_cli_import_pushes_summary_and_survives_bad_webhook(self):
        temp = tempfile.TemporaryDirectory(prefix='notify-it-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / 'batch'
        (source / 'seg_001').mkdir(parents=True)
        for filename, text in demo_cases()['demo_deceleration']['files'].items():
            (source / 'seg_001' / filename).write_text(text, encoding='utf-8')
        argv = ['import', str(source), '--runtime', str(root / 'runtime'),
                '--notify', self.webhook, '--dry-run']
        with patch('app.notify.urllib.request.urlopen', return_value=ok_response()) as urlopen:
            self.assertEqual(app_main(argv), 0)
        self.assertEqual(urlopen.call_count, 1)
        payload = json.loads(urlopen.call_args.args[0].data.decode('utf-8'))
        self.assertIn('可导入', payload['card']['elements'][0]['text']['content'])
        # 坏 webhook：警告不中断，退出码仍由导入结果决定
        with patch('app.notify.urllib.request.urlopen', side_effect=OSError('offline')):
            self.assertEqual(app_main(argv), 0)
        # 未配置 webhook：完全不发请求
        with patch('app.notify.urllib.request.urlopen') as quiet:
            self.assertEqual(app_main(['import', str(source), '--runtime', str(root / 'runtime2'),
                                      '--dry-run']), 0)
        quiet.assert_not_called()


if __name__ == '__main__':
    unittest.main()
