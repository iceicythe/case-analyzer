"""Notification delivery is isolated from real Feishu and model providers."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app.notify import DEFAULT_CONFIG, load_config, send_card
from app.workbench_notifications import WorkbenchNotifications
from app.__main__ import main
from test_notify import ok_response
import test_automatic_pipeline as pipeline
import test_daily_workflow as daily


def config(**values):
    return dict(DEFAULT_CONFIG, enabled=True, webhook_url='https://open.feishu.cn/open-apis/bot/v2/hook/test-only',
                error=None, **values)


class NotificationConfigTests(unittest.TestCase):
    def test_file_environment_precedence_validation_and_safe_errors(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            path = Path(temporary)/'feishu.json'
            value = dict(DEFAULT_CONFIG, enabled=True, webhook_url='https://file.invalid/hook', signing_secret='file-secret')
            path.write_text(json.dumps(value), encoding='utf-8-sig')
            self.assertEqual(load_config(path=path)['signing_secret'], 'file-secret')
            with patch.dict(os.environ, {'FEISHU_WEBHOOK_URL':'https://env.invalid/hook'}):
                self.assertEqual(load_config(path=path)['signing_secret'], '')
                self.assertEqual(load_config(path=path)['webhook_url'], 'https://env.invalid/hook')
                self.assertEqual(load_config('https://cli.invalid/hook',path)['webhook_url'], 'https://cli.invalid/hook')
            for bad in ({'enabled':'yes'}, {'summary_window_seconds':0}, {'webhook_url':'secret-invalid-url'},
                        {'workbench_url':'javascript:secret-invalid-url'}, {'signing_secret':123}):
                path.write_text(json.dumps(dict(value, **bad)),encoding='utf-8')
                result = load_config(path=path)
                self.assertFalse(result['enabled'])
                self.assertTrue(result['error'])
                self.assertNotIn('file-secret',json.dumps(result))
                self.assertNotIn('secret-invalid-url',json.dumps(result))

    def test_signature_and_keyword_and_cli_configuration_check(self):
        with patch('app.notify.time.time',return_value=1599360473), patch('app.notify.urllib.request.urlopen',return_value=ok_response()) as request:
            self.assertIsNone(send_card('https://example.invalid/hook','标题',['内容'],signing_secret='demo',keyword='测试工具'))
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload['timestamp'],'1599360473')
        self.assertEqual(payload['sign'],'l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=')
        self.assertIn('测试工具',json.dumps(payload,ensure_ascii=False))
        self.assertNotIn('demo',json.dumps(payload))
        with patch('app.workbench_notifications.load_config',return_value=config()), patch('app.workbench_notifications.send_card',return_value=None) as sender:
            self.assertEqual(main(['notify']),0)
            sender.assert_not_called()
            self.assertEqual(main(['notify','--test']),0)
            sender.assert_called_once()


class NotificationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'notifications.sqlite3'
        self.reviews = MagicMock()
        self.reviews.get_case.return_value = {'input_fingerprint':'a'*64,'candidates':[],
            'quality':{'can_complete':True,'issues':[]}}
        self.sender = MagicMock(return_value=None)
        self.notices = self.make()

    def make(self):
        return WorkbenchNotifications(self.path,self.reviews,lambda cid:Path(cid),config(),self.sender)

    def deliver(self):
        self.notices.base_url = 'http://127.0.0.1:8770'
        with self.notices._db() as db:
            db.execute('UPDATE notices SET created=0')
        self.notices._deliver()

    def test_coalesces_persists_deduplicates_and_does_not_store_credentials(self):
        self.notices.record('case_1')
        self.notices.record('case_2')
        self.notices.record('case_1')
        self.assertEqual(self.notices.status()['counts']['pending'],2)
        self.deliver()
        self.sender.assert_called_once()
        self.assertIn('本次更新 2 个 Case',self.sender.call_args.args[2][0])
        self.assertEqual(self.notices.status()['counts']['sent'],2)
        self.notices = self.make()
        self.notices.record('case_1')
        self.deliver()
        self.sender.assert_called_once()
        self.assertNotIn(b'test-only',self.path.read_bytes())

    def test_failure_requires_explicit_retry_and_crash_becomes_unknown(self):
        self.sender.return_value = '飞书推送失败：TimeoutError'
        self.notices.record('case_1')
        self.deliver()
        self.deliver()
        self.sender.assert_called_once()
        self.assertEqual(self.notices.status()['counts']['failed'],1)
        self.notices.retry()
        self.sender.return_value = None
        self.deliver()
        self.assertEqual(self.notices.status()['counts']['sent'],1)
        with self.notices._db() as db:
            db.execute("UPDATE notices SET state='sending'")
        self.notices = self.make()
        self.assertEqual(self.notices.status()['counts']['unknown'],1)
        self.deliver()
        self.assertEqual(self.sender.call_count,2)

    def test_new_result_supersedes_old_unsent_failure(self):
        self.notices.record('case_1',{'job_id':'old','status':'failed'})
        self.sender.return_value = 'offline'
        self.deliver()
        self.notices.record('case_1',{'job_id':'new','status':'complete'})
        self.assertEqual(self.notices.status()['counts']['superseded'],1)
        self.notices.retry()
        self.assertEqual(self.notices.status()['counts']['pending'],1)

    def test_transport_does_not_hold_record_lock(self):
        entered, release = threading.Event(), threading.Event()
        self.notices.sender = lambda *args,**kwargs:(entered.set(),release.wait(5),None)[-1]
        self.notices.record('case_1')
        thread = threading.Thread(target=self.deliver)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.notices.record('case_2')
            self.assertEqual(self.notices.status()['counts']['pending'],1)
        finally:
            release.set(); thread.join(5)

    def test_corrupt_outbox_is_preserved_and_notifications_disabled(self):
        self.path.write_bytes(b'not sqlite')
        self.notices = self.make()
        self.notices.record('case_1')
        self.assertTrue(self.notices.status()['error'])
        self.assertEqual(self.path.read_bytes(),b'not sqlite')
        self.sender.assert_not_called()


class WorkbenchNotificationIntegrationTests(unittest.TestCase):
    setUp = daily.DailyWorkflowTests.setUp
    tearDown = daily.DailyWorkflowTests.tearDown
    restart = daily.DailyWorkflowTests.restart
    request = daily.DailyWorkflowTests.request
    wait_job = daily.DailyWorkflowTests.wait_job
    idle = pipeline.AutomaticPipelineTests.idle
    adapter = pipeline.AutomaticPipelineTests.adapter

    def wait_notice(self, state, count=1):
        deadline = time.monotonic()+8
        while time.monotonic()<deadline:
            result = self.request('/api/notifications')[1]
            if result['counts'][state] >= count:
                return result
            time.sleep(.05)
        self.fail(str(result))

    def test_import_to_report_notification_and_restart_do_not_repeat(self):
        with patch('app.workbench_notifications.load_config',return_value=config(summary_window_seconds=1)), patch('app.workbench_notifications.send_card',return_value=None) as sender:
            adapter = self.adapter()
            self.restart(True,adapter,investigate=True)
            self.request('/api/import',daily.payload())
            self.wait_job('complete')
            self.wait_notice('sent')
            self.assertIn('AI 排查报告 1 份',sender.call_args.args[2][1])
            self.assertNotIn('test-only',json.dumps(self.request('/api/notifications')[1]))
            self.restart(True,adapter,investigate=True)
            self.idle()
            sender.assert_called_once()
            self.assertEqual(adapter.calls,['review','investigation'])
            negative = daily.payload('no_event')
            negative['files']['vehicle_state.csv']='timestamp,speed_mps\n'+''.join(f'{i/10},20\n' for i in range(61))
            self.request('/api/import',negative)
            self.wait_notice('sent',2)
            self.assertIn('确认事件区间 0 段',sender.call_args.args[2][1])

    def test_uncertain_notice_failure_and_retry_do_not_rerun_model(self):
        with patch('app.workbench_notifications.load_config',return_value=config(summary_window_seconds=1)), patch('app.workbench_notifications.send_card',return_value='飞书推送失败：TimeoutError') as sender:
            adapter = self.adapter(uncertain=True)
            self.restart(True,adapter,investigate=True)
            self.request('/api/import',daily.payload())
            self.wait_job('complete')
            self.wait_notice('failed')
            self.assertIn('待人工 1',sender.call_args.args[2][0])
            self.assertEqual(self.request('/api/notifications/retry',{})[0],400)
            sender.return_value = None
            self.assertEqual(self.request('/api/notifications/retry',{'confirm':True})[0],200)
            self.wait_notice('sent')
            self.assertEqual(adapter.calls,['review'])
            self.assertEqual(sender.call_count,2)

    def test_provider_error_notice_explains_pause_without_exception_text(self):
        class Adapter:
            def generate(self, package):
                raise PermissionError('private provider token')
            def get_last_call_metrics(self):
                return {'http_status':401,'status':'failed'}
        with patch('app.workbench_notifications.load_config',return_value=config(summary_window_seconds=1)), patch('app.workbench_notifications.send_card',return_value=None) as sender:
            self.restart(True,Adapter,investigate=True)
            self.request('/api/import',daily.payload())
            self.wait_job('failed')
            self.wait_notice('sent')
            content = json.dumps(sender.call_args.args,ensure_ascii=False)
            self.assertIn('失败/变更 1',content)
            self.assertIn('AI 队列已暂停',content)
            self.assertNotIn('private provider token',content)
