/* Shared case-level review labels, filters and ordering. */
(function () {
  'use strict';
  const labels = {needs_human:'待人工复核', pending_ai:'待 AI 复核', reviewed:'复核完成',
    no_candidates:'无候选事件', data_issue:'数据待核查', read_error:'读取异常', loading:'读取中', unavailable:'状态未读取'};
  const tones = {needs_human:'amber', pending_ai:'blue', reviewed:'green', data_issue:'amber', read_error:'red'};
  const priorities = {read_error:0, needs_human:1, pending_ai:2, data_issue:3, loading:4, reviewed:5, no_candidates:6};
  function matches(item, filter) {
    if (filter === 'all') return true;
    if (filter === 'pending') return ['needs_human','pending_ai','data_issue','read_error'].includes(item.status);
    if (['needs_human','pending_ai'].includes(filter)) return !!(item.counts && item.counts[filter]);
    if (filter === 'data_issue') return item.status !== 'read_error' && item.data_quality_confirmed === false;
    return item.status === filter;
  }
  function compare(a, b) {
    return (priorities[a.status] ?? 4) - (priorities[b.status] ?? 4) || a.case_id.localeCompare(b.case_id);
  }
  window.CaseWorkflow = Object.freeze({labels, tones, matches, compare});
}());
