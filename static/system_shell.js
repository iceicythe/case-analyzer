/* Shared navigation for the daily application. No external assets or services. */
(function () {
  'use strict';
  const page = document.body.dataset.page || 'overview';
  const reports = location.pathname.startsWith('/reports');
  const active = reports ? 'reports' : page;
  const icons = {
    overview: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    workbench: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    reports: '<path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6"/>',
    imports: '<path d="M12 3v12m-4-4 4 4 4-4M4 15v6h16v-6"/>',
    evaluation: '<path d="M4 20V10m8 10V4m8 16v-7M2 20h20"/>'
  };
  const names = {overview:'任务总览',workbench:'事件工作台',reports:'报告中心',imports:'数据接入',evaluation:'效果评估'};
  const svg = key => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">'+icons[key]+'</svg>';
  const nav = document.createElement('aside'); nav.className = 'app-sidebar';
  nav.innerHTML = '<a class="app-brand" href="/" aria-label="Case Analyzer 首页"><span class="app-mark">'+svg('workbench')+'</span><span>Case<span class="brand-light"> Analyzer</span><small>智驾测试分析系统</small></span></a>'+
    '<div class="nav-caption">工作空间</div><nav aria-label="主导航">'+
    [['overview','/'],['workbench','/workbench/'],['reports','/reports/'],['imports','/import/']].map(([key,url]) => '<a class="nav-link '+(key===active?'active':'')+'" href="'+url+'"'+(key===active?' aria-current="page"':'')+'>'+svg(key)+'<span>'+names[key]+'</span></a>').join('')+
    '</nav><div class="sidebar-bottom"><span class="local-dot"></span> <span id="workspaceName">本地工作空间</span><small id="systemVersion">CASE ANALYZER</small></div>';
  const header = document.createElement('header'); header.className = 'app-header';
  header.innerHTML = '<div class="breadcrumb">工作空间<span>/</span><strong>'+names[active]+'</strong></div><button type="button" class="workspace-tag" id="workspaceStatus" title="点击刷新工作空间信息">连接中</button>';
  document.body.prepend(header); document.body.prepend(nav);
  document.title = names[active]+' · Case Analyzer';
  function storageKey(key,workspace){
    if(!workspace||!/^[a-f0-9]{24}$/.test(workspace.storage_scope))throw Error('工作空间信息不可用');
    return 'case-workspace:'+workspace.storage_scope+':'+key;
  }
  function readStorage(storage,key,workspace){
    const scoped=storageKey(key,workspace);let value=storage.getItem(scoped);
    if(value===null&&workspace.allow_legacy_drafts){
      value=storage.getItem(key);
      if(value!==null){try{storage.setItem(scoped,value);storage.removeItem(key);}catch(_){/* Keep the original when migration cannot persist. */}}
    }
    return value;
  }
  window.CaseSystem = {page:active,storageKey,readStorage};
  let systemLoading=false;
  async function refreshSystem(){
    if(systemLoading)return;systemLoading=true;
    const badge=document.getElementById('workspaceStatus');
    try{
      const response=await fetch('/api/system',{cache:'no-store'});
      if(!response.ok)throw Error('Unavailable');
      const system=await response.json();
      if(system.evaluation?.enabled){
        nav.querySelectorAll('.nav-link').forEach(link=>{link.hidden=link.dataset.evaluationNav?false:!system.evaluation.unblinded||link.href.endsWith('/import/');});
        if(!nav.querySelector('[data-evaluation-nav]')){
          const link=document.createElement('a');link.href='/evaluation/';link.dataset.evaluationNav='true';
          link.className='nav-link'+(active==='evaluation'?' active':'');link.innerHTML=svg('evaluation')+'<span>效果评估</span>';
          nav.querySelector('nav').append(link);
        }
        nav.querySelector('.app-brand').href='/evaluation/';
      }
      document.getElementById('workspaceName').textContent=system.workspace.name;
      document.getElementById('systemVersion').textContent='CASE ANALYZER · '+system.version;
      badge.textContent=system.workspace.name;
      badge.title=system.case_count+' 个 Case · '+system.protocol+' · '+(system.model?.ready?'AI 已配置':'AI 未配置，人工复核可用')+'；点击刷新';
    }catch(_){badge.textContent='连接待检查';badge.title='无法读取工作空间信息，点击重试';}
    finally{systemLoading=false;}
  }
  document.getElementById('workspaceStatus').addEventListener('click',refreshSystem);
  document.addEventListener('evaluation-phase-changed',refreshSystem);
  refreshSystem();
  // Menus remain keyboard accessible and close after an action or outside click.
  document.addEventListener('click', event => {
    document.querySelectorAll('details.action-menu[open]').forEach(menu => {
      if (!menu.contains(event.target) || event.target.closest('button,a')) menu.open = false;
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') document.querySelectorAll('details.action-menu[open]').forEach(menu => { menu.open=false; menu.querySelector('summary').focus(); });
  });
}());
