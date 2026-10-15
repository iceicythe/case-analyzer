"""Import-to-report acceptance with isolated workspaces and explicit fake providers."""
import json
import time
import unittest
from datetime import datetime, timezone

import test_daily_workflow as daily
from app.candidate_decision import CandidateDecisionMockAdapter
from app.event_investigation import InvestigationMockAdapter


class PipelineAdapter:
    calls = []
    fail_investigation = False
    uncertain = False

    def generate(self, package):
        investigation = 'events' in package['analysis_context']
        type(self).calls.append('investigation' if investigation else 'review')
        if investigation:
            if self.fail_investigation:
                raise TimeoutError('isolated fixture')
            return InvestigationMockAdapter().generate(package)
        value = CandidateDecisionMockAdapter().generate(package)
        if self.uncertain:
            value.update(decision='needs_human', confirmed_intervals=[], human_review_reasons=['需核对来源'])
        return value


class AutomaticPipelineTests(unittest.TestCase):
    setUp = daily.DailyWorkflowTests.setUp
    tearDown = daily.DailyWorkflowTests.tearDown
    request = daily.DailyWorkflowTests.request
    restart = daily.DailyWorkflowTests.restart
    wait_job = daily.DailyWorkflowTests.wait_job

    def idle(self):
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            state = self.request('/api/automatic-review')[1]
            if not state['running'] and not state['queued_count'] and not state['discovery_count'] and not state['discovering']:
                return state
            time.sleep(.05)
        self.fail(str(state))

    def adapter(self, **options):
        return type('FixtureAdapter', (PipelineAdapter,), {'calls':[], **options})

    def test_import_runs_review_and_report_without_opening_case_and_restart_reuses_results(self):
        adapter = self.adapter()
        self.restart(True,adapter,investigate=True)
        code, result = self.request('/api/import',daily.payload())
        self.assertEqual(code,201,result)
        self.assertEqual(result['analysis'],'scheduled')
        self.wait_job('complete')
        self.assertEqual(adapter.calls,['review','investigation'])
        record = self.request('/api/investigation/fixture_001/C001')[1]
        self.assertEqual(record['status'],'model_validated')
        brief = record['briefings']['I001']
        self.assertTrue(brief['next_action'])
        self.assertIn('control_state.csv',brief['gaps'][0])
        self.restart(True,adapter,investigate=True)
        self.idle()
        self.assertEqual(adapter.calls,['review','investigation'])

    def test_investigation_failure_retains_evidence_and_retries_only_failed_stage(self):
        adapter = self.adapter(fail_investigation=True)
        self.restart(True,adapter,investigate=True)
        self.request('/api/import',daily.payload())
        job = self.wait_job('failed')['jobs'][0]
        self.assertEqual(job['stage'],'investigation')
        record = self.request('/api/investigation/fixture_001/C001')[1]
        self.assertEqual(record['status'],'model_failed')
        self.assertTrue(record['report'])
        self.restart(True,adapter,investigate=True)
        self.idle()
        self.assertEqual(adapter.calls,['review','investigation'])
        adapter.fail_investigation = False
        self.request('/api/automatic-review/fixture_001',{'retry':True})
        self.wait_job('complete')
        self.assertEqual(adapter.calls,['review','investigation','investigation'])

    def test_uncertain_and_no_event_cases_do_not_enter_investigation(self):
        adapter = self.adapter(uncertain=True)
        self.restart(True,adapter,investigate=True)
        self.request('/api/import',daily.payload())
        self.wait_job('complete')
        self.assertEqual(adapter.calls,['review'])

        case = self.request('/api/candidates/fixture_001')[1]
        self.assertEqual(case['candidates'][0]['disposition']['status'],'needs_human')
        negative = daily.payload('no_event')
        negative['files']['vehicle_state.csv']='timestamp,speed_mps\n'+''.join(f'{i/10},20\n' for i in range(61))
        self.request('/api/import',negative)
        self.idle()
        self.assertEqual(adapter.calls,['review'])

    def test_authentication_failure_pauses_remaining_queue(self):
        class Adapter(PipelineAdapter):
            calls = []
            def generate(inner, package):
                Adapter.calls.append('review')
                raise PermissionError('isolated unauthorized fixture')
            def get_last_call_metrics(inner):
                return {'http_status':401,'status':'failed'}
        self.restart(True,Adapter,investigate=True,paused=True)
        self.request('/api/import',daily.payload())
        self.request('/api/import',daily.payload('second_case'))
        self.request('/api/automatic-review',{'paused':False})
        state = self.wait_job('failed')
        self.assertTrue(state['paused'])
        self.assertEqual(Adapter.calls,['review'])

    def test_startup_discovers_case_and_human_confirmation_can_generate_report(self):
        # Build a real human confirmation with the existing acceptance helper.
        daily.DailyWorkflowTests.confirmed_report(self)
        adapter = self.adapter()
        self.restart(True,adapter,investigate=True)
        self.wait_job('complete')
        self.assertEqual(adapter.calls,['investigation'])
        record = self.request('/api/investigation/fixture_001/C001')[1]
        self.assertEqual(record['status'],'model_validated')
        self.assertEqual(record['bundle']['acceleration_provenance']['human_event_confirmation']['source'],'human')

    def test_interrupted_investigation_requires_retry_before_another_call(self):
        adapter = self.adapter(fail_investigation=True)
        self.restart(True,adapter,investigate=True)
        self.request('/api/import',daily.payload())
        self.wait_job('failed')
        self.http.shutdown(); self.http.server_close(); self.thread.join()
        path = self.root/'model_reviews/automatic_queue.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        state['jobs'][-1].update(status='running',stage='investigation',in_flight=True,candidate_id='C001')
        path.write_text(json.dumps(state),encoding='utf-8')
        self.restart(True,adapter,investigate=True)
        self.assertEqual(self.wait_job('failed')['jobs'][0]['error_type'],'InterruptedRequest')
        self.assertEqual(adapter.calls,['review','investigation'])

    def test_live_human_confirmation_automatically_continues_to_report(self):
        adapter = self.adapter(uncertain=True)
        self.restart(True,adapter,investigate=True)
        self.request('/api/import',daily.payload())
        self.wait_job('complete')
        assist = self.request('/api/assist/fixture_001')[1]
        annotation = self.request('/api/annotation/fixture_001')[1]
        source = assist['quality']['original_can']
        confirmation = {'protocol_id':assist['protocol']['protocol_id'], 'whole_case_reviewed':True,
            'boundary_reviewed':True, 'vehicle_sha256':assist['vehicle_sha256'],
            'protocol_sha256':assist['protocol_sha256'], 'metadata_sha256':source['metadata_sha256'],
            'original_can_sha256':source['original_can_sha256']}
        events = [dict(event_id=f'E{i+1:03}',start_time=e['start_time'],end_time=e['end_time'],notes='fixture')
                  for i,e in enumerate(assist['reference_events'])]
        code, saved = self.request('/api/annotation/fixture_001',{
            '_expected_revision':annotation['_revision'], 'annotation_status':'completed',
            'case_label':'positive','annotator':'test engineer','annotated_at':datetime.now(timezone.utc).isoformat(),
            'events':events,'review_confirmation':confirmation}, 'PUT')
        self.assertEqual(code,200,saved)
        case = self.request('/api/candidates/fixture_001')[1]
        candidate = case['candidates'][0]
        code, result = self.request('/api/resolve-candidate/fixture_001/C001',{
            'input_fingerprint':case['input_fingerprint'],'human_decision':'confirmed_event',
            'selected_event_ids':[e['event_id'] for e in events],
            'annotation_sha256':saved['annotation']['_revision'],
            'expected_record_fingerprint':candidate['human_resolution']['record_fingerprint']})
        self.assertEqual(code,200,result)
        self.idle()
        self.assertEqual(adapter.calls,['review','investigation'])
        self.assertEqual(self.request('/api/investigation/fixture_001/C001')[1]['status'],'model_validated')

    def test_import_preflight_is_read_only_and_distinguishes_identical_and_conflict(self):
        data = daily.payload()
        self.assertEqual(self.request('/api/import/validate',data)[1]['status'],'ready')
        self.assertEqual(self.request('/api/data')[1]['cases'],[])
        self.assertEqual(self.request('/api/import',data)[0],201)
        self.assertEqual(self.request('/api/import/validate',data)[1]['status'],'skipped')
        data['source_name']='different source'
        self.assertEqual(self.request('/api/import/validate',data)[1]['status'],'conflict')
        data['case_id']='FIXTURE_001'
        self.assertEqual(self.request('/api/import/validate',data)[1]['status'],'conflict')
        data['case_id']='another_case'; data['files']['vehicle_state.csv']='timestamp,speed_mps\n0,invalid\n'
        self.assertEqual(self.request('/api/import/validate',data)[0],400)
        self.assertEqual(self.request('/api/import',daily.payload('healthy_case'))[0],201)
        self.assertFalse((self.root/'data/another_case').exists())
