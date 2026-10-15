"""Daily acceptance: persistence, queue recovery, notes and real browser actions.

All cases live in temporary workspaces. Provider responses are explicit fixtures.
"""
import io
import json
import queue
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import server
from app.candidate_decision import CandidateDecisionMockAdapter
from app.human_event_review import HumanEventReviewStore
from app.paths import PROJECT_ROOT
import test_workbench as fixtures

payload = fixtures.payload


class DailyWorkflowTests(unittest.TestCase):
    setUp = fixtures.WorkbenchTests.setUp
    tearDown = fixtures.WorkbenchTests.tearDown
    request = fixtures.WorkbenchTests.request

    def restart(self, automatic=False, adapter=None):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.status.stop()
        self.context = server.WorkbenchContext(
            batch_dir=self.root/'index', data_root=self.root/'data',
            annotations_root=self.root/'annotations', evaluation_output=self.root/'evaluations',
            model_reviews_root=self.root/'model_reviews', automatic_review=automatic)
        self.status = patch('app.server.model_status', return_value={'ready':automatic, 'message':'acceptance fixture'})
        self.status.start()
        def store(*args, **kwargs):
            return HumanEventReviewStore(*args, **kwargs, adapter_factory=adapter)
        with patch('app.server.HumanEventReviewStore', side_effect=store):
            self.http = server.create_server(self.context, 0)
        self.http.RequestHandlerClass.log_message = lambda *args: None
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def confirmed_report(self):
        fixtures.WorkbenchTests.test_human_confirmation_to_report_and_export(self)
        return self.request('/api/investigation/fixture_001/C001')[1]

    def save_notes(self, record=None):
        record = record or self.confirmed_report()
        saved = record['tracking']
        check_id = next(iter(record['display_checks'].values()))[0]['check_id']
        value = {'investigation_fingerprint':saved['investigation_fingerprint'],
                 'expected_record_fingerprint':saved['record_fingerprint'],
                 'entries':{check_id:{'status':'blocked','note':'验收：待补规划日志。'}},
                 'notes':'验收：人工保存的排查备注。'}
        status, result = self.request('/api/investigation-tracking/fixture_001/C001',value)
        self.assertEqual(status,200,result)
        return value, result

    def export(self):
        overview = self.request('/api/overview')[1]
        case = overview['cases'][0]; candidate = case['candidates'][0]
        item = {key:case[key] for key in ('case_id','input_fingerprint')}
        item.update({key:candidate[key] for key in ('candidate_id','confirmed_intervals','report_fingerprint')})
        item['human_confirmation'] = {key:candidate[key] for key in ('annotation_sha256','selected_event_ids')}
        return self.request('/api/local-reports/archive', {'items':[item]})

    def wait_job(self, expected, timeout=8):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            state = self.request('/api/automatic-review')[1]
            if state.get('jobs') and state['jobs'][0]['status'] == expected:
                return state
            time.sleep(.05)
        self.fail(f'Queue did not reach {expected}: {state}')

    def test_saved_notes_survive_restart_and_enter_archive(self):
        value, saved = self.save_notes()
        self.restart()
        report = self.request('/api/investigation/fixture_001/C001')[1]
        self.assertEqual(report['status'],'local_ready')
        self.assertEqual(report['tracking']['entries'],value['entries'])
        self.assertEqual(report['tracking']['notes'],value['notes'])
        self.assertEqual(report['tracking']['updated_at'],saved['updated_at'])
        self.assertEqual(self.request('/api/candidates/fixture_001')[1]['candidates'][0]['disposition']['human_decision'],'confirmed_event')
        status, content = self.export()
        self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            text = '\n'.join(archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith(('.md','.json','.html')))
            self.assertIn(value['notes'],text)
            self.assertIn('待补规划日志',text)

    def test_notes_conflict_and_disk_failure_preserve_saved_record(self):
        value, saved = self.save_notes()
        self.assertEqual(self.request('/api/investigation-tracking/fixture_001/C001',value)[0],409)
        value['expected_record_fingerprint'] = saved['record_fingerprint']
        value['notes'] = '不能覆盖成功记录的失败保存'
        original = Path.replace
        def fail_notes(path, destination):
            if 'investigation_notes' in str(destination):
                raise OSError('isolated write failure')
            return original(path,destination)
        with patch.object(Path,'replace',fail_notes):
            self.assertEqual(self.request('/api/investigation-tracking/fixture_001/C001',value)[0],500)
        current = self.request('/api/investigation-tracking/fixture_001/C001')[1]
        self.assertEqual(current['notes'],saved['notes'])
        self.assertEqual(self.request('/api/investigation-tracking/fixture_001/C001',value)[0],200)

    def test_changed_module_log_invalidates_report_and_blocks_old_export(self):
        self.confirmed_report()
        case = self.request('/api/overview')[1]['cases'][0]
        c = case['candidates'][0]
        export = {'case_id':case['case_id'],'candidate_id':c['candidate_id'],
                  'input_fingerprint':case['input_fingerprint'],'confirmed_intervals':c['confirmed_intervals'],
                  'human_confirmation':{key:c[key] for key in ('annotation_sha256','selected_event_ids')},
                  'report_fingerprint':c['report_fingerprint']}
        (self.root/'data/fixture_001/planning_state.csv').write_text(
            'timestamp,target_speed_mps,planned_acceleration_mps2,decision_reason\n1.0,20,0,cruise\n2.0,17,-3,decelerate\n',encoding='utf-8')
        record = self.request('/api/investigation/fixture_001/C001')[1]
        self.assertEqual(record['status'],'stale')
        status, archive = self.request('/api/local-reports/archive',{'items':[export]})
        if status == 200:
            with zipfile.ZipFile(io.BytesIO(archive)) as package:
                manifest = json.loads(package.read('manifest.json'))
                self.assertEqual(manifest['included'],[])
        else:
            self.assertEqual(status,400)

    def test_no_candidate_case_is_visible_and_does_not_request_ai(self):
        data = payload('steady_001')
        data['files']['vehicle_state.csv'] = 'timestamp,speed_mps\n'+'\n'.join(f'{i/10:.1f},20' for i in range(61))+'\n'
        self.assertEqual(self.request('/api/import',data)[0],201)
        self.assertEqual(self.request('/api/candidates/steady_001')[1]['candidates'],[])
        self.assertEqual(self.request('/api/review-queue')[1]['cases'][0]['status'],'no_candidates')

    def test_gap_is_a_data_issue_before_manual_decision(self):
        self.request('/api/import',payload(gap=True))
        case=self.request('/api/candidates/fixture_001')[1]
        self.assertTrue(case['candidates'])
        self.assertEqual(case['workflow']['status'],'data_issue')
        self.assertEqual(self.request('/api/review-queue')[1]['cases'][0]['status'],'data_issue')
        self.assertEqual(self.request('/api/overview')[1]['cases'][0]['status'],'data_issue')
        self.assertEqual(case['candidates'][0]['disposition']['status'],'needs_human')

    def test_damaged_case_does_not_block_other_cases(self):
        self.request('/api/import',payload('good_001'))
        self.request('/api/import',payload('bad_001'))
        path = self.root/'annotations/bad_001.json'
        damaged = '{bad annotation'
        path.write_text(damaged,encoding='utf-8')
        status, data = self.request('/api/data')
        self.assertEqual(status,200,data)
        self.assertEqual(len(data['cases']),2)
        self.assertEqual(self.request('/api/candidates/good_001')[0],200)
        annotation = self.request('/api/annotation/good_001')[1]
        self.assertEqual(self.request('/api/annotation/good_001',
            {'_expected_revision':annotation['_revision'],'annotation_status':'pending','notes':'healthy case remains editable'},'PUT')[0],200)
        self.assertIn('读取',next(c for c in data['cases'] if c['case_id']=='bad_001')['read_error'])
        self.assertEqual(path.read_text(encoding='utf-8'),damaged)

    def test_damaged_review_returns_error_without_dropping_connection(self):
        self.request('/api/import',payload())
        directory = self.root/'model_reviews/fixture_001'
        directory.mkdir()
        for damaged in ({},{'candidate':{'review':[]}}, {'candidate':[], 'input_fingerprint':'invalid'}):
            (directory/'C001.json').write_text(json.dumps(damaged),encoding='utf-8')
            status, response = self.request('/api/candidates/fixture_001')
            self.assertGreaterEqual(status,400,response)
            self.assertIn('error',response)
            self.assertEqual(self.request('/api/data')[0],200)
        self.assertEqual(self.request('/api/health')[0],200)

    def test_damaged_report_is_visible_as_error_without_overwriting_file(self):
        self.confirmed_report()
        path = self.root/'model_reviews/fixture_001/investigations/C001.json'
        path.write_text('{}',encoding='utf-8')
        status, response = self.request('/api/investigation/fixture_001/C001')
        self.assertGreaterEqual(status,400,response)
        self.assertIn('error',response)
        self.assertEqual(self.request('/api/overview')[0],200)
        self.assertEqual(path.read_text(encoding='utf-8'),'{}')

    def test_ai_investigation_failure_preserves_local_report_and_notes(self):
        original = self.confirmed_report()
        value, saved = self.save_notes(original)
        class Adapter(CandidateDecisionMockAdapter):
            def generate(inner, package):
                raise TimeoutError('isolated investigation timeout')
        self.restart(False,Adapter)
        case = self.request('/api/candidates/fixture_001')[1]
        disposition = case['candidates'][0]['disposition']
        status, response = self.request('/api/investigation/fixture_001/C001',
            {'input_fingerprint':case['input_fingerprint'],'use_model':True,
             'human_confirmation':{key:disposition[key] for key in ('annotation_sha256','selected_event_ids')}})
        self.assertEqual(status,200,response)
        report = response['investigation']
        self.assertEqual(report['status'],'model_failed')
        self.assertTrue(report['report'])
        self.assertEqual(report['tracking']['notes'],value['notes'])
        self.assertEqual(self.export()[0],200)

    def test_automatic_failure_needs_explicit_retry_and_reuses_success(self):
        self.request('/api/import',payload())
        class Adapter(CandidateDecisionMockAdapter):
            calls = 0
            def generate(inner, package):
                Adapter.calls += 1
                if Adapter.calls == 1:
                    raise TimeoutError('isolated provider timeout')
                return super().generate(package)
        self.restart(True,Adapter)
        self.request('/api/automatic-review/fixture_001',{})
        self.assertEqual(self.wait_job('failed')['jobs'][0]['error_type'],'ModelCallFailed')
        self.assertEqual(self.request('/api/candidates/fixture_001')[1]['candidates'][0]['review']['status'],'call_failed')
        self.restart(True,Adapter)
        self.request('/api/automatic-review/fixture_001',{})
        self.assertEqual(Adapter.calls,1)
        self.request('/api/automatic-review/fixture_001',{'retry':True})
        self.wait_job('complete')
        self.assertEqual(Adapter.calls,2)
        self.restart(True,Adapter)
        self.request('/api/automatic-review/fixture_001',{})
        self.assertEqual(self.request('/api/candidates/fixture_001')[1]['candidates'][0]['disposition']['status'],'ai_confirmed_event')
        self.assertEqual(Adapter.calls,2)

    def test_paused_queue_survives_restart_and_resumes(self):
        self.request('/api/import',payload())
        class Adapter(CandidateDecisionMockAdapter):
            calls = 0
            def generate(inner, package):
                Adapter.calls += 1
                return super().generate(package)
        self.restart(True,Adapter)
        self.request('/api/automatic-review',{'paused':True})
        self.request('/api/automatic-review/fixture_001',{})
        self.restart(True,Adapter)
        state = self.request('/api/automatic-review')[1]
        self.assertTrue(state['paused'])
        self.assertEqual(state['jobs'][0]['status'],'queued')
        self.assertEqual(Adapter.calls,0)
        self.request('/api/automatic-review',{'paused':False})
        self.wait_job('complete')
        self.assertEqual(Adapter.calls,1)

    def test_interrupted_request_is_not_silently_sent_again(self):
        self.request('/api/import',payload())
        class Adapter(CandidateDecisionMockAdapter):
            calls = 0
            def generate(inner, package):
                Adapter.calls += 1
                return super().generate(package)
        self.restart(True,Adapter)
        self.request('/api/automatic-review',{'paused':True})
        self.request('/api/automatic-review/fixture_001',{})
        self.http.shutdown(); self.http.server_close(); self.thread.join()
        path = self.root/'model_reviews/automatic_queue.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        state['paused'] = False
        state['jobs'][0].update(status='running',in_flight=True,candidate_id='C001')
        path.write_text(json.dumps(state),encoding='utf-8')
        self.restart(True,Adapter)
        result = self.wait_job('failed')
        self.assertEqual(result['jobs'][0]['error_type'],'InterruptedRequest')
        self.assertEqual(Adapter.calls,0)
        self.request('/api/automatic-review/fixture_001',{'retry':True})
        self.wait_job('complete')
        self.assertEqual(Adapter.calls,1)

    def test_invalid_model_output_routes_to_human(self):
        self.request('/api/import',payload())
        class Adapter(CandidateDecisionMockAdapter):
            def generate(inner, package):
                return {'decision':'confirmed_event','confirmed_intervals':[[0,6]]}
        self.restart(True,Adapter)
        self.request('/api/automatic-review/fixture_001',{})
        self.wait_job('complete')
        candidate = self.request('/api/candidates/fixture_001')[1]['candidates'][0]
        self.assertEqual(candidate['disposition']['status'],'needs_human')
        self.assertEqual(candidate['review']['status'],'needs_manual_review')

    def test_below_strict_rule_candidate_can_be_automatically_rejected(self):
        data = payload()
        rows = ['timestamp,speed_mps']
        for i in range(61):
            speed = 20-max(0,min(i-10,10))*.14
            rows.append(f'{i/10:.1f},{speed:.6f}')
        data['files']['vehicle_state.csv'] = '\n'.join(rows)+'\n'
        self.request('/api/import',data)
        self.restart(True,CandidateDecisionMockAdapter)
        self.request('/api/automatic-review/fixture_001',{})
        self.wait_job('complete')
        case = self.request('/api/candidates/fixture_001')[1]
        self.assertTrue(case['candidates'])
        self.assertEqual(case['candidates'][0]['disposition']['status'],'ai_rejected_candidate')
        self.assertGreaterEqual(self.request('/api/investigation/fixture_001/C001',
            {'input_fingerprint':case['input_fingerprint'],'use_model':False})[0],400)

    def test_valid_ai_uncertainty_routes_to_human(self):
        self.request('/api/import',payload())
        class Adapter(CandidateDecisionMockAdapter):
            def generate(inner, package):
                output = super().generate(package)
                output.update(decision='needs_human',confirmed_intervals=[],
                              human_review_reasons=['验收：需要人工核对信号来源。'])
                return output
        self.restart(True,Adapter)
        self.request('/api/automatic-review/fixture_001',{})
        self.wait_job('complete')
        candidate = self.request('/api/candidates/fixture_001')[1]['candidates'][0]
        self.assertEqual(candidate['review']['status'],'validated')
        self.assertEqual(candidate['disposition']['status'],'needs_human')
        self.assertEqual(candidate['disposition']['reason_code'],'model_uncertain')

    def test_application_process_restart_preserves_workspace(self):
        self.save_notes()
        self.http.shutdown(); self.http.server_close(); self.thread.join()
        for _ in range(2):
            process = subprocess.Popen([sys.executable,'-B','-u','-X','utf8','-m','app',
                '--runtime',str(self.root),'--port','0','--no-open','--no-auto-review'],
                cwd=PROJECT_ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
            try:
                lines = queue.Queue()
                reader = threading.Thread(target=lambda:lines.put(process.stdout.readline()),daemon=True)
                reader.start()
                line = lines.get(timeout=20)
                self.assertIn('http://127.0.0.1:',line,line)
                self.url = line.split('：',1)[1].strip().rstrip('/')
                status, result = self.request('/api/investigation/fixture_001/C001')
                self.assertEqual(status,200,result)
                self.assertEqual(result['tracking']['notes'],'验收：人工保存的排查备注。')
                self.assertEqual(self.request('/api/system')[1]['case_count'],1)
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()
        self.restart()


if __name__ == '__main__':
    unittest.main()
