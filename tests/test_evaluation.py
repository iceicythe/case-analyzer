"""Evaluation correctness and separation of human answers from predictions."""
import hashlib
import json
import unittest
from pathlib import Path

import test_workbench as fixtures
from app.evaluation import EvaluationStore, coverage, freeze, match_events, score


RULES={'event_iou':.5,'candidate_coverage':.9,'rejection_coverage':.5}


def candidate(interval=(1,2),status='ai_confirmed_event',confirmed=None):
    return {'candidate_id':'C001','interval':list(interval),'status':status,
            'confirmed_intervals':confirmed if confirmed is not None else [list(interval)],'review_status':'validated'}


class MetricTests(unittest.TestCase):
    def test_matching_maximizes_number_and_does_not_double_count(self):
        self.assertEqual(len(match_events([[0,1.7],[0,.8]],[[0,1],[.7,1.7]])),2)
        snapshot={'cases':{'case':{'candidates':[candidate(confirmed=[[1,2],[1,2]])]}},'reports':{}}
        labels={'case':{'decision':'positive','events':[[1,2]]}}
        result=score(snapshot,labels,{},RULES)
        self.assertEqual((result['tp'],result['fp'],result['fn']),(1,1,0))
        self.assertEqual(result['automatic_confirmation_precision'],.5)

    def test_candidate_recall_uses_union_and_zero_denominators_are_unknown(self):
        self.assertAlmostEqual(coverage([0,2],[[0,.8],[.8,2],[.2,.5]]),1)
        snapshot={'cases':{'case':{'candidates':[]}},'reports':{}}
        result=score(snapshot,{'case':{'decision':'negative','events':[]}}, {},RULES)
        for key in ('candidate_recall','automatic_confirmation_precision','automatic_event_recall','report_useful_rate','automatic_processing_coverage'):
            self.assertIsNone(result[key],key)

    def test_uncertain_is_excluded_and_wrong_rejection_is_counted(self):
        snapshot={'cases':{'known':{'candidates':[candidate(status='ai_rejected_candidate',confirmed=[])]},
                           'unknown':{'candidates':[candidate()]}},'reports':{}}
        labels={'known':{'decision':'positive','events':[[1,2]]},'unknown':{'decision':'uncertain','events':[]}}
        result=score(snapshot,labels,{},RULES)
        self.assertEqual(result['excluded_cases'],1)
        self.assertEqual(result['wrongly_rejected_gold_events'],1)
        self.assertEqual(result['fn'],1)
        self.assertEqual(result['fp'],0)
        self.assertEqual(result['candidate_recall'],1)

    def test_failed_model_report_remains_in_evaluation_population(self):
        snapshot={'cases':{'case':{'candidates':[]}},'reports':{'valid':{'status':'model_validated'},'failed':{'status':'model_failed'}}}
        labels={'case':{'decision':'negative','events':[]}}
        result=score(snapshot,labels,{},RULES)
        self.assertEqual(result['reports_total'],2)
        self.assertEqual(result['model_reports_validated'],1)
        self.assertEqual(result['model_reports_failed'],1)


