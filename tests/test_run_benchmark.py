"""Benchmark script helpers that must not need a model call or a runtime."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import run_benchmark


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


if __name__ == '__main__':
    unittest.main()
