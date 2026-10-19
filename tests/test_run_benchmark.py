"""Benchmark script helpers that must not need a model call or a runtime."""
import sys
import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import run_benchmark
from app.candidate_decision import CandidateDecisionMockAdapter
from app.human_event_review import HumanEventReviewStore
from app.workspace import initialize


class BenchmarkScriptTests(unittest.TestCase):
    def test_event_verdict_requires_passing_quality_gate(self):
        label = {'decision': 'positive', 'events': [[1.0, 4.5]], 'label_origin': 'human_verified'}
        decision, events, notes = run_benchmark.resolve_label_decision(label, True, [])
        self.assertEqual((decision, events), ('positive', [[1.0, 4.5]]))
        self.assertIn('human_verified', notes)
        decision, events, notes = run_benchmark.resolve_label_decision(
            label, False, ['CSV 时间不是连续的 0.1 秒网格'])
        self.assertEqual((decision, events), ('data_issue', []))
        self.assertIn('数据质量门未过', notes)

    def test_uncertain_label_survives_gate_failure(self):
        label = {'decision': 'uncertain', 'events': [], 'label_origin': 'human_verified'}
        decision, events, _ = run_benchmark.resolve_label_decision(label, False, ['时间网格不连续'])
        self.assertEqual((decision, events), ('uncertain', []))

    def test_context_paths_follow_runtime_layout(self):
        context = run_benchmark.build_context(Path('/tmp/bench-runtime'))
        self.assertEqual(context.batch_dir, Path('/tmp/bench-runtime/index'))
        self.assertEqual(context.data_root, Path('/tmp/bench-runtime/data'))
        self.assertEqual(context.evaluation_output, Path('/tmp/bench-runtime/evaluations'))
        self.assertEqual(context.model_reviews_root, Path('/tmp/bench-runtime/model_reviews'))

    def test_returned_failure_and_invalid_output_are_not_counted_as_validated(self):
        store=MagicMock()
        candidates=[{'candidate_id':f'C{i:03}','disposition':{'status':'pending_ai'}} for i in range(1,5)]
        store.get_case.return_value={'input_fingerprint':'0'*64,'candidates':candidates}
        store.review.side_effect=[{'candidates':[{'candidate_id':f'C{i:03}','review':{'status':status}}]}
                                  for i,status in enumerate(('validated','call_failed','stale','needs_manual_review'),1)]
        with patch('run_benchmark.HumanEventReviewStore',return_value=store):
            result=run_benchmark.review_all(MagicMock(),['fixture'])['fixture']
        self.assertEqual((result['attempted'],result['reviewed'],result['failed']),(4,1,3))
        self.assertEqual([e['status'] for e in result['errors']],['call_failed','stale','needs_manual_review'])

    def test_event_benchmark_retains_failures_and_declares_report_scope(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'examples'))
        from generate_demo import demo_cases
        class FailingAdapter:
            def generate(self, package):
                raise TimeoutError('isolated fixture')
        with tempfile.TemporaryDirectory(prefix='benchmark-flow-') as tmp:
            root=Path(tmp); dataset=root/'dataset'; case=dataset/'case_001'; case.mkdir(parents=True)
            labels=root/'labels'; labels.mkdir()
            for name,content in demo_cases()['demo_deceleration']['files'].items():
                (case/name).write_bytes(content.encode('utf-8'))
            (labels/'case_001.json').write_text(json.dumps({'decision':'positive','events':[[1.5,4.5]],
                                                         'label_origin':'synthetic_test_fixture'}),encoding='utf-8')
            for adapter,exit_code in ((CandidateDecisionMockAdapter,0),(FailingAdapter,1)):
                runtime=root/adapter.__name__
                argv=['--dataset',str(dataset),'--labels',str(labels),'--runtime',str(runtime),'--reviewer','fixture']
                def store(*args,**kwargs):
                    return HumanEventReviewStore(*args,**kwargs,adapter_factory=adapter)
                with patch('run_benchmark.HumanEventReviewStore',side_effect=store), patch.dict(os.environ,{'FEISHU_WEBHOOK_URL':''}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(run_benchmark.main(argv),exit_code)
                summary=json.loads((runtime/'metrics.json').read_text(encoding='utf-8'))
                self.assertEqual(summary['evaluation_scope'],'event_review')
                self.assertEqual(summary['report_quality']['status'],'not_evaluated')
                self.assertEqual(summary['execution']['failed_candidates'],exit_code)
                self.assertEqual(summary['metrics']['evaluated_cases'],1)
                self.assertEqual(summary['metrics']['fn'],exit_code)
                frozen=(runtime/'evaluations/predictions.json').read_bytes()
                with patch('run_benchmark.HumanEventReviewStore') as factory, redirect_stderr(io.StringIO()):
                    self.assertEqual(run_benchmark.main(argv),1)
                    factory.assert_not_called()
                self.assertEqual((runtime/'evaluations/predictions.json').read_bytes(),frozen)

    def test_import_failure_stops_before_model_and_freeze(self):
        with tempfile.TemporaryDirectory(prefix='benchmark-invalid-') as tmp:
            root=Path(tmp); source=root/'source'; (source/'broken').mkdir(parents=True)
            labels=root/'labels'; labels.mkdir()
            runtime=root/'runtime'
            with patch('run_benchmark.HumanEventReviewStore') as factory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(run_benchmark.main(['--dataset',str(source),'--labels',str(labels),'--runtime',str(runtime)]),1)
                factory.assert_not_called()
            self.assertFalse((runtime/'evaluations/benchmark.json').exists())


if __name__ == '__main__':
    unittest.main()
