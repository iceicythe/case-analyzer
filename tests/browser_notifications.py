"""Optional browser acceptance for the compact notification status/retry control."""
import unittest
from unittest.mock import patch

from playwright.sync_api import expect
import browser_daily as browser
import test_daily_workflow as daily
import test_automatic_pipeline as pipeline
from test_workbench_notifications import config
from app.paths import PROJECT_ROOT


class BrowserNotificationTests(unittest.TestCase):
    setUp = browser.BrowserDailyTests.setUp
    tearDown = browser.BrowserDailyTests.tearDown
    restart = daily.DailyWorkflowTests.restart
    request = daily.DailyWorkflowTests.request

    def test_failure_and_retry_are_visible_without_running_model_again(self):
        adapter = type('BrowserAdapter',(pipeline.PipelineAdapter,),{'calls':[],'uncertain':True})
        with patch('app.workbench_notifications.load_config',return_value=config(summary_window_seconds=1)), patch('app.workbench_notifications.send_card',return_value='飞书推送失败：TimeoutError') as sender:
            self.restart(True,adapter,investigate=True)
            self.request('/api/import',daily.payload())
            self.page.goto(self.url+'/')
            expect(self.page.locator('#notificationStatus')).to_contain_text('失败/送达未知 1',timeout=20000)
            expect(self.page.locator('#notificationRetry')).to_be_visible()
            output = PROJECT_ROOT/'artifacts/feishu-notification-status.png'
            output.parent.mkdir(parents=True,exist_ok=True)
            self.page.screenshot(path=str(output),full_page=True)
            sender.return_value = None
            self.page.locator('#notificationRetry').click()
            expect(self.page.locator('#notificationStatus')).to_contain_text('已发送 1',timeout=20000)
            expect(self.page.locator('#notificationRetry')).to_be_hidden()
            self.assertEqual(sender.call_count,2)
            self.assertEqual(adapter.calls,['review'])
            self.page.goto(self.url+'/reports/')
            expect(self.page.locator('#aiQueue')).to_be_hidden()
