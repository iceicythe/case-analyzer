(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const names = ['vehicle_state.csv','object_state.csv','planning_state.csv','control_state.csv'];
  const accepted = new Set(names);
  let busy = false, rows = [], ignored = 0;
  const failed = row => ['error','conflict'].includes(row.status);
  const text = (tag, value) => { const el=document.createElement(tag); el.textContent=value; return el; };
  function mode() {
    const batch=$('importMode').value==='batch';
    $('batchPicker').hidden=!batch; $('singleId').hidden=batch; $('singlePicker').hidden=batch;
    for (const id of ['caseId','files']) { $(id).disabled=batch; $(id).required=!batch; }
    $('batchFiles').disabled=!batch; $('batchFiles').required=batch;
    rows=[]; ignored=0; render(); $('importResult').replaceChildren();
  }
  function render() {
    $('fileList').textContent=rows.length ? `${rows.length} 条 Case · 忽略 ${ignored} 个非日志文件` : '';
    const table=document.createElement('table');
    const header=document.createElement('tr');
    ['Case 编号','日志 / 缺失模块','状态'].forEach(v=>header.append(text('th',v)));
    table.append(header);
    for (const row of rows) {
      const tr=document.createElement('tr'), cell=document.createElement('td');
      if (row.status==='imported' || row.status==='skipped') cell.textContent=row.id;
      else {
        const input=document.createElement('input'); input.value=row.id; input.maxLength=64;
        input.setAttribute('aria-label','Case 编号 '+row.path); input.disabled=busy;
        input.style.cssText='width:150px;padding:7px;border:1px solid #dce3ee;border-radius:6px';
        input.addEventListener('input',()=>{row.id=input.value.trim(); row.status='pending'; row.message='待校验'; if($('importMode').value==='single') $('caseId').value=row.id;}); cell.append(input);
      }
      cell.append(text('div',row.path)); tr.append(cell);
      const missing=names.filter(n=>!row.files.some(f=>f.name===n)).map(n=>n.replace('_state.csv',''));
      tr.append(text('td',`${row.files.length} 份日志${missing.length ? ' · 缺 '+missing.join(' / ') : ''}`));
      tr.append(text('td',row.message)); table.append(tr);
    }
    $('batchPreview').replaceChildren(...(rows.length ? [table] : []));
    $('retryImport').hidden=!rows.some(failed); $('retryImport').disabled=busy;
    $('importForm').setAttribute('aria-busy',String(busy));
  }
  function selectFiles(files, batch) {
    const groups=new Map(); ignored=0;
    for (const file of files) {
      if (!accepted.has(file.name)) { ignored++; continue; }
      const parts=(file.webkitRelativePath || file.name).split('/'); parts.pop();
      const path=batch ? parts.join('/') : '所选日志';
      if (!groups.has(path)) groups.set(path,[]);
      groups.get(path).push(file);
    }
    rows=Array.from(groups, ([path, group])=>({path,files:group,id:batch ? path.split('/').pop() : $('caseId').value.trim(),status:'pending',message:'待校验'}));
    render(); $('importResult').replaceChildren();
  }
  async function payload(row) {
    if (!row.files.some(f=>f.name==='vehicle_state.csv')) throw Error('缺少 vehicle_state.csv');
    if (new Set(row.files.map(f=>f.name)).size!==row.files.length) throw Error('同一 Case 有重复文件');
    if (row.files.reduce((sum,f)=>sum+f.size,0)>24*1024*1024) throw Error('单条 Case 超过 24 MB');
    const value={case_id:row.id,source_type:$('sourceType').value,source_name:$('sourceName').value.trim(),acceleration_source:$('accelerationSource').value.trim(),files:{}};
    for (const file of row.files) value.files[file.name]=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());
    return value;
  }
  async function post(url, value) {
    const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
    const data=await response.json(); if(!response.ok) throw Error(data.error || '请求失败'); return data;
  }
  async function run(retry) {
    if (busy) return;
    if ($('importMode').value==='single') {
      if (!retry) selectFiles(Array.from($('files').files),false);
      if (rows[0]) rows[0].id=$('caseId').value.trim();
    }
    if (!rows.length) { $('importResult').textContent='没有找到支持的日志，请检查目录。'; return; }
    const targets=rows.filter(r=>retry ? failed(r) : !['imported','skipped'].includes(r.status));
    busy=true; $('submitImport').disabled=true; $('submitImport').textContent='处理中…';
    ['importMode','sourceType','sourceName','accelerationSource','caseId','files','batchFiles'].forEach(id=>$(id).disabled=true);
    $('importResult').replaceChildren();
    try {
      // Validate all rows first; hold only one Case in memory. Labels are never read.
      for (const row of targets) {
        row.message='校验中…'; render();
        try {
          if (rows.some(other=>other!==row && other.id.toLowerCase()===row.id.toLowerCase())) throw Error('本批次编号重复，请修改编号');
          const check=await post('/api/import/validate',await payload(row)); row.status=check.status; row.message=check.message;
        } catch(error) {row.status='error'; row.message=error.message;}
        render();
      }
      for (const row of targets.filter(r=>r.status==='ready')) {
        row.message='导入中…'; render();
        try {
          const data=await post('/api/import',await payload(row)); row.status='imported';
          row.message=({scheduled:'已导入 · 后台分析已排队',paused:'已导入 · 自动队列已暂停',disabled:'已导入 · 自动分析未启用',queue_unavailable:'已导入 · 自动队列异常'})[data.analysis] || '已导入';
          if(data.warnings.length) row.message+=' · '+data.warnings.join('；');
        } catch(error) {row.status='error'; row.message=error.message;}
        render();
      }
      const count=status=>rows.filter(r=>r.status===status).length;
      $('importResult').append(text('strong',`已导入 ${count('imported')} · 已跳过 ${count('skipped')} · 失败 ${rows.filter(failed).length}`));
      const link=document.createElement('a'); link.href='/'; link.className='btn'; link.textContent='查看任务'; link.style.marginLeft='16px'; $('importResult').append(link);
    } finally {
      busy=false; $('submitImport').disabled=false; $('submitImport').textContent='校验并导入';
      ['importMode','sourceType','sourceName','accelerationSource'].forEach(id=>$(id).disabled=false);
      const batch=$('importMode').value==='batch'; $('batchFiles').disabled=!batch; $('caseId').disabled=batch; $('files').disabled=batch;
      render();
    }
  }
  $('importMode').addEventListener('change',mode);
  $('batchFiles').addEventListener('change',()=>selectFiles(Array.from($('batchFiles').files),true));
  $('files').addEventListener('change',()=>selectFiles(Array.from($('files').files),false));
  $('importForm').addEventListener('submit',event=>{event.preventDefault();run(false);});
  $('retryImport').addEventListener('click',()=>{if($('importForm').reportValidity()) run(true);});
  window.addEventListener('beforeunload',event=>{if(busy){event.preventDefault();event.returnValue='';}});
  mode();
}());
