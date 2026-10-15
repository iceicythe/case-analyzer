(function () {
  'use strict';
  var STYLE = '.inv-tracking{margin:16px 0;padding:12px;border:1px solid #dce3e8;border-radius:8px}.inv-tracking summary{cursor:pointer;font-weight:600}.inv-tracking .it-row{padding:12px 0;border-bottom:1px solid #e6ebef}.inv-tracking .it-step{font-size:13px;margin:0 0 6px;line-height:1.6}.inv-tracking select,.inv-tracking textarea{box-sizing:border-box;max-width:100%;font:inherit;border:1px solid #ced7df;border-radius:5px;padding:6px}.inv-tracking select{font-size:12px;margin-bottom:6px}.inv-tracking textarea{display:block;width:100%;resize:vertical;font-size:13px;min-height:54px}.inv-tracking .it-actions{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.inv-tracking .it-message{font-size:12px;color:#64748b;overflow-wrap:anywhere}.inv-tracking .it-message.error{color:#b42318}.inv-tracking label{display:block;font-size:12px;color:#64748b;margin:8px 0 4px}';
  function escape(value) { return String(value).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function request(url, options) {
    return fetch(url, options).then(function (response) { return response.json().then(function (value) {
      if (!response.ok) { var error = new Error(value.error || '请求失败'); error.status = response.status; throw error; }
      return value;
    }); });
  }
  function Tracking(root, onSaved) {
    this.root = root; this.onSaved = onSaved; this.epoch = 0; this.revision = 0; this.dirty = false; this.drafts = {};
    if (!document.getElementById('investigation-tracking-style')) {
      var style = document.createElement('style'); style.id = 'investigation-tracking-style'; style.textContent = STYLE; document.head.appendChild(style);
    }
    var self = this;
    root.addEventListener('input', function () { self.changed(); });
    root.addEventListener('change', function () { self.changed(); });
    root.addEventListener('click', function (event) {
      if (event.target.closest('[data-it-save]')) self.save();
      else if (event.target.closest('[data-it-reload]')) self.reload();
      else if (event.target.closest('[data-it-restore]')) self.restore();
    });
    window.addEventListener('beforeunload', function (event) {
      self.backup(); if (self.dirty || self.saving) { event.preventDefault(); event.returnValue = ''; }
    });
    window.addEventListener('pagehide', function () { self.backup(); });
  }
  Tracking.prototype.storageKey = function () {
    var key='investigation-draft-v1:' + this.key;
    return this.workspace ? window.CaseSystem.storageKey(key,this.workspace) : key;
  };
  Tracking.prototype.readDraft = function () {
    var value = this.drafts[this.key];
    if (!value) { try { value = JSON.parse((this.workspace ? window.CaseSystem.readStorage(localStorage,'investigation-draft-v1:' + this.key,this.workspace) : localStorage.getItem(this.storageKey())) || 'null'); } catch (_) {} }
    if (!value || value.key !== this.key || !Array.isArray(value.changed_checks) || typeof value.notes !== 'string' || !value.entries || typeof value.entries !== 'object') return null;
    return value;
  };
  Tracking.prototype.capture = function () {
    var entries = clone(this.workingEntries || this.saved.entries || {});
    this.root.querySelectorAll('[data-it-check]').forEach(function (row) {
      entries[row.dataset.itCheck] = {status:row.querySelector('select').value,note:row.querySelector('textarea').value};
    });
    return {entries:entries, notes:this.root.querySelector('[data-it-notes]').value};
  };
  Tracking.prototype.backup = function () {
    clearTimeout(this.backupTimer);
    if (!this.key || !this.dirty || !this.root.querySelector('[data-it-notes]')) return;
    var value = this.capture(), base = this.saved.entries || {};
    value.key = this.key; value.base_fingerprint = this.saved.record_fingerprint;
    value.changed_checks = Object.keys(value.entries).filter(function (id) {
      return JSON.stringify(value.entries[id]) !== JSON.stringify(base[id] || {status:'pending',note:''});
    });
    value.notes_changed = value.notes !== (this.saved.notes || '');
    this.drafts[this.key] = value;
    try { localStorage.setItem(this.storageKey(), JSON.stringify(value)); } catch (_) { this.storageFailed = true; }
  };
  Tracking.prototype.changed = function () {
    if (!this.key) return;
    this.dirty = true; this.revision++; this.progress();
    this.message(this.saving ? '正在保存；新增修改留在草稿中。' : '未保存 · 点击保存写入项目。');
    var self = this; clearTimeout(this.backupTimer); this.backupTimer = setTimeout(function () { self.backup(); }, 350);
  };
  Tracking.prototype.message = function (text, error) {
    var target = this.root.querySelector('[data-it-message]'); if (!target) return;
    target.textContent = text; target.classList.toggle('error', !!error);
  };
  Tracking.prototype.progress = function () {
    if (!this.root.querySelector('[data-it-progress]')) return;
    var statuses = Array.from(this.root.querySelectorAll('[data-it-check] select')).map(function (select) { return select.value; });
    this.root.querySelector('[data-it-progress]').textContent = '排查记录 · 已核查 ' + statuses.filter(function (s) { return s === 'checked'; }).length + '/' + statuses.length + ' · 待数据 ' + statuses.filter(function (s) { return s === 'blocked'; }).length + (this.dirty ? ' · 未保存' : '');
    var disabled = this.disabled || this.saving;
    this.root.querySelectorAll('[data-it-save],[data-it-reload],[data-it-restore]').forEach(function (button) { button.disabled = !!disabled; });
  };
  Tracking.prototype.setRecord = function (record, disabled) {
    this.disabled = disabled;
    var key = JSON.stringify([record.bundle.case_id,record.bundle.candidate_id,record.bundle.input_fingerprint]);
    if (!record.tracking) {
      if (this.errorKey === key) return;
      this.clear(); this.errorKey = key; this.root.className = 'inv-tracking';
      this.root.innerHTML = '<details><summary>排查记录暂不可用</summary><p class="it-message error"></p></details>';
      this.root.querySelector('p').textContent = record.tracking_error || '排查记录无法读取，原文件未覆盖。';
      return;
    }
    this.errorKey = null;
    var checks = [];
    record.bundle.events.forEach(function (event) {
      (record.display_checks[event.event_id] || []).forEach(function (check) { checks.push({event:event,check:check}); });
    });
    var layoutKey = JSON.stringify(checks.map(function (item) { return [item.event.event_id,item.check.check_id,item.check.step]; }));
    if (key === this.key && layoutKey === this.layoutKey) { this.progress(); return; }
    var wasOpen = key === this.key && this.root.querySelector('details') && this.root.querySelector('details').open;
    this.backup(); this.epoch++; this.key = key; this.layoutKey = layoutKey; this.record = record;
    this.saved = clone(record.tracking); this.saving = false; this.dirty = false; this.revision++;
    this.root.className = 'inv-tracking';
    this.root.innerHTML = '<details' + (wasOpen ? ' open' : '') + '><summary data-it-progress></summary>' + checks.map(function (item) {
      return '<div class="it-row" data-it-check="' + escape(item.check.check_id) + '"><p class="it-step">' + item.event.confirmed_interval[0].toFixed(1) + '～' + item.event.confirmed_interval[1].toFixed(1) + ' 秒 · ' + escape(item.check.step) + '</p><select aria-label="' + escape(item.check.check_id) + ' 检查状态"><option value="pending">待核查</option><option value="blocked">待数据</option><option value="checked">已核查</option></select><textarea rows="2" maxlength="2000" aria-label="' + escape(item.check.check_id) + ' 检查结果" placeholder="检查结果或缺少的数据"></textarea></div>';
    }).join('') + '<label>排查备注（人工填写）</label><textarea data-it-notes rows="2" maxlength="4000" aria-label="排查备注"></textarea><div class="it-actions"><button type="button" class="button primary" data-it-save>保存记录</button><button type="button" class="button" data-it-reload>读取最新记录</button><button type="button" class="button" data-it-restore hidden>恢复本页草稿</button></div><div class="it-message" data-it-message role="status"></div></details>';
    this.fill(this.saved);
    var draft = this.readDraft();
    if (draft && draft.base_fingerprint === this.saved.record_fingerprint) { this.applyDraft(draft); this.message('已恢复本浏览器草稿 · 点击保存写入项目。'); }
    else if (draft) { this.message('服务器记录已更新；本页草稿保留，可读取后恢复。', true); this.root.querySelector('[data-it-restore]').hidden = false; }
    else this.message(this.saved.persisted ? '已保存 · ' + new Date(this.saved.updated_at).toLocaleString() : this.saved.previous_record_count ? '证据已更新，请重新核查；旧证据的记录已保留。' : '输入在本浏览器备份；点击保存写入项目。');
    this.progress();
  };
  Tracking.prototype.fill = function (value) {
    this.workingEntries = clone(value.entries || {});
    this.root.querySelectorAll('[data-it-check]').forEach(function (row) {
      var entry = value.entries[row.dataset.itCheck] || {status:'pending',note:''};
      row.querySelector('select').value = entry.status; row.querySelector('textarea').value = entry.note;
    });
    this.root.querySelector('[data-it-notes]').value = value.notes || '';
  };
  Tracking.prototype.applyDraft = function (draft) {
    var value = clone(this.saved);
    draft.changed_checks.forEach(function (id) { if (draft.entries[id]) value.entries[id] = draft.entries[id]; });
    if (draft.notes_changed) value.notes = draft.notes;
    this.fill(value); this.dirty = true; this.revision++; this.backup(); this.progress();
  };
  Tracking.prototype.restore = function () {
    if (this.disabled || this.saving) return;
    var draft = this.readDraft(); if (!draft) return;
    this.applyDraft(draft); this.root.querySelector('[data-it-restore]').hidden = true;
    this.message('本页修改已恢复，请核对后保存。');
  };
  Tracking.prototype.endpoint = function () { return '/api/investigation-tracking/' + encodeURIComponent(this.saved.case_id) + '/' + encodeURIComponent(this.saved.candidate_id); };
  Tracking.prototype.save = function () {
    if (this.disabled || this.saving || !this.key) return;
    var invalid = Array.from(this.root.querySelectorAll('[data-it-check]')).find(function (row) { return row.querySelector('select').value !== 'pending' && !row.querySelector('textarea').value.trim(); });
    if (invalid) { this.message('已核查或待数据的检查项需要填写结果。', true); invalid.querySelector('textarea').focus(); return; }
    this.backup(); var self = this, epoch = this.epoch, revision = this.revision, key = this.key, storageKey = this.storageKey();
    var value = this.capture(); value.investigation_fingerprint = this.saved.investigation_fingerprint; value.expected_record_fingerprint = this.saved.record_fingerprint;
    this.saving = true; this.progress(); this.message('正在保存…');
    request(this.endpoint(), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)}).then(function (saved) {
      if (self.epoch !== epoch) {
        // A successful response may arrive after navigation. Keep edits made meanwhile.
        var oldDraft = self.drafts[key];
        if (oldDraft && JSON.stringify(oldDraft.entries) === JSON.stringify(value.entries) && oldDraft.notes === value.notes) {
          delete self.drafts[key]; try { localStorage.removeItem(storageKey); } catch (_) {}
        }
        return;
      }
      self.saved = saved; self.record.tracking = saved; if (self.onSaved) self.onSaved(saved);
      if (self.revision === revision) {
        self.dirty = false; delete self.drafts[key]; try { localStorage.removeItem(storageKey); } catch (_) {}
        self.message('已保存 · ' + new Date(saved.updated_at).toLocaleString());
      } else { self.backup(); self.message('记录已保存；新增修改仍在草稿中。'); }
    }).catch(function (error) {
      if (self.epoch !== epoch) return;
      self.backup(); self.message(error.message + (error.status === 409 ? '；本页草稿已保留。' : ''), true);
    }).finally(function () { if (self.epoch === epoch) { self.saving = false; self.progress(); } });
  };
  Tracking.prototype.reload = function () {
    if (this.disabled || this.saving || !this.key) return;
    this.backup(); var self = this, epoch = this.epoch, revision = this.revision;
    this.saving = true; this.progress(); this.message('正在读取…');
    request(this.endpoint()).then(function (saved) {
      if (self.epoch !== epoch) return;
      if (self.revision !== revision) { self.message('读取期间有新增修改，草稿已保留；结束输入后再读取。'); return; }
      self.saved = saved; self.record.tracking = saved; if (self.onSaved) self.onSaved(saved);
      self.fill(saved); self.dirty = false;
      self.root.querySelector('[data-it-restore]').hidden = !self.readDraft();
      self.message('已读取最新记录；本页草稿仍可恢复。');
    }).catch(function (error) { if (self.epoch === epoch) self.message(error.message, true); })
      .finally(function () { if (self.epoch === epoch) { self.saving = false; self.progress(); } });
  };
  Tracking.prototype.clear = function () { this.backup(); this.epoch++; this.key = null; this.layoutKey = null; this.errorKey = null; this.saved = null; this.record = null; this.workingEntries = null; this.saving = false; this.dirty = false; this.root.replaceChildren(); this.root.className = ''; };
  Tracking.prototype.getSaved = function () { return this.key ? this.saved : null; };
  window.InvestigationTracking = Tracking;
}());
