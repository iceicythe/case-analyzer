(function () {
      "use strict";
      var workflow = window.CaseWorkflow;
      var taskVersions = {}, refreshingTasks = false;
      var state = {
        data: {summary: {}, cases: []},
        filter: "all",
        search: "",
        currentId: "",
        annotation: null,
        saveTimer: null,
        loading: false,
        saving: false,
        savePromise: null,
        composing: false,
        completing: false,
        assist: null,
        windowIndex: 0,
        zoom: false,
        dirty: false,
        revision: 0,
        navigating: false,
        submittingHuman: false, annotationConflict: false, submittedHumanKey: '',
        view: "ai", aiCase: null, aiCandidateId: "", aiBusy: null, loadToken: 0,
        aiFilter: "all", investigation: null,
        investigationLookupKey: '', investigationLookupSerial: 0, investigationLookupStatus: ''
      };
      var elements = {
        visibleCount: document.getElementById("visibleCount"),
        caseList: document.getElementById("caseList"),
        searchInput: document.getElementById("searchInput"),
        taskFilter: document.getElementById("taskFilter"),
        caseOverview: document.getElementById("caseOverview"),
        annotatorInput: document.getElementById("annotatorInput"),
        eventList: document.getElementById("eventList"),
        eventHint: document.getElementById("eventHint"),
        notesInput: document.getElementById("notesInput"),
        addEventButton: document.getElementById("addEventButton"),
        saveButton: document.getElementById("saveButton"),
        completeButton: document.getElementById("completeButton"),
        saveState: document.getElementById("saveState"),
        assistSummary: document.getElementById("assistSummary"),
        qualityStatus: document.getElementById("qualityStatus"),
        speedChart: document.getElementById("speedChart"),
        accelerationChart: document.getElementById("accelerationChart"),
        accelerationNote: document.getElementById("accelerationNote"),
        windowSlider: document.getElementById("windowSlider"),
        windowDetail: document.getElementById("windowDetail"),
        referenceEvents: document.getElementById("referenceEvents"),
        reviewConfirm: document.getElementById("reviewConfirm"),
        boundaryConfirm: document.getElementById("boundaryConfirm"),
        boundaryCheck: document.getElementById("boundaryCheck"),
        completionHint: document.getElementById("completionHint"),
        negativeEvidence: document.getElementById("negativeEvidence")
      };
      elements.protocolNotice = document.getElementById('protocolNotice');
      ["assistPanel", "aiPanel", "aiSummary", "aiCandidates", "aiContent", "aiRange", "aiChart", "aiAccelerationChart", "aiAccelerationNote", "aiRule", "aiStatus", "aiAdvice", "aiRun", "aiLocate", "aiMessage", "aiEvidence", "aiDetails", "aiMore"].forEach(function (id) { elements[id] = document.getElementById(id); });
      ["aiFilter", "aiResultExport", "aiDecisionRange"].forEach(function (id) { elements[id] = document.getElementById(id); });
      elements.aiResolve = document.getElementById('aiResolve');
      ['aiInvestigate','investigationPanel','investigationStatus','investigationSummary','investigationModel','investigationMarkdown','investigationJson','investigationReport'].forEach(function (id) { elements[id] = document.getElementById(id); });
      var investigationViewer = new EventEvidenceViewer(document.getElementById('investigationEvidence'));
      var investigationTracking = new InvestigationTracking(document.getElementById('investigationTracking'), function (saved) {
        if (hasCurrentInvestigation()) state.investigation.tracking = saved;
      });
      var humanResolutionKey = '';
      var humanResolutionBase = '', humanResolutionDirty = false, humanResolutionConflict = false;
      var humanResolutionRestore = false, humanResolutionReadLatest = false, humanResolutionRevision = 0;
      var humanResolutionDrafts = {}, humanResolutionMessage = '', humanStorageFailed = false;
      var humanDecision = document.getElementById('humanDecision');
      var humanEventOptions = document.getElementById('humanEventOptions');
      var humanReload = document.getElementById('humanReload'), humanRestore = document.getElementById('humanRestore');
      var humanOptionsSignature = '';

      function humanEligible(candidate) {
        return !!candidate && ['needs_human','human_reviewed'].indexOf(candidate.disposition.status) >= 0;
      }
      function draftMatchingEvents(candidate) {
        if (!candidate || !state.annotation) return [];
        return formEvents().filter(function (event) {
          return event.event_id && Number.isFinite(event.start_time) && Number.isFinite(event.end_time) &&
            event.end_time > event.start_time && Math.min(event.end_time,candidate.end_time)-Math.max(event.start_time,candidate.start_time)>1e-6;
        });
      }
      function renderHumanEventOptions(candidate, choice) {
        var events = draftMatchingEvents(candidate), signature = JSON.stringify(events.map(function (e) { return [e.event_id,e.start_time,e.end_time]; }));
        if (signature !== humanOptionsSignature) {
          choice = choice || humanChoice(); humanOptionsSignature = signature;
          humanEventOptions.innerHTML = events.map(function (event) {
            return '<label><input type="checkbox" value="'+escapeHtml(event.event_id)+'"> '+escapeHtml(event.event_id)+' · '+numberText(event.start_time,2)+'–'+numberText(event.end_time,2)+' s</label>';
          }).join('');
          fillHumanChoice(choice);
        }
      }

      function humanChoice() {
        return {decision:humanDecision.value,event_ids:humanDecision.value === 'confirmed_event' ? Array.from(humanEventOptions.querySelectorAll('input:checked')).map(function (input) { return input.value; }).sort() : []};
      }
      function humanDraftKey(key) { return window.CaseSystem.storageKey('human-confirmation-draft-v1:' + key, state.data.workspace); }
      function readHumanDraft(key) {
        var draft = humanResolutionDrafts[key];
        if (!draft) { try { draft = JSON.parse(window.CaseSystem.readStorage(localStorage,'human-confirmation-draft-v1:' + key,state.data.workspace) || 'null'); } catch (_) {} }
        if (!draft || draft.key !== key || typeof draft.base_record_fingerprint !== 'string' || ['', 'confirmed_event', 'no_event'].indexOf(draft.decision) < 0 || !Array.isArray(draft.event_ids) || draft.event_ids.some(function (id) { return typeof id !== 'string'; })) return null;
        return draft;
      }
      function backupHumanChoice() {
        if (!humanResolutionKey || !humanResolutionDirty) return;
        var draft = Object.assign(humanChoice(), {key:humanResolutionKey,base_record_fingerprint:humanResolutionBase});
        humanResolutionDrafts[humanResolutionKey] = draft;
        try { localStorage.setItem(humanDraftKey(humanResolutionKey), JSON.stringify(draft)); } catch (_) { humanStorageFailed = true; }
      }
      function fillHumanChoice(choice) {
        humanDecision.value = choice.decision || '';
        humanEventOptions.querySelectorAll('input').forEach(function (input) { input.checked = (choice.event_ids || []).indexOf(input.value) >= 0; });
        humanEventOptions.hidden = humanDecision.value !== 'confirmed_event';
      }
      function clearHumanResolution() {
        backupHumanChoice(); humanResolutionKey = ''; humanResolutionDirty = false;
        humanResolutionConflict = false; humanResolutionRestore = false; humanResolutionReadLatest = false;
        humanOptionsSignature = ''; humanEventOptions.innerHTML = ''; humanDecision.value = '';
        document.getElementById('humanResolution').hidden = true;
        elements.aiResolve.hidden = true; elements.completeButton.hidden = false;
        document.getElementById('humanSaveStatus').textContent = '';
        document.getElementById('reviewFollowup').hidden = true;
      }
      function forgetHumanDraft(key, submitted) {
        var draft = humanResolutionDrafts[key];
        if (draft && JSON.stringify(draft) === JSON.stringify(submitted)) delete humanResolutionDrafts[key];
        try {
          if (localStorage.getItem(humanDraftKey(key)) === JSON.stringify(submitted)) localStorage.removeItem(humanDraftKey(key));
        } catch (_) {}
      }

      function confirmedForInvestigation(candidate) {
        if (!candidate) return false;
        var d = candidate.disposition;
        return d.status === 'ai_confirmed_event' || (d.status === 'human_reviewed' && d.policy_id === 'human-candidate-confirmation-1.0' && d.human_decision === 'confirmed_event' && d.confirmed_intervals.length > 0);
      }
      function humanResolutionReady() {
        var c = aiCandidate(), choice = humanChoice(), matching = draftMatchingEvents(c);
        if (!humanEligible(c) || !choice.decision || humanResolutionConflict || humanResolutionRestore || state.annotationConflict) return false;
        return choice.decision === 'no_event' ? !matching.length : labelValue()==='positive' && choice.event_ids.length>0 &&
          choice.event_ids.every(function (id) { return matching.some(function (e) { return e.event_id===id; }); });
      }
      function humanSubmitReady() {
        var quality = state.assist && state.assist.quality;
        var checked = !state.dirty && currentConfirmationMatches() || quality && quality.can_complete &&
          elements.reviewConfirm.checked && (!quality.boundary_review_required || elements.boundaryConfirm.checked);
        return labelValue()!=='uncertain' && checked && humanResolutionReady();
      }
      function humanConfirmation(candidate) {
        var d = candidate && candidate.disposition;
        return d && d.status === 'human_reviewed' && d.human_decision === 'confirmed_event' ? {annotation_sha256:d.annotation_sha256,selected_event_ids:d.selected_event_ids} : null;
      }
      function confirmationKey(candidate) {
        return JSON.stringify([humanConfirmation(candidate),candidate.disposition.confirmed_intervals]);
      }
      function renderHumanResolution(candidate) {
        var info = candidate.human_resolution || {}, eligible = humanEligible(candidate);
        if (!eligible) { clearHumanResolution(); return; }
        document.getElementById('humanResolution').hidden = false;
        document.getElementById('humanCandidateTitle').textContent = candidate.candidate_id+' · '+numberText(candidate.start_time,1)+'–'+numberText(candidate.end_time,1)+' s';
        elements.completeButton.hidden = true; elements.aiResolve.hidden = false;
        if (state.loading || !state.annotation || !state.assist) { humanDecision.disabled = true; elements.aiResolve.disabled = true; return; }
        // Choice belongs to this candidate/source, while annotation revisions are
        // checked separately. Saving our own interval edits must not erase it.
        var key = JSON.stringify([state.currentId,candidate.candidate_id,state.aiCase.input_fingerprint]);
        var version = info.record_fingerprint || '', readLatest = humanResolutionReadLatest;
        humanResolutionReadLatest = false;
        if (key !== humanResolutionKey || readLatest || (!humanResolutionDirty && !humanResolutionRestore && version !== humanResolutionBase)) {
          backupHumanChoice();
          var oldIdentity = humanResolutionKey ? JSON.parse(humanResolutionKey) : [];
          var sourceChanged = humanResolutionDirty && key !== humanResolutionKey && oldIdentity[0] === state.currentId && oldIdentity[1] === candidate.candidate_id;
          humanResolutionKey = key; humanResolutionBase = version; humanResolutionDirty = false; humanResolutionConflict = false; humanResolutionRestore = false;
          humanResolutionRevision++;
          humanOptionsSignature = ''; renderHumanEventOptions(candidate);
          var d = candidate.disposition;
          fillHumanChoice(d.policy_id === 'human-candidate-confirmation-1.0' ? {decision:d.human_decision,event_ids:d.selected_event_ids} : {});
          humanResolutionMessage = sourceChanged ? '标注或来源已更新，请重新选择区间。' : d.human_decision ? '已保存。' : '';
          var draft = readHumanDraft(key);
          if (!draft && info.annotation_sha256) {
            var legacyKey = JSON.stringify([state.currentId,candidate.candidate_id,state.aiCase.input_fingerprint,info.annotation_sha256]);
            var legacy = readHumanDraft(legacyKey);
            if (legacy) {
              draft = Object.assign({},legacy,{key:key}); humanResolutionDrafts[key] = draft;
              try { localStorage.setItem(humanDraftKey(key),JSON.stringify(draft)); localStorage.removeItem(humanDraftKey(legacyKey)); } catch (_) {}
            }
          }
          if (draft) {
            var allowed = draftMatchingEvents(candidate).map(function (event) { return event.event_id; });
            if (draft.event_ids.every(function (id) { return allowed.indexOf(id) >= 0; })) {
              if (!readLatest && draft.base_record_fingerprint === version) {
                fillHumanChoice(draft); humanResolutionDirty = true;
                humanResolutionMessage = '已恢复本浏览器选择，核对后保存。';
              } else {
                humanResolutionRestore = true;
                humanResolutionMessage = '已读取最新结论；本页选择可恢复，核对后再保存。';
              }
            }
          }
        } else if (humanResolutionDirty && version !== humanResolutionBase) {
          humanResolutionConflict = true;
        }
        renderHumanEventOptions(candidate);
        var busy = !!state.aiBusy || state.loading || state.submittingHuman || state.completing || state.navigating;
        humanDecision.disabled = busy;
        humanEventOptions.querySelectorAll('input').forEach(function (input) { input.disabled = busy; });
        humanReload.hidden = !humanResolutionConflict;
        humanRestore.hidden = !humanResolutionRestore;
        humanReload.disabled = humanRestore.disabled = busy;
        document.getElementById('humanSaveStatus').textContent = (humanResolutionConflict ? '其他页面已更新结论；本页选择已保留，请先读取最新结论。' : humanResolutionMessage) + (humanStorageFailed ? ' 本浏览器暂存失败，刷新前请保存。' : '');
        elements.aiResolve.hidden = false; elements.completeButton.hidden = true;
        elements.aiResolve.disabled = busy || state.saving || state.completing || !humanSubmitReady();
        elements.aiResolve.textContent = state.submittingHuman ? '正在提交…' : '提交复核';
        elements.aiResolve.title = '保存整段人工标注，并提交本候选的明确结论';
        document.getElementById('humanChoiceHint').textContent = humanDecision.value === 'no_event' && draftMatchingEvents(candidate).length ? '仍有重叠事件，请先核对下方区间。' :
          humanDecision.value === 'confirmed_event' ? '勾选属于本候选的事件；区间可在下方调整。' : '';
        document.getElementById('reviewFollowup').hidden = state.submittedHumanKey !== state.currentId+'/'+candidate.candidate_id || state.dirty || humanResolutionDirty;
      }
      document.getElementById('humanResolution').addEventListener('change', function () {
        humanResolutionDirty = true; humanResolutionRevision++; humanResolutionRestore = false;
        humanResolutionMessage = '待提交';
        humanEventOptions.hidden = humanDecision.value !== 'confirmed_event';
        backupHumanChoice();
        renderHumanResolution(aiCandidate());
      });
      humanReload.addEventListener('click', function () {
        if (state.aiBusy || state.loading) return;
        var candidate = aiCandidate(), caseId = state.currentId, token = state.loadToken;
        if (!candidate) return;
        backupHumanChoice(); state.aiBusy = {caseId:caseId,candidateId:candidate.candidate_id}; renderAi();
        request('/api/candidates/' + encodeURIComponent(caseId)).then(function (result) {
          if (state.currentId !== caseId || state.loadToken !== token || state.aiCandidateId !== candidate.candidate_id) return;
          acceptAiCase(result, caseId); humanResolutionReadLatest = true; renderAi();
        }).catch(function (error) {
          if (state.currentId === caseId && state.loadToken === token) { humanResolutionMessage = error.message; elements.aiMessage.textContent = error.message; }
        }).finally(function () { state.aiBusy = null; renderAi(); });
      });
      humanRestore.addEventListener('click', function () {
        if (state.aiBusy || state.loading) return;
        var draft = readHumanDraft(humanResolutionKey); if (!draft) return;
        fillHumanChoice(draft); humanResolutionRestore = false; humanResolutionDirty = true; humanResolutionRevision++;
        humanResolutionMessage = '本页选择已恢复，请核对后保存。'; backupHumanChoice(); renderAi();
      });
      window.addEventListener('pagehide', backupHumanChoice);

      function escapeHtml(value) {
        return String(value == null ? "" : value)
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;")
          .replace(/'/g, "&#039;");
      }

      function request(url, options) {
        return fetch(url, options).then(function (response) {
          return response.json().catch(function () { return {}; }).then(function (body) {
            if (!response.ok) {
              var error = new Error(body.error || ("请求失败：" + response.status));
              error.status = response.status; error.code = body.code; throw error;
            }
            return body;
          });
        });
      }

      function setView(view) {
        if (state.submittingHuman) return;
        if (['ai','annotation','investigation'].indexOf(view) < 0) view = 'ai';
        state.view = view;
        document.body.dataset.view = view;
        document.body.classList.toggle('ai-view', view === 'ai');
        elements.assistPanel.hidden = view !== 'annotation';
        elements.aiPanel.hidden = view !== 'ai';
        document.getElementById('investigationTab').hidden = view !== 'investigation';
        document.getElementById('candidateStrip').hidden = false;
        document.querySelectorAll('.review-tabs [data-view]').forEach(function (button) {
          var active = button.getAttribute('data-view') === view;
          button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
        });
        renderInvestigation();
        updateCaseUrl();
        document.querySelector('.visual').scrollTop = 0;
      }
      function updateCaseUrl() {
        if (!state.currentId) return;
        var url = new URL('/workbench/', location.origin);
        url.searchParams.set('case', state.currentId);
        if (state.aiCandidateId) url.searchParams.set('candidate', state.aiCandidateId);
        url.hash = state.view === 'investigation' ? 'investigationPanel' : state.view === 'annotation' ? 'assistPanel' : 'aiPanel';
        history.replaceState(null, '', url);
        try { sessionStorage.setItem(window.CaseSystem.storageKey('case-analyzer-last-case',state.data.workspace),state.currentId); } catch (_) {}
      }

      function aiCandidate() {
        return state.aiCase && state.aiCase.candidates.find(function (c) { return c.candidate_id === state.aiCandidateId; });
      }

      var automaticTimer = null, automaticState = null;
      function renderAutomatic() {
        if (!state.data.automatic_review) return;
        var job = automaticState && automaticState.jobs[0];
        var paused = automaticState && automaticState.paused;
        document.getElementById('automaticPause').textContent = paused ? '继续自动复核' : '暂停';
        document.getElementById('automaticRetry').hidden = !job || job.status !== 'failed';
        document.getElementById('automaticStatus').textContent = automaticState && automaticState.storage_error ? '队列存储异常，已暂停' : paused ? '已暂停 · 当前请求完成后停止' :
          !job ? (automaticState && (automaticState.discovering || automaticState.discovery_count) ? '正在准备分析任务…' : '无待 AI 任务') : job.status === 'running' ? (job.stage === 'investigation' ? 'AI 正在整理排查报告' : 'AI 正在复核 · ' + (job.completed_count || 0) + '/' + job.total_count) :
          job.status === 'queued' ? '已排队 · 后台依次分析' : job.status === 'changed' ? '数据已更新，请刷新 Case' : job.status === 'failed' ? (job.error_type === 'InterruptedRequest' ? '请求中断，点击重试后继续' : '任务未完成，请重试或检查模型配置') : '自动分析完成';
      }
      function startAutomatic(caseId, token, retry) {
        return request('/api/automatic-review/' + encodeURIComponent(caseId), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({retry:!!retry})}).then(function (result) {
          if (state.currentId === caseId && state.loadToken === token) { automaticState = result; renderAutomatic(); }
        }).catch(function (error) {
          if (state.currentId === caseId && state.loadToken === token) document.getElementById('automaticStatus').textContent = error.message;
        });
      }
      function pollAutomatic(caseId, token) {
        clearTimeout(automaticTimer);
        automaticTimer = setTimeout(async function () {
          if (state.currentId !== caseId || state.loadToken !== token) return;
          try {
            if (!state.aiBusy) {
              var result = await request('/api/candidates/' + encodeURIComponent(caseId));
              if (state.currentId !== caseId || state.loadToken !== token || state.submittingHuman) return;
              var changed = state.aiCase && result.input_fingerprint !== state.aiCase.input_fingerprint;
              acceptAiCase(result, caseId); automaticState = result.automatic_review;
              if (state.investigationLookupStatus !== 'loading' && state.investigationLookupStatus !== 'error' &&
                  (!state.investigation || state.investigation.status !== 'model_validated')) state.investigationLookupKey = '';
              renderAi(); renderList();
              if (changed) await startAutomatic(caseId, token, false);
            }
          } catch (error) {
            if (state.currentId === caseId && state.loadToken === token) document.getElementById('automaticStatus').textContent = '连接中断，正在重新连接…';
          }
          if (state.currentId === caseId && state.loadToken === token) pollAutomatic(caseId, token);
        }, 2000);
      }
      document.getElementById('automaticPause').addEventListener('click', async function () {
        var button = this; button.disabled = true;
        try {
          var result = await request('/api/automatic-review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({paused:!(automaticState && automaticState.paused)})});
          if (automaticState) automaticState.paused = result.paused;
          renderAutomatic();
        } catch (error) { document.getElementById('automaticStatus').textContent = error.message; }
        finally { button.disabled = false; }
      });
      document.getElementById('automaticRetry').addEventListener('click', function () { startAutomatic(state.currentId, state.loadToken, true); });
      function accelerationNote(provenance) {
        var source = provenance && provenance.acceleration_source;
        if (provenance && provenance.source_type === 'synthetic') return '合成加速度 · 车速派生，非独立测量 · 不参与事件判定';
        return '加速度辅助参考 · ' + (source && /finite difference/i.test(source) ? '车速平滑求导，非独立测量' : source ? source : '来源未声明') + ' · 不参与事件判定';
      }

      function drawAcceleration(canvas, points, range, bands) {
        var ctx = canvas.getContext('2d'); ctx.clearRect(0, 0, canvas.width, canvas.height);
        canvas.dataset.timeRange = JSON.stringify(range || []);
        if (!range) return;
        var shown = points.filter(function (p) { return Number.isFinite(p.timestamp) && p.timestamp >= range[0] - 1e-6 && p.timestamp <= range[1] + 1e-6; });
        var values = shown.filter(function (p) { return Number.isFinite(p.acceleration_mps2); }).map(function (p) { return p.acceleration_mps2; });
        ctx.font = '12px Microsoft YaHei, sans-serif'; ctx.fillStyle = '#64727a';
        ctx.fillText('加速度 m/s²', 62, 14);
        if (!values.length) { ctx.fillText('当前范围没有有效加速度数据', 250, 80); return; }
        var low = Math.min(0, Math.min.apply(null, values)), high = Math.max(0, Math.max.apply(null, values));
        var pad = Math.max(.2, (high-low)*.1); low -= pad; high += pad;
        var left = 62, right = 782, top = 23, bottom = 137;
        function x(t) { return left + (t-range[0]) / Math.max(.001,range[1]-range[0]) * (right-left); }
        function y(a) { return bottom - (a-low)/(high-low)*(bottom-top); }
        bands.forEach(function (band) {
          if (band.start == null || band.end == null || band.end < range[0] || band.start > range[1]) return;
          ctx.fillStyle = band.color; ctx.fillRect(x(Math.max(range[0],band.start)),top,Math.max(1,x(Math.min(range[1],band.end))-x(Math.max(range[0],band.start))),bottom-top);
        });
        for (var i=0;i<=4;i++) {
          var a=low+(high-low)*i/4, t=range[0]+(range[1]-range[0])*i/4;
          ctx.strokeStyle='#e2e7ea'; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(left,y(a)); ctx.lineTo(right,y(a)); ctx.stroke();
          ctx.fillStyle='#64727a'; ctx.fillText(a.toFixed(1),9,y(a)+4); ctx.fillText(t.toFixed(1)+'s',Math.min(right-40,x(t)),158);
        }
        ctx.strokeStyle='#84929b'; ctx.setLineDash([4,4]); ctx.beginPath(); ctx.moveTo(left,y(0)); ctx.lineTo(right,y(0)); ctx.stroke(); ctx.setLineDash([]);
        ctx.strokeStyle='#9163b6'; ctx.lineWidth=2; ctx.beginPath();
        var previous=null;
        shown.forEach(function (p) {
          if (!Number.isFinite(p.acceleration_mps2)) { previous=null; return; }
          if (!previous || p.timestamp-previous.timestamp>.15 || p.timestamp<=previous.timestamp) ctx.moveTo(x(p.timestamp),y(p.acceleration_mps2));
          else ctx.lineTo(x(p.timestamp),y(p.acceleration_mps2));
          previous=p;
        });
        ctx.stroke();
      }

      function drawAiChart(candidate) {
        var bands = [{start:candidate.start_time,end:candidate.end_time,color:'#fff0d6'}].concat(candidate.strict_reference_intervals.map(function (r) { return {start:r[0],end:r[1],color:'#d8efdf'}; }));
        drawAcceleration(elements.aiAccelerationChart,candidate.points,candidate.context_range,bands);
        elements.aiAccelerationNote.textContent = accelerationNote(candidate.supplemental_context.provenance);
        elements.aiChart.dataset.timeRange = JSON.stringify(candidate.context_range);
        var ctx = elements.aiChart.getContext('2d'), points = candidate.points.filter(function (p) { return Number.isFinite(p.speed_mps); });
        ctx.clearRect(0, 0, 800, 220);
        if (!points.length) return;
        var range = candidate.context_range, speeds = points.map(function (p) { return p.speed_mps; });
        var low = Math.max(0, Math.min.apply(null, speeds) - 1), high = Math.max.apply(null, speeds) + 1;
        function x(t) { return 62 + (t - range[0]) / Math.max(.1, range[1] - range[0]) * 720; }
        function y(v) { return 185 - (v - low) / Math.max(.1, high - low) * 155; }
        function shade(start, end, color) { ctx.fillStyle = color; ctx.fillRect(x(start), 20, Math.max(1, x(end) - x(start)), 165); }
        shade(candidate.start_time, candidate.end_time, '#fff0d6');
        candidate.strict_reference_intervals.forEach(function (r) { shade(r[0], r[1], '#d8efdf'); });
        ctx.fillStyle = '#64727a'; ctx.font = '12px sans-serif';
        for (var i = 0; i <= 4; i++) {
          var v = low + (high - low) * i / 4, t = range[0] + (range[1] - range[0]) * i / 4;
          ctx.fillText(v.toFixed(1), 10, y(v) + 4); ctx.fillText(t.toFixed(1) + 's', x(t) - 14, 209);
          ctx.strokeStyle = '#e2e7ea'; ctx.beginPath(); ctx.moveTo(62, y(v)); ctx.lineTo(782, y(v)); ctx.stroke();
        }
        ctx.fillText('m/s', 10, 15);
        ctx.strokeStyle = '#0068eb'; ctx.lineWidth = 2; ctx.beginPath();
        points.forEach(function (p, i) { if (i === 0 || p.timestamp - points[i-1].timestamp > .15) ctx.moveTo(x(p.timestamp), y(p.speed_mps)); else ctx.lineTo(x(p.timestamp), y(p.speed_mps)); }); ctx.stroke();
      }

      function renderAi() {
        renderAutomatic();
        elements.investigationPanel.hidden = !hasCurrentInvestigation();
        elements.aiCandidates.innerHTML = '';
        elements.aiContent.hidden = !state.aiCase;

        elements.aiResultExport.disabled = !state.aiCase;

        if (!state.aiCase) { clearHumanResolution(); elements.aiSummary.textContent = '正在读取候选…'; renderInvestigation(); return; }
        elements.accelerationNote.textContent = accelerationNote(state.aiCase.acceleration_provenance);
        var counts = state.aiCase.triage_summary;
        var confirmed = state.aiCase.candidates.filter(confirmedForInvestigation);
        elements.aiSummary.textContent = state.aiCase.candidates.length + ' 个候选 · ' + confirmed.length + ' 已确认' + (counts.needs_human ? ' · ' + counts.needs_human + ' 待人工' : '') + (counts.pending_ai ? ' · ' + counts.pending_ai + ' 待 AI' : '');
        var candidates = state.aiCase.candidates.filter(function (c) { return state.aiFilter === 'all' || (state.aiFilter === 'automatic' ? c.disposition.status.indexOf('ai_') === 0 : c.disposition.status === state.aiFilter); });
        if (!candidates.length) { clearHumanResolution(); elements.aiCandidates.textContent = state.aiCase.candidates.length ? '此状态下没有候选。' : '本 Case 未检出候选事件。'; elements.aiContent.hidden = true; elements.investigationPanel.hidden = true; investigationViewer.clear(); investigationTracking.clear(); renderInvestigation(); return; }
        var candidate = aiCandidate();
        if (!candidate || candidates.indexOf(candidate) < 0) {
          candidate = candidates.find(function (c) { return c.disposition.status === 'needs_human'; }) || candidates.find(confirmedForInvestigation) || candidates[0];
          state.aiCandidateId = candidate.candidate_id;
        }
        updateCaseUrl();
        elements.aiCandidates.innerHTML = candidates.map(function (c) {
          var statusText = {ai_confirmed_event:'已确认', ai_rejected_candidate:'已排除', needs_human:'待人工', pending_ai:'待 AI', human_reviewed:'人工已处理'}[c.disposition.status];
          if (c.disposition.status === 'human_reviewed' && c.disposition.human_decision) statusText = c.disposition.human_decision === 'confirmed_event' ? '人工确认' : '人工排除';
          return '<button class="chip ' + (c === candidate ? 'active' : '') + '" data-ai-candidate="' + escapeHtml(c.candidate_id) + '">' + escapeHtml(c.candidate_id) + ' · ' + c.start_time.toFixed(1) + '～' + c.end_time.toFixed(1) + 's · ' + statusText + '</button>';
        }).join('');
        elements.aiContent.hidden = false;
        elements.aiRange.textContent = '候选范围 ' + candidate.start_time.toFixed(1) + '～' + candidate.end_time.toFixed(1) + ' 秒';
        drawAiChart(candidate);
        var rule = {meets_numeric_rule:'达到数值规则', below_numeric_rule:'未达到数值规则', source_unverified:'来源待核查'};
        var w = candidate.representative_window;
        elements.aiRule.textContent = (rule[candidate.event_assessment] || candidate.event_assessment) + ' · 代表窗口下降 ' + w.speed_drop_mps.toFixed(3) + ' m/s';
        var review = candidate.review, output = review.output;
        var disposition = candidate.disposition;
        elements.aiInvestigate.hidden = !confirmedForInvestigation(candidate);
        elements.aiInvestigate.disabled = !!state.aiBusy || state.loading || state.investigationLookupStatus === 'loading';
        elements.aiInvestigate.textContent = state.investigationLookupStatus === 'loading' ? '读取报告…' : '进入排查';
        elements.aiStatus.textContent = {ai_confirmed_event:'AI 已确认减速事件', ai_rejected_candidate:'AI 已排除候选', needs_human:'需要人工复核', human_reviewed:disposition.human_decision === 'confirmed_event' ? '人工已确认事件' : disposition.human_decision === 'no_event' ? '人工已排除候选' : '人工已处理', pending_ai:output ? '等待重新复核' : '等待 AI 复核'}[disposition.status];
        renderHumanResolution(candidate);
        elements.aiAdvice.textContent = disposition.status === 'pending_ai' && output ? output.boundary_advice : disposition.reason;
        elements.aiDecisionRange.textContent = disposition.confirmed_intervals.length ? '事件区间：' + disposition.confirmed_intervals.map(function (r) { return r[0].toFixed(1) + '～' + r[1].toFixed(1) + 's'; }).join('；') : '';
        elements.aiRun.textContent = state.aiBusy ? '模型复核中…' : review.status === 'not_requested' ? '调用 AI 复核' : '重新复核';
        elements.aiRun.disabled = !!state.aiBusy || !state.aiCase.quality.can_complete;
        elements.aiRun.hidden = !state.data.model?.ready;
        elements.aiLocate.disabled = !state.assist;
        elements.aiLocate.textContent = disposition.status === 'needs_human' ? '人工核查' : '核对窗口';


        elements.aiDetails.hidden = !output;
        elements.aiMore.innerHTML = output ? '<p>' + escapeHtml(output.boundary_advice) + '</p><p>' + escapeHtml(output.uncertainty.join('；')) + '</p><p>' + escapeHtml(output.next_steps.join('；')) + '</p>' : '';
        elements.aiEvidence.innerHTML = '<p>代表窗口 ' + w.start_time.toFixed(1) + '～' + w.end_time.toFixed(1) + 's · ' + w.decreasing_intervals + '/5 个间隔下降 · 回升 ' + w.cumulative_recovery_mps.toFixed(3) + ' m/s</p>' +
          '<table class="window-table"><thead><tr><th>时间(s)</th><th>车速(m/s)</th><th>加速度(m/s²)</th><th>CSV 行</th></tr></thead><tbody>' + candidate.window_points.map(function (p) { return '<tr><td>' + p.timestamp.toFixed(1) + '</td><td>' + p.speed_mps.toFixed(6) + '</td><td>' + (p.acceleration_mps2 == null ? '缺失' : p.acceleration_mps2.toFixed(6)) + '</td><td>' + p.source_row + '</td></tr>'; }).join('') + '</tbody></table>' +
          '<details><summary>完整上下文 · ' + candidate.points.length + ' 点</summary><p>' + escapeHtml(candidate.supplemental_context.provenance.acceleration_source || '加速度来源未声明') + '</p><table class="window-table"><thead><tr><th>时间(s)</th><th>车速(m/s)</th><th>加速度(m/s²)</th><th>CSV 行</th></tr></thead><tbody>' + candidate.points.map(function (p) { return '<tr><td>' + p.timestamp.toFixed(1) + '</td><td>' + (p.speed_mps == null ? '缺失' : p.speed_mps.toFixed(6)) + '</td><td>' + (p.acceleration_mps2 == null ? '缺失' : p.acceleration_mps2.toFixed(6)) + '</td><td>' + p.source_row + '</td></tr>'; }).join('') + '</tbody></table></details>' +
          (output ? output.facts.map(function (f) { return '<p>' + escapeHtml(f.evidence_id + ' · ' + f.statement) + '</p>'; }).join('') : '');
        renderInvestigation();
        loadExistingInvestigation();
      }

      function resetInvestigation() {
        state.investigation = null;
        state.investigationLookupKey = '';
        state.investigationLookupStatus = '';
        state.investigationLookupSerial++;
        investigationViewer.clear();
        investigationTracking.clear();
      }

      function loadExistingInvestigation() {
        var candidate = aiCandidate();
        if (!confirmedForInvestigation(candidate) || state.aiBusy) return;
        var caseId = state.currentId, candidateId = candidate.candidate_id, token = state.loadToken, fingerprint = state.aiCase.input_fingerprint;
        var confirmation = confirmationKey(candidate);
        var key = JSON.stringify([caseId,candidateId,fingerprint,confirmation]);
        if (state.investigationLookupKey === key) return;
        state.investigationLookupKey = key; state.investigationLookupStatus = 'loading';
        var serial = ++state.investigationLookupSerial;
        elements.aiInvestigate.disabled = true; elements.aiInvestigate.textContent = '读取已有报告…';
        request('/api/investigation/' + encodeURIComponent(caseId) + '/' + encodeURIComponent(candidateId)).then(function (record) {
          if (state.currentId !== caseId || state.loadToken !== token || state.aiCandidateId !== candidateId || state.aiCase.input_fingerprint !== fingerprint || state.investigationLookupSerial !== serial || confirmationKey(aiCandidate()) !== confirmation) return;
          state.investigationLookupStatus = record.status;
          state.investigation = record.report ? record : null;
          renderAi();
          if (record.report && state.view === 'investigation') document.querySelector('.visual').scrollTop = 0;
        }).catch(function (error) {
          if (state.currentId !== caseId || state.loadToken !== token || state.aiCandidateId !== candidateId || state.investigationLookupSerial !== serial) return;
          state.investigationLookupStatus = 'error'; elements.aiMessage.textContent = '已有报告读取失败：' + error.message; renderAi();
        });
      }

      function hasCurrentInvestigation() {
        var record = state.investigation, candidate = aiCandidate();
        if (!(record && record.report && confirmedForInvestigation(candidate) && record.bundle.case_id === state.currentId && record.bundle.candidate_id === candidate.candidate_id && record.bundle.review_fingerprint === state.aiCase.input_fingerprint)) return false;
        var source = record.bundle.acceleration_provenance.human_event_confirmation, expected = humanConfirmation(candidate);
        return JSON.stringify(record.bundle.events.map(function (event) { return event.confirmed_interval; })) === JSON.stringify(candidate.disposition.confirmed_intervals) && (expected ? !!source && source.annotation_sha256 === expected.annotation_sha256 && JSON.stringify(source.selected_event_ids) === JSON.stringify(expected.selected_event_ids) : !source);
      }
      function investigationActionDetails(check) {
        if (!check.procedure) return '';
        var rows = check.procedure.map(function (step) { return '<li>' + escapeHtml(step) + '</li>'; }).join('');
        var gaps = (check.required_data || []).map(function (text) { return '<li>' + escapeHtml(text) + '</li>'; }).join('');
        var branches = (check.result_branches || []).map(function (b) { return '<li>' + escapeHtml(b.condition + ' → ' + b.next_action) + '</li>'; }).join('');
        return '<details><summary>核查步骤' + (gaps ? ' · 需补材料' : '') + '</summary><ol>' + rows + '</ol>' +
          (gaps ? '<p>待补材料</p><ul>' + gaps + '</ul>' : '') + '<p>结果判断</p><ul>' + branches + '</ul>' +
          '<p>' + escapeHtml(check.caution || '') + '</p></details>';
      }

      function renderInvestigation() {
        var record = state.investigation;
        var valid = hasCurrentInvestigation(), candidate = aiCandidate();
        var empty = document.getElementById('investigationEmpty'), generate = document.getElementById('generateInvestigation');
        empty.hidden = valid; elements.investigationPanel.hidden = !valid;
        generate.hidden = valid || !confirmedForInvestigation(candidate) || !!state.data.automatic_review && state.investigationLookupStatus !== 'error';
        generate.disabled = !!state.aiBusy || state.loading || state.investigationLookupStatus === 'loading';
        generate.textContent = state.investigationLookupStatus === 'error' ? '重试读取报告' : state.investigationLookupStatus === 'stale' ? '更新本地报告' : '生成本地报告';
        document.getElementById('investigationEmptyTitle').textContent = !candidate ? '选择已确认事件' : !confirmedForInvestigation(candidate) ? '此候选尚未确认' : state.investigationLookupStatus === 'loading' ? '正在读取报告' : state.investigationLookupStatus === 'error' ? '报告暂时无法读取' : state.data.automatic_review ? '后台正在生成排查报告' : '事件已确认，待整理证据';
        document.getElementById('investigationEmptyText').textContent = candidate && confirmedForInvestigation(candidate) ? '关联车辆、目标、规划与控制记录，生成可追溯的排查报告。' : '在事件复核中确认区间后，进入证据排查。';
        if (!valid) { investigationViewer.clear(); investigationTracking.clear(); return; }
        elements.investigationStatus.textContent = {local_ready:'本地证据报告 · 未调用模型',model_validated:'AI 建议已校验 · 根因尚未确认',model_failed:'AI 整理失败 · 已保留本地报告'}[record.status] || '待重新排查';
        if (humanConfirmation(aiCandidate())) elements.investigationStatus.textContent = '人工确认 · ' + elements.investigationStatus.textContent;
        elements.investigationSummary.innerHTML = record.bundle.events.map(function (event) {
          var output = record.model_output && record.model_output.events.find(function (e) { return e.event_id === event.event_id; });
          var checks = record.display_checks && record.display_checks[event.event_id] || (output ? output.prioritized_checks : []);
          var brief = record.briefings && record.briefings[event.event_id];
          function action(check) {
            return '<div>' + escapeHtml(check.step) + investigationActionDetails(check) + '<div>' + check.evidence_ids.map(function (id) {
              var label = /-R$/.test(id) ? '原因记录' : /-J$/.test(id) ? '目标关联' : /-V$/.test(id) ? '车辆' : /-O$/.test(id) ? '目标' : /-P$/.test(id) ? '规划' : /-C$/.test(id) ? '控制' : /-T$/.test(id) ? '覆盖' : '链路';
              return '<button type="button" class="ev-reference" data-investigation-event="' + escapeHtml(event.event_id) + '" data-investigation-evidence="' + escapeHtml(id) + '">' + label + '证据</button>';
            }).join('') + '</div></div>';
          }
          return '<section class="investigation-brief"><p><strong>' + event.confirmed_interval[0].toFixed(1) + '～' + event.confirmed_interval[1].toFixed(1) + ' 秒</strong> · ' + escapeHtml(event.observation) + '</p>' +
            (brief ? (brief.gaps.length ? '<p class="brief-gap"><strong>证据缺口</strong> · ' + brief.gaps.map(escapeHtml).join('；') + '</p>' : '') +
              '<p class="brief-next"><strong>下一步</strong> · ' + escapeHtml(brief.next_action) + '</p><p class="brief-reason">依据：' + escapeHtml(brief.reason) + '</p>' : '') +
            (checks[0] ? action(checks[0]) : '') + (checks.length > 1 ? '<details><summary>其他检查（' + (checks.length-1) + '）</summary>' + checks.slice(1).map(action).join('') + '</details>' : '') + '</section>';
        }).join('');
        investigationViewer.setBundle(record.bundle);
        investigationTracking.setRecord(record, !!state.aiBusy);
        elements.investigationReport.textContent = record.report;
        [elements.investigationModel,elements.investigationMarkdown,elements.investigationJson].forEach(function (button) { button.disabled = !!state.aiBusy; });
        elements.investigationModel.hidden = !state.data.model?.ready;
      }
      elements.investigationSummary.addEventListener('click', function (event) {
        var button = event.target.closest('[data-investigation-evidence]');
        if (button) investigationViewer.showEvidence(button.dataset.investigationEvent,button.dataset.investigationEvidence);
      });

      function investigateCandidate(useModel) {
        var candidate = aiCandidate(); if (!confirmedForInvestigation(candidate) || state.aiBusy) return;
        var caseId = state.currentId, candidateId = candidate.candidate_id, token = state.loadToken;
        var confirmation = confirmationKey(candidate), expectedHuman = humanConfirmation(candidate);
        state.investigationLookupSerial++; state.investigationLookupStatus = '';
        state.aiBusy = {caseId:caseId,candidateId:candidateId}; renderAi(); elements.aiMessage.textContent = useModel ? 'AI 正在整理排查建议…' : '正在整理事件证据…';
        request('/api/investigation/' + encodeURIComponent(caseId) + '/' + encodeURIComponent(candidateId), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input_fingerprint:state.aiCase.input_fingerprint,use_model:useModel,human_confirmation:expectedHuman})}).then(function (result) {
          if (state.currentId !== caseId || state.loadToken !== token || state.aiCandidateId !== candidateId || confirmationKey(aiCandidate()) !== confirmation) return;
          candidate.investigation_available = true;
          if (state.aiCandidateId === candidateId) state.investigation = result.investigation;
          if (state.aiCandidateId === candidateId) {
            state.investigationLookupKey = JSON.stringify([caseId,candidateId,state.aiCase.input_fingerprint,confirmation]);
            state.investigationLookupStatus = result.investigation.status;
          }
          elements.aiMessage.textContent = result.investigation.status === 'stale' ? '数据已变化，请重新打开 Case 后分析。' : '排查报告已保存。';
        }).catch(function (error) {
          if (state.currentId === caseId && state.loadToken === token) { state.investigation = null; elements.aiMessage.textContent = error.message; }
        }).finally(function () {
          state.aiBusy = null; renderAi();
          if (state.currentId === caseId && state.loadToken === token && state.aiCandidateId === candidateId && state.view === 'investigation') document.querySelector('.visual').scrollTop = 0;
        });
      }
      elements.aiInvestigate.addEventListener('click', function () { setView('investigation'); });
      document.getElementById('generateInvestigation').addEventListener('click', function () {
        if (state.investigationLookupStatus === 'error') { state.investigationLookupKey=''; loadExistingInvestigation(); renderInvestigation(); }
        else investigateCandidate(false);
      });
      elements.investigationModel.addEventListener('click', function () { investigateCandidate(true); });
      function downloadInvestigation(format) {
        var record = state.investigation; if (!record || !record.report) return;
        var tracking = investigationTracking.getSaved();
        if (investigationTracking.dirty) elements.aiMessage.textContent = '报告仅包含已保存的排查记录；当前草稿仍保留。';
        var content = format === 'md' ? record.report + (tracking && tracking.report_appendix || '') : JSON.stringify({status:record.status,bundle:record.bundle,model_output:record.model_output,human_tracking:tracking},null,2);
        var url = URL.createObjectURL(new Blob([content],{type:format === 'md' ? 'text/markdown;charset=utf-8' : 'application/json'}));
        var link = document.createElement('a'); link.href = url; link.download = record.bundle.case_id + '_' + record.bundle.candidate_id + '_investigation.' + format; link.click(); setTimeout(function () { URL.revokeObjectURL(url); },1000);
      }
      elements.investigationMarkdown.addEventListener('click', function () { downloadInvestigation('md'); });
      elements.investigationJson.addEventListener('click', function () { downloadInvestigation('json'); });

      document.querySelectorAll('.review-tabs [data-view]').forEach(function (button) { button.addEventListener('click', function () { setView(button.getAttribute('data-view')); }); });
      elements.aiCandidates.addEventListener('click', function (event) {
        if (state.submittingHuman || state.completing) return;
        var button = event.target.closest('[data-ai-candidate]'); if (!button) return;
        resetInvestigation(); state.aiCandidateId = button.getAttribute('data-ai-candidate'); elements.aiMessage.textContent = ''; renderAi();
      });
      elements.aiLocate.addEventListener('click', function () {
        openHumanReview();
      });
      function openHumanReview(candidateId) {
        if (candidateId) { state.aiFilter = 'all'; elements.aiFilter.value = 'all'; state.aiCandidateId = candidateId; resetInvestigation(); renderAi(); }
        setView('annotation');
        document.querySelector('.form-scroll').scrollTop = 0;
        var c = aiCandidate(); if (!c || !state.assist) return;
        var windows = state.assist.windows.filter(function (w) { return w.start_time != null; });
        if (!windows.length) return;
        state.windowIndex = windows.reduce(function (best, w) { return Math.abs(w.start_time - c.representative_window.start_time) < Math.abs(best.start_time - c.representative_window.start_time) ? w : best; }).index;
        state.zoom = true; renderWindow();
      }
      function callAi(caseId, candidateId, fingerprint, token) {
        resetInvestigation();
        state.aiBusy = {caseId:caseId, candidateId:candidateId}; renderAi(); elements.aiMessage.textContent = '正在复核…';
        return request('/api/model-review/' + encodeURIComponent(caseId) + '/' + encodeURIComponent(candidateId), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({input_fingerprint:fingerprint})}).then(function (result) {
          if (state.currentId === caseId && state.loadToken === token) {
            acceptAiCase(result.case, caseId);
            elements.aiMessage.textContent = '结果已保存；人工标注未改动。';
          }
          return result.case;
        }).finally(function () { state.aiBusy = null; renderAi(); });
      }
      elements.aiRun.addEventListener('click', function () {
        var c = aiCandidate(); if (!c || state.aiBusy) return;
        var caseId = state.currentId, token = state.loadToken;
        callAi(caseId, c.candidate_id, state.aiCase.input_fingerprint, token).catch(function (error) {
          if (state.currentId === caseId && state.loadToken === token) elements.aiMessage.textContent = error.message;
        });
      });
      elements.aiFilter.addEventListener('change', function () { if (state.submittingHuman) return; state.aiFilter = this.value; state.aiCandidateId = ''; renderAi(); });
      elements.aiResolve.addEventListener('click', async function () {
        var candidate = aiCandidate(); if (!candidate || state.aiBusy || state.submittingHuman || !humanSubmitReady()) return;
        var caseId = state.currentId, token = state.loadToken;
        backupHumanChoice();
        var key = humanResolutionKey, submitted = readHumanDraft(key), choice = humanChoice();
        var fingerprint = state.aiCase.input_fingerprint, expectedRecord = humanResolutionBase, annotationSaved = false;
        clearTimeout(state.saveTimer); state.submittingHuman = true; state.completing = true;
        state.aiBusy = {caseId:caseId,candidateId:candidate.candidate_id}; editability(); renderAi();
        try {
          if (state.savePromise) await state.savePromise;
          var fresh = await request('/api/candidates/'+encodeURIComponent(caseId));
          var current = fresh.candidates.find(function (c) { return c.candidate_id===candidate.candidate_id; });
          if (fresh.input_fingerprint!==fingerprint || !humanEligible(current)) throw Error('候选或证据已变化，请重新核查');
          if (current.human_resolution.record_fingerprint!==expectedRecord) {
            humanResolutionConflict = true; throw Error('其他页面已更新候选结论，请先读取最新结论');
          }
          if (state.dirty || !currentConfirmationMatches()) { await save('completed',false); annotationSaved = true; }
          fresh = await request('/api/candidates/'+encodeURIComponent(caseId));
          current = fresh.candidates.find(function (c) { return c.candidate_id===candidate.candidate_id; });
          if (fresh.input_fingerprint!==fingerprint || !current || !current.human_resolution) throw Error('当前标注或证据未通过校验，请重新核查');
          // Never adopt a different annotation revision that arrived between save and submit.
          if (!current.human_resolution.available || current.human_resolution.annotation_sha256!==state.annotation._revision) {
            state.annotationConflict = true; document.getElementById('annotationReload').hidden = false;
            throw Error('人工标注已变化或未通过校验，请先读取最新标注');
          }
          var body = {input_fingerprint:fingerprint,human_decision:choice.decision,selected_event_ids:choice.event_ids,
            annotation_sha256:current.human_resolution.annotation_sha256,expected_record_fingerprint:expectedRecord};
          var result = await request('/api/resolve-candidate/'+encodeURIComponent(caseId)+'/'+encodeURIComponent(candidate.candidate_id),
            {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
          if (submitted) forgetHumanDraft(key, submitted);
          if (state.currentId === caseId && state.loadToken === token) {
            humanResolutionDirty = false; humanResolutionConflict = false; humanResolutionRestore = false; humanStorageFailed = false;
            resetInvestigation(); state.aiFilter = 'all'; elements.aiFilter.value = 'all';
            acceptAiCase(result.case, caseId);
            humanResolutionBase = result.case.candidates.find(function (c) { return c.candidate_id===candidate.candidate_id; }).human_resolution.record_fingerprint;
            humanResolutionMessage = ''; state.submittedHumanKey = caseId+'/'+candidate.candidate_id;
            document.querySelector('#reviewFollowup span').textContent = '';
            document.querySelector('.form-scroll').scrollTop = 0;
            setSaveState('复核已提交','good');
          }
        } catch (error) {
          if (state.currentId === caseId && state.loadToken === token && state.aiCandidateId === candidate.candidate_id) {
            if (error.code === 'human_confirmation_conflict') humanResolutionConflict = true;
            humanResolutionMessage = (annotationSaved ? '标注已保存，候选结论提交未确认。' : '复核提交未确认。') + error.message;
            fillHumanChoice(choice); humanResolutionDirty = true; backupHumanChoice();
          }
        } finally {
          state.aiBusy = null; state.submittingHuman = false; state.completing = false; editability(); renderAi();
        }
      });
      elements.aiResultExport.addEventListener('click', function () {
        if (!state.aiCase) return;
        var data = {case_id:state.currentId, input_fingerprint:state.aiCase.input_fingerprint, purpose:'候选自动复核结果；不是人工真值或整段 Case 阴性标签', summary:state.aiCase.triage_summary, candidates:state.aiCase.candidates.map(function (c) { return {candidate_id:c.candidate_id, start_time:c.start_time, end_time:c.end_time, disposition:c.disposition, review:c.review}; })};
        var url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json'})), link = document.createElement('a'); link.href = url; link.download = state.currentId + '_ai_results.json'; link.click(); setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      });
      document.getElementById('nextHuman').addEventListener('click', async function () {
        if (state.dirty || humanResolutionDirty || state.submittingHuman || state.loading) return;
        var button = this, caseId = state.currentId, candidateId = state.aiCandidateId, token = state.loadToken;
        function stillHere() { return state.currentId===caseId && state.loadToken===token && !state.dirty && !humanResolutionDirty; }
        function needsHuman(c) { return c.disposition.status==='needs_human' || c.disposition.status==='human_reviewed' && !c.disposition.human_decision; }
        button.disabled = true;
        try {
          var current = await request('/api/candidates/'+encodeURIComponent(caseId));
          if (!stillHere()) return;
          acceptAiCase(current,caseId);
          var next = current.candidates.find(function (c) { return c.candidate_id!==candidateId && needsHuman(c); });
          if (next) { openHumanReview(next.candidate_id); return; }
          var queue = await request('/api/review-queue');
          if (!stillHere()) return;
          var cases = queue.cases.filter(function (c) { return c.case_id!==caseId && c.counts && c.counts.needs_human; }).sort(workflow.compare);
          for (var i=0;i<cases.length;i++) {
            var result = await request('/api/candidates/'+encodeURIComponent(cases[i].case_id));
            if (!stillHere()) return;
            next = result.candidates.find(needsHuman);
            if (!next) continue;
            var moved = await selectCase(cases[i].case_id,next.candidate_id);
            if (moved) openHumanReview(next.candidate_id);
            return;
          }
          document.querySelector('#reviewFollowup span').textContent = '暂无其他待人工候选';
        } catch (error) {
          if (stillHere()) humanResolutionMessage = '读取下一项失败：'+error.message;
        } finally { button.disabled = false; if (aiCandidate()) renderHumanResolution(aiCandidate()); }
      });

      var annotationConflictDraft = null;
      document.getElementById('annotationReload').addEventListener('click', async function () {
        if (state.loading || state.submittingHuman || state.saving) return;
        var caseId = state.currentId, token = state.loadToken;
        annotationConflictDraft = {case_id:caseId, body:payload('pending')};
        state.completing = true; editability();
        try {
          var results = await Promise.all([request('/api/annotation/'+encodeURIComponent(caseId)),request('/api/assist/'+encodeURIComponent(caseId)),request('/api/candidates/'+encodeURIComponent(caseId))]);
          if (state.currentId!==caseId || state.loadToken!==token) return;
          state.annotation = results[0]; state.assist = results[1]; acceptAiCase(results[2],caseId);
          state.loading = true; renderForm(); state.loading = false; state.dirty = false; state.annotationConflict = false;
          document.getElementById('annotationReload').hidden = true; document.getElementById('annotationRestore').hidden = false;
          setSaveState('已读取最新标注；可恢复本页草稿后核对',''); renderAssist(); renderAi();
        } catch (error) { setSaveState(error.message,'error'); }
        finally { state.completing = false; editability(); }
      });
      document.getElementById('annotationRestore').addEventListener('click', function () {
        if (!annotationConflictDraft || annotationConflictDraft.case_id!==state.currentId || state.completing) return;
        state.loading = true; renderForm(annotationConflictDraft.body); state.loading = false;
        this.hidden = true; scheduleDraft(); setSaveState('本页草稿已恢复，请核对后提交','');
      });
      function currentCase() {
        return state.data.cases.find(function (item) { return item.case_id === state.currentId; });
      }

      function taskOf(item) {
        return Object.assign({case_id:item.case_id, status:'loading'}, item.workflow || {});
      }

      function updateTask(caseId, task) {
        var item = state.data.cases.find(function (entry) { return entry.case_id === caseId; });
        if (!item) return;
        taskVersions[caseId] = (taskVersions[caseId] || 0) + 1;
        var changed = JSON.stringify(item.workflow) !== JSON.stringify(task);
        item.workflow = task;
        if (changed) { renderList(); if (caseId === state.currentId) renderVisual(); }
      }

      function acceptAiCase(result, caseId) {
        updateTask(caseId, result.workflow);
        if (state.currentId === caseId) state.aiCase = result;
      }

      async function refreshTasks() {
        if (refreshingTasks) return;
        refreshingTasks = true;
        var button = document.getElementById('refreshTasks'), notice = document.getElementById('taskNotice');
        var versions = Object.assign({}, taskVersions);
        button.disabled = true; button.setAttribute('aria-busy','true'); notice.hidden = true;
        try {
          var result = await request('/api/review-queue');
          result.cases.forEach(function (task) {
            // A slow full scan must not replace a newer live review/save response.
            if ((taskVersions[task.case_id] || 0) !== (versions[task.case_id] || 0)) return;
            var item = state.data.cases.find(function (entry) { return entry.case_id === task.case_id; });
            if (item) item.workflow = task;
          });
          renderList(); renderVisual();
        } catch (error) {
          state.data.cases.forEach(function (item) { if (!item.workflow) item.workflow = {status:'unavailable'}; });
          renderList(); renderVisual();
          notice.textContent = '状态刷新失败，保留已读取结果。点击 ↻ 重试。'; notice.hidden = false;
        } finally {
          refreshingTasks = false; button.disabled = false; button.removeAttribute('aria-busy');
        }
      }

      function visibleCases() {
        var query = state.search.toLowerCase();
        return state.data.cases.filter(function (item) {
          var matchesSearch = !query ||
            item.case_id.toLowerCase().indexOf(query) >= 0 ||
            item.source_route.toLowerCase().indexOf(query) >= 0;
          if (!matchesSearch) return false;
          return workflow.matches(taskOf(item), state.filter);
        }).sort(function (a, b) {
          return workflow.compare(taskOf(a), taskOf(b));
        });
      }

      function renderList() {
        var cases = visibleCases();
        elements.visibleCount.textContent = "（" + cases.length + "）";
        if (!cases.length) {
          elements.caseList.innerHTML = '<div class="empty">没有符合筛选条件的 Case</div>';
          return;
        }
        var markup = cases.map(function (item, index) {
          var task = taskOf(item), counts = task.counts || {};
          var status = '<span class="status-pill ' + (workflow.tones[task.status] || '') + '">' + escapeHtml(workflow.labels[task.status]) + '</span>';
          var detail = counts.needs_human && counts.pending_ai ? counts.needs_human + ' 待人工 · ' + counts.pending_ai + ' 待 AI' : task.candidate_count ? task.candidate_count + ' 个候选' : '';
          return '<button class="case-item ' + (item.case_id === state.currentId ? "active" : "") +
            '" data-case-id="' + escapeHtml(item.case_id) + '" data-task-status="' + task.status + '" aria-current="' + (item.case_id === state.currentId) + '">' +
            '<div class="case-head"><span class="case-number">' + (index + 1) + '</span>' +
            '<span class="case-name" title="' + escapeHtml(item.case_id) + '">' +
            escapeHtml(state.data.automatic_review ? shortCaseName(item.case_id) : item.case_id) + '</span></div>' +
            '<div class="case-meta">' + status + (detail ? '<span class="task-counts">' + detail + '</span>' : '') + '</div></button>';
        }).join("");
        if (elements.caseList.innerHTML !== markup) {
          var scrollTop = elements.caseList.scrollTop;
          var active = document.activeElement, focusedId = active && active.getAttribute('data-case-id');
          elements.caseList.innerHTML = markup;
          if (focusedId) Array.from(elements.caseList.querySelectorAll('[data-case-id]')).some(function (button) {
            if (button.dataset.caseId !== focusedId) return false;
            button.focus({preventScroll:true}); return true;
          });
          elements.caseList.scrollTop = scrollTop;
        }
      }

      function renderVisual() {
        var item = currentCase();
        if (!item) return;
        var task = taskOf(item);
        elements.caseOverview.innerHTML =
          '<div class="case-title-row"><h2 title="' + escapeHtml(item.case_id) + '">' + escapeHtml(state.data.automatic_review ? shortCaseName(item.case_id) : item.case_id) + '</h2>' +
          '<span class="status-pill ' + (workflow.tones[task.status] || '') + '">' + escapeHtml(workflow.labels[task.status]) + '</span></div>' +
          '<div class="subtle">路线 ' + escapeHtml(item.source_route || "—") +
          ' · 片段 ' + escapeHtml(item.source_segment || "—") + '</div>';
      }

      function numberText(value, digits) {
        return value == null ? "—" : Number(value).toFixed(digits == null ? 2 : digits);
      }

      function shortCaseName(id) {
        return id.replace(/^comma2k19_[a-f0-9]+_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(.+)$/, '$2-$3 $4:$5:$6 · 片段 $7');
      }

      function drawChart() {
        var a = state.assist;
        var canvas = elements.speedChart;
        var c = canvas.getContext("2d");
        c.clearRect(0, 0, canvas.width, canvas.height);
        drawAcceleration(elements.accelerationChart,[],null,[]);
        elements.accelerationNote.textContent = accelerationNote(state.aiCase && state.aiCase.acceleration_provenance);
        if (!a) return;
        var points = a.points.filter(function (p) { return p.timestamp != null && p.speed_mps != null; });
        if (points.length < 2) return;
        var w = a.windows[state.windowIndex];
        var lo = points[0].timestamp, hi = points[points.length - 1].timestamp;
        if (state.zoom && w && w.start_time != null) {
          lo = Math.max(lo, w.start_time - 2); hi = Math.min(hi, w.end_time + 2);
        }
        state.chartRange = [lo, hi];
        canvas.dataset.timeRange = JSON.stringify(state.chartRange);
        var accelerationBands = a.reference_events.map(function (e) { return {start:e.start_time,end:e.end_time,color:'#d5efdf'}; })
          .concat(a.quality.original_can.gaps.map(function (g) { return {start:g.start_time,end:g.end_time,color:'#f6c8ce'}; }));
        if (w) accelerationBands.push({start:w.start_time,end:w.end_time,color:'rgba(238,184,44,.35)'});
        drawAcceleration(elements.accelerationChart,a.points,state.chartRange,accelerationBands);
        var shown = points.filter(function (p) { return p.timestamp >= lo && p.timestamp <= hi; });
        if (!shown.length) return;
        var low = Math.min.apply(null, shown.map(function (p) { return p.speed_mps; }));
        var high = Math.max.apply(null, shown.map(function (p) { return p.speed_mps; }));
        var pad = Math.max(0.2, (high - low) * 0.1); low = Math.max(0, low - pad); high += pad;
        var left = 62, right = 782, top = 18, bottom = 187;
        function x(t) { return left + (t - lo) / Math.max(0.001, hi - lo) * (right - left); }
        function y(v) { return bottom - (v - low) / Math.max(0.01, high - low) * (bottom - top); }
        function band(start, end, color) {
          if (start == null || end == null || end < lo || start > hi) return;
          c.fillStyle = color; c.fillRect(x(Math.max(start, lo)), top, Math.max(2, x(Math.min(end, hi)) - x(Math.max(start, lo))), bottom - top);
        }
        a.reference_events.forEach(function (e) { band(e.start_time, e.end_time, "#d5efdf"); });
        a.quality.original_can.gaps.forEach(function (g) { band(g.start_time, g.end_time, "#f6c8ce"); });
        if (w) band(w.start_time, w.end_time, "rgba(238,184,44,.35)");
        c.font = "13px Microsoft YaHei, sans-serif";
        c.fillStyle = "#64727a"; c.fillText("车速 m/s", left, 13);
        for (var j = 0; j <= 4; j++) {
          var v = low + (high - low) * j / 4;
          c.strokeStyle = "#d9e1e5"; c.beginPath(); c.moveTo(left, y(v)); c.lineTo(right, y(v)); c.stroke();
          c.fillStyle = "#64727a"; c.fillText(v.toFixed(1), 9, y(v) + 4);
          var at = lo + (hi - lo) * j / 4;
          c.fillText(at.toFixed(1) + "s", Math.min(right - 40, x(at)), 211);
        }
        c.strokeStyle = "#0068eb"; c.lineWidth = 2; c.beginPath();
        shown.forEach(function (p, i) { if (i === 0 || p.timestamp - shown[i - 1].timestamp > .15) c.moveTo(x(p.timestamp), y(p.speed_mps)); else c.lineTo(x(p.timestamp), y(p.speed_mps)); });
        c.stroke();
      }

      function renderWindow() {
        var a = state.assist;
        if (!a || !a.windows.length) { elements.windowDetail.textContent = "没有完整的半秒窗口"; drawChart(); return; }
        state.windowIndex = Math.max(0, Math.min(a.windows.length - 1, state.windowIndex));
        var w = a.windows[state.windowIndex];
        elements.windowSlider.value = state.windowIndex;
        var checks = [
          [w.checks.speed_drop, "半秒速度下降 " + numberText(w.speed_drop_mps, 3) + " m/s（要求 ≥1.0）"],
          [w.checks.sustained, "持续下降 " + w.decreasing_intervals + "/5 段（要求 ≥4）"],
          [w.checks.recovery, "累计回升 " + numberText(w.cumulative_recovery_mps, 3) + " m/s（要求 ≤0.10）"]
        ];
        elements.windowDetail.innerHTML = '<strong>窗口 ' + (w.index + 1) + '/' + a.windows.length + '：' +
          numberText(w.start_time, 1) + '～' + numberText(w.end_time, 1) + ' 秒 · ' + (w.qualifies ? "数值达标" : "未达标 / 数据不足") + '</strong>' +
          '<div class="check-grid">' + checks.map(function (p) { return '<span class="check ' + (p[0] ? 'pass' : '') + '">' + (p[0] ? '✓ ' : '× ') + p[1] + '</span>'; }).join('') + '</div>' +
          '<table class="window-table"><thead><tr><th>时间（秒）</th><th>车速 m/s（km/h）</th><th>加速度（辅助）</th><th>CSV 行号</th></tr></thead><tbody>' +
          a.points.slice(w.index, w.index + 6).map(function (p) { return '<tr><td>' + numberText(p.timestamp, 1) + '</td><td>' + numberText(p.speed_mps, 3) + '（' + numberText(p.speed_mps == null ? null : p.speed_mps * 3.6, 1) + '）</td><td>' + numberText(p.acceleration_mps2, 3) + '</td><td>' + p.source_row + '</td></tr>'; }).join('') + '</tbody></table>';
        drawChart();
      }

      function completionState() {
        var uncertain = labelValue() === "uncertain";
        var quality = state.assist && state.assist.quality;
        var ready = quality && quality.can_complete && elements.reviewConfirm.checked &&
          (!quality.boundary_review_required || elements.boundaryConfirm.checked);
        elements.completeButton.disabled = state.loading || state.saving || state.completing || state.navigating || (!uncertain && !ready);
        elements.completionHint.textContent = uncertain ? "不确定项保存为草稿，保留待核查状态。" :
          !quality ? "等待参考计算加载。" : !quality.can_complete ? "数据质量尚未确认，可保存草稿或选择不确定。" :
          !elements.reviewConfirm.checked ? "核对整段数据后，勾选人工确认。" :
          quality.boundary_review_required && !elements.boundaryConfirm.checked ? "首尾存在减速，请核对相邻片段；拿不准可选择不确定。" : "已核对，可保存标注并确认候选。";
        if (aiCandidate()) renderHumanResolution(aiCandidate());
      }

      function currentConfirmationMatches() {
        var a = state.assist, saved = state.annotation;
        if (!a || !saved || saved.annotation_status !== 'completed' || saved.annotation_protocol_id !== a.protocol.protocol_id) return false;
        var c = saved.review_confirmation || {}, source = a.quality.original_can;
        return c.protocol_id === a.protocol.protocol_id && c.whole_case_reviewed === true && a.quality.can_complete &&
          c.protocol_sha256 === a.protocol_sha256 && c.vehicle_sha256 === a.vehicle_sha256 &&
          c.metadata_sha256 === source.metadata_sha256 && c.original_can_sha256 === source.original_can_sha256 &&
          (!a.quality.boundary_review_required || c.boundary_reviewed === true);
      }

      function renderProtocolNotice() {
        var id = state.assist ? state.assist.protocol.protocol_id : state.data.annotation_protocol_id;
        var saved = state.annotation;
        elements.protocolNotice.textContent = '人工标注 · ' + (id || 'SD-WINDOW-1.1');
        if (saved && saved.annotation_protocol_id && saved.annotation_protocol_id !== id) {
          elements.protocolNotice.textContent += ' · 已存 ' + saved.annotation_protocol_id + '，重新确认后升级';
        }
      }

      function renderAssist() {
        var a = state.assist;
        if (!a) return;
        renderProtocolNotice();
        elements.assistSummary.textContent = a.protocol.protocol_id + ' · 已检查 ' + a.summary.window_count + ' 个窗口 · 数值达标 ' + a.summary.qualifying_window_count + ' 个 · 合并为 ' + a.summary.reference_event_count + ' 段参考区间';
        var q = a.quality;
        elements.qualityStatus.className = 'assist-status' + (!q.can_complete || q.boundary_review_required ? ' warn' : '');
        elements.qualityStatus.textContent = q.can_complete ? (q.original_can.source_type === 'synthetic' ? '合成数据 · 文件与采样检查通过。' : '原始 CAN 连续性检查通过，最大间隔 ' + numberText(q.original_can.maximum_gap_seconds, 3) + ' 秒。') : q.issues.join('；');
        if (q.boundary_review_required) elements.qualityStatus.textContent += ' 首尾存在持续减速，完成前需核对相邻片段；无法核对可标为不确定。';
        elements.boundaryCheck.hidden = !q.boundary_review_required;
        elements.windowSlider.max = Math.max(0, a.windows.length - 1);
        elements.windowSlider.disabled = !a.windows.length;
        elements.referenceEvents.innerHTML = a.reference_events.length ? a.reference_events.map(function (e, i) {
          return '<div class="reference"><strong>参考区间 ' + (i + 1) + '：' + numberText(e.start_time, 1) + '～' + numberText(e.end_time, 1) + ' 秒</strong>' +
            '<div class="subtle">合并 ' + e.window_count + ' 个合格窗口。核对后可加入草稿。</div><div class="assist-tools">' +
            '<button class="button" data-view-reference="' + i + '">查看数值</button>' +
            '<button class="button" data-add-reference="' + i + '">加入标注草稿</button></div></div>';
        }).join('') : '<div class="reference">本版数值规则未找到参考区间。查看整段曲线与质量提示后，再确认是否无事件。</div>';
        elements.negativeEvidence.hidden = a.reference_events.length > 0 || !q.can_complete;
        renderWindow(); completionState();
      }

      function labelValue() {
        var selected = document.querySelector('input[name="caseLabel"]:checked');
        return selected ? selected.value : "uncertain";
      }

      function setLabel(value) {
        var target = document.querySelector('input[name="caseLabel"][value="' + value + '"]');
        if (target) target.checked = true;
      }

      function eventValue(row) {
        return {
          event_id: row.querySelector('[data-field="event_id"]').value.trim(),
          start_time: Number(row.querySelector('[data-field="start_time"]').value),
          end_time: Number(row.querySelector('[data-field="end_time"]').value),
          event_type: "sustained_deceleration",
          notes: row.querySelector('[data-field="notes"]').value.trim()
        };
      }

      function formEvents() {
        return Array.prototype.map.call(
          elements.eventList.querySelectorAll(".event-row"),
          eventValue
        );
      }

      function addEvent(event) {
        event = event || {};
        var count = elements.eventList.querySelectorAll(".event-row").length + 1;
        var ids = formEvents().map(function (e) { return e.event_id; });
        while (ids.indexOf("GT" + String(count).padStart(3, "0")) >= 0) count += 1;
        var wrapper = document.createElement("div");
        wrapper.className = "event-row";
        wrapper.innerHTML =
          '<div class="event-fields">' +
          '<div><small>编号</small><input data-field="event_id" value="' +
          escapeHtml(event.event_id || ("GT" + String(count).padStart(3, "0"))) + '"></div>' +
          '<div><small>开始时间（秒）</small><input type="number" step="0.01" data-field="start_time" value="' +
          escapeHtml(event.start_time == null ? "" : event.start_time) + '"></div>' +
          '<div><small>结束时间（秒）</small><input type="number" step="0.01" data-field="end_time" value="' +
          escapeHtml(event.end_time == null ? "" : event.end_time) + '"></div></div>' +
          '<small>判断依据</small><input data-field="notes" value="' +
          escapeHtml(event.notes || "") + '" placeholder="人工观察到的信号或场景">' +
          '<div class="event-actions"><button class="button danger" data-remove-event>删除</button></div>';
        elements.eventList.appendChild(wrapper);
        scheduleDraft();
      }

      function renderForm(draft) {
        var annotation = draft || state.annotation;
        if (!annotation) return;
        renderProtocolNotice();
        clearTimeout(state.saveTimer);
        setLabel(annotation.case_label || (
          annotation.annotation_status === "completed"
            ? (annotation.events.length ? "positive" : "negative")
            : "uncertain"
        ));
        elements.annotatorInput.value = annotation.annotator ||
          localStorage.getItem("caseAnalyzerAnnotator") || "";
        elements.notesInput.value = annotation.notes || "";
        elements.reviewConfirm.checked = false;
        elements.boundaryConfirm.checked = false;
        elements.eventList.innerHTML = "";
        (annotation.events || []).forEach(addEvent);
        clearTimeout(state.saveTimer);
        updateFormMode();
        var currentCompleted = currentConfirmationMatches();
        setSaveState(
          annotation.annotation_status === "completed" ? (currentCompleted ? "此 Case 已完成，可继续修改" : "已存标注需按当前口径重新确认") : "草稿已加载",
          currentCompleted ? "good" : ""
        );
      }

      function updateFormMode() {
        var label = labelValue();
        var hasEvents = elements.eventList.children.length > 0;
        elements.addEventButton.disabled = label === "negative";
        elements.eventHint.textContent = label === "positive"
          ? "“有事件”至少需要一个有效时间区间。"
          : label === "negative"
            ? "“无事件”完成时必须没有人工事件。"
            : "不确定项只能保存为草稿，不进入指标计算。";
        if (label === "negative" && hasEvents) {
          setSaveState("选择“无事件”前请删除已有人工事件", "error");
        }
        elements.completeButton.textContent =
          label === "uncertain" ? "保存不确定" : "完成标注";
        completionState();
      }

      function setSaveState(message, kind) {
        elements.saveState.textContent = message;
        elements.saveState.className = "save-state" + (kind ? " " + kind : "");
      }

      function payload(status) {
        var annotator = elements.annotatorInput.value.trim();
        if (annotator) localStorage.setItem("caseAnalyzerAnnotator", annotator);
        return {
          _expected_revision: state.annotation && state.annotation._revision,
          annotation_status: status,
          annotator: annotator,
          annotated_at: status === "completed" ? new Date().toISOString() : "",
          case_label: labelValue(),
          events: formEvents(),
          notes: elements.notesInput.value.trim(),
          review_confirmation: status === "completed" && state.assist ? {
            protocol_id: state.assist.protocol.protocol_id,
            whole_case_reviewed: elements.reviewConfirm.checked, boundary_reviewed: elements.boundaryConfirm.checked,
            vehicle_sha256: state.assist.vehicle_sha256, protocol_sha256: state.assist.protocol_sha256,
            metadata_sha256: state.assist.quality.original_can.metadata_sha256,
            original_can_sha256: state.assist.quality.original_can.original_can_sha256
          } : null
        };
      }

      function updateLocalCase(annotation, summary) {
        var item = currentCase();
        item.annotation_status = annotation.annotation_status;
        item.completed = annotation.annotation_status === "completed";
        item.annotation_protocol_id = annotation.annotation_protocol_id;
        item.requires_protocol_review = !!annotation.annotation_protocol_id && annotation.annotation_protocol_id !== state.data.annotation_protocol_id;
        item.case_label = annotation.case_label || "uncertain";
        item.annotated_event_count = annotation.events.length;
        item.annotator = annotation.annotator;
        state.annotation = annotation;
        renderProtocolNotice();
        if (state.aiCase) {
          state.aiCase.candidates.forEach(function (candidate) {
            if (candidate.disposition.status === 'human_reviewed') {
              candidate.disposition = {status:'needs_human', reason:'人工标注已更新，请重新确认处理。', confirmed_intervals:[]};
              state.aiCase.triage_summary.human_reviewed--; state.aiCase.triage_summary.needs_human++;
            }
          });
        }
        state.data.summary = summary;
        updateTask(state.currentId, {status:'loading'});
      }

      function editing() {
        var active = document.activeElement;
        return state.composing || !!(active && active.closest('.form-panel') &&
          active.matches('textarea, input:not([type=radio]):not([type=checkbox])'));
      }

      function editability() {
        var blocked = state.loading || state.navigating || state.completing;
        document.querySelectorAll('.form-panel input, .form-panel textarea, .event-row button').forEach(function (el) { el.disabled = blocked; });
        elements.saveButton.disabled = blocked || state.saving;
        elements.addEventButton.disabled = blocked || labelValue() === 'negative';
        completionState();
      }

      function queueDraft() {
        clearTimeout(state.saveTimer);
        if (!state.dirty || state.loading || state.saving || state.navigating || state.completing || state.annotationConflict || editing()) return;
        state.saveTimer = setTimeout(function () {
          if (!editing() && state.dirty && !state.navigating && !state.completing) save('pending', false).catch(function () {});
        }, 3000);
      }

      async function save(status, moveNext) {
        clearTimeout(state.saveTimer);
        if (state.savePromise) {
          await state.savePromise;
          if (status === 'pending' && !state.dirty && !moveNext) return;
        }
        if (!state.currentId || state.loading) throw Error('当前无法保存');
        if (state.composing) throw Error('请先完成当前输入');
        if (moveNext) state.completing = true;
        var savedRevision = state.revision;
        var body = payload(status);
        state.saving = true;
        editability();
        setSaveState(status === 'completed' ? '正在完成…' : '正在保存…', '');
        state.savePromise = (async function () {
          try {
            var response = await request('/api/annotation/' + encodeURIComponent(state.currentId), {
              method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
            });
            updateLocalCase(response.annotation, response.summary);
            state.dirty = savedRevision !== state.revision;
            setSaveState(state.dirty ? '有未保存修改…' : status === 'completed' ? '已完成并写入本地 JSON' : '草稿已保存', state.dirty ? '' : 'good');
          } catch (error) {
            if (error.code === 'annotation_conflict') {
              state.annotationConflict = true; document.getElementById('annotationReload').hidden = false;
            }
            setSaveState(error.message, 'error');
            throw error;
          } finally {
            state.saving = false;
            state.savePromise = null;
            editability();
            if (state.aiCase) renderAi();
            queueDraft();
          }
        })();
        try {
          await state.savePromise;
          if (moveNext && status === 'completed') {
            var savedCaseId=state.currentId, savedToken=state.loadToken;
            var refreshed=await request('/api/candidates/'+encodeURIComponent(savedCaseId));
            if (state.currentId===savedCaseId && state.loadToken===savedToken) {
              acceptAiCase(refreshed, savedCaseId); renderAi();
            }
          }
        } finally {
          if (moveNext) { state.completing = false; editability(); queueDraft(); }
        }
      }

      function scheduleDraft() {
        if (!state.annotation || state.loading) return;
        state.dirty = true; state.revision += 1;
        elements.reviewConfirm.checked = false; elements.boundaryConfirm.checked = false;
        completionState();
        state.submittedHumanKey = '';
        if (aiCandidate()) renderHumanResolution(aiCandidate());
        setSaveState("有未保存修改…", "");
        queueDraft();
      }

      async function selectCase(caseId, candidateId) {
        if (state.loading || state.navigating || state.completing) return false;
        state.navigating = true; editability(); clearTimeout(state.saveTimer);
        try {
          if (state.savePromise) await state.savePromise;
          if (state.dirty) await save('pending', false);
          await loadCase(caseId, candidateId); return true;
        } catch (error) { setSaveState(error.message, 'error'); }
        finally { state.navigating = false; editability(); queueDraft(); }
      }

      function loadCase(caseId, candidateId) {
        clearTimeout(state.saveTimer);
        state.composing = false;
        clearTimeout(automaticTimer); automaticState = null;
        var token = ++state.loadToken;
        state.currentId = caseId;
        state.annotationConflict = false; state.submittedHumanKey = '';
        document.getElementById('annotationReload').hidden = true;
        document.getElementById('annotationRestore').hidden = true;

        state.aiCase = null; state.aiCandidateId = ''; resetInvestigation(); elements.aiMessage.textContent = ''; renderAi();
        var candidateLoad = request('/api/candidates/' + encodeURIComponent(caseId)).then(function (result) {
          if (state.currentId !== caseId || state.loadToken !== token) return;
          acceptAiCase(result, caseId);
          var selection = new URLSearchParams(window.location.search);
          var requestedCandidate = candidateId || (selection.get('case') === caseId ? selection.get('candidate') : null);
          if (requestedCandidate && result.candidates.some(function (candidate) { return candidate.candidate_id === requestedCandidate; })) state.aiCandidateId = requestedCandidate;
          renderAi();
          if (state.data.automatic_review) {
            automaticState = result.automatic_review;
            startAutomatic(caseId, token, false);
            pollAutomatic(caseId, token);
          }
        }).catch(function (error) {
          if (state.currentId === caseId && state.loadToken === token) {
            elements.aiSummary.textContent = 'AI 候选加载失败：' + error.message;
            updateTask(caseId, {status:'read_error',error:error.message});
          }
        });
        state.annotation = null;
        state.assist = null; state.windowIndex = 0; state.zoom = false; state.dirty = false;
        state.loading = true;
        elements.saveButton.disabled = true;
        elements.completeButton.disabled = true;
        renderList();
        var selected = elements.caseList.querySelector('.case-item.active');
        if (selected) elements.caseList.scrollTop += selected.getBoundingClientRect().top - elements.caseList.getBoundingClientRect().top - 36;
        renderVisual();
        elements.eventList.innerHTML = "";
        elements.windowDetail.textContent = "正在计算…";
        elements.referenceEvents.textContent = ""; elements.qualityStatus.textContent = "";
        elements.assistSummary.textContent = "正在检查整段记录与数据来源…";
        elements.negativeEvidence.hidden = true;
        elements.reviewConfirm.checked = false; elements.boundaryConfirm.checked = false;
        elements.boundaryCheck.hidden = true; drawChart();
        setSaveState("正在加载标注…", "");
        var annotationLoad = Promise.all([request("/api/annotation/" + encodeURIComponent(caseId)), request("/api/assist/" + encodeURIComponent(caseId))]).then(function (results) {
          if (state.currentId !== caseId || state.loadToken !== token) return;
          state.annotation = results[0]; state.assist = results[1];
          renderForm();
          state.dirty = false;
          var firstQualified = state.assist.windows.find(function (w) { return w.qualifies; });
          state.windowIndex = firstQualified ? firstQualified.index : 0;
          renderAssist();
          if (state.aiCase) renderAi();
        }).catch(function (error) {
          if (state.currentId === caseId && state.loadToken === token) setSaveState(error.message, "error");
        }).finally(function () {
          if (state.currentId !== caseId || state.loadToken !== token) return;
          state.loading = false;
          editability();
          if (state.aiCase) renderAi();
        });
        return Promise.all([candidateLoad, annotationLoad]);
      }

      elements.caseList.addEventListener("click", function (event) {
        var button = event.target.closest("[data-case-id]");
        if (button) selectCase(button.getAttribute("data-case-id"));
      });
      elements.eventList.addEventListener("click", function (event) {
        var button = event.target.closest("[data-remove-event]");
        if (!button) return;
        button.closest(".event-row").remove();
        scheduleDraft();
      });
      elements.eventList.addEventListener("input", scheduleDraft);
      elements.addEventButton.addEventListener("click", function () { addEvent(); });
      elements.notesInput.addEventListener("input", scheduleDraft);
      elements.annotatorInput.addEventListener("input", scheduleDraft);
      var formPanel = document.querySelector('.form-panel');
      formPanel.addEventListener('compositionstart', function () { state.composing = true; clearTimeout(state.saveTimer); });
      formPanel.addEventListener('compositionend', function () { state.composing = false; queueDraft(); });
      formPanel.addEventListener('focusin', function () { clearTimeout(state.saveTimer); });
      formPanel.addEventListener('focusout', function () { setTimeout(queueDraft, 0); });
      document.querySelectorAll('input[name="caseLabel"]').forEach(function (input) {
        input.addEventListener("change", function () {
          updateFormMode();
          scheduleDraft();
        });
      });
      elements.saveButton.addEventListener("click", function () {
        clearTimeout(state.saveTimer);
        save("pending", false).catch(function () {});
      });
      elements.completeButton.addEventListener("click", function () {
        clearTimeout(state.saveTimer);
        var status = labelValue() === "uncertain" ? "pending" : "completed";
        save(status, true).catch(function () {});
      });
      elements.searchInput.addEventListener("input", function () {
        state.search = elements.searchInput.value.trim();
        renderList();
      });
      elements.taskFilter.addEventListener("change", function () {
        state.filter = elements.taskFilter.value;
        renderList();
      });
      document.getElementById('refreshTasks').addEventListener('click', refreshTasks);
      window.addEventListener('focus', function () { if (state.data.cases.length) refreshTasks(); });
      elements.windowSlider.addEventListener("input", function () { state.windowIndex = Number(this.value); renderWindow(); });
      document.getElementById("previousWindow").addEventListener("click", function () { state.windowIndex -= 1; renderWindow(); });
      document.getElementById("nextWindow").addEventListener("click", function () { state.windowIndex += 1; renderWindow(); });
      document.getElementById("nextQualified").addEventListener("click", function () {
        if (!state.assist) return;
        var next = state.assist.windows.find(function (w) { return w.index > state.windowIndex && w.qualifies; }) || state.assist.windows.find(function (w) { return w.qualifies; });
        if (next) { state.windowIndex = next.index; renderWindow(); }
      });
      document.getElementById("wholeChart").addEventListener("click", function () { state.zoom = false; drawChart(); });
      document.getElementById("zoomChart").addEventListener("click", function () { state.zoom = true; drawChart(); });
      elements.speedChart.addEventListener("click", function (event) {
        if (!state.assist || !state.chartRange || !state.assist.windows.length) return;
        var box = this.getBoundingClientRect();
        var fraction = (event.clientX - box.left) / box.width;
        var t = state.chartRange[0] + Math.max(0, Math.min(1, (fraction * 800 - 62) / 720)) * (state.chartRange[1] - state.chartRange[0]);
        var valid = state.assist.windows.filter(function (w) { return w.start_time != null; });
        if (valid.length) state.windowIndex = valid.reduce(function (best, w) { return Math.abs(w.start_time - t) < Math.abs(best.start_time - t) ? w : best; }).index;
        renderWindow();
      });
      elements.referenceEvents.addEventListener("click", function (event) {
        var view = event.target.closest('[data-view-reference]');
        var add = event.target.closest('[data-add-reference]');
        if (!state.assist || (!view && !add) || state.loading) return;
        var ref = state.assist.reference_events[Number((view || add).getAttribute(view ? 'data-view-reference' : 'data-add-reference'))];
        state.windowIndex = ref.representative_window_index; state.zoom = true; renderWindow();
        if (add) {
          if (formEvents().some(function (e) { return e.start_time === ref.start_time && e.end_time === ref.end_time; })) { setSaveState('这个区间已经在草稿中，可直接修改。', ''); return; }
          addEvent(ref); setLabel('positive'); updateFormMode();
          if (!elements.notesInput.value.trim()) elements.notesInput.value = state.assist.protocol.protocol_id + '；已加入工具参考区间，人工判断依据见事件备注。';
          scheduleDraft();
        }
      });
      elements.negativeEvidence.addEventListener("click", function () {
        if (!state.assist || state.loading) return;
        if (formEvents().length) { setSaveState('草稿中还有事件，请先核对并删除。', 'error'); return; }
        elements.notesInput.value = state.assist.negative_notes; setLabel('negative'); updateFormMode(); scheduleDraft();
      });
      elements.reviewConfirm.addEventListener('change', completionState);
      elements.boundaryConfirm.addEventListener('change', completionState);
      window.addEventListener('beforeunload', function (event) { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });

      request("/api/data").then(async function (data) {
        state.data = data;
        investigationTracking.workspace = data.workspace;
        if (!data.automatic_review) {
          document.getElementById('automaticStatus').textContent = data.model?.ready ? '自动队列已关闭' : '人工复核模式 · AI 未配置';
          document.getElementById('automaticPause').hidden = true;
          document.getElementById('automaticRetry').hidden = true;
        }
        document.body.classList.add('auto-review');
        setView(window.location.hash === '#investigationPanel' ? 'investigation' : window.location.hash === '#assistPanel' ? 'annotation' : 'ai');
        var requestedId = new URLSearchParams(window.location.search).get('case');
        var requestedCase = data.cases.find(function (item) { return item.case_id === requestedId; });
        state.filter = 'all';
        renderList();
        var lastId = ''; try { lastId=window.CaseSystem.readStorage(sessionStorage,'case-analyzer-last-case',data.workspace)||''; } catch (_) {}
        var preferred = requestedCase || data.cases.find(function(item){return item.case_id===lastId;});
        // Deep links remain responsive while the other case states are scanned.
        if (preferred && !state.currentId) selectCase(preferred.case_id);
        await refreshTasks();
        var first = preferred || visibleCases()[0];
        if (first && !state.currentId) selectCase(first.case_id);
      }).catch(function (error) {
        elements.caseList.innerHTML = '<div class="empty">' + escapeHtml(error.message) + '</div>';
      });
    }());
