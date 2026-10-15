(function () {
  'use strict';
  const reports = window.CaseSystem.page === 'reports';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const shortName = id => id.replace(/^comma2k19_[a-f0-9]+_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(.+)$/,'$2-$3 $4:$5:$6 · 片段 $7');
  const readyStates = ['local_ready','model_validated','model_failed'];
  const workflow = window.CaseWorkflow, stateLabels = workflow.labels;
  const reportLabels = {not_applicable:'—',not_requested:'待生成',stale:'待更新',read_error:'读取异常',local_ready:'本地报告',model_validated:'AI 整理报告',model_failed:'本地报告 · AI 失败'};
  const trackingLabels = {not_applicable:'—',no_report:'—',pending:'待核查',blocked:'待数据',checked:'已核查',read_error:'记录异常'};
  let snapshot = null, page = 1, busy = false, refreshing = false;
  let queue = null, queueBusy = false, queuePolling = false, queueTimer = null, queueGeneration = 0;
  const selected = new Map();
  const pageSize = 15;
  const filters = reports ? [['all','全部确认事件'],['ready','报告已就绪'],['reports_pending','待生成报告'],['tracking_blocked','排查待数据'],['tracking_checked','已核查'],['errors','读取异常']] :
    [['active','有候选任务'],['pending','待处理'],['needs_human','待人工复核'],['pending_ai','待 AI 复核'],['confirmed','已确认事件'],['reports_pending','待生成报告'],['tracking_blocked','排查待数据'],['reviewed','复核完成'],['no_candidates','无候选事件'],['data_issue','数据待核查'],['errors','读取异常'],['all','全部 Case']];
  $('filter').innerHTML=filters.map(([value,label])=>'<option value="'+value+'">'+label+'</option>').join('');
  const savedFilter = new URLSearchParams(location.search).get('filter');
  if (filters.some(([key])=>key===savedFilter)) $('filter').value=savedFilter;
  if (reports) {
    $('pageTitle').textContent='报告中心'; $('eyebrow').textContent='INVESTIGATION REPORTS';
    $('pageDescription').textContent='查看确认事件的证据、排查进度与处理结果。';
    $('queueTitle').firstChild.textContent='事件报告 ';
    $('scopeNote').textContent='报告仅包含已保存记录；批量操作覆盖全部确认事件。';
  }
  $('tableHead').innerHTML=reports ? '<tr><th>Case / 事件区间</th><th>确认来源</th><th>报告</th><th>排查进度</th><th></th></tr>' : '<tr><th>Case / 路线</th><th>事件复核</th><th>排查报告</th><th>当前状态</th><th></th></tr>';
  function pill(text,kind=''){return '<span class="status-pill '+kind+'">'+esc(text)+'</span>';}
  function caseMatch(item,filter){
    const counts=item.counts || {};
    if(filter==='active') return item.candidate_count>0 || item.status==='read_error';
    if(filter==='errors') return item.status==='read_error' || item.candidates.some(c=>c.report_status==='read_error'||c.tracking_status==='read_error');
    if(['confirmed','reports_pending','tracking_blocked'].includes(filter)) return counts[filter]>0;
    return workflow.matches(item,filter);
  }
  function reportMatch(c,filter){
    return filter==='all'||filter==='ready'&&readyStates.includes(c.report_status)||filter==='reports_pending'&&!readyStates.includes(c.report_status)||filter==='tracking_blocked'&&c.tracking_status==='blocked'||filter==='tracking_checked'&&c.tracking_status==='checked'||filter==='errors'&&(c.report_status==='read_error'||c.tracking_status==='read_error');
  }
  function filtered(){
    if(!snapshot)return [];
    const query=$('search').value.trim().toLowerCase(), filter=$('filter').value;
    const items=snapshot.cases.filter(item=>(item.case_id+' '+item.source_route).toLowerCase().includes(query));
    if(reports)return items.flatMap(item=>item.candidates.filter(c=>c.confirmed&&reportMatch(c,filter)).map(candidate=>({item,candidate})));
    return items.filter(item=>caseMatch(item,filter)).sort(workflow.compare);
  }
  function caseCandidate(item){
    const filter=$('filter').value;
    let selected;
    if(filter==='pending_ai')selected=item.candidates.find(c=>c.status==='pending_ai');
    if(['confirmed','reports_pending','tracking_blocked'].includes(filter))selected=item.candidates.find(c=>c.confirmed&&(filter!=='reports_pending'||!readyStates.includes(c.report_status))&&(filter!=='tracking_blocked'||c.tracking_status==='blocked'));
    return selected||item.candidates.find(c=>c.needs_human)||item.candidates.find(c=>c.status==='pending_ai')||item.candidates.find(c=>c.confirmed)||item.candidates[0];
  }
  function candidateLabel(c){return c.needs_human?'待人工':c.human_decision==='confirmed_event'?'人工确认':c.human_decision==='no_event'?'人工排除':({pending_ai:'待 AI',ai_confirmed_event:'AI 确认',ai_rejected_candidate:'已排除',human_reviewed:'人工已处理'}[c.status]||c.status);}
  function caseRow(item){
    const c=item.counts, candidate=caseCandidate(item), url=candidate?(candidate.confirmed?candidate.investigation_url:candidate.workbench_url):item.workbench_url;
    const details=item.candidates.length>1?'<details class="row-details"><summary>'+item.candidates.length+' 个候选</summary>'+item.candidates.map(candidate=>'<div class="candidate-entry"><a href="'+esc(candidate.confirmed?candidate.investigation_url:candidate.workbench_url)+'">'+esc(candidate.candidate_id)+' · '+candidate.start_time.toFixed(1)+'–'+candidate.end_time.toFixed(1)+' s</a>'+pill(candidateLabel(candidate),candidate.needs_human?'amber':candidate.confirmed?'green':'')+'</div>').join('')+'</details>':'';
    const stateKind=workflow.tones[item.status]||'';
    const issue=item.error||item.quality_issues.join('；');
    const select=queue&&queue.enabled&&c&&c.pending_ai?'<input class="case-select" type="checkbox" data-case="'+esc(item.case_id)+'" aria-label="选择 '+esc(shortName(item.case_id))+'" '+(selected.has(item.case_id)?'checked ':'')+(queueBusy?'disabled':'')+'>':'';
    return '<tr><td><div class="case-title" title="'+esc(item.case_id)+'">'+select+esc(shortName(item.case_id))+'</div><div class="case-meta-line" title="'+esc(item.source_route)+'">'+esc(item.source_route||'—')+'</div>'+details+'</td>'+
      '<td data-label="事件复核">'+(c?'<span class="mono">'+item.candidate_count+' 个候选</span><div class="cell-sub">'+(c.needs_human?c.needs_human+' 待人工 · ':'')+(c.pending_ai?c.pending_ai+' 待 AI':c.confirmed+' 已确认')+'</div>':'—')+'</td>'+
      '<td data-label="排查报告">'+(c&&c.reports_ready?'<span class="mono">'+c.reports_ready+' 份报告</span>':c&&c.reports_pending?'待生成':'—')+(c&&c.tracking_blocked?'<div class="cell-sub">'+c.tracking_blocked+' 待数据</div>':'')+'</td>'+
      '<td data-label="当前状态">'+pill(stateLabels[item.status],stateKind)+(issue?'<details class="row-details"><summary>查看异常</summary><div class="cell-sub">'+esc(issue)+'</div></details>':'')+'</td><td><a class="row-link" href="'+esc(url)+'">'+(candidate&&candidate.needs_human?'复核':candidate&&candidate.confirmed?'排查':'打开')+'</a></td></tr>';
  }
  function reportRow({item,candidate:c}){
    const summary=c.tracking_summary, ready=readyStates.includes(c.report_status);
    return '<tr><td><div class="case-title" title="'+esc(item.case_id)+'">'+esc(shortName(item.case_id))+'</div><div class="cell-sub mono">'+esc(c.candidate_id)+' · '+c.confirmed_intervals.map(r=>r[0].toFixed(1)+'–'+r[1].toFixed(1)+' s').join(' / ')+'</div></td>'+
      '<td data-label="确认来源">'+pill(c.confirmation_source==='human'?'人工确认':'AI 确认',c.confirmation_source==='human'?'':'blue')+'</td>'+
      '<td data-label="报告">'+pill(reportLabels[c.report_status]||c.report_status,ready?'green':c.report_status==='read_error'?'red':'amber')+(c.report_error?'<div class="cell-sub">'+esc(c.report_error)+'</div>':'')+'</td>'+
      '<td data-label="排查进度">'+pill(trackingLabels[c.tracking_status]||c.tracking_status,c.tracking_status==='blocked'?'amber':c.tracking_status==='checked'?'green':c.tracking_status==='read_error'?'red':'')+(summary?'<div class="cell-sub mono">'+summary.checked+' / '+summary.total+' 项已核查</div>':'')+(c.tracking_error?'<div class="cell-sub">'+esc(c.tracking_error)+'</div>':'')+'</td>'+
      '<td><a class="row-link" href="'+esc(c.investigation_url)+'">'+(ready?'查看报告':'进入排查')+'</a></td></tr>';
  }
  function render(){
    if(!snapshot)return;
    const s=snapshot.summary;
    const cards=reports?[['已就绪报告','reports_ready','ready','含证据与已保存记录','份'],['待生成报告','reports_pending','reports_pending','已确认、待整理证据','个'],['排查待数据','tracking_blocked','tracking_blocked','需要补充日志或证据','个'],['核查已完成','tracking_checked','tracking_checked','检查项已填写结果','个']]:
      [['待人工复核','needs_human','needs_human','优先处理 AI 不确定候选','个'],['待 AI 复核','pending_ai','pending_ai','可勾选后批量复核','个'],['已确认候选','confirmed','confirmed','可进入证据排查','个'],['测试记录','total_cases','all',s.no_candidate_cases+' 个 Case 未检出候选','个']];
    $('cards').innerHTML=cards.map(([label,key,filter,foot,unit])=>'<button class="metric '+($('filter').value===filter?'active':'')+'" data-filter="'+filter+'"><span class="metric-marker"></span><div class="metric-label">'+label+'</div><div class="metric-value">'+s[key]+'<span class="metric-unit">'+unit+'</span></div><div class="metric-foot">'+foot+'</div></button>').join('');
    const items=filtered(),pages=Math.max(1,Math.ceil(items.length/pageSize));page=Math.min(page,pages);
    $('rows').innerHTML=items.slice((page-1)*pageSize,page*pageSize).map(reports?reportRow:caseRow).join('');
    $('resultCount').textContent=items.length;$('empty').hidden=items.length>0;
    $('empty').innerHTML=snapshot.cases.length ? '<strong>没有符合条件的'+(reports?'报告':'任务')+'</strong>试试其他状态或搜索条件。' : '<strong>'+(reports?'还没有事件报告':'工作空间已就绪')+'</strong><p>导入日志后，即可开始事件复核和排查。</p><a class="btn primary" href="/import/">导入数据</a>';
    $('pageText').textContent=page+' / '+pages;$('prev').disabled=page<=1;$('next').disabled=page>=pages;
    $('footerCount').textContent='共 '+items.length+' '+(reports?'个确认候选':'个 Case')+' · 每页 '+pageSize+' 条';
    $('stamp').textContent='更新于 '+new Date(snapshot.completed_at).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
    $('stamp').title=new Date(snapshot.completed_at).toLocaleString('zh-CN');buttons();
  }
  function reportItems(exporting){
    if(!snapshot)return [];
    return snapshot.cases.flatMap(item=>item.candidates.filter(c=>c.confirmed&&(exporting?readyStates.includes(c.report_status):['not_requested','stale'].includes(c.report_status))).map(c=>{
      const entry={case_id:item.case_id,candidate_id:c.candidate_id,input_fingerprint:item.input_fingerprint,confirmed_intervals:c.confirmed_intervals,human_confirmation:c.confirmation_source==='human'?{annotation_sha256:c.annotation_sha256,selected_event_ids:c.selected_event_ids}:null};
      if(exporting)entry.report_fingerprint=c.report_fingerprint;return entry;
    }));
  }
  function buttons(){
    $('refresh').disabled=busy||refreshing;$('refresh').textContent=refreshing?'刷新中…':'刷新';
    $('completeReports').disabled=busy||refreshing||!reportItems(false).length;
    $('archiveReports').disabled=busy||refreshing||!reportItems(true).length;
    $('exportCsv').disabled=$('exportJson').disabled=!snapshot;
    batchButtons();
  }
  async function refresh(){
    refreshing=true;buttons();$('message').className='notice-line';$('message').textContent=snapshot?'正在更新任务状态…':'';
    try{
      const response=await fetch('/api/overview',{cache:'no-store'}),result=await response.json();
      if(!response.ok)throw Error(result.error||'读取失败');snapshot=result;
      for(const [id,fp] of selected){const item=snapshot.cases.find(c=>c.case_id===id);if(!item||item.input_fingerprint!==fp||!item.counts?.pending_ai)selected.delete(id);}
      render();$('message').textContent='';
    }catch(error){$('message').className='notice-line error';$('message').textContent=error.message+(snapshot?'；当前保留上次结果':'');if(!snapshot)$('empty').innerHTML='<strong>暂时无法读取任务</strong>请点击刷新重试。';}
    finally{refreshing=false;buttons();}
  }
  function download(content,filename,type){const url=URL.createObjectURL(content instanceof Blob?content:new Blob([content],{type})),a=document.createElement('a');a.href=url;a.download=filename;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  async function reportOperation(exporting){
    if(busy||refreshing)return;const items=reportItems(exporting);if(!items.length)return;
    busy=true;buttons();$('reportOperationStatus').className='notice-line';$('reportOperationStatus').textContent=(exporting?'正在打包 ':'正在生成 ')+items.length+' 份报告…';$('reportOperationDetails').hidden=true;
    try{
      const response=await fetch('/api/local-reports/'+(exporting?'archive':'complete'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items})});
      if(!response.ok){const error=await response.json();throw Error(error.error||'报告操作失败');}
      if(exporting){
        const filename=(response.headers.get('Content-Disposition')||'').match(/filename="(case-reports-[0-9-]+\.zip)"/);
        download(await response.blob(),filename?filename[1]:'case-reports.zip');
        const skipped=Number(response.headers.get('X-Report-Skipped')||0);
        $('reportOperationStatus').textContent='已打包 '+response.headers.get('X-Report-Included')+' 份报告，解压后打开 index.html'+(skipped?'；'+skipped+' 份已跳过，见清单':'');
      }else{
        const result=await response.json(),s=result.summary,failed=s.changed+s.busy+s.error;
        $('reportOperationResults').innerHTML=result.results.map(r=>'<li>'+esc(shortName(r.case_id)+' · '+r.candidate_id+'：'+r.message)+'</li>').join('');
        $('reportOperationDetails').hidden=false;$('reportOperationDetails').open=!!failed;
        await refresh();$('reportOperationStatus').textContent=(s.ready+s.reused)+' 份报告已就绪'+(failed?'；'+failed+' 份未完成，见处理结果':'');
      }
    }catch(error){$('reportOperationStatus').className='notice-line error';$('reportOperationStatus').textContent=error.message;}
    finally{busy=false;buttons();}
  }
  function csvCell(value){let text=value==null?'':String(value);if(typeof value==='string'&&/^[\s]*[=+@-]|^[\t\r\n]/.test(text))text="'"+text;return '"'+text.replace(/"/g,'""')+'"';}
  function csv(){
    const rows=[['observed_at','case_id','source_route','source_segment','case_status','candidate_id','start_time','end_time','status','confirmation_source','confirmed_intervals','selected_event_ids','annotation_sha256','reason','review_status','report_status','tracking_status','tracking_checked','tracking_blocked','tracking_pending','tracking_updated_at','input_fingerprint','report_fingerprint','error']];
    snapshot.cases.forEach(item=>(item.candidates.length?item.candidates:[{}]).forEach(c=>{const s=c.tracking_summary||{};rows.push([item.observed_at,item.case_id,item.source_route,item.source_segment,item.status,c.candidate_id,c.start_time,c.end_time,c.status,c.confirmation_source,c.confirmed_intervals?JSON.stringify(c.confirmed_intervals):'',c.selected_event_ids?JSON.stringify(c.selected_event_ids):'',c.annotation_sha256,c.reason,c.review_status,c.report_status,c.tracking_status,s.checked,s.blocked,s.pending,c.tracking_updated_at,item.input_fingerprint,c.report_fingerprint,item.error||c.report_error||c.tracking_error]);}));
    return '\uFEFF'+rows.map(row=>row.map(csvCell).join(',')).join('\r\n')+'\r\n';
  }
  $('refresh').onclick=refresh;$('completeReports').onclick=()=>reportOperation(false);$('archiveReports').onclick=()=>reportOperation(true);
  function changeFilter(){page=1;selected.clear();const url=new URL(location);url.searchParams.set('filter',$('filter').value);history.replaceState(null,'',url);render();}
  $('filter').onchange=changeFilter;$('search').oninput=()=>{page=1;selected.clear();render();};
  $('cards').onclick=event=>{const card=event.target.closest('[data-filter]');if(card){$('filter').value=card.dataset.filter;changeFilter();}};
  $('prev').onclick=()=>{page--;render();};$('next').onclick=()=>{page++;render();};
  $('exportJson').onclick=()=>{if(snapshot)download(JSON.stringify(snapshot,null,2)+'\n','case-overview.json','application/json;charset=utf-8');};
  $('exportCsv').onclick=()=>{if(snapshot)download(csv(),'case-overview.csv','text/csv;charset=utf-8');};
  function eligible(){return reports?[]:filtered().filter(c=>c.counts?.pending_ai>0);}
  function batchButtons(){
    if(reports)return;
    const available=!!queue?.enabled,items=eligible(),count=items.filter(c=>selected.has(c.case_id)).length;
    $('aiBatch').hidden=!available;
    $('aiSelectAll').checked=items.length>0&&count===items.length;
    $('aiSelectAll').indeterminate=count>0&&count<items.length;
    $('aiSelectAll').disabled=queueBusy||refreshing||!items.length;
    $('aiClear').hidden=!selected.size;$('aiClear').disabled=queueBusy;
    $('aiBatchStart').disabled=queueBusy||refreshing||!available||!!queue.storage_error||!selected.size;
    $('aiBatchStart').textContent=queueBusy?'提交中…':'AI 复核所选'+(selected.size?'（'+selected.size+'）':'');
    $('rows').querySelectorAll('[data-case]').forEach(el=>{el.checked=selected.has(el.dataset.case);el.disabled=queueBusy;});
    $('aiQueuePause').disabled=queueBusy||!available||queue.storage_error==='QueueStateUnreadable';
    $('aiQueueRetry').disabled=queueBusy||!!queue?.storage_error;
  }
  const jobLabels={queued:'排队中',running:'复核中',complete:'已完成',failed:'待重试',changed:'数据已更新'};
  function jobError(job){return ({InterruptedRequest:'请求中断，需明确重试',ModelCallFailed:'调用失败，待重试',InputChanged:'数据已更新，请刷新后重新选择',CandidatesChanged:'候选已更新，请刷新后重新选择'})[job.error_type]||(job.status==='failed'?'处理失败，请检查配置后重试':'');}
  function acceptQueue(value){
    if(queue&&value.instance_id===queue.instance_id&&value.revision<queue.revision)return;
    const wasActive=queue&&(queue.running||queue.queued_count),first=!queue;
    queue=value;$('aiQueue').hidden=!queue.enabled;
    if(!queue.enabled){batchButtons();return;}
    const jobs=queue.jobs||[],counts=queue.counts||{};
    $('aiQueueState').textContent=queue.storage_error?'存储异常':queue.paused?'已暂停':queue.running?'运行中':queue.queued_count?'等待处理':'空闲';
    $('aiQueuePause').textContent=queue.paused?'继续队列':'暂停';
    $('aiQueueRetry').hidden=!counts.failed;
    $('aiQueueSummary').textContent='排队 '+queue.queued_count+' · 已完成 '+(counts.complete||0)+' · 待重试 '+(counts.failed||0)+(queue.paused&&queue.running?' · 当前请求结束后暂停':'');
    if(queue.storage_error){$('aiQueueMessage').textContent=queue.storage_error==='QueueStateUnreadable'?'队列文件无法读取，已暂停。请检查本地队列记录。':'队列保存失败，已暂停。检查磁盘后点击继续队列。';$('aiQueueMessage').className='notice-line error';}
    else if($('aiQueueMessage').textContent==='队列连接中断，正在重连…'||$('aiQueueMessage').textContent.startsWith('队列保存失败')||$('aiQueueMessage').textContent.startsWith('队列文件无法读取')){$('aiQueueMessage').textContent='';}
    $('aiQueueDetails').hidden=!jobs.length;
    const recent=[...jobs].sort((a,b)=>b.updated_at.localeCompare(a.updated_at)).slice(0,12);
    $('aiQueueDetails').querySelector('summary').textContent=jobs.length>12?'最近 12 个任务':'任务进度';
    $('aiQueueJobs').innerHTML=recent.map(j=>'<div class="ai-job"><a href="/workbench/?case='+encodeURIComponent(j.case_id)+'">'+esc(shortName(j.case_id))+'</a><span>'+esc(jobLabels[j.status]||j.status)+' · '+j.completed_count+'/'+j.total_count+'</span>'+(jobError(j)?'<small>'+esc(jobError(j))+'</small>':'')+'</div>').join('');
    if(first)render();else batchButtons();
    if(wasActive&&!queue.running&&!queue.queued_count&&!refreshing)refresh();
  }
  async function queueRequest(path,body){
    const response=await fetch(path,{cache:'no-store',...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    const value=await response.json();if(!response.ok)throw Error(value.error||'队列连接失败');return value;
  }
  async function pollQueue(){
    clearTimeout(queueTimer);
    if(!queuePolling&&!queueBusy&&!document.hidden){
      queuePolling=true;const generation=queueGeneration;
      try{const value=await queueRequest('/api/automatic-review');if(generation===queueGeneration)acceptQueue(value);}
      catch(error){$('aiQueue').hidden=false;$('aiQueueMessage').className='notice-line error';$('aiQueueMessage').textContent='队列连接中断，正在重连…';}
      finally{queuePolling=false;}
    }
    queueTimer=setTimeout(pollQueue,queue&&(queue.running||queue.queued_count)?2500:10000);
  }
  async function submitBatch(items,retry=false){
    if(queueBusy||!items.length)return;
    queueBusy=true;queueGeneration++;batchButtons();$('aiQueueMessage').textContent='';
    try{
      const value=await queueRequest('/api/automatic-review/batch',{items,retry});acceptQueue(value.queue);
      const accepted=value.results.filter(r=>['queued','running','complete','no_pending'].includes(r.status));
      accepted.forEach(r=>selected.delete(r.case_id));
      const failed=value.results.length-accepted.length;
      $('aiQueueMessage').className='notice-line'+(failed?' error':'');
      $('aiQueueMessage').textContent=accepted.length+' 个 Case 已受理'+(failed?'；'+failed+' 个未入队，请刷新状态或重试失败任务':'');
    }catch(error){$('aiQueueMessage').className='notice-line error';$('aiQueueMessage').textContent=error.message+'；保留选择，可重试提交';}
    finally{queueBusy=false;batchButtons();pollQueue();}
  }
  $('rows').addEventListener('change',event=>{const id=event.target.dataset.case;if(!id)return;const item=snapshot.cases.find(c=>c.case_id===id);if(event.target.checked)selected.set(id,item.input_fingerprint);else selected.delete(id);batchButtons();});
  $('aiSelectAll').onchange=()=>{eligible().forEach(c=>{if($('aiSelectAll').checked)selected.set(c.case_id,c.input_fingerprint);else selected.delete(c.case_id);});batchButtons();};
  $('aiClear').onclick=()=>{selected.clear();batchButtons();};
  $('aiBatchStart').onclick=()=>submitBatch([...selected].map(([case_id,input_fingerprint])=>({case_id,input_fingerprint})));
  $('aiQueueRetry').onclick=()=>submitBatch(queue.jobs.filter(j=>j.status==='failed').map(j=>({case_id:j.case_id,input_fingerprint:j.input_fingerprint})),true);
  $('aiQueuePause').onclick=async()=>{
    if(queueBusy||!queue)return;queueBusy=true;queueGeneration++;batchButtons();
    try{acceptQueue(await queueRequest('/api/automatic-review',{paused:!queue.paused}));$('aiQueueMessage').textContent='';}
    catch(error){$('aiQueueMessage').className='notice-line error';$('aiQueueMessage').textContent=error.message;}
    finally{queueBusy=false;batchButtons();pollQueue();}
  };
  document.addEventListener('visibilitychange',()=>{if(!reports&&!document.hidden)pollQueue();});
  refresh();if(!reports)pollQueue();
}());
