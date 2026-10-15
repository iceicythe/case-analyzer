(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const accepted = new Set(['vehicle_state.csv','object_state.csv','planning_state.csv','control_state.csv']);
  let busy = false;
  $('files').addEventListener('change', () => {
    $('fileList').replaceChildren(...Array.from($('files').files, file => {
      const li = document.createElement('li');
      li.textContent = file.name + ' · ' + (file.size / 1024).toFixed(1) + ' KB';
      return li;
    }));
    $('importResult').replaceChildren();
  });
  $('importForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    const files = Array.from($('files').files);
    const result = $('importResult');
    result.replaceChildren();
    try {
      if (!files.some(f => f.name === 'vehicle_state.csv')) throw Error('请加入 vehicle_state.csv。');
      if (files.some(f => !accepted.has(f.name))) throw Error('文件名需要与格式说明一致。');
      if (new Set(files.map(f => f.name)).size !== files.length) throw Error('同名文件只能选择一份。');
      if (files.reduce((sum, f) => sum + f.size, 0) > 24 * 1024 * 1024) throw Error('文件合计超过 24 MB，请按片段拆分。');
      busy = true;
      $('submitImport').disabled = true;
      $('submitImport').textContent = '正在导入…';
      const payload = {case_id: $('caseId').value.trim(), source_type: $('sourceType').value,
        source_name: $('sourceName').value.trim(), acceleration_source: $('accelerationSource').value.trim(), files: {}};
      for (const file of files) {
        payload.files[file.name] = new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer());
      }
      const response = await fetch('/api/import', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) throw Error(data.error || '导入失败，请重试。');
      const title = document.createElement('strong'); title.textContent = data.case_id + ' 已导入'; result.append(title);
      for (const warning of data.warnings) { const p = document.createElement('p'); p.textContent = warning; result.append(p); }
      const links = document.createElement('p');
      const open = document.createElement('a'); open.className='btn primary'; open.href=data.workbench_url; open.textContent='打开 Case';
      const home = document.createElement('a'); home.className='btn'; home.href='/'; home.textContent='返回总览'; home.style.marginLeft='12px';
      links.append(open, home); result.append(links);
      $('importForm').reset(); $('fileList').replaceChildren();
    } catch (error) {
      result.textContent = error instanceof TypeError ? '无法读取文件或连接服务，请检查 UTF-8 编码和服务状态后重试。' : error.message;
    } finally {
      busy=false; $('submitImport').disabled=false; $('submitImport').textContent='校验并导入';
    }
  });
  window.addEventListener('beforeunload', event => { if(busy) {event.preventDefault(); event.returnValue='';} });
}());
