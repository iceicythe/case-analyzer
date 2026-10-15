"""Optional browser acceptance. Install Playwright and Edge separately.

Run: python -B -X utf8 -m unittest discover -s tests -p browser_daily.py -v
"""
import os
import json
import threading
import unittest
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

import test_daily_workflow as daily
from app.candidate_decision import CandidateDecisionMockAdapter


class BrowserDailyTests(unittest.TestCase):
    request = daily.DailyWorkflowTests.request
    restart = daily.DailyWorkflowTests.restart
    confirmed_report = daily.DailyWorkflowTests.confirmed_report
    save_notes = daily.DailyWorkflowTests.save_notes
    export = daily.DailyWorkflowTests.export

    def setUp(self):
        daily.DailyWorkflowTests.setUp(self)
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(channel=os.environ.get('BROWSER_CHANNEL') or None,headless=True)
        self.browser_context = self.browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
        self.page = self.browser_context.new_page()
        self.errors = []
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.on('dialog',lambda dialog:dialog.accept())

    def tearDown(self):
        self.browser_context.close(); self.browser.close(); self.playwright.stop()
        daily.DailyWorkflowTests.tearDown(self)
        self.assertEqual(self.errors,[])

    def open_report(self):
        self.page.goto(self.url+'/workbench/?case=fixture_001&candidate=C001#investigationPanel')
        expect(self.page.locator('#investigationPanel')).to_be_visible(timeout=15000)
        self.page.locator('#investigationTracking summary').click()

    def test_import_review_report_notes_export_and_restart(self):
        page = self.page
        page.goto(self.url+'/import/')
        page.locator('#importMode').select_option('single')
        page.locator('#caseId').fill('fixture_001')
        page.locator('#sourceType').select_option('synthetic')
        page.locator('#sourceName').fill('browser acceptance')
        page.locator('#accelerationSource').fill('功能验收用合成信号')
        page.locator('#files').set_input_files({'name':'vehicle_state.csv','mimeType':'text/csv',
            'buffer':daily.payload()['files']['vehicle_state.csv'].encode('utf-8')})
        page.locator('#submitImport').click()
        expect(page.locator('#importResult')).to_contain_text('已导入')
        page.goto(self.url+'/workbench/?case=fixture_001')
        expect(page.locator('#aiSummary')).to_contain_text('1 个候选')
        page.locator('#aiLocate').click()
        page.locator('[data-add-reference]').first.click()
        page.locator('#annotatorInput').fill('browser reviewer')
        page.locator('#notesInput').fill('验收：整段曲线及参考区间已核对。')
        page.wait_for_timeout(3400)
        self.assertEqual(self.request('/api/annotation/fixture_001')[1]['notes'],'')
        self.assertEqual(page.locator('#notesInput').input_value(),'验收：整段曲线及参考区间已核对。')
        page.locator('#saveButton').click()
        expect(page.locator('#saveState')).to_have_text('草稿已保存')
        page.reload()
        expect(page.locator('#notesInput')).to_have_value('验收：整段曲线及参考区间已核对。')
        page.locator('#humanDecision').select_option('confirmed_event')
        options = page.locator('#humanEventOptions input[type=checkbox]')
        if options.count():
            options.first.check()
        page.locator('#reviewConfirm').check()
        expect(page.locator('#aiResolve')).to_be_enabled()
        page.locator('#aiResolve').click()
        expect(page.locator('#saveState')).to_have_text('复核已提交',timeout=15000)
        page.locator('[data-view=investigation]').click()
        page.locator('#generateInvestigation').click()
        expect(page.locator('#investigationPanel')).to_be_visible()
        detail = page.locator('#investigationSummary details').first
        detail.locator('summary').click()
        expect(detail).to_contain_text('待补材料')
        expect(detail).to_contain_text('结果判断')
        expect(detail).to_contain_text('时间基准')
        page.locator('#investigationTracking summary').click()
        row = page.locator('[data-it-check]').first
        row.locator('select').select_option('blocked')
        row.locator('textarea').fill('验收：需补充规划日志。')
        page.locator('[data-it-notes]').fill('验收：页面保存的记录。')
        page.locator('[data-it-save]').click()
        expect(page.locator('[data-it-message]')).to_contain_text('已保存')
        page.reload()
        expect(page.locator('#investigationPanel')).to_be_visible()
        page.locator('#investigationTracking summary').click()
        expect(page.locator('[data-it-notes]')).to_have_value('验收：页面保存的记录。')

        with page.expect_download() as download:
            page.locator('#investigationMarkdown').click()
        self.assertIn('验收：页面保存的记录。',Path(download.value.path()).read_text(encoding='utf-8'))
        page.goto(self.url+'/reports/')
        expect(page.locator('#archiveReports')).to_be_enabled()
        page.locator('.heading-actions .action-menu > summary').click()
        with page.expect_download() as download:
            page.locator('#archiveReports').click()
        with zipfile.ZipFile(download.value.path()) as archive:
            self.assertIn('index.html',archive.namelist())
            content = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith('.md'))
            self.assertIn('验收：页面保存的记录。',content)
            offline = self.root/'offline.html'
            offline.write_bytes(archive.read('index.html'))
        page.goto(offline.as_uri())
        expect(page.locator('#eventChecks')).to_contain_text('下一步')
        expect(page.locator('#eventChecks')).to_contain_text('证据缺口')
        expect(page.locator('#savedNotesText')).to_have_text('验收：页面保存的记录。')
        self.restart()
        self.open_report()
        expect(page.locator('[data-it-notes]')).to_have_value('验收：页面保存的记录。')

    def test_batch_directory_import_skips_labels_and_isolates_and_retries_failures(self):
        directory = self.root/'browser_batch'
        for cid in ('batch_a','batch_b','broken'):
            folder=directory/cid; folder.mkdir(parents=True)
            (folder/'vehicle_state.csv').write_bytes(daily.payload(cid)['files']['vehicle_state.csv'].encode('utf-8')
                if cid!='broken' else b'timestamp,speed_mps\n0,invalid\n')
        (directory/'batch_a/reference.json').write_text('LABEL_MUST_NOT_BE_IMPORTED',encoding='utf-8')
        attempts=[]
        def intercept(route):
            value=json.loads(route.request.post_data); attempts.append(value['case_id'])
            self.assertEqual(set(value['files']),{'vehicle_state.csv'})
            if value['case_id']=='batch_b' and attempts.count('batch_b')==1:
                route.fulfill(status=503,content_type='application/json',body='{"error":"模拟暂时失败"}')
            else: route.continue_()
        self.page.route('**/api/import',intercept)
        page=self.page; page.goto(self.url+'/import/')
        page.locator('#batchFiles').set_input_files(directory)
        page.locator('#sourceType').select_option('synthetic')
        expect(page.locator('#fileList')).to_contain_text('3 条 Case · 忽略 1')
        page.locator('#submitImport').click()
        expect(page.locator('#importResult')).to_contain_text('已导入 1 · 已跳过 0 · 失败 2')
        self.assertEqual(attempts.count('batch_a'),1)
        page.locator('#retryImport').click()
        expect(page.locator('#importResult')).to_contain_text('已导入 2 · 已跳过 0 · 失败 1')
        self.assertEqual(attempts.count('batch_a'),1)
        self.assertEqual(attempts.count('batch_b'),2)
        self.assertFalse((self.root/'data/batch_a/reference.json').exists())
        (directory/'broken/vehicle_state.csv').write_bytes(daily.payload()['files']['vehicle_state.csv'].encode('utf-8'))
        page.locator('#batchFiles').set_input_files(directory)
        page.locator('#submitImport').click()
        expect(page.locator('#importResult')).to_contain_text('已导入 1 · 已跳过 2 · 失败 0')
        self.assertEqual(self.request('/api/system')[1]['case_count'],3)

    def test_tracking_network_failure_refresh_draft_and_conflict_recovery(self):
        self.confirmed_report()
        self.open_report()
        page = self.page
        page.locator('[data-it-notes]').fill('验收：失败后保留的草稿。')
        page.route('**/api/investigation-tracking/**',lambda route:route.abort() if route.request.method=='POST' else route.continue_())
        page.locator('[data-it-save]').click()
        expect(page.locator('[data-it-message]')).to_have_class('it-message error')
        expect(page.locator('[data-it-notes]')).to_have_value('验收：失败后保留的草稿。')
        self.assertFalse(self.request('/api/investigation-tracking/fixture_001/C001')[1]['persisted'])
        page.unroute('**/api/investigation-tracking/**')
        page.reload()
        expect(page.locator('#investigationPanel')).to_be_visible()
        page.locator('#investigationTracking summary').click()
        expect(page.locator('[data-it-notes]')).to_have_value('验收：失败后保留的草稿。')
        value, saved = self.save_notes(self.request('/api/investigation/fixture_001/C001')[1])
        page.locator('[data-it-save]').click()
        expect(page.locator('[data-it-message]')).to_contain_text('其他页面更新')
        page.locator('[data-it-reload]').click()
        expect(page.locator('[data-it-message]')).to_contain_text('已读取最新记录')
        expect(page.locator('[data-it-notes]')).to_have_value(value['notes'])
        page.locator('[data-it-restore]').click()
        expect(page.locator('[data-it-notes]')).to_have_value('验收：失败后保留的草稿。')
        page.locator('[data-it-save]').click()
        expect(page.locator('[data-it-message]')).to_contain_text('已保存')
        self.assertEqual(self.request('/api/investigation-tracking/fixture_001/C001')[1]['notes'],'验收：失败后保留的草稿。')
        page.set_viewport_size({'width':390,'height':844})
        self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'))

    def test_automatic_ui_failure_retry_and_reload(self):
        self.request('/api/import',daily.payload())
        class Adapter(CandidateDecisionMockAdapter):
            calls = 0
            def generate(inner, package):
                Adapter.calls += 1
                if Adapter.calls==1:
                    raise TimeoutError('browser acceptance failure')
                return super().generate(package)
        self.restart(True,Adapter)
        page = self.page
        page.goto(self.url+'/workbench/?case=fixture_001')
        expect(page.locator('#automaticRetry')).to_be_visible(timeout=15000)
        page.locator('#automaticRetry').click()
        expect(page.locator('#aiSummary')).to_contain_text('1 已确认',timeout=15000)
        page.reload()
        expect(page.locator('#aiSummary')).to_contain_text('1 已确认')
        self.assertEqual(Adapter.calls,2)

    def test_damaged_case_keeps_healthy_case_usable(self):
        self.request('/api/import',daily.payload('good_001'))
        self.request('/api/import',daily.payload('bad_001'))
        (self.root/'annotations/bad_001.json').write_text('{invalid',encoding='utf-8')
        self.page.goto(self.url+'/workbench/?case=good_001')
        expect(self.page.locator('#aiSummary')).to_contain_text('1 个候选')
        self.page.locator('#aiLocate').click()
        self.page.locator('#notesInput').fill('健康 Case 可正常保存')
        self.page.locator('#saveButton').click()
        expect(self.page.locator('#saveState')).to_have_text('草稿已保存')
        self.page.goto(self.url+'/')
        expect(self.page.locator('#rows')).to_contain_text('good_001')
        expect(self.page.locator('#rows')).to_contain_text('bad_001')

    def test_background_report_updates_without_any_generate_button(self):
        from test_automatic_pipeline import PipelineAdapter
        release=threading.Event()
        class Adapter(PipelineAdapter):
            calls=[]
            def generate(inner, package):
                if 'events' in package['analysis_context']:
                    release.wait(15)
                return super().generate(package)
        self.addCleanup(release.set)
        self.restart(True,Adapter,investigate=True)
        self.request('/api/import',daily.payload())
        page=self.page
        page.goto(self.url+'/workbench/?case=fixture_001&candidate=C001#investigationPanel')
        expect(page.locator('#investigationPanel')).to_be_visible(timeout=15000)
        expect(page.locator('#investigationStatus')).to_contain_text('本地证据报告')
        release.set()
        expect(page.locator('#investigationStatus')).to_contain_text('AI 建议已校验',timeout=15000)
        expect(page.locator('#investigationSummary')).to_contain_text('下一步')
        expect(page.locator('#investigationSummary')).to_contain_text('证据缺口')
        self.assertEqual(Adapter.calls,['review','investigation'])


if __name__ == '__main__':
    unittest.main()
