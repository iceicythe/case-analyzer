"""CLI batch import tested against generated CSVs; the browser path stays untouched."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from app.__main__ import main as app_main
from app.cli_import import collect_cases, format_report, run_import
from app.server import WorkbenchContext
from app.workspace import initialize

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'examples'))
from generate_demo import demo_cases


def write_case(directory: Path, name: str, files: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for filename, text in files.items():
        (directory / filename).write_text(text, encoding='utf-8')
    return directory


class CliImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cli-import-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = initialize(self.root / 'runtime')
        self.context = WorkbenchContext(batch_dir=self.runtime / 'index', data_root=self.runtime / 'data',
                                        annotations_root=self.runtime / 'annotations',
                                        evaluation_output=self.runtime / 'evaluations',
                                        model_reviews_root=self.runtime / 'model_reviews', automatic_review=False)
        cases = demo_cases()
        self.source = self.root / 'batch'
        write_case(self.source / 'seg_001', 'seg_001', cases['demo_deceleration']['files'])
        write_case(self.source / 'seg_002', 'seg_002', cases['demo_multitarget']['files'])
        (self.source / 'seg_003').mkdir()  # 缺 vehicle_state.csv

    def test_collect_reports_missing_vehicle_without_dropping_others(self):
        candidates, problems = collect_cases(self.source)
        self.assertEqual([name for _, name in candidates], ['seg_001', 'seg_002'])
        self.assertEqual([p['case_id'] for p in problems], ['seg_003'])
        self.assertIn('vehicle_state.csv', problems[0]['message'])

    def test_import_then_skip_then_conflict(self):
        first = run_import(self.source, self.context)
        self.assertEqual((first['totals']['imported'], first['totals']['invalid']), (2, 1))
        self.assertTrue((self.runtime / 'data' / 'seg_001' / 'vehicle_state.csv').is_file())
        second = run_import(self.source, self.context)
        self.assertEqual((second['totals']['skipped'], second['totals']['imported']), (2, 0))
        # 同编号不同内容：不覆盖，报冲突
        other = 'timestamp,speed_mps\n' + ''.join(f'{i * .1:.1f},{10 - i:.1f}\n' for i in range(6))
        (self.source / 'seg_001' / 'vehicle_state.csv').write_text(other, encoding='utf-8')
        third = run_import(self.source, self.context)
        self.assertEqual(third['totals']['conflict'], 1)
        kept = (self.runtime / 'data' / 'seg_001' / 'vehicle_state.csv').read_text(encoding='utf-8')
        self.assertEqual(kept, demo_cases()['demo_deceleration']['files']['vehicle_state.csv'])

    def test_dry_run_writes_nothing(self):
        summary = run_import(self.source, self.context, dry_run=True)
        self.assertEqual(summary['totals']['ready'], 2)
        self.assertFalse((self.runtime / 'data' / 'seg_001').exists())

    def test_single_case_directory_and_source_fields(self):
        cases = demo_cases()
        single = write_case(self.root / 'road_009', 'road_009', cases['demo_deceleration']['files'])
        summary = run_import(single, self.context, source_type='synthetic', source_name='实验室回放')
        self.assertEqual(summary['cases'][0]['case_id'], 'road_009')
        metadata = json.loads((self.runtime / 'data' / 'road_009' / 'source_metadata.json').read_text(encoding='utf-8'))
        self.assertEqual((metadata['source_type'], metadata['source_name']), ('synthetic', '实验室回放'))
        self.assertFalse(metadata['source_claim_verified'])

    def test_invalid_directory_name_fails_only_that_case(self):
        (self.source / '路测 片段').mkdir()
        write_case(self.source / '路测 片段', '路测 片段', demo_cases()['demo_deceleration']['files'])
        summary = run_import(self.source, self.context)
        statuses = {item['case_id']: item['status'] for item in summary['cases']}
        self.assertEqual(statuses['seg_001'], 'imported')
        self.assertEqual(statuses['路测 片段'], 'error')
        self.assertEqual(summary['totals']['imported'], 2)

    def test_empty_directory_raises(self):
        with self.assertRaises(ValueError):
            collect_cases(self.root / 'nowhere')

    def test_report_lines_and_exit_codes(self):
        summary = run_import(self.source, self.context)
        text = format_report(summary)
        self.assertIn('已导入 2', text)
        self.assertIn('seg_003', text)
        runtime = str(self.root / 'runtime')
        self.assertEqual(app_main(['import', str(self.source), '--runtime', runtime]), 1)
        self.assertEqual(app_main(['import', str(self.source / 'seg_001'), '--runtime', runtime,
                                   '--dry-run', '--report', str(self.root / 'report.json')]), 0)
        written = json.loads((self.root / 'report.json').read_text(encoding='utf-8'))
        self.assertTrue(written['dry_run'])
        self.assertEqual(written['totals']['conflict'], 1)  # 前一步已真实导入，预检报冲突不写入
        self.assertEqual(written['totals']['ready'], 0)


if __name__ == '__main__':
    unittest.main()
