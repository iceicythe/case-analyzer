"""Ranking constraints tested with generated data, without private run archives."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from app.event_investigation import build_findings, build_investigation, build_investigation_prompt, render_investigation, validate_investigation, InvestigationMockAdapter
from app.investigation_summary import briefings
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

    def test_findings_timeline_is_sourced_and_ordered(self):
        event = self.bundle('demo_multitarget')['events'][0]
        findings = build_findings(event)
        timed = [f for f in findings if f['time'] is not None]
        self.assertEqual([f['kind'] for f in timed], ['object_gap', 'planning_reason'])
        self.assertEqual((timed[0]['time'], timed[0]['end']), (1.5, 4.5))
        self.assertEqual(timed[1]['detail'], '规划原因 CRUISE → FOLLOW_LEAD_VEHICLE')
        self.assertEqual(timed[1]['csv_rows'], [17, 18])
        self.assertEqual(timed[1]['source_file'], 'planning_state.csv')
        coverage = findings[-1]  # 无时点的统计条目垫底，不猜时刻
        self.assertEqual((coverage['kind'], coverage['time']), ('alignment', None))

    def test_render_report_lists_timeline_with_source_rows(self):
        text = render_investigation(self.bundle('demo_multitarget'))
        self.assertIn('### 证据时间线', text)
        self.assertIn('1.60 秒：规划原因 CRUISE → FOLLOW_LEAD_VEHICLE（planning_state.csv；CSV 行 17,18）', text)
        self.assertIn('1.50～4.50 秒：目标记录匹配歧义', text)
        self.assertIn('事件全程：事件内有效对齐：规划 31/31 点、控制 31/31 点', text)

    def test_timeline_reports_missing_object_log_as_full_event_gap(self):
        text = render_investigation(self.bundle('demo_no_event'))
        self.assertIn('1.50～4.50 秒：目标记录无近邻记录', text)  # 缺失如实呈现，不写成“未出现”
        self.assertIn('事件全程：事件内有效对齐：规划 0/31 点、控制 0/31 点', text)

    def test_multitarget_is_ambiguity_not_absence(self):
        event = self.bundle('demo_multitarget')['events'][0]
        self.assertEqual(event['context_summary']['object_id_count'],2)
        self.assertGreater(event['context_summary']['multi_object_timestamps'],0)
        self.assertGreater(event['context_summary']['object_ambiguous_samples'],0)
        self.assertEqual(event['context_summary']['object_missing_samples'],0)
        from app.event_investigation import default_investigation_checks
        brief=briefings({'events':[event]}, {event['event_id']:default_investigation_checks(event)})[event['event_id']]
        self.assertTrue(any('歧义' in gap for gap in brief['gaps']))
        self.assertNotIn('目标不存在',brief['next_action'])

    def test_missing_control_requires_coverage_first(self):
        package = build_investigation_prompt(self.bundle(missing_control=True))
        value = InvestigationMockAdapter().generate(package)
        self.assertEqual(value['events'][0]['prioritized_check_ids'][0],'I001-K0')
        normalized=validate_investigation(value,package)
        brief=briefings(package['analysis_context'], {e['event_id']:e['prioritized_checks'] for e in normalized['events']})['I001']
        self.assertIn('control_state.csv',brief['gaps'][0])
        self.assertEqual(brief['primary_check_id'],'I001-K0')
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

    def test_report_cites_knowledge_with_this_case_numbers(self):
        text = render_investigation(self.bundle('demo_multitarget'))
        self.assertIn('### 手册依据（结合本案数据）', text)
        self.assertIn('KB003', text)
        self.assertIn('本事件 1.5～4.5 秒', text)
        self.assertIn('FOLLOW_LEAD_VEHICLE 30 条', text)
        self.assertIn('歧义 31 点', text)
        self.assertNotIn('{', text.split('### 手册依据')[1].split('### 下一步检查')[0])

    def test_knowledge_notes_fill_from_event_and_flag_unknown_slots(self):
        from app.knowledge_notes import case_notes, render_case_note
        event = self.bundle('demo_multitarget')['events'][0]
        note = case_notes(event)[0]
        self.assertEqual(note['knowledge_id'], 'KB003')
        self.assertIn('CRUISE 1 条', note['note'])
        self.assertEqual(render_case_note('缺口 {object_missing} 点，来源 {unknown_slot}', event),
                         '缺口 0 点，来源 未提供')

    def test_entry_without_template_yields_empty_note(self):
        import json
        from app.knowledge_notes import case_notes
        event = self.bundle('demo_multitarget')['events'][0]
        catalog = [{'knowledge_id': 'KB003', 'title': '无模板条目'}]
        path = Path(self.temp.name)/'catalog.json'
        path.write_text(json.dumps(catalog, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(case_notes(event, path)[0]['note'], '')

    def test_briefings_carry_knowledge_notes(self):
        from app.event_investigation import default_investigation_checks
        event = self.bundle('demo_multitarget')['events'][0]
        brief = briefings({'events': [event]}, {event['event_id']: default_investigation_checks(event)})[event['event_id']]
        self.assertTrue(brief['knowledge_notes'])
        self.assertIn('KB003', brief['knowledge_notes'][0]['knowledge_id'])

    def test_finding_notes_schema_is_optional_and_wire_carries_indexed_findings(self):
        bundle = self.bundle('demo_multitarget')
        package = build_investigation_prompt(bundle)
        event_schema = package['response_schema']['properties']['events']['prefixItems'][0]
        self.assertNotIn('finding_notes', event_schema['required'])
        self.assertIn('finding_notes', event_schema['properties'])
        import json
        wire = json.loads(package['messages'][1]['content'])['context']
        findings = wire['events'][0]['findings']
        self.assertEqual([f['finding_index'] for f in findings], list(range(len(findings))))
        local = build_findings(bundle['events'][0])
        self.assertEqual([{k: v for k, v in f.items() if k != 'finding_index'} for f in findings], local)

    def test_mock_notes_are_conservative_and_survive_replay(self):
        package = build_investigation_prompt(self.bundle('demo_multitarget'))
        raw = InvestigationMockAdapter().generate(package)
        output = validate_investigation(raw, package)
        notes = output['events'][0]['finding_notes']
        self.assertEqual([n['finding_index'] for n in notes], [0, 1, 2])
        for note in notes:
            self.assertTrue(note['interpretation'])
            self.assertNotIn('根因', note['interpretation'])
        self.assertEqual(validate_investigation(output, package), output)

    def test_finding_notes_reject_out_of_range_duplicate_and_unknown_fields(self):
        package = build_investigation_prompt(self.bundle())
        base = InvestigationMockAdapter().generate(package)
        out_of_range = copy.deepcopy(base)
        out_of_range['events'][0]['finding_notes'][0]['finding_index'] = 99
        with self.assertRaises(ValueError): validate_investigation(out_of_range, package)
        duplicate = copy.deepcopy(base)
        duplicate['events'][0]['finding_notes'].append(
            dict(duplicate['events'][0]['finding_notes'][0], interpretation='另一句解读'))
        with self.assertRaises(ValueError): validate_investigation(duplicate, package)
        unknown_field = copy.deepcopy(base)
        unknown_field['events'][0]['finding_notes'][0]['root_cause'] = 'controller'
        with self.assertRaises(ValueError): validate_investigation(unknown_field, package)
        no_interpretation = copy.deepcopy(base)
        del no_interpretation['events'][0]['finding_notes'][0]['interpretation']
        with self.assertRaises(ValueError): validate_investigation(no_interpretation, package)

    def test_legacy_output_without_notes_still_validates(self):
        package = build_investigation_prompt(self.bundle())
        raw = InvestigationMockAdapter().generate(package)
        del raw['events'][0]['finding_notes']
        output = validate_investigation(raw, package)
        self.assertNotIn('finding_notes', output['events'][0])
        self.assertEqual(validate_investigation(output, package), output)

    def test_render_attaches_interpretation_to_each_finding(self):
        bundle = self.bundle('demo_multitarget')
        package = build_investigation_prompt(bundle)
        output = validate_investigation(InvestigationMockAdapter().generate(package), package)
        text = render_investigation(bundle, output)
        timeline = text.split('### 证据时间线')[1].split('### 数据限制')[0]
        self.assertEqual(timeline.count('证据说明（AI 选用，程序组装）：'), 3)
        self.assertIn('核对动作：', timeline)
        self.assertIn('需补数据：缺口区间对应的原始目标日志', timeline)
        self.assertIn('1.60 秒：规划原因 CRUISE → FOLLOW_LEAD_VEHICLE（planning_state.csv；CSV 行 17,18）', timeline)
        # 无模型输出时不出现解读小节内容
        plain = render_investigation(bundle)
        self.assertNotIn('证据说明（AI 选用，程序组装）：', plain)

    def test_free_form_causes_and_numbers_are_rejected_in_all_note_fields(self):
        package=build_investigation_prompt(self.bundle('demo_multitarget'))
        raw=InvestigationMockAdapter().generate(package)
        for field in ('interpretation','verification','needed_data'):
            bad=copy.deepcopy(raw)
            bad['events'][0]['finding_notes'][0][field]='已确定控制器故障，制动延迟为 999 秒。'
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_investigation(bad,package)
        saved=validate_investigation(raw,package)
        saved['events'][0]['finding_notes'][0]['interpretation']='确认感知误检导致制动。'
        with self.assertRaises(ValueError): validate_investigation(saved,package)
        with self.assertRaises(ValueError): render_investigation(package['analysis_context'],saved)

    def test_notes_cannot_be_reassigned_to_a_different_observation(self):
        package=build_investigation_prompt(self.bundle('demo_multitarget'))
        raw=InvestigationMockAdapter().generate(package)
        note=raw['events'][0]['finding_notes'][0]
        note['finding_index']=len(raw['events'][0]['finding_notes'])-1
        raw['events'][0]['finding_notes']=[note]
        with self.assertRaises(ValueError): validate_investigation(raw,package)


if __name__ == '__main__': unittest.main()
