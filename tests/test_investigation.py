"""Ranking constraints tested with generated data, without private run archives."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.event_investigation import build_investigation, build_investigation_prompt, validate_investigation, InvestigationMockAdapter
from app.model_runner import DeepSeekAdapter, DeepSeekConfig, ModelInvocationError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'examples'))
from generate_demo import demo_cases


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='case-ranking-')
        self.addCleanup(self.temp.cleanup)

    def bundle(self, name='demo_deceleration', missing_control=False):
        payload = demo_cases()[name]
        directory = Path(self.temp.name)/name
        directory.mkdir(exist_ok=True)
        for filename,text in payload['files'].items():
            if missing_control and filename=='control_state.csv': continue
            (directory/filename).write_text(text,encoding='utf-8')
        return build_investigation(directory,[[1.5,4.5]],'C001','0'*64,{'source_type':'synthetic'})

    def test_multitarget_is_ambiguity_not_absence(self):
        event = self.bundle('demo_multitarget')['events'][0]
        self.assertEqual(event['context_summary']['object_id_count'],2)
        self.assertGreater(event['context_summary']['multi_object_timestamps'],0)
        self.assertGreater(event['context_summary']['object_ambiguous_samples'],0)
        self.assertEqual(event['context_summary']['object_missing_samples'],0)

    def test_missing_control_requires_coverage_first(self):
        package = build_investigation_prompt(self.bundle(missing_control=True))
        value = InvestigationMockAdapter().generate(package)
        self.assertEqual(value['events'][0]['prioritized_check_ids'][0],'I001-K0')
        validate_investigation(value,package)
        value['events'][0]['prioritized_check_ids'][0] = 'I001-K4'
        with self.assertRaises(ValueError): validate_investigation(value,package)

    def test_model_cannot_change_facts_or_procedure(self):
        package = build_investigation_prompt(self.bundle())
        output = validate_investigation(InvestigationMockAdapter().generate(package),package)
        self.assertEqual(validate_investigation(output,package),output)
        for field in ('facts','prioritized_checks'):
            bad = copy.deepcopy(output)
            if field=='facts': bad['events'][0][field][0]['statement'] = 'control fault'
            else: bad['events'][0][field][0]['procedure'] = ['repair controller']
            with self.assertRaises(ValueError): validate_investigation(bad,package)

    def test_wire_keeps_evidence_and_full_local_bundle_without_raw_rows(self):
        bundle = self.bundle(); before = copy.deepcopy(bundle)
        package = build_investigation_prompt(bundle)
        wire = json.loads(package['messages'][1]['content'])['context']
        self.assertEqual(bundle,before)
        self.assertIs(package['analysis_context'],bundle)
        event, compact = bundle['events'][0],wire['events'][0]
        for key in ('facts','context_summary','alignment_summary','limitations','confirmed_interval'):
            self.assertEqual(event[key],compact[key])
        self.assertNotIn('module_evidence',compact)
        self.assertNotIn('aligned_event_samples',compact)
        for old,new in zip(event['checks'],compact['checks']):
            self.assertEqual(old['procedure'],new['procedure'])
            self.assertEqual(old['required_data'],new['required_data'])
            self.assertEqual(old['result_branches'],new['result_branches'])
            self.assertEqual([{k:v for k,v in s.items() if k!='source_rows'} for s in old['signals']],
                             [compact['signals'][key] for key in new['signal_ids']])

    def test_adapter_mode_is_scoped_and_truncation_stays_failure(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='{}'))])
        adapter = DeepSeekAdapter(config=DeepSeekConfig(),client=client)
        adapter.generate({'messages':[],'thinking_mode':'disabled'})
        self.assertNotIn('reasoning_effort',client.chat.completions.create.call_args.kwargs)
        self.assertEqual(adapter.get_last_call_metrics()['thinking_mode'],'disabled')
        adapter.generate({'messages':[]})
        self.assertEqual(client.chat.completions.create.call_args.kwargs['reasoning_effort'],'low')
        client.chat.completions.create.return_value.choices[0].finish_reason = 'length'
        with self.assertRaises(ModelInvocationError): adapter.generate({'messages':[]})
        self.assertEqual(adapter.get_last_call_metrics()['status'],'truncated')


if __name__ == '__main__': unittest.main()
