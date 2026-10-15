/* Read-only plots and row references from the validated investigation bundle. */
(function () {
  'use strict';
  const modules = {
    'vehicle_state.csv': {label: '车辆', fields: ['speed_mps', 'acceleration_mps2', 'brake']},
    'object_state.csv': {label: '目标', fields: ['distance_m', 'relative_speed_mps', 'ttc_seconds', 'confidence']},
    'planning_state.csv': {label: '规划', fields: ['planned_acceleration_mps2', 'target_speed_mps']},
    'control_state.csv': {label: '控制', fields: ['brake_command', 'requested_acceleration_mps2', 'throttle_command']}
  };
  const labels = {
    csv_row: 'CSV 行', timestamp: '时间 (s)', speed_mps: '车速 (m/s)', acceleration_mps2: '加速度 (m/s²)',
    brake: '制动量', object_id: '目标 ID', distance_m: '距离 (m)', relative_speed_mps: '相对速度 (m/s)',
    ttc_seconds: 'TTC (s)', confidence: '置信度', planned_acceleration_mps2: '规划加速度 (m/s²)',
    target_speed_mps: '目标车速 (m/s)', decision_reason: '决策原因', brake_command: '控制制动量',
    requested_acceleration_mps2: '请求加速度 (m/s²)', throttle_command: '油门请求',
    vehicle_times: '车速采样时间 (s)', time_offsets: '时间偏差 (s)'
  };
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const format = (value, digits = 4) => value == null ? '—' : typeof value === 'number' ? String(Number(value.toFixed(digits))) : String(value);
  const objectKey = row => String(row.object_id ?? '未提供 ID');
  function installStyle() {
    if (document.getElementById('event-evidence-style')) return;
    const style = document.createElement('style'); style.id = 'event-evidence-style';
    style.textContent = `
      .event-evidence{border:1px solid #dfe7e9;border-radius:9px;padding:14px;margin:14px 0;min-width:0}
      .event-evidence [hidden]{display:none!important}.ev-tools{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:8px 0}
      .ev-tools label{display:flex;align-items:center;gap:6px;font-size:13px;margin:0;color:#526770}
      .ev-tools select{width:auto;max-width:100%;font:inherit;padding:6px;border:1px solid #cedadd;border-radius:5px}
      .ev-modules{display:flex;gap:5px;flex-wrap:wrap}.ev-modules button,.ev-reference{font:inherit;background:white;border:1px solid #cddadc;border-radius:5px;padding:5px 10px;cursor:pointer}
      .ev-modules button.active{background:#087f8c;color:white;border-color:#087f8c}.ev-reference{font-size:12px;color:#087f8c;padding:2px 7px;margin:4px 4px 0 0}
      .ev-note,.ev-caption,.ev-cursor{font-size:12px;color:#61747c;overflow-wrap:anywhere;margin:7px 0}
      .ev-cursor{min-height:20px;color:#087f8c}.ev-chart{width:100%;height:230px;display:block;touch-action:pan-y;border-radius:6px;background:#fbfdfd}
      .event-evidence details{margin-top:9px}.ev-scroll{overflow:auto;max-height:320px;max-width:100%;border:1px solid #e0e8ea;border-radius:5px}
      .ev-table{width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap}.ev-table td,.ev-table th{padding:7px 9px;border-bottom:1px solid #e2e9eb;text-align:right}
      .ev-table th{background:#edf4f5;position:sticky;top:0;z-index:1}.ev-table .text{text-align:left;white-space:normal;max-width:240px;overflow-wrap:anywhere}
      .ev-table tr.cited{background:#fff4d9}.ev-table .selected-field{color:#087f8c;font-weight:600}.ev-empty{padding:12px;font-size:13px;color:#61747c}
      @media(max-width:600px){.event-evidence{padding:10px}.ev-chart{height:210px}.ev-tools label{max-width:100%;flex-wrap:wrap}}
    `;
    document.head.appendChild(style);
  }
  class EventEvidenceViewer {
    constructor(root) {
      installStyle(); this.root = root; root.classList.add('event-evidence');
      root.innerHTML = `<div class="ev-tools"><label data-ev="event-label">区间 <select data-ev="event"></select></label><nav class="ev-modules" data-ev="modules" aria-label="排查数据模块"></nav></div>
        <div class="ev-tools"><label>信号 <select data-ev="field"></select></label><label data-ev="object-label">目标 <select data-ev="object"></select></label></div>
        <p class="ev-caption" data-ev="overview"></p><canvas class="ev-chart" data-ev="chart" tabindex="0" role="img"></canvas><div class="ev-cursor" data-ev="cursor" aria-live="polite"></div>
        <p class="ev-note" data-ev="note"></p><details data-ev="rows"><summary>数据行与引用</summary><div class="ev-tools"><label>表格范围 <select data-ev="scope"><option value="event">事件对齐行</option><option value="context">前后文</option><option value="references">引用行</option></select></label></div><p class="ev-caption" data-ev="caption"></p><div data-ev="table"></div></details>`;
      this.nodes = Object.fromEntries([...root.querySelectorAll('[data-ev]')].map(node => [node.dataset.ev, node]));
      this.nodes.event.onchange = () => {this.eventId = this.nodes.event.value; this.resetSelection(); this.render();};
      this.nodes.field.onchange = () => {this.field = this.nodes.field.value; this.cursorIndex = null; this.draw(); this.renderTable();};
      this.nodes.object.onchange = () => {this.objectId = this.nodes.object.value; this.cursorIndex = null; this.draw(); this.renderTable();};
      this.nodes.scope.onchange = () => {this.scope = this.nodes.scope.value; this.renderTable();};
      this.nodes.chart.onmousemove = event => {
        if (!this.validPoints?.length || !this.graph) return;
        const box = this.nodes.chart.getBoundingClientRect(), time = this.graph.min + (event.clientX - box.left - this.graph.left) / (this.graph.right - this.graph.left) * (this.graph.max - this.graph.min);
        this.cursorIndex = this.validPoints.reduce((best, row, i) => Math.abs(row.timestamp - time) < Math.abs(this.validPoints[best].timestamp - time) ? i : best, 0);
        this.draw();
      };
      this.nodes.chart.onmouseleave = () => {this.cursorIndex = null; this.draw();};
      this.nodes.chart.onkeydown = event => {
        if (!['ArrowLeft','ArrowRight'].includes(event.key) || !this.validPoints?.length) return;
        event.preventDefault(); this.cursorIndex = Math.max(0, Math.min(this.validPoints.length - 1, (this.cursorIndex ?? 0) + (event.key === 'ArrowRight' ? 1 : -1))); this.draw();
      };
      this.observer = new ResizeObserver(() => {if (this.bundle && !this.root.hidden) this.draw();});
      this.observer.observe(this.nodes.chart); this.clear();
    }
    clear() {this.bundle = null; this.key = null; this.root.hidden = true;}
    resetSelection() {this.module = 'vehicle_state.csv'; this.field = 'speed_mps'; this.objectId = null; this.scope = 'event'; this.citedRows = []; this.cursorIndex = null;}
    setBundle(bundle) {
      if (!bundle?.events?.length) {this.clear(); return;}
      const key = JSON.stringify([bundle.case_id, bundle.candidate_id, bundle.input_fingerprint]);
      if (this.key === key) return;
      this.key = key; this.bundle = bundle; this.eventId = bundle.events[0].event_id; this.resetSelection(); this.root.hidden = false;
      this.nodes.event.innerHTML = bundle.events.map(event => `<option value="${esc(event.event_id)}">${event.confirmed_interval.map(time => format(time, 3)).join('–')} s</option>`).join('');
      this.nodes['event-label'].hidden = bundle.events.length === 1; this.nodes.rows.open = false; this.render();
    }
    get event() {return this.bundle?.events.find(event => event.event_id === this.eventId);}
    eventRows() {
      return new Set(this.module === 'vehicle_state.csv' ? this.event.aligned_event_samples.map(sample => sample.vehicle_csv_row) : this.event.aligned_event_samples.map(sample => sample.matches[this.module]).filter(match => match && !match.ambiguous && match.row_transmitted !== false).map(match => match.csv_row));
    }
    rows(filtered = true) {
      const rows = this.event.module_evidence[this.module].rows;
      return filtered && this.module === 'object_state.csv' ? rows.filter(row => objectKey(row) === this.objectId) : rows;
    }
    render() {
      const event = this.event; if (!event) return;
      this.nodes.event.value = this.eventId;
      this.nodes.modules.innerHTML = Object.entries(modules).map(([file, module]) => `<button type="button" data-module="${file}" class="${this.module === file ? 'active' : ''}">${module.label}</button>`).join('');
      this.nodes.modules.querySelectorAll('button').forEach(button => button.onclick = () => {this.module = button.dataset.module; this.field = modules[this.module].fields[0]; this.objectId = null; this.citedRows = []; this.scope = 'event'; this.cursorIndex = null; this.render();});
      this.nodes.field.innerHTML = modules[this.module].fields.map(field => `<option value="${field}">${esc(labels[field])}</option>`).join(''); this.nodes.field.value = this.field;
      this.nodes['object-label'].hidden = this.module !== 'object_state.csv';
      if (this.module === 'object_state.csv') {
        const all = this.rows(false), ids = [...new Set(all.map(objectKey))], eventIds = this.eventRows();
        if (!ids.includes(this.objectId)) this.objectId = objectKey(all.find(row => eventIds.has(row.csv_row)) || all[0] || {});
        this.nodes.object.innerHTML = ids.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join(''); this.nodes.object.value = this.objectId;
      }
      const summary = event.alignment_summary;
      this.nodes.overview.textContent = `区间 ${event.confirmed_interval.map(time => format(time, 3)).join('–')} s` + (summary ? ` · 规划对齐 ${summary['planning_state.csv'].valid_event_samples}/${summary.vehicle_event_sample_count} · 控制对齐 ${summary['control_state.csv'].valid_event_samples}/${summary.vehicle_event_sample_count}` : '');
      this.nodes.scope.value = this.scope; this.draw(); this.renderTable();
    }
    showEvidence(eventId, evidenceId) {
      const event = this.bundle?.events.find(item => item.event_id === eventId), fact = event?.facts.find(item => item.evidence_id === evidenceId);
      if (!fact) return;
      this.eventId = eventId; this.module = modules[fact.source_file] ? fact.source_file : 'planning_state.csv'; this.field = modules[this.module].fields[0]; this.citedRows = fact.source_rows; this.scope = this.citedRows.length ? 'references' : 'event'; this.cursorIndex = null;
      if (this.module === 'object_state.csv') {const row = event.module_evidence[this.module].rows.find(item => this.citedRows.includes(item.csv_row)); this.objectId = row ? objectKey(row) : null;}
      this.render(); this.nodes.rows.open = true; this.root.scrollIntoView({block:'nearest'});
    }
    renderTable() {
      if (!this.event) return;
      const module = this.event.module_evidence[this.module], eventRows = this.eventRows();
      const rows = this.scope === 'references' ? this.rows(false).filter(row => this.citedRows.includes(row.csv_row)) : this.rows().filter(row => this.scope === 'context' || eventRows.has(row.csv_row));
      this.nodes.caption.textContent = `${this.module} · 表格 ${rows.length} 行 · 已提供上下文 ${module.rows_sent}/${module.total_context_rows} 行${module.truncated ? '（已截取）' : ''}`;
      if (!rows.length) {
        const reason = {missing:'缺少日志',empty:'日志仅有表头',no_context:'当前前后文没有记录'}[module.status] || '当前范围没有可展示行';
        this.nodes.table.innerHTML = `<p class="ev-empty">${esc(reason)}。可切换表格范围查看；缺失不代表零请求。</p>`; return;
      }
      const columns = ['csv_row','timestamp',...(this.module === 'vehicle_state.csv' ? [] : ['vehicle_times','time_offsets']),...Object.keys(rows[0]).filter(key => !['csv_row','timestamp'].includes(key))];
      const cell = (row, field) => {
        if (!['vehicle_times','time_offsets'].includes(field)) return row[field];
        const samples = this.event.aligned_event_samples.filter(sample => {const match = sample.matches[this.module]; return match && !match.ambiguous && match.csv_row === row.csv_row;});
        return samples.length ? samples.map(sample => format(field === 'vehicle_times' ? sample.time : sample.matches[this.module].time_offset_seconds, 6)).join(', ') : null;
      };
      this.nodes.table.innerHTML = `<div class="ev-scroll"><table class="ev-table"><thead><tr>${columns.map(field => `<th>${esc(labels[field] || field)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr class="${this.citedRows.includes(row.csv_row) ? 'cited' : ''}">${columns.map(field => {const value = cell(row, field); return `<td class="${typeof value === 'string' ? 'text ' : ''}${field === this.field ? 'selected-field' : ''}" title="${esc(value ?? '缺失')}">${esc(format(value))}</td>`;}).join('')}</tr>`).join('')}</tbody></table></div>`;
    }
    draw() {
      const event = this.event; if (!event) return;
      const canvas = this.nodes.chart, width = canvas.getBoundingClientRect().width, height = canvas.getBoundingClientRect().height;
      if (!width || !height) return;
      const ratio = window.devicePixelRatio || 1; canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
      const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio); ctx.font = '12px system-ui, Microsoft YaHei';
      const rows = [...this.rows()].sort((a,b) => (a.timestamp ?? Infinity) - (b.timestamp ?? Infinity)), module = event.module_evidence[this.module];
      const points = rows.filter(row => Number.isFinite(row.timestamp) && Number.isFinite(row[this.field])); this.validPoints = points;
      const left = Math.min(65, width * .24), right = width - 16, top = 25, bottom = height - 34;
      const times = [...event.module_evidence['vehicle_state.csv'].rows, ...rows].map(row => row.timestamp).filter(Number.isFinite);
      const min = Math.min(event.confirmed_interval[0], ...times), max = Math.max(event.confirmed_interval[1], ...times, min + .1);
      this.graph = {left,right,min,max}; const x = time => left + (time-min) / (max-min) * (right-left);
      ctx.fillStyle = 'rgba(18,140,114,.12)'; ctx.fillRect(x(event.confirmed_interval[0]),top,Math.max(1,x(event.confirmed_interval[1])-x(event.confirmed_interval[0])),bottom-top);
      ctx.fillStyle = '#526770'; ctx.fillText(labels[this.field],left,15);
      canvas.setAttribute('aria-label', `${modules[this.module].label}${labels[this.field]}曲线，绿色为已确认区间；按左右方向键查看采样点`);
      canvas.dataset.validPoints = String(points.length); canvas.dataset.truncated = String(module.truncated); this.nodes.cursor.textContent = '';
      let sourceNote = '';
      if (this.field === 'acceleration_mps2') {
        const source = this.bundle.acceleration_provenance?.acceleration_source;
        sourceNote = source && /finite difference/i.test(source) ? '加速度由平滑车速求导，非独立测量。' : source ? `加速度来源：${source}。` : '加速度来源未声明。';
        if (this.bundle.acceleration_provenance?.source_type === 'synthetic') sourceNote = '合成加速度由车速派生，非独立测量，仅作参考。';
      }
      if (this.field === 'ttc_seconds') sourceNote = 'TTC 为基于距离和相对速度的估计，仅用于排查参考。';
      this.nodes.note.textContent = `绿色：已确认区间。曲线展示已提供的前后文；— 表示缺失，缺失/重复时间/长间隔断线。近邻匹配不证明时钟已校准。${sourceNote}${this.module === 'object_state.csv' ? ` 曲线目标：${this.objectId}。` : ''}${module.truncated ? ' 上下文已截取，只绘采样点。' : ''}`;
      if (!points.length) {
        ctx.fillStyle = '#647780'; ctx.fillText(({missing:'缺少日志',empty:'日志仅有表头'}[module.status] || '该信号没有有效数值') + '，未绘制为 0',left,Math.min(100,bottom)); canvas.dataset.segments = '0'; return;
      }
      let low = Math.min(...points.map(row => row[this.field])), high = Math.max(...points.map(row => row[this.field]));
      const padding = Math.max((high-low)*.12, this.field === 'confidence' ? .02 : .1); low -= padding; high += padding;
      const y = value => bottom - (value-low)/(high-low)*(bottom-top);
      for (let i=0;i<=4;i++) {
        const value = low+(high-low)*i/4, time=min+(max-min)*i/4;
        ctx.strokeStyle='#e1e9eb';ctx.beginPath();ctx.moveTo(left,y(value));ctx.lineTo(right,y(value));ctx.stroke();
        ctx.fillStyle='#61747c';ctx.fillText(format(value,3),3,y(value)+4);ctx.fillText(format(time,2)+'s',Math.max(left-5,Math.min(right-28,x(time)-14)),height-10);
      }
      const positiveGaps = rows.slice(1).map((row,i)=>row.timestamp-rows[i].timestamp).filter(gap=>gap>1e-8&&Number.isFinite(gap)).sort((a,b)=>a-b);
      const gapLimit = Math.min(.5,Math.max(.15,(positiveGaps[Math.floor(positiveGaps.length/2)] || .1)*2.5));
      const counts = new Map(); rows.forEach(row=>counts.set(row.timestamp,(counts.get(row.timestamp)||0)+1));
      let previous = null, segments = 0; ctx.strokeStyle='#087f8c';ctx.lineWidth=1.8;ctx.beginPath();
      rows.forEach(row=>{
        if (!Number.isFinite(row.timestamp)||!Number.isFinite(row[this.field])) {previous=null;return;}
        if (!module.truncated && previous && row.timestamp-previous.timestamp<=gapLimit && counts.get(row.timestamp)===1 && counts.get(previous.timestamp)===1) {ctx.lineTo(x(row.timestamp),y(row[this.field]));segments++;}
        else ctx.moveTo(x(row.timestamp),y(row[this.field]));previous=row;
      });ctx.stroke();canvas.dataset.segments=String(segments);
      points.forEach(row=>{const cited=this.citedRows.includes(row.csv_row);ctx.fillStyle=cited?'#cc8b18':'#087f8c';ctx.beginPath();ctx.arc(x(row.timestamp),y(row[this.field]),cited?4:2,0,Math.PI*2);ctx.fill();});
      const selected = this.cursorIndex == null ? null : points[this.cursorIndex];
      if (selected) {
        ctx.strokeStyle='#8498a0';ctx.beginPath();ctx.moveTo(x(selected.timestamp),top);ctx.lineTo(x(selected.timestamp),bottom);ctx.stroke();
        this.nodes.cursor.textContent=`${format(selected.timestamp,6)} s · ${labels[this.field]} ${format(selected[this.field],6)} · CSV 行 ${selected.csv_row}`;
      }
    }
  }
  window.EventEvidenceViewer = EventEvidenceViewer;
}());