class EvaluationApiTests(unittest.TestCase):
    request=fixtures.WorkbenchTests.request
    def setUp(self):
        fixtures.WorkbenchTests.setUp(self)
        self.request('/api/import',fixtures.payload())
        text='明确的模拟报告，用于评价保存测试。'
        snapshot={'cases':{'fixture_001':{'candidates':[candidate()]}},
            'reports':{'fixture_001_C001':{'case_id':'fixture_001','candidate_id':'C001','report':text,
                'report_sha256':hashlib.sha256(text.encode()).hexdigest()}}}
        freeze(self.context,snapshot,123,{'fixture':'hash'})
        self.store=EvaluationStore(self.context)
    tearDown=fixtures.WorkbenchTests.tearDown

    def label_body(self):
        value=self.request('/api/evaluation/case/fixture_001')[1]['label']
        return {'expected_revision':value['revision'],'decision':'positive','events':[[1,2]],
                'reviewer':'unit fixture','notes':'independent fixture label','whole_case_reviewed':True}

    def open_results(self):
        self.assertEqual(self.request('/api/evaluation/label/fixture_001',self.label_body())[0],200)
        status,value=self.request('/api/evaluation/unblind',{'confirm':True})
        self.assertEqual(status,200,value)
        return value

    def test_blind_page_and_api_do_not_expose_predictions(self):
        summary=self.request('/api/evaluation')[1]
        self.assertIsNone(summary['metrics']); self.assertEqual(summary['reports'],[])
        self.assertNotIn('prediction',self.request('/api/evaluation/case/fixture_001')[1])
        for path in ('/api/data','/api/candidates/fixture_001','/api/assist/fixture_001','/api/overview','/api/automatic-review'):
            self.assertEqual(self.request(path)[0],403,path)
        self.assertEqual(self.request('/evaluation/')[0],200)
        self.assertEqual(self.request('/api/evaluation/report/fixture_001_C001')[0],409)

    def test_requires_human_label_then_scores_without_changing_daily_annotations(self):
        self.assertEqual(self.request('/api/evaluation/unblind',{'confirm':True})[0],409)
        value=self.open_results()
        self.assertEqual(value['metrics']['tp'],1)
        self.assertEqual(value['metrics']['automatic_confirmation_precision'],1)
        annotation=json.loads((self.root/'annotations/fixture_001.json').read_text(encoding='utf-8'))
        self.assertEqual(annotation['annotation_status'],'pending')
        self.assertIn('prediction',self.request('/api/evaluation/case/fixture_001')[1])

    def test_invalid_intervals_and_stale_revision_cannot_overwrite(self):
        body=self.label_body()
        for intervals in ([[1,1.1]],[[1,float('nan')]],[[1,2],[1.5,3]],[[1,100]]):
            self.assertEqual(self.request('/api/evaluation/label/fixture_001',dict(body,events=intervals))[0],400)
        self.assertEqual(self.request('/api/evaluation/label/fixture_001',body)[0],200)
        self.assertEqual(self.request('/api/evaluation/label/fixture_001',body)[0],409)

    def test_results_are_read_only_and_input_change_invalidates_batch(self):
        self.assertEqual(self.request('/api/model-review/fixture_001/C001',{})[0],409)
        self.assertEqual(self.request('/api/import',fixtures.payload('other'))[0],409)
        path=self.root/'data/fixture_001/vehicle_state.csv'
        path.write_text(path.read_text().replace('20.000000','20.000001',1),encoding='utf-8')
        self.assertEqual(self.request('/api/evaluation')[0],409)

    def test_report_ratings_need_explicit_assessment_and_have_revision_protection(self):
        self.open_results()
        report=self.request('/api/evaluation/report/fixture_001_C001')[1]
        body={'expected_revision':report['rating']['revision'],'reviewer':'unit fixture','notes':'',
              'checks':{'evidence_correct':True,'actionable':True,'no_unsupported_claim':True}}
        self.assertEqual(self.request('/api/evaluation/rating/fixture_001_C001',body)[0],200)
        self.assertEqual(self.request('/api/evaluation/rating/fixture_001_C001',body)[0],409)
        metrics=self.request('/api/evaluation')[1]['metrics']
        self.assertEqual(metrics['reports_assessed'],1)
        self.assertEqual(metrics['report_useful_rate'],1)

    def test_post_unblind_corrections_are_recorded_separately(self):
        self.open_results()
        body=self.label_body()
        self.assertEqual(self.request('/api/evaluation/label/fixture_001',dict(body,notes=''))[0],400)
        self.assertEqual(self.request('/api/evaluation/label/fixture_001',dict(body,notes='修正边界',events=[[1.1,2.1]]))[0],200)
        self.assertEqual(self.request('/api/evaluation')[1]['post_unblind_edits'],1)
        self.assertTrue((self.root/'evaluations/blind_labels.json').is_file())


if __name__=='__main__':
    unittest.main()
