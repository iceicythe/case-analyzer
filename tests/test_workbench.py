"""Small isolated regression suite. Fixtures are generated in temporary folders."""
import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import server, storage
from app.annotation_assist import build_annotation_assist
from app.candidate_decision import CandidateDecisionMockAdapter
from app.human_event_review import HumanEventReviewStore
from app.paths import PROJECT_ROOT
from app.workspace import initialize


def payload(cid='fixture_001', gap=False):
    rows = ['timestamp,speed_mps,acceleration_mps2,brake']
    for i in range(61):
        if gap and i == 16:
            continue
        speed = 20 - max(0, min(i - 10, 10)) * .3
        rows.append(f'{i/10:.1f},{speed:.6f},{-3 if 10 < i <= 20 else 0},0')
    return {'case_id': cid, 'source_type': 'synthetic', 'source_name': 'isolated fixture',
            'acceleration_source': '由车速差分生成，仅供功能检查',
            'files': {'vehicle_state.csv': '\n'.join(rows) + '\n'}}


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='case-workbench-')
        self.root = initialize(Path(self.temp.name))
        self.context = server.WorkbenchContext(batch_dir=self.root/'index', data_root=self.root/'data',
            annotations_root=self.root/'annotations', evaluation_output=self.root/'evaluations',
            model_reviews_root=self.root/'model_reviews', automatic_review=False)
        self.status = patch('app.server.model_status', return_value={'ready':False, 'message':'test manual mode'})
        self.status.start()
        self.http = server.create_server(self.context, 0)
        self.http.RequestHandlerClass.log_message = lambda *args: None
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.status.stop()
        self.temp.cleanup()

    def request(self, path, value=None, method=None, origin=None):
        headers = {'Content-Type':'application/json'}
        if origin:
            headers['Origin'] = origin
        request = urllib.request.Request(self.url + path, data=json.dumps(value).encode() if value is not None else None,
                                         headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            result = json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw
            return response.status, result

    def test_empty_without_dataset_or_model(self):
        for path in ('/','/workbench/','/reports/','/import/','/api/health','/api/system','/api/data','/api/overview','/api/review-queue'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 200)
        self.assertEqual(self.request('/api/data')[1]['cases'], [])
        self.assertIn('还没有测试数据', self.request('/workbench/')[1].decode())
        self.assertFalse(self.request('/api/system')[1]['model']['ready'])

    def test_import_is_visible_without_restart(self):
        status, result = self.request('/api/import', payload())
        self.assertEqual(status, 201, result)
        self.assertEqual(self.request('/api/system')[1]['case_count'], 1)
        case = self.request('/api/candidates/fixture_001')[1]
        self.assertTrue(case['quality']['can_complete'])
        self.assertEqual(case['candidates'][0]['disposition']['status'], 'needs_human')
        self.assertEqual(self.request('/api/data')[1]['cases'][0]['case_id'], 'fixture_001')
        self.assertEqual(self.request('/assets/workbench.js')[0], 200)

    def test_invalid_and_duplicate_imports_preserve_existing(self):
        self.assertEqual(self.request('/api/import', payload())[0], 201)
        original = (self.root/'index/summary.csv').read_bytes()
        for changed in (payload(), dict(payload('bad_001'), case_id='../escape'),
                        dict(payload('bad_002'), files={'vehicle_state.csv':'timestamp,speed_mps\n0,NaN\n'}),
                        dict(payload('bad_003'), files={'other.csv':'timestamp\n0\n'})):
            self.assertEqual(self.request('/api/import', changed)[0], 400)
        self.assertEqual(original, (self.root/'index/summary.csv').read_bytes())

    def test_parallel_imports_do_not_lose_index_rows(self):
        with ThreadPoolExecutor(max_workers=3) as executor:
            outcomes = list(executor.map(lambda i:self.request('/api/import',payload('parallel_'+str(i)))[0], range(3)))
        self.assertEqual(outcomes, [201,201,201])
        self.assertEqual(len(self.request('/api/data')[1]['cases']), 3)

    def test_failed_commit_rolls_back(self):
        original = storage.atomic_json
        def fail_registry(path, value):
            if Path(path).name == 'sources.json':
                raise OSError('isolated disk failure')
            return original(path,value)
        with patch('app.storage.atomic_json', side_effect=fail_registry):
            self.assertEqual(self.request('/api/import', payload())[0], 500)
        self.assertFalse((self.root/'data/fixture_001').exists())
        self.assertFalse((self.root/'annotations/fixture_001.json').exists())
        self.assertEqual(self.request('/api/data')[1]['cases'], [])

    def test_gap_and_modified_input_block_confirmation(self):
        self.assertEqual(self.request('/api/import', payload(gap=True))[0], 201)
        case = self.request('/api/candidates/fixture_001')[1]
        self.assertFalse(case['quality']['can_complete'])
        self.request('/api/import', payload('fixture_002'))
        path = self.root/'data/fixture_002/vehicle_state.csv'
        path.write_text(path.read_text().replace('20.000000','19.999999',1), encoding='utf-8')
        case = self.request('/api/candidates/fixture_002')[1]
        self.assertFalse(case['quality']['can_complete'])

    def test_human_confirmation_to_report_and_export(self):
        self.request('/api/import',payload())
        assist = self.request('/api/assist/fixture_001')[1]
        current = self.request('/api/annotation/fixture_001')[1]
        source = assist['quality']['original_can']
        confirmation = {'protocol_id':assist['protocol']['protocol_id'], 'whole_case_reviewed':True,
            'boundary_reviewed':True, 'vehicle_sha256':assist['vehicle_sha256'],
            'protocol_sha256':assist['protocol_sha256'], 'metadata_sha256':source['metadata_sha256'],
            'original_can_sha256':source['original_can_sha256']}
        events = [dict(event_id=f'E{i+1:03}', start_time=e['start_time'], end_time=e['end_time'], notes='isolated fixture')
                  for i,e in enumerate(assist['reference_events'])]
        updates = {'_expected_revision':current['_revision'], 'annotation_status':'completed',
            'case_label':'positive', 'annotator':'isolated test', 'annotated_at':datetime.now(timezone.utc).isoformat(),
            'events':events, 'review_confirmation':confirmation}
        status, saved = self.request('/api/annotation/fixture_001', updates, 'PUT')
        self.assertEqual(status,200,saved)
        self.assertEqual(self.request('/api/annotation/fixture_001', updates, 'PUT')[0],409)
        case = self.request('/api/candidates/fixture_001')[1]
        c = case['candidates'][0]
        resolution = c['human_resolution']
        status, resolved = self.request('/api/resolve-candidate/fixture_001/'+c['candidate_id'],
            {'input_fingerprint':case['input_fingerprint'], 'human_decision':'confirmed_event',
             'selected_event_ids':[e['event_id'] for e in events], 'annotation_sha256':saved['annotation']['_revision'],
             'expected_record_fingerprint':resolution['record_fingerprint']})
        self.assertEqual(status,200,resolved)
        disposition = resolved['case']['candidates'][0]['disposition']
        status, response = self.request('/api/investigation/fixture_001/'+c['candidate_id'],
            {'input_fingerprint':case['input_fingerprint'], 'use_model':False,
             'human_confirmation':{key:disposition[key] for key in ('annotation_sha256','selected_event_ids')}})
        self.assertEqual(status,200,response)
        record = response['investigation']
        self.assertEqual(record['status'],'local_ready')
        self.assertIn('缺失',record['report'])
        overview = self.request('/api/overview')[1]
        item = overview['cases'][0]; candidate = item['candidates'][0]
        export = {'case_id':item['case_id'],'candidate_id':candidate['candidate_id'],
            'input_fingerprint':item['input_fingerprint'],'confirmed_intervals':candidate['confirmed_intervals'],
            'human_confirmation':{'annotation_sha256':candidate['annotation_sha256'],'selected_event_ids':candidate['selected_event_ids']},
            'report_fingerprint':candidate['report_fingerprint']}
        status, package = self.request('/api/local-reports/archive', {'items':[export]})
        self.assertEqual(status,200,package if isinstance(package,dict) else 'archive')
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            self.assertIn('index.html',archive.namelist())

    def test_model_routing_with_explicit_fake_adapter(self):
        storage.import_case(self.context,payload())
        store = HumanEventReviewStore(None,self.root/'mock_outputs',adapter_factory=CandidateDecisionMockAdapter,
                                     annotations_root=self.context.annotations_root)
        directory = self.context.case_dir('fixture_001')
        case = store.get_case(directory)
        result = store.review(directory,'C001',case['input_fingerprint'])
        self.assertEqual(result['candidates'][0]['disposition']['status'],'ai_confirmed_event')
        self.assertEqual(store.get_case(directory)['candidates'][0]['review']['status'],'validated')

    def test_private_files_and_cross_origin_requests_rejected(self):
        for path in ('/configs/deepseek.local.json','/runtime/index/sources.json','/api/annotation/unknown','/report-feedback/'):
            self.assertGreaterEqual(self.request(path)[0],400)
        self.assertEqual(self.request('/api/import',payload(),origin='https://outside.example')[0],403)
        self.assertEqual(self.request('/api/data')[1]['cases'], [])

    def test_package_imports_are_self_contained(self):
        for path in (PROJECT_ROOT/'app').glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom) and node.module:
                    self.assertFalse(node.module.startswith('src.'),path.name)
                    if node.module.startswith('app.'):
                        self.assertTrue((PROJECT_ROOT/(node.module.replace('.','/')+'.py')).is_file(),node.module)

    def test_copied_folder_starts_without_parent_project(self):
        portable = self.root/'portable'
        shutil.copytree(PROJECT_ROOT, portable, ignore=shutil.ignore_patterns('runtime','acceptance_runtime','archive','artifacts','.git','.venv','__pycache__','deepseek.local.json'))
        environment = dict(os.environ, PYTHONPATH='')
        environment.pop('DEEPSEEK_API_KEY', None)
        completed = subprocess.run([sys.executable,'-B','-X','utf8','-m','app','--check'],
            cwd=portable, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(completed.returncode,0,completed.stderr)
        self.assertTrue(json.loads(completed.stdout)['ok'])
        self.assertEqual((portable/'runtime/index/summary.csv').read_text().count('\n'),1)


if __name__ == '__main__':
    unittest.main()
