const $ = (id) => document.getElementById(id);
let appState = null;
let pendingModalAction = null;
let activeUiTask = null;
let taskStartedAt = 0;
let progressPollHandle = null;
let taskHistory = [];
function showTask(action, stage, detail='', percent=2, status='running') {
  activeUiTask = action;
  const panel=$('taskPanel'); if(!panel) return;
  panel.classList.remove('hidden'); panel.setAttribute('aria-busy', status==='running'?'true':'false');
  const indicator=$('taskIndicator');
  if(indicator){indicator.className=`task-indicator ${status==='running'?'is-running':status==='complete'?'is-complete':'is-failed'}`;$('taskRingLabel').textContent=status==='complete'?'✓':status==='failed'?'!':`${Math.max(0,Math.min(100,percent))}%`;}
  $('taskTitle').textContent=action; $('taskDetail').textContent=detail || 'Waiting for the server to report the current step.';
  $('taskStage').textContent=stage; $('taskPercent').textContent=`${Math.max(0,Math.min(100,percent))}%`;
  $('taskProgressBar').style.width=`${Math.max(0,Math.min(100,percent))}%`;
  $('taskBadge').className=`pill ${status==='complete'?'pill-good':status==='failed'?'pill-bad':'pill-warn'}`;
  $('taskBadge').textContent=status==='complete'?'COMPLETE':status==='failed'?'FAILED':'IN PROGRESS';
  const time=new Date().toLocaleTimeString();
  const key=`${action}|${stage}|${detail}`;
  if(!taskHistory.length || taskHistory[taskHistory.length-1].key!==key) taskHistory.push({key,time,stage,detail,status}); else Object.assign(taskHistory[taskHistory.length-1],{time,stage,detail,status});
  taskHistory=taskHistory.slice(-7);
  $('taskSteps').innerHTML=taskHistory.map((x,i)=>`<li class="${x.status==='failed'?'failed':i===taskHistory.length-1&&status==='running'?'current':'done'}"><span class="task-step-time">${esc(x.time)}</span><span><b>${esc(x.stage)}</b>${x.detail?`<small>${esc(x.detail)}</small>`:''}</span><span class="task-step-state">${x.status==='failed'?'Failed':i===taskHistory.length-1&&status==='running'?'In progress':'Logged'}</span></li>`).join('');
}
async function pollServerProgress(){
  if(!activeUiTask) return;
  try {
    const data=await api('/api/progress'); const p=data.progress||{};
    const aliases={'Scan GPU market':['market scan'],'Refresh Kryptex data':['kryptex refresh'],'Rent GPU':['rent gpu'],'Destroy GPU instance':['destroy gpu instance'],'Deploy target':['deploy target']};
    const expected=(aliases[activeUiTask]||[String(activeUiTask).toLowerCase()]).some(x=>String(p.action).toLowerCase().includes(x));
    if(!p.action || !expected) return;
    const progressTime=Date.parse(p.updated_at||'');
    if(Number.isFinite(progressTime) && taskStartedAt && progressTime < taskStartedAt-2000) return;
    showTask(activeUiTask,p.stage||'Working',p.detail||'',p.percent??5,p.status||'running');
  } catch (_) { /* The action request still reports its own success/failure. */ }
}
async function runUiTask(action, button, fn, startDetail='Sending request to the server') {
  if(activeUiTask) { setNotice(`Please wait for “${activeUiTask}” to finish before starting another action.`, 'info'); return; }
  if(button?.disabled) return;
  const old=button?.innerHTML; if(button){button.disabled=true;button.dataset.busy='1';button.innerHTML='<span class="button-spinner" aria-hidden="true"></span> Working…';}
  const scanButtons=action==='Scan GPU market'?[$('scanBtn'),$('heroScanBtn')]:[];
  scanButtons.forEach(b=>{if(b&&b!==button){b.disabled=true;b.dataset.taskLock='1';}});
  taskStartedAt=Date.now(); taskHistory=[]; showTask(action,'Starting',startDetail,3,'running');
  if(progressPollHandle) clearInterval(progressPollHandle);
  progressPollHandle=setInterval(pollServerProgress,1500);
  try { showTask(action,'Request sent','Waiting for the server response; no completion is assumed yet.',8,'running'); await fn(); }
  catch(err){ showTask(action,'Action failed',err?.message||'Unexpected error',100,'failed'); throw err; }
  finally { if(button){button.disabled=false;button.innerHTML=old;delete button.dataset.busy;} scanButtons.forEach(b=>{if(b&&b!==button&&b.dataset.taskLock){b.disabled=false;delete b.dataset.taskLock;}}); await pollServerProgress(); if(progressPollHandle){clearInterval(progressPollHandle);progressPollHandle=null;} activeUiTask=null; }
}
const currency = (value, digits=2) => value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? '—' : '$' + Number(value).toLocaleString(undefined,{minimumFractionDigits:digits,maximumFractionDigits:digits});
function inrCurrency(value, digits=2){
  const rate=Number(appState?.fx?.rate);
  if(!appState?.fx?.available || !Number.isFinite(rate) || rate<=0 || value===null || value===undefined || value==='' || !Number.isFinite(Number(value))) return null;
  return '₹'+(Number(value)*rate).toLocaleString('en-IN',{minimumFractionDigits:digits,maximumFractionDigits:digits});
}
function inrLabel(value, digits=2){return inrCurrency(value,digits) || '₹— (FX stale/unavailable)';}
function renderFx(fx={}){
  const button=$('refreshFxBtn'); if(!button) return;
  if(fx.available && Number.isFinite(Number(fx.rate))){
    const stamp=fx.source_updated_at ? new Date(fx.source_updated_at).toLocaleString() : 'source timestamp unavailable';
    button.textContent=`USD/INR ${number(fx.rate,2)} · ${stamp} ↻`;
    button.classList.remove('fx-unavailable');
    button.title=`Reference rate from ${fx.source||'FX provider'}, source updated ${stamp}. Conversion only; USD remains the accounting base. Click to refresh.`;
  } else {
    button.textContent='USD/INR unavailable · refresh ↻';
    button.classList.add('fx-unavailable');
    button.title=fx.error||'No fresh exchange rate. INR conversions are hidden rather than using a stale number. Click to retry.';
  }
}
const number = (value, digits=2) => value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString(undefined,{minimumFractionDigits:digits,maximumFractionDigits:digits});
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function setNotice(message, type='info') { const node=$('notice'); node.className=`notice ${type}`; node.textContent=message; node.classList.remove('hidden'); }
function clearNotice(){ $('notice').classList.add('hidden'); }
function statusDotClass(ok){return ok?'good':'bad';}
function gotoSection(name){
  document.querySelectorAll('.page-section').forEach(x=>x.classList.add('hidden'));
  const section = $(`${name}Section`); if(section) section.classList.remove('hidden');
  document.querySelectorAll('.nav[data-section]').forEach(b=>{const active=b.dataset.section===name;b.classList.toggle('active',active);if(active)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
  const titles={overview:['CONTROL CENTER','Overview'],opportunities:['LIVE MARKET DATA','Market scanner'],instances:['RENTAL CONTROL','Instances'],safety:['CONFIGURATION','Safety & setup'],events:['AUDIT TRAIL','Event log']};
  $('pageEyebrow').textContent=titles[name]?.[0]||'CONTROL CENTER'; $('pageTitle').textContent=titles[name]?.[1]||'Overview';
  if(name==='opportunities') renderOpportunities();
}
function pill(kind,text){return `<span class="pill ${kind}">${esc(text)}</span>`;}
function decision(item){
  if(item.qualified) return pill('pill-good','AUTO-READY');
  if(item.economics_qualified && item.profile_available) return pill('pill-warn','LIVE GATES BLOCKED');
  if(item.economics_qualified) return pill('pill-warn','ECONOMICS ONLY');
  return pill('pill-bad','REJECTED');
}
function reasonText(item){return (item.reasons||[]).join(' · ');}
function opportunityRow(item, compact=false){
  const ratio=Number(item.revenue_cost_ratio||0); const net=Number(item.expected_net_day_usd||0); const monitorOnly=Boolean(appState?.config?.monitor_only); const canRent=Boolean(item.qualified)&&!monitorOnly;
  const action=monitorOnly?`<span class="sub-cell">Monitor only</span>`:(canRent?`<button class="btn btn-primary rent-btn" data-id="${esc(item.id)}">Rent</button>`:`<button class="btn btn-secondary" disabled title="${esc(reasonText(item))}">Blocked</button>`);
  return `<tr>
    <td><div class="gpu-cell">${esc(item.gpu_name||'Unknown GPU')}</div><div class="sub-cell">Offer #${esc(item.offer_id)} · ${esc(item.region||'region unknown')} · ${number(item.reliability*100,1)}% reliable</div></td>
    <td><span class="coin-chip">${esc(item.coin_tag)}</span><div class="sub-cell">${esc(item.algorithm||'Algorithm unavailable')}</div></td>
    <td><div class="money">${currency(item.total_hourly_usd,4)}/h</div><div class="sub-cell">GPU ${currency(item.gpu_hourly_usd,4)} + disk</div><div class="sub-cell fx-sub">${esc(inrLabel(item.total_hourly_usd,4))}/h all-in</div><div class="sub-cell ${Number(item.total_hourly_usd||0)<=Number(item.max_affordable_total_hourly_usd||0)&&Number(item.max_affordable_total_hourly_usd||0)>0?'positive':'negative'}">Max affordable: ${Number(item.max_affordable_total_hourly_usd||0)>0?currency(item.max_affordable_total_hourly_usd,4)+'/h':'none at current profit rules'}</div></td>
    <td><div class="money">${currency(item.conservative_gross_day_usd)}</div><div class="sub-cell">Raw WTM ${currency(item.whattomine_gross_day_usd??item.gross_day_usd)}</div>${item.kryptex_daily_revenue_usd!==null&&item.kryptex_daily_revenue_usd!==undefined?`<div class="sub-cell">Kryptex GPU reference ${currency(item.kryptex_daily_revenue_usd)}/day · conservative lower source used</div>`:`<div class="sub-cell">Kryptex pool API: account/worker telemetry only; no verified per-GPU daily estimate matched</div>`}<div class="sub-cell">Estimate source: ${esc(item.revenue_source||'WhatToMine')}</div><div class="sub-cell">Source updated: ${esc(item.revenue_source_updated_at||item.data_updated_at||'timestamp unavailable')}</div>${item.kryptex_pool_coin_price_usd!==null&&item.kryptex_pool_coin_price_usd!==undefined?`<div class="sub-cell">Kryptex ${esc(item.coin_tag)} price: ${currency(item.kryptex_pool_coin_price_usd,8)} · ${esc(item.kryptex_pool_coin_price_updated_at||'timestamp unavailable')} (price reference, not revenue)</div>`:''}<div class="sub-cell fx-sub">${esc(inrLabel(item.conservative_gross_day_usd))}/day</div></td>
    <td><div class="money ${net>=0?'positive':'negative'}">${currency(net)}</div><div class="sub-cell">Rent ${currency(item.total_cost_day_usd)} / day</div><div class="sub-cell fx-sub">${esc(inrLabel(net))}/day net estimate</div></td>
    <td><span class="coverage ${ratio>=1.5?'good':'bad'}">${number(ratio,2)}×</span></td>
    ${compact?'':`<td>${number(item.reliability*100,1)}%</td><td>${item.data_fresh?pill('pill-good','FRESH'):pill('pill-bad','STALE / UNKNOWN')}<div class="sub-cell">${esc(item.data_freshness||'unknown')}</div></td>`}
    <td>${action}<div class="sub-cell" title="${esc(reasonText(item))}">${esc((item.reasons||[])[0]||'Passes configured guardrails')}</div></td>
  </tr>`;
}
function scanFlag(value){return value===true?'Yes':value===false?'No':'Unknown';}
function renderScanDiagnostics(state=appState){
  const container=$('scanDiagnostics'); if(!container||!state)return;
  const cfg=state.config||{}; const automation=state.automation||{}; const summary=state.last_scan_summary||{};
  const provider=summary.provider_diagnostics||{}; const progress=state.ui_progress||{};
  const progressAge=Date.now()-Date.parse(progress.updated_at||'');
  const isRunning=String(progress.action||'').toLowerCase().includes('market scan')&&String(progress.status||'')==='running'&&Number.isFinite(progressAge)&&progressAge<120000;
  const isFailed=String(progress.action||'').toLowerCase().includes('market scan')&&String(progress.status||'')==='failed'&&Number.isFinite(progressAge)&&progressAge<10*60*1000;
  const autoOn=Boolean(automation.auto_scan_enabled&&cfg.vast_configured&&cfg.whattomine_configured);
  let message='No completed scan has been saved yet. The background scanner will retry when both providers are configured.';
  let label='WAITING'; let orb='idle'; let percent=0;
  if(isRunning){label='SCANNING';orb='spin';percent=Math.max(1,Math.min(100,Number(progress.percent||1)));message=`${progress.stage||'Scanning providers'} · ${progress.detail||'Checking GPU offers and coin economics'}`;}
  else if(Number(automation.vast_search?.cooldown_seconds||0)>0){const cd=Number(automation.vast_search.cooldown_seconds);const h=Math.floor(cd/3600),m=Math.floor((cd%3600)/60);label='SEARCH PAUSED';orb='failed';percent=100;message=`${automation.vast_search.cooldown_reason||'Vast search quota reached.'} Resumes in about ${h?h+'h ':''}${m}m. Your last saved results stay on screen.`;}
  else if(isFailed&&String(progress.stage||'').startsWith('Paused')){label='SEARCH PAUSED';orb='failed';percent=100;message=`${progress.detail||'Vast search quota reached.'} Your last saved results stay on screen; scanning resumes automatically.`;}
  else if(isFailed){label='SCAN FAILED';orb='failed';percent=100;message=`${progress.detail||'The automatic scan failed.'} The supervisor will retry after the configured interval.`;}
  else if(state.last_scan_at){
    const strictCount=Number(provider.strict_count??summary.offers_seen??0); const fallbackCount=Number(provider.fallback_count||0); const getCount=Number(provider.get_fallback_count||0);
    label='LAST SCAN COMPLETE';orb='complete';percent=100;
    message=`${summary.offers_seen??0} offers evaluated · ${summary.opportunities??0} coin-level rows · ${summary.qualified??0} live-qualified. Search rows: strict ${strictCount}, broad audit ${fallbackCount}${provider.rows_used_today!=null?` · ${provider.rows_used_today} of ${provider.usable_daily_rows||provider.daily_row_budget} daily Vast rows used`:''}.`
  }
  const reason=provider.diagnostic||(state.last_scan_at&&!Number(summary.offers_seen)?'Vast returned no offer rows. This means no records were returned by the API queries, not that the profitability model proved every GPU unprofitable.':'');
  const autoText=autoOn?`Automatic scans ON · recheck every ${Math.max(1,Math.round((automation.effective_scan_interval_seconds||automation.scan_interval_seconds||60)/60))} minute(s) to stay inside the daily Vast search quota.`:'Automatic scans OFF or both provider credentials are not configured.';
  container.innerHTML=`<div class="scan-diagnostics-head"><span class="scan-orb ${orb}">${isRunning?'<span class="scan-orb-core"></span>':label==='LAST SCAN COMPLETE'?'✓':(label==='SCAN FAILED'||label==='SEARCH PAUSED')?'!':'·'}</span><div class="scan-diagnostics-copy"><b>${esc(label)} · ${esc(autoText)}</b><small>${esc(message)}</small></div>${isRunning?`<strong class="scan-live-percent">${percent}%</strong>`:`<span class="pill ${isFailed?'pill-bad':state.last_scan_at?'pill-good':'pill-muted'}">${state.last_scan_at?'SAVED':'NO RESULT'}</span>`}</div>${reason?`<div class="scan-diagnostics-reason"><b>Provider diagnosis:</b> ${esc(reason)}</div>`:''}${summary.errors?.length?`<div class="scan-diagnostics-reason scan-error"><b>Calculation/API errors:</b> ${esc(summary.errors.slice(0,3).join(' · '))}</div>`:''}`;
}
function renderOfferDiagnostics(){
  const table=$('offerReviewTable'); if(!table||!appState)return;
  const rows=appState.offer_diagnostics||[]; const query=($('searchFilter')?.value||'').toLowerCase().trim();
  const visible=rows.filter(x=>{const text=`${x.gpu_name||''} ${x.offer_id||''} ${x.region||''} ${x.status||''} ${(x.reasons||[]).join(' ')}`.toLowerCase();return !query||text.includes(query);});
  $('offerReviewCount').textContent=`${visible.length} / ${rows.length} offers reviewed`;
  if(!rows.length){
    const summary=appState.last_scan_summary||{}; const provider=summary.provider_diagnostics||{};
    const note=(appState.last_scan_at&&Number(summary.offers_seen||0)===0)
      ? `${summary.provider_diagnostics?.diagnostic||'No records were returned by the Vast preferred query or broad fallback.'} API keys may be configured while the search endpoint still returns zero; check the saved provider diagnostic above and the account/base URL. The next automatic scan is scheduled when auto-scan is enabled.`
      : 'No offer-level rows have been saved yet. Run a market scan; each returned offer will appear here even when it cannot be matched to a supported benchmark.';
    table.innerHTML=`<tr><td colspan="7" class="empty-cell">${esc(note)}</td></tr>`; return;
  }
  table.innerHTML=visible.map(x=>{
    const verified=scanFlag(x.verified); const rentable=scanFlag(x.rentable);
    const state=String(x.status||'Uncertain');
    const tone=state.toLowerCase().includes('qualified')?'pill-good':state.toLowerCase().includes('positive')?'pill-warn':'pill-bad';
    const reasons=(x.reasons||[]).slice(0,3).join(' · ')||'No rejection reason recorded; inspect matching coin rows.';
    return `<tr><td><div class="gpu-cell">${esc(x.gpu_name||'Unknown GPU')}</div><div class="sub-cell">Offer #${esc(x.offer_id)} · ${esc(x.region||'Unknown region')} · ${esc(x.search_stage||'unknown')} query</div></td><td><div class="money">${currency(x.total_hourly_usd,4)}/h</div><div class="sub-cell">${esc(inrLabel(x.total_hourly_usd,4))}/h</div></td><td>${number(Number(x.reliability||0)*100,1)}%</td><td><span class="scan-flag ${x.verified===true?'good':x.verified===false?'bad':'unknown'}">V ${esc(verified)}</span> <span class="scan-flag ${x.rentable===true?'good':x.rentable===false?'bad':'unknown'}">R ${esc(rentable)}</span></td><td>${x.benchmark_matched?`<span class="pill pill-good">MATCHED</span><div class="sub-cell">${esc(x.benchmark_gpu_name||'GPU benchmark')}</div>`:`<span class="pill pill-bad">NO MATCH</span><div class="sub-cell">No calculation</div>`}</td><td><div class="money ${Number(x.best_expected_net_day_usd||0)>=0?'positive':'negative'}">${x.best_expected_net_day_usd===null||x.best_expected_net_day_usd===undefined?'—':currency(x.best_expected_net_day_usd)}</div><div class="sub-cell">best supported coin/algorithm</div></td><td><span class="pill ${tone}">${esc(state)}</span><div class="diagnostic-reasons">${esc(reasons)}</div><div class="sub-cell">${Number(x.coin_pairs_evaluated||0)} pairs · ${Number(x.economically_positive_pairs||0)} economics-positive</div></td></tr>`;
  }).join('');
}
function renderAffordabilityCatalogue(catalog, kryptex){
  const table=$('affordabilityTable'); if(!table)return;
  const models=Array.isArray(catalog?.models)?catalog.models:[];
  const poolCoin=String(kryptex?.coin||'').toUpperCase();
  const poolPrice=Number(kryptex?.coin_price_sample);
  const poolUpdated=Date.parse(kryptex?.updated_at||'');
  const poolAgeMs=Number.isFinite(poolUpdated)?Date.now()-poolUpdated:Infinity;
  const poolMaxAgeMs=Math.max(180000,Number(appState?.config?.kryptex_poll_interval_seconds||60)*3000);
  const poolPriceFresh=Boolean(kryptex?.coin_price_available&&poolCoin===String(catalog?.coin_tag||'PRL').toUpperCase()&&Number.isFinite(poolPrice)&&poolPrice>0&&poolAgeMs>=-60000&&poolAgeMs<=poolMaxAgeMs);
  const poolPriceCell=poolPriceFresh?`${currency(poolPrice,8)} <div class="sub-cell">${esc(kryptex.updated_at||'timestamp unavailable')} · reference only</div>`:'Unavailable / stale';
  if(!models.length){table.innerHTML='<tr><td colspan="6" class="empty-cell">No fresh positive-affordability models. If WhatToMine has no current PRL benchmark, GPU Hunter will not invent revenue or rent a GPU.</td></tr>';return;}
  table.innerHTML=models.map(m=>`<tr><td><div class="gpu-cell">${esc(m.gpu_name||'Unknown GPU')}</div></td><td>${esc(m.coin_tag||'—')}</td><td><div class="money">${Number(m.max_total_hourly_usd||0)>0?currency(m.max_total_hourly_usd,4)+'/h':'Not affordable under current gates'}</div></td><td><div class="money ${Number(m.best_expected_net_day_usd||0)>=0?'positive':'negative'}">${currency(m.best_expected_net_day_usd)}</div></td><td>${poolPriceCell}</td><td>${esc(m.source_updated_at||catalog.updated_at||'timestamp unavailable')}<div class="sub-cell">${esc(m.source||catalog.source||'Benchmark source')}</div></td></tr>`).join('');
}
function renderOpportunities(){
  if(!appState) return;
  renderScanDiagnostics(appState); renderOfferDiagnostics();
  const rows=appState.opportunities||[]; const filter=$('decisionFilter')?.value||'all'; const query=($('searchFilter')?.value||'').toLowerCase().trim();
   const visible=rows.filter(x=>{
    if(filter==='economics'&&!x.economics_qualified)return false;
    if(filter==='qualified'&&!x.qualified)return false;
    if(filter==='rejected'&&x.qualified)return false;
     const text=`${x.gpu_name} ${x.coin_tag} ${x.coin_name} ${x.algorithm} ${x.offer_id} ${(x.reasons||[]).join(' ')}`.toLowerCase(); return !query||text.includes(query);
  });
  renderAffordabilityCatalogue(appState.gpu_affordability_catalog||{}, appState.kryptex||{});
  const cat=appState.gpu_affordability_catalog||{}; const modelCount=Array.isArray(cat.models)?cat.models.length:0;
  const status=$('smartScanStatus'); if(status)status.textContent=modelCount? `Auto-estimated ${modelCount} GPU models for ${cat.coin_tag||'PRL'} from ${cat.source||'provider benchmarks'} (updated ${cat.updated_at||'timestamp unavailable'}). Targeted Vast searches use only positive affordability ceilings; offers are revalidated before any paid action.` : 'No fresh positive-affordability catalogue yet. The first scan must load current WhatToMine GPU/coin data; if PRL has no supported calculation, the system will report that instead of guessing revenue.';
  let empty='No coin-level rows match these filters. Offer-level audit below still shows every returned Vast offer and its reason for exclusion.';
  if(!rows.length){
    const summary=appState.last_scan_summary||{};
    empty=appState.last_scan_at&&Number(summary.offers_seen||0)===0
      ? 'Vast returned zero offer records after the preferred and fallback queries. This is a provider search result, not proof that zero GPUs exist or that every GPU is unprofitable. See the provider diagnosis above.'
      : 'No coin-level opportunities have been saved yet. Run a scan; unmatched offers and unsupported coin calculations are reported in the offer-level audit above.';
  }
  $('allOpportunities').innerHTML=visible.length?visible.map(x=>opportunityRow(x,false)).join(''):`<tr><td colspan="9" class="empty-cell">${esc(empty)}</td></tr>`;
  const summary=appState.last_scan_summary||{}; const auto=appState.automation||{};
  $('scannerFoot').textContent=`Showing ${visible.length} of ${rows.length} coin-level rows · ${Number(summary.offer_review_count||0)} offer-level reviews. Auto-scan ${auto.auto_scan_enabled?'ON':'OFF'}; target is controlled separately. Benchmark availability limits which coin pairs can be priced; uncertain rows can never be rented.`;
  document.querySelectorAll('.rent-btn').forEach(b=>b.addEventListener('click',()=>rentOpportunity(b.dataset.id,b)));
}
function renderInstances(){
  const rows=appState?.instances||[];
  $('instanceTable').innerHTML=rows.length?rows.map(x=>{
    const managed=x.managed_by_gph;
    const status=(x.status||'unknown').toLowerCase(); const statusPill=['running','active'].includes(status)?pill('pill-good',status.toUpperCase()):pill('pill-warn',status.toUpperCase());
    const action=appState?.config?.monitor_only?`<span class="sub-cell">Monitoring only</span>`:(managed?`<button class="btn btn-danger destroy-btn" data-id="${Number(x.instance_id)}">Destroy</button>`:`<span class="sub-cell">External / not managed</span>`);
    const hr=Number(x.observed_hashrate_hs||0); const hrText=hr>=1e12?number(hr/1e12,2)+' TH/s':hr>=1e9?number(hr/1e9,2)+' GH/s':hr>0?number(hr,0)+' H/s':'—';
    const poolRate = x.pool_revenue_24h_usd;
    const poolStatus = poolRate!==null && poolRate!==undefined ? 'Pool-reported current 24h run-rate' : (x.pool_metrics_error ? 'Pool telemetry error' : 'Pool run-rate not connected');
    const poolSub = x.pool_metrics_source ? esc(x.pool_metrics_source) : (x.pool_metrics_error ? esc(x.pool_metrics_error) : 'Not configured');
    return `<tr><td><div class="gpu-cell">${esc(x.gpu_name)}</div><div class="sub-cell">#${esc(x.instance_id)}${x.coin_tag?' · '+esc(x.coin_tag):''}</div>${x.destroy_reason?`<div class="sub-cell negative">${status==='destroy_requested'?'Termination requested':'Stop reason'}: ${esc(x.destroy_reason)}</div>`:''}</td><td>${statusPill}</td><td><div class="money">${currency(x.hourly_cost_usd,4)}/h</div><div class="sub-cell">${currency(Number(x.hourly_cost_usd||0)*24)}/day</div><div class="sub-cell fx-sub">${esc(inrLabel(x.hourly_cost_usd,4))}/h · ${esc(inrLabel(Number(x.hourly_cost_usd||0)*24))}/day</div></td><td><b>${hrText}</b></td><td>${x.gpu_temp?number(x.gpu_temp,0)+'°C':'—'} / ${x.gpu_util?number(x.gpu_util,0)+'%':'—'}</td><td>${x.accepted_share_lines??0}</td><td><div class="money">${currency(x.conservative_gross_day_usd)}</div><div class="sub-cell">Conservative model forecast; not payout</div><div class="sub-cell fx-sub">${esc(inrLabel(x.conservative_gross_day_usd))}/day forecast</div>${poolRate!==null&&poolRate!==undefined?`<div class="sub-cell">${esc(poolStatus)}: ${currency(poolRate)}/day</div><div class="sub-cell">${poolSub} · ${esc(x.pool_metrics_updated_at||'timestamp unavailable')}</div><div class="sub-cell fx-sub">${esc(inrLabel(poolRate))}/day pool run-rate</div>`:`<div class="sub-cell">Pool-reported 24h run-rate unavailable</div>${x.pool_metrics_error?`<div class="sub-cell">${esc(x.pool_metrics_error)}</div>`:''}`}</td><td><div class="money ${(Number(x.expected_net_day_usd||0)>=0)?'positive':'negative'}">${currency(x.expected_net_day_usd)}</div><div class="sub-cell">forecast; not paid revenue</div><div class="sub-cell fx-sub">${esc(inrLabel(x.expected_net_day_usd))}/day net estimate</div></td><td>${action}</td></tr>`;
  }).join(''):`<tr><td colspan="9" class="empty-cell">No active Vast instances. Scan or connect the Vast API to see resources.</td></tr>`;
  document.querySelectorAll('.destroy-btn').forEach(b=>b.addEventListener('click',()=>showModal('Destroy this instance?','This permanently destroys the managed Vast instance and deletes its local disk. This stops future compute and storage charges, but cannot refund past charges.',()=>destroyInstance(Number(b.dataset.id),b))));
  $('miniInstances').innerHTML=rows.length?rows.slice(0,5).map(x=>`<div class="mini-instance"><div><strong>${esc(x.gpu_name)} ${x.coin_tag?'· '+esc(x.coin_tag):''}</strong><small>Instance #${esc(x.instance_id)} · ${esc(x.status)}</small></div><div class="mini-right"><b>${currency(x.hourly_cost_usd,4)}/h</b><small>${x.managed_by_gph?'GPH monitored':'External instance'}</small></div></div>`).join(''):`<div class="empty-state">No connected Vast instances.</div>`;
  $('navInstanceCount').textContent=rows.length;
}
function renderAutomation(a,blockers,summary={}){
  const monitorOnly=Boolean(appState?.config?.monitor_only);
  const input=$('targetInstances'); const desired=Number(a.target_active_instances??1); const cap=25;
  input.max=String(cap);
  if(document.activeElement!==input) input.value=String(Math.min(cap,Math.max(0,desired)));
  $('autoRentEnabled').checked=monitorOnly?false:Boolean(a.auto_rent_enabled); $('autoRentEnabled').disabled=monitorOnly||!(a.live_mode&&!blockers.length);
  $('deployTargetBtn').disabled=monitorOnly||!(a.live_mode&&!blockers.length&&a.target_active_instances>0);
  if($('saveAutomationBtn')) $('saveAutomationBtn').disabled=monitorOnly;
  $('automationModeLabel').className=`pill ${monitorOnly?'pill-good':a.auto_rent_enabled?'pill-warn':'pill-muted'}`;
  $('automationModeLabel').textContent=monitorOnly?'MONITOR ONLY':a.auto_rent_enabled?'AUTO-DEPLOY ON':'SCAN ONLY';
  if(monitorOnly){$('automationHelp').textContent='Monitor-only safety lock is active. Market prices, coin economics and worker telemetry update automatically; GPU Hunter cannot rent or destroy instances.'; return;}
  $('automationHelp').textContent=blockers.length?`Auto-deploy blocked: ${blockers.slice(0,3).join(' · ')}${blockers.length>3?' …':''}`:`Target can be set from 0–${cap}. Automatic scans ${a.auto_scan_enabled?'ON':'OFF'} every ~${Math.max(1,Math.round((a.effective_scan_interval_seconds||a.scan_interval_seconds||60)/60))} min (paced to the daily Vast search quota). Qualified candidates are ranked by conservative net/day; spend and safety gates may keep the active count below your target.`;
  updateTargetPreview(a,summary);
}
function updateTargetPreview(a=appState?.automation||{},summary=appState?.summary||{}){
  const select=$('targetInstances'); if(!select) return;
  const raw=select.value; const numeric=Number(raw);
  const selected=Number.isInteger(numeric)?Math.min(25,Math.max(0,numeric)):0;
  const saved=Math.max(0,Number(a.target_active_instances??1));
  const active=Math.max(0,Number(summary.live_instances??0));
  const qualified=Math.max(0,Number(summary.fully_auto_qualified_count??0));
  $('targetSelectedCount').textContent=`${selected} ${selected===1?'instance':'instances'}${selected!==saved?' · unsaved':''}`;
  $('targetActiveCount').textContent=`${active} ${active===1?'instance':'instances'}`;
  $('targetQualifiedCount').textContent=String(qualified);
  const remaining=Math.max(0,selected-active);
  $('targetSelectionHelp').textContent=select.value===''||!Number.isInteger(Number(select.value))||Number(select.value)<0||Number(select.value)>25
    ? 'Enter a whole number from 0 to 25, then save.'
    : selected===0
      ? `Scan-only mode: market data is evaluated, but this target requests no rentals. ${selected!==saved?'Save target to apply this selection.':'Target is saved.'}`
      : `At most ${selected} simultaneous instance${selected===1?'':'s'} in total. ${active} active now; up to ${remaining} additional slot${remaining===1?'':'s'} under this target. The app will never rent one GPU for every offer. ${selected!==saved?'Save target to apply this selection.':'Target is saved.'}`;
}
function formatHashrate(value){
  if(value===null||value===undefined||value==='') return '—';
  const n=Number(value); if(!Number.isFinite(n)) return String(value).slice(0,24);
  if(n>=1e12) return number(n/1e12,2)+' TH/s'; if(n>=1e9) return number(n/1e9,2)+' GH/s'; if(n>=1e6) return number(n/1e6,2)+' MH/s';
  return number(n,2)+' (API units)';
}
function renderKryptex(k,market={}){
  const connected=Boolean(k.connected), accountData=Boolean(k.account_data_available); $('kryptexDot').className='status-dot '+(k.refreshing?'idle':(connected&&accountData?'good':(connected?'idle':'bad')));
  const hasWorker=Number(k.worker_count||0)>0 || (k.hashrate_raw!==null&&k.hashrate_raw!==undefined&&Number(k.hashrate_raw)>0);
  const checked=Boolean(k.updated_at||k.refresh_finished_at);
  $('kryptexConnection').textContent=k.refreshing?'Checking Kryptex API…':(connected?(accountData?(hasWorker?'API connected · worker active':'API connected · account data available'):(checked?'Pool connected · no active worker data':'Pool API connected · awaiting first check')):(k.configured?(checked?'Connection issue':'Waiting for first check'):'Not configured'));
  $('kryptexCoin').textContent=(k.coin||appState?.config?.kryptex_coin||'prl').toUpperCase();
  $('kryptexWorkers').textContent=k.worker_count??(checked?'0 active':'—'); $('kryptexHashrate').textContent=formatHashrate(k.hashrate_raw);
  $('kryptexBalance').textContent=k.balance_available&&k.balance_raw!==null&&k.balance_raw!==undefined?number(k.balance_raw,8)+' coin units':'—';
  $('kryptexPrice').textContent=k.coin_price_available&&k.coin_price_sample!==null&&k.coin_price_sample!==undefined?number(k.coin_price_sample,8):'—';
  $('kryptexNote').textContent=k.error||k.note||(checked&&!hasWorker?'No active worker telemetry was returned. This is expected when no miner is running; worker count and hashrate are shown as zero/unavailable, not as a loading state.':'Enter a public pool username/wallet above. Pool balance is not daily income.');
  const models=market.models||[]; const available=models.filter(x=>x.available);
  $('kryptexGpuCoverage').className=`pill ${available.length?'pill-good':'pill-muted'}`;
  $('kryptexGpuCoverage').textContent=market.updated_at?`${available.length}/${market.attempted_models||models.length} models · ${market.updated_at.replace('T',' ').replace('Z',' UTC')}`:'Not scanned';
  const rows=[];
  for(const model of available){
    const coinRows=model.coin_rows||[];
    if(!coinRows.length){rows.push(`<tr><td>${esc(model.gpu_name)}</td><td colspan="5" class="sub-cell">Kryptex endpoint answered, but structured coin rows were not exposed in a recognized format (${esc((model.data_keys||[]).slice(0,8).join(', '))}).</td></tr>`);continue;}
    for(const coin of coinRows){
      rows.push(`<tr><td><b>${esc(model.gpu_name)}</b></td><td>${esc(coin.coin||'—')}<div class="sub-cell">${esc(coin.algorithm||'Algorithm not specified')}</div></td><td>${coin.hashrate_display|| (coin.hashrate!==null&&coin.hashrate!==undefined?number(coin.hashrate,2):'—')}</td><td>${coin.power_w!==null&&coin.power_w!==undefined?number(coin.power_w,1)+' W':'—'}</td><td>${coin.daily_revenue_usd!==null&&coin.daily_revenue_usd!==undefined?`${currency(coin.daily_revenue_usd)}<div class="sub-cell fx-sub">${esc(inrLabel(coin.daily_revenue_usd))}</div>`:'Not explicitly daily'}</td><td>${coin.daily_profit_usd!==null&&coin.daily_profit_usd!==undefined?`${currency(coin.daily_profit_usd)}<div class="sub-cell fx-sub">${esc(inrLabel(coin.daily_profit_usd))}</div>`:'Not explicitly daily'}</td></tr>`);
    }
  }
  $('kryptexGpuRows').innerHTML=rows.length?rows.slice(0,100).join(''):`<tr><td colspan="6" class="empty-cell">Kryptex GPU model data is not available yet. The scan will retry; no missing rate is assumed profitable.</td></tr>`;
  const missing=models.filter(x=>!x.available).length;
  $('kryptexGpuNote').textContent=market.note||'Kryptex GPU references are secondary. The API may not list every rental GPU or expose daily earning values for every model.';
  if(market.errors?.length) $('kryptexGpuNote').textContent+=` Last refresh had ${market.errors.length} request error(s).`;
}

async function saveAutomation(){
  const btn=$('saveAutomationBtn');
  const targetRaw=$('targetInstances').value; const target=Number(targetRaw); const auto=$('autoRentEnabled').checked;
  if(targetRaw.trim()===''||!Number.isInteger(target)||target<0||target>25){setNotice('Target must be a whole number from 0 to 25. Nothing was saved.','error');$('targetInstances').focus();return;}
  if(auto&&!confirm('Enable automatic paid GPU rentals? This only works if all live safety gates pass, but cloud costs can still occur. Continue?')){ $('autoRentEnabled').checked=false; return; }
  try { await runUiTask('Save automation settings',btn,async()=>{
    showTask('Save automation settings','Validating selected target','Checking target count and auto-rent toggle',20,'running');
    const data=await api('/api/automation-settings',{method:'POST',body:JSON.stringify({target_active_instances:target,auto_rent_enabled:auto})});
    setNotice(data.message||'Automation settings saved.','success'); showTask('Save automation settings','Refreshing dashboard state','Loading saved settings from server',85,'running'); await refresh({throwOnError:true}); showTask('Save automation settings','Complete','Server accepted the settings and dashboard state refreshed',100,'complete');
  },'Validating target and sending settings'); }
  catch(err){$('autoRentEnabled').checked=false;setNotice(err.message,'error');await refresh();}
}

async function deployTarget(){
  showModal('Deploy the selected mining target?','The service will create up to the selected number of managed instances using only fresh, fully qualified opportunities. Any opportunity failing a configured safety check will be skipped. This creates real rental charges.',async()=>{
    const btn=$('deployTargetBtn');
    try { await runUiTask('Deploy target',btn,async()=>{
      showTask('Deploy target','Checking server-side safety gates','The server checks auto-deploy enablement and live blockers before any paid action',15,'running');
      const result=await api('/api/deploy-target',{method:'POST',body:JSON.stringify({})});
      showTask('Deploy target','Refreshing instance list',result.message||'Checking the returned deployment result',85,'running'); setNotice(result.message||'Deployment evaluation finished.','success'); await refresh({throwOnError:true}); gotoSection('instances');
      showTask('Deploy target','Complete',`${result.count??0} rental request(s) returned. Verify provider status in Instances.`,100,'complete');
    },'Checking configuration and deployment permissions'); }
    catch(err){setNotice(err.message,'error');}
  });
}

async function waitForKryptexResult(initial={},timeoutMs=35000){
  let status=initial||{}; const deadline=Date.now()+timeoutMs;
  while(status.refreshing&&Date.now()<deadline){
    await new Promise(resolve=>setTimeout(resolve,600));
    const next=await api('/api/kryptex/status',{signal:AbortSignal.timeout(6000)});
    status=next.kryptex||status;
  }
  if(status.refreshing) status.error='The Kryptex API check is still running. The connection result has not been confirmed yet.';
  return status;
}
async function refreshKryptex(){
  const btn=$('refreshKryptexBtn');
  try { await runUiTask('Refresh Kryptex data',btn,async()=>{
    showTask('Refresh Kryptex data','Starting background pool check','Kryptex telemetry is checked in the background; dashboard requests remain responsive.',15,'running');
    const started=await api('/api/kryptex/refresh',{method:'POST',body:JSON.stringify({}),signal:AbortSignal.timeout(6000)});
    let status=await waitForKryptexResult(started.kryptex||{},35000);
    renderKryptex(status,started.kryptex_gpu_market||{});
    if(appState) appState.kryptex=status;
    // Reload persisted market results independently; a dashboard DB outage must not
    // overwrite a successful in-memory Kryptex API result with a spinner.
    await refresh({keepNotice:true});
    const message=status.connected
      ? (status.account_data_available?'Kryptex Pool API connected; account/worker telemetry returned.':'Kryptex Pool API is reachable, but this username/coin has no worker telemetry yet.')
      : (status.error||'Kryptex connection failed; verify the public username/wallet and coin slug.');
    setNotice(message,status.connected?(status.account_data_available?'success':'info'):'error');
    showTask('Refresh Kryptex data',status.connected?'Complete':'Connection check failed',message,100,status.connected?(status.account_data_available?'complete':'failed'):'failed');
  },'Checking the documented Kryptex Pool API'); }
  catch(err){setNotice(err.message,'error');}
}
async function configureKryptex(){
  const btn=$('saveKryptexConfigBtn'); if(btn.disabled)return;
  const prior=btn.textContent; btn.disabled=true; btn.textContent='Saving & testing…';
  try{
    const username=$('kryptexUsernameInput').value.trim();
    const coin=$('kryptexCoinInput').value.trim().toLowerCase()||'prl';
    const worker=$('kryptexWorkerInput').value.trim();
    const result=await api('/api/kryptex/configure',{method:'POST',body:JSON.stringify({username,coin,worker}),signal:AbortSignal.timeout(8000)});
    let status=await waitForKryptexResult(result.kryptex||{},35000);
    renderKryptex(status,{}); if(appState)appState.kryptex=status;
    await refresh({keepNotice:true});
    const message=status.connected
      ? (status.account_data_available?'Kryptex identity saved and account/worker API verified.':'Kryptex Pool API is reachable, but no account worker data was returned yet. Confirm that coin and public username match your Kryptex pool miner.')
      : (status.error||'Kryptex configuration was saved but the live API check failed.');
    setNotice(message,status.connected?(status.account_data_available?'success':'info'):'error');
  }catch(err){setNotice(err.message,'error');}
  finally{btn.disabled=false;btn.textContent=prior;}
}

async function refreshVastBalance(){
  const btn=$('refreshVastBalanceBtn'); if(btn.disabled)return;
  const original=btn.textContent; btn.disabled=true; btn.innerHTML='<span class="button-spinner dark" aria-hidden="true"></span> Checking Vast…';
  try{
    const started=await api('/api/vast/balance/refresh',{method:'POST',body:JSON.stringify({})});
    let status=started.vast_balance||{};
    const deadline=Date.now()+15000;
    while(status.refreshing && Date.now()<deadline){
      await new Promise(resolve=>setTimeout(resolve,1000));
      const current=await api('/api/vast/balance/status'); status=current.vast_balance||status;
    }
    // Render the provider result before reloading the PostgreSQL-backed dashboard.
    // Previously a /api/state database exception jumped to catch first and left the
    // balance card on its initial "Checking…" text even after the API call finished.
    if(appState){appState.vast_balance=status;renderFinance(appState);}
    let dashboardRefreshError='';
    try{ await refresh({keepNotice:true,throwOnError:true}); }
    catch(err){ dashboardRefreshError=err.message||'Dashboard state is temporarily unavailable'; }
    if(status.refreshing){
      setNotice('Vast balance check is still running. The request is read-only; paid rentals remain blocked while the current balance is unverified.'+(dashboardRefreshError?` Dashboard data is also degraded: ${dashboardRefreshError}`:''),'info');
    }else{
      const balanceMessage=status.available?`Vast wallet balance refreshed: ${currency(status.balance_usd,4)}.`:`Vast balance could not be verified: ${status.error||'Unknown provider response'}`;
      setNotice(balanceMessage+(dashboardRefreshError?` Dashboard data is temporarily unavailable: ${dashboardRefreshError}`:''),status.available?(dashboardRefreshError?'info':'success'):'error');
    }
  }catch(err){setNotice(`Could not refresh Vast wallet balance: ${err.message}`,'error');}
  finally{btn.disabled=false;btn.textContent=original;}
}

function renderSafety(){
  const c=appState?.config||{}; const p=[];
  const login=appState?.login||{};
  $('loginSetup').innerHTML=[
    checkRow('Configured username',Boolean(login.username),`This deployment expects username: ${login.username||'gph'}.`),
    checkRow('Password is configured',Boolean(login.password_configured),login.password_configured?'A password is set; its value stays server-side and is never returned to this page.':'No APP_PASSWORD is set. Public/Render deployment should refuse to start until it is configured.'),
    checkRow(login.password_configured?'Sign-in session':'Local access mode',login.password_configured?Boolean(login.session_authenticated):true,login.password_configured?'The current dashboard session is authenticated. Sign out from the top bar to end it.':'No password is configured, so no sign-in session is used. This is permitted only when bound to localhost.')
  ].join('');
  $('loginSetupPill').className=`pill ${login.password_configured?'pill-good':'pill-muted'}`;
  $('loginSetupPill').textContent=login.password_configured?'SESSION LOGIN':'LOCAL ACCESS';
  p.push({title:'Vast.ai Rental API',ok:c.vast_configured,desc:c.vast_configured?'API key detected (value hidden).':'Add VAST_API_KEY to .env to scan offers and inspect billing/instances.'});
  p.push({title:'WhatToMine profitability data',ok:c.whattomine_configured,desc:c.whattomine_configured?'API token detected (value hidden).':'Add WHATTOMINE_API_TOKEN to retrieve coin/algorithm/hardware benchmarks.'});
  const kryptexModels=appState?.kryptex_gpu_market?.available_models||0;
  p.push({title:'Kryptex GPU profitability references',ok:kryptexModels>0,desc:`${kryptexModels} GPU model endpoint(s) currently responded. Models/daily fields vary by Kryptex response.`});
  p.push({title:'Kryptex Pool account / worker API',ok:c.kryptex_configured,desc:c.kryptex_configured?`Configured for ${c.kryptex_coin?.toUpperCase()||'coin'}; username value is hidden.`:'Enter your public Kryptex mining username/payout wallet and coin slug in the Kryptex connection panel, or configure KRYPTEX_MINING_USERNAME and KRYPTEX_COIN in Render.'});
  p.push({title:'Remote SSH monitoring key',ok:c.ssh_key_configured,desc:c.ssh_key_configured?'Private key file exists. The matching public key must also be registered in Vast.':'Set SSH_PRIVATE_KEY_PATH and register its public key in Vast before live mode.'});
  $('providerList').innerHTML=p.map(x=>checkRow(x.title,x.ok,x.desc)).join('');
  const gpuRateCap=Number(c.max_gpu_hourly_usd)>0?currency(c.max_gpu_hourly_usd,3)+'/hr':'No fixed cap (profit gate applies)';
  const totalRateCap=Number(c.max_total_hourly_usd)>0?currency(c.max_total_hourly_usd,3)+'/hr':'No fixed cap (profit + budget gates apply)';
  const runtimeCap=Number(c.max_instance_runtime_hours)>0?`${c.max_instance_runtime_hours} hours`:'No fixed time limit; stop on loss/stale data/health failure';
  const limits=[['Min host reliability',number(c.min_reliability*100,1)+'%'],['Max GPU price',gpuRateCap],['Max all-in price',totalRateCap],['Max projected daily spend',currency(c.max_daily_spend_usd)+'/day · '+(inrCurrency(c.max_daily_spend_usd)||'INR unavailable')],['Saved dashboard target ceiling','25 instances (target field; safety/budget gates still apply)'],['Legacy MAX_ACTIVE_INSTANCES setting',c.max_active_instances],['Runtime ceiling',runtimeCap],['Minimum revenue / cost',number(c.min_revenue_cost_ratio,2)+'×'],['Minimum conservative net',currency(c.min_net_profit_usd_day)+'/day · '+(inrCurrency(c.min_net_profit_usd_day)||'INR unavailable')],['Revenue haircut',number(c.revenue_haircut_pct,0)+'%'],['Bandwidth reserve',currency(c.bandwidth_reserve_usd_day)+'/day'],['Disk limit',`${c.default_disk_gb} GB default / ${c.max_disk_gb} GB max`]];
  $('limitList').innerHTML=limits.map(([a,b])=>`<div class="limit-row"><span>${esc(a)}</span><b>${esc(b)}</b></div>`).join('');
  const blockers=appState?.live_blockers||[];
  $('blockerList').innerHTML=blockers.length?blockers.map(x=>checkRow('Blocked: '+x,false,'Required before any live rental.')).join(''):checkRow('All configured live gates pass',true,'Live spending can still be blocked by opportunity economics, profile-specific settings, budget caps or the current API response.');
  $('liveGatesPill').className=`pill ${blockers.length?'pill-bad':'pill-good'}`;$('liveGatesPill').textContent=blockers.length?`${blockers.length} BLOCKER${blockers.length===1?'':'S'}`:'GATES PASS';
}
function checkRow(title,ok,desc){return `<div class="check-row"><div class="check-icon ${ok?'pass':'fail'}">${ok?'✓':'!'}</div><div><strong>${esc(title)}</strong><small>${esc(desc)}</small></div></div>`;}
function renderEvents(){
  const rows=appState?.events||[];
  $('eventTable').innerHTML=rows.length?rows.map(x=>`<tr><td>${esc((x.created_at||'').replace('T',' ').replace('+00:00','Z'))}</td><td>${pill(x.level==='ERROR'?'pill-bad':x.level==='WARNING'?'pill-warn':'pill-muted',x.level)}</td><td><b>${esc(x.event)}</b></td><td><code>${esc(JSON.stringify(x.details||{}))}</code></td></tr>`).join(''):`<tr><td colspan="4" class="empty-cell">No events recorded.</td></tr>`;
}
function renderSetupGaps(gaps){
  const box=$('setupGaps'); if(!box)return;
  gaps=Array.isArray(gaps)?gaps:[];
  const now=gaps.filter(g=>g.severity==='required'), opt=gaps.filter(g=>g.severity==='optional'), live=gaps.filter(g=>g.severity==='live');
  if(!now.length&&!opt.length){box.classList.add('hidden');box.innerHTML='';return;}
  const li=g=>`<li><span class="${g.severity==='required'?'gap-required':''}">${esc(g.label)}</span> — add <code>${esc(g.where)}</code>. ${esc(g.impact)}</li>`;
  box.innerHTML=`<b class="setup-title">${now.length?'Some API keys are missing, so parts of the dashboard cannot load':'Optional connections are not set up'}</b>`
    +(now.length?`<ul>${now.map(li).join('')}</ul>`:'')
    +(opt.length?`<ul>${opt.map(li).join('')}</ul>`:'')
    +(live.length?`<details><summary>${live.length} more item(s) only needed for live mining</summary><ul>${live.map(li).join('')}</ul></details>`:'')
    +'<div style="margin-top:6px">Set these in Render → your service → Environment (or the files named above), then redeploy.</div>';
  box.classList.remove('hidden');
}
function renderState(state){
  appState=state; const c=state.config||{}; const s=state.summary||{}; const rows=state.opportunities||[]; renderFx(state.fx||{});
  $('apiStatus').innerHTML=providerBadgeHtml(c,state.vast_balance);
  $('sidebarMode').textContent=c.automation_mode==='live'&&c.enable_live_mining?'Live guard active':'Scanner mode'; $('sidebarModeCaption').textContent=c.automation_mode==='live'&&c.enable_live_mining?'Guarded; spending still gated':'Live spending disabled by default'; $('sidebarDot').className='status-dot '+(c.automation_mode==='live'&&c.enable_live_mining?'good':'idle');
  $('modePill').textContent=c.automation_mode==='live'&&c.enable_live_mining?'GUARDED LIVE':'SCAN ONLY';
  $('heroHeadline').textContent=rows.length?'Market estimates loaded':'Find profit before spending';
  $('heroText').textContent=rows.length?'The scanner has benchmark estimates. Auto-rent still requires fresh data, compatible miner configuration, monitoring and every risk gate.':'Connect the rental and profitability APIs to compare GPU rent against conservative mining revenue. No rental is started by default.';
  $('lastScanText').textContent=state.last_scan_at?`Last scan ${state.last_scan_at.replace('T',' ').replace('+00:00',' UTC')}`:'No market scan run yet';
  $('profileCount').textContent=`${state.profiles_enabled||0} / ${state.profiles_total||0}`; $('blockerCount').textContent=(state.live_blockers||[]).length; $('guardTitle').textContent=(state.live_blockers||[]).length?'Live mode blocked':'Configuration gates pass'; $('guardCopy').textContent=(state.live_blockers||[]).length?'The app refuses paid rentals until every blocker is resolved.':'Base live gates pass. Each opportunity must still pass its own profit and runtime checks.';
  $('metricNet').textContent=s.best_candidate?currency(s.best_candidate.expected_net_day_usd):'—'; $('metricNetSub').textContent=s.best_candidate?`${s.best_candidate.gpu_name||'GPU'} · ${s.best_candidate.coin_tag||'coin'} · estimated, not guaranteed`:'No economically qualified opportunity'; $('metricNetInr').textContent=s.best_candidate?`${inrLabel(s.best_candidate.expected_net_day_usd)} /day · display conversion only`:'INR appears here when a qualified estimate exists';
  $('metricBurn').innerHTML=`${currency(s.current_hourly_cost_usd||0)}<span class="unit">/hr</span>`; $('metricBurnSub').textContent=`${currency(s.projected_current_daily_rent_usd||0)} current daily run-rate`; $('metricBurnInr').textContent=`${inrLabel(s.current_hourly_cost_usd||0)}/hr · ${inrLabel(s.projected_current_daily_rent_usd||0)}/day`;
  const scanSummary=state.last_scan_summary||{}; $('metricOffers').textContent=scanSummary.offers_seen??'—'; $('metricCoinsSub').textContent=`${scanSummary.coins_seen??'—'} coin records · ${scanSummary.gpu_benchmarks??'—'} GPU benchmarks`;
  renderAutomation(state.automation||{},state.live_blockers||[],s); renderFinance(state); renderKryptex(state.kryptex||{},state.kryptex_gpu_market||{});
  $('metricQualified').textContent=s.fully_auto_qualified_count??'—'; $('metricQualifiedSub').textContent=`${s.economically_positive_count||0} economically positive; profiles and guards also required`;
  $('navOpportunityCount').textContent=s.opportunities_count??'—';
  $('riskPill').className=`pill ${(state.live_blockers||[]).length?'pill-warn':'pill-good'}`;$('riskPill').textContent=(state.live_blockers||[]).length?'BLOCKED':'READY';
  const blockers=(state.live_blockers||[]); $('riskMini').innerHTML=blockers.length?blockers.slice(0,4).map(x=>`<div class="risk-item"><span class="risk-symbol">!</span><span>${esc(x)}</span></div>`).join(''):`<div class="risk-item pass"><span class="risk-symbol">✓</span><span>Base live configuration gates currently pass. Opportunity-specific checks remain mandatory.</span></div>`;
  const econ=rows.filter(x=>x.economics_qualified).slice(0,6); $('topOpportunities').innerHTML=econ.length?econ.map(x=>opportunityRow(x,true)).join(''):`<tr><td colspan="7" class="empty-cell">No conservative positive-profit candidates. This is a valid outcome—do not lower safety limits just to force a rental.</td></tr>`;
  renderOpportunities(); renderInstances(); renderSafety(); renderEvents(); renderSetupGaps(state.setup_gaps);
}

let vastAutoCheckDone=false;
async function ensureVastBalanceChecked(b){
  // First visit with a key configured but no balance ever fetched: start the read-only check
  // once instead of leaving the wallet panel on "Balance has not been checked yet".
  if(vastAutoCheckDone||!b||b.configured!==true||b.available||b.refreshing||b.updated_at)return;
  vastAutoCheckDone=true;
  try{
    const r=await api('/api/vast/balance/refresh',{method:'POST',body:'{}'});
    appState.vast_balance=r.vast_balance||appState.vast_balance; renderFinance(appState);
    for(let i=0;i<12;i++){
      await new Promise(x=>setTimeout(x,1200));
      const st=await api('/api/vast/balance/status',{signal:AbortSignal.timeout(4000)});
      appState.vast_balance=st.vast_balance||{}; renderFinance(appState);
      if(!appState.vast_balance.refreshing)break;
    }
  }catch(_){ /* the panel already shows the provider's error text */ }
}
function renderFinance(state){
  const b=state.vast_balance||{}; const f=state.funding||{}; const s=state.summary||{};
  const selectedTarget=Math.max(0,Number($('targetInstances')?.value??f.target_instances??0));
  const activeCount=Math.max(0,Number(f.active_instances??0));
  const remainingSlots=selectedTarget>0?Math.max(0,selectedTarget-activeCount):0;
  const activeCost=Math.max(0,Number(f.active_rental_24h_usd??0));
  const unitCost=f.candidate_cost_per_instance_24h_usd;
  const req=remainingSlots>0?(unitCost===null||unitCost===undefined?null:activeCost+remainingSlots*Math.max(0,Number(unitCost))):activeCost;
  const balanceOkay=Boolean(b.available&&Number.isFinite(Number(b.balance_usd)));
  const balanceValue=balanceOkay?Number(b.balance_usd):null;
  const gap=req!==null&&req!==undefined&&balanceValue!==null?Math.max(0,req-balanceValue):null;
  if(b.refreshing){
    $('vastBalanceValue').textContent='Checking…';
    $('vastBalanceInr').textContent='Waiting for the read-only Vast account response';
    $('vastBalanceUpdated').textContent=b.refresh_started_at?`Balance check started ${new Date(b.refresh_started_at).toLocaleTimeString()}`:'Checking current Vast credits';
  }else if(b.available && Number.isFinite(Number(b.balance_usd))){
    $('vastBalanceValue').textContent=currency(b.balance_usd,4);
    $('vastBalanceInr').textContent=inrLabel(b.balance_usd)+' · display conversion';
    $('vastBalanceUpdated').textContent=`Live Vast account balance · updated ${b.updated_at?new Date(b.updated_at).toLocaleString():'timestamp unavailable'}`;
  }else{
    $('vastBalanceValue').textContent=b.configured===false?'Vast API key missing':(b.balance_usd!==null&&b.balance_usd!==undefined?`Last known ${currency(b.balance_usd,4)}`:'Unable to verify');
    $('vastBalanceInr').textContent=b.configured===false?'Add VAST_API_KEY in Render → Environment, then redeploy':'Current balance not confirmed';
    $('vastBalanceUpdated').textContent=b.last_success_at?`Last successful check ${new Date(b.last_success_at).toLocaleString()}`:(b.error||'Use Refresh balance to query Vast directly.');
  }
  $('vastRequired24h').textContent=req===null||req===undefined?'Awaiting candidate':currency(req,4);
  $('vastRequired24hInr').textContent=req===null||req===undefined?'Cannot estimate until a positive candidate is found':`${inrLabel(req)} · 24h estimate`;
  $('vastFundingSource').textContent=`${f.estimate_source||'Estimate includes active rentals and selected target slots'}${selectedTarget!==Number(f.target_instances??0)?' · preview for unsaved target':''}`;
  $('vastFundingCoverage').textContent='—';
  const alert=$('vastFundsAlert'); alert.className='funds-alert hidden'; alert.innerHTML='';
  if(!b.available){
    $('vastFundingGap').textContent=b.refreshing?'Checking balance…':'Balance unverified';
    $('vastFundingCoverage').textContent=b.refreshing?'Waiting for the provider':'Do not rely on a stale balance';
    alert.className='funds-alert funds-alert-warn';
    alert.innerHTML=`<b>Cannot verify current Vast credits.</b> ${esc(b.error||'Refresh the account balance before any paid deployment.')} <a href="https://cloud.vast.ai/billing/" target="_blank" rel="noopener noreferrer">Open Vast Billing ↗</a>`;
  }else if(req===null||req===undefined){
    $('vastFundingGap').textContent='Not estimated';
    $('vastFundingCoverage').textContent='No positive candidate to price yet';
    alert.className='funds-alert funds-alert-info';
    alert.innerHTML=`<b>Daily funding requirement is not available yet.</b> Run a fresh profitability scan and identify an economically positive candidate before estimating the full-day cost. <a href="https://cloud.vast.ai/billing/" target="_blank" rel="noopener noreferrer">Open Vast Billing ↗</a>`;
  }else{
    const gapValue=Number(gap||0); const bal=Number(b.balance_usd||0); const hasSpendPlan=req>0;
    $('vastFundingGap').textContent=gapValue>0?`Add ${currency(gapValue,4)}`:(hasSpendPlan?'24h covered':'No new rental planned');
    if(req>0){$('vastFundingCoverage').textContent=`Balance covers ${(bal/req).toFixed(2)} × the modeled 24h requirement`;} else {$('vastFundingCoverage').textContent='Target is scan-only and no current rental costs are projected';}
    if(gapValue>0){
      alert.className='funds-alert funds-alert-low';
      alert.innerHTML=`<b>Low Vast wallet balance.</b> Add at least <strong>${esc(currency(gapValue,4))}</strong> to cover the modeled next 24 hours at the selected target of ${esc(selectedTarget)} instance(s). This is an estimate, not a billing guarantee. <a href="https://cloud.vast.ai/billing/" target="_blank" rel="noopener noreferrer">Add more credits in Vast ↗</a>`;
    }else if(hasSpendPlan){
      alert.className='funds-alert funds-alert-good';
      alert.innerHTML=`<b>Wallet currently covers the modeled next 24 hours.</b> Available balance ${esc(currency(bal,4))}; estimated requirement ${esc(currency(req,4))}. Actual charges can differ. <a href="https://cloud.vast.ai/billing/" target="_blank" rel="noopener noreferrer">Manage Vast credits ↗</a>`;
    }else{
      alert.className='funds-alert funds-alert-info';
      alert.textContent='No additional rental funds are forecast while the target is scan-only. Current active rentals, if any, are still included in the daily requirement.';
    }
  }

  const candidate=s.best_candidate;
  $('expectedNetProfit').textContent=candidate?currency(candidate.expected_net_day_usd):'—';
  $('expectedNetInr').textContent=candidate?`${inrLabel(candidate.expected_net_day_usd)} /day`:'INR appears when a candidate is available';
  $('expectedNetNote').textContent=candidate?`${candidate.gpu_name||'GPU'} · ${candidate.coin_tag||'coin'}; conservative forecast for one candidate, not actual earnings.`:'No economically positive candidate in the latest saved scan.';
  const achieved=s.observed_net_24h_usd;
  $('achievedNetProfit').textContent=achieved===null||achieved===undefined?'Not available yet':currency(achieved);
  $('achievedNetInr').textContent=achieved===null||achieved===undefined?'INR unavailable without aligned observed net':`${inrLabel(achieved)} · rolling 24h`;
  $('achievedNetNote').textContent=s.observed_net_24h_note||'Requires a pool API field for actual trailing-24h USD earnings, not only a current USD/day run-rate.';
  ensureVastBalanceChecked(b);
}
async function api(url,opts={}){
  // Bound every dashboard request. A fetch with no deadline can leave wallet/progress
  // controls showing "Checking…" forever when a Gunicorn thread or database read stalls.
  const controller=new AbortController();
  const timeoutMs=Number(opts.timeoutMs)||12000;
  const timer=setTimeout(()=>controller.abort(),timeoutMs);
  const requestOpts={...opts,headers:{'Content-Type':'application/json',...(opts.headers||{})},signal:opts.signal||controller.signal};
  delete requestOpts.timeoutMs;
  try {
    const response=await fetch(url,requestOpts);
    const contentType=response.headers.get('content-type')||'unknown content type';
    const raw=await response.text();
    let data={};
    try { data=raw?JSON.parse(raw):{}; }
    catch(_){
      const hint=raw.replace(/<script[\s\S]*?<\/script>/gi,' ').replace(/<style[\s\S]*?<\/style>/gi,' ').replace(/<[^>]*>/g,' ').replace(/\s+/g,' ').trim().slice(0,180);
      const detail=hint?` Server response: ${hint}`:'';
      throw new Error(`HTTP ${response.status} returned ${contentType}, not JSON. This may be an application error or Render proxy/restart response.${detail}`);
    }
    if(response.status===401 && data.login_url){ window.location.assign(data.login_url); throw new Error(data.error||'Please sign in again.'); }
    if(!response.ok||data.ok===false) throw new Error(data.error||`Request failed (${response.status})`);
    return data;
  } catch(err) {
    if(err?.name==='AbortError') throw new Error(`Request timed out after ${Math.round(timeoutMs/1000)}s: ${url}`);
    throw err;
  } finally { clearTimeout(timer); }
}
let providerFallbackInFlight=false;
let dashboardRetryHandle=null;
let dashboardRetryAttempts=0;
function scheduleDashboardRetry(){
  if(dashboardRetryHandle) clearTimeout(dashboardRetryHandle);
  const delays=[2500,5000,10000,20000,30000];
  const delay=delays[Math.min(dashboardRetryAttempts,delays.length-1)];
  dashboardRetryAttempts=Math.min(dashboardRetryAttempts+1,delays.length-1);
  dashboardRetryHandle=setTimeout(()=>{
    dashboardRetryHandle=null;
    if(activeUiTask){scheduleDashboardRetry();return;}
    refresh();
  },delay);
}
function cancelDashboardRetry(){
  if(dashboardRetryHandle) clearTimeout(dashboardRetryHandle);
  dashboardRetryHandle=null;
  dashboardRetryAttempts=0;
}
function providerBadgeHtml(c={},b=null){
  // \"Configured\" only means an environment variable exists. Say so, and only show a green
  // \"connected\" once Vast has actually answered an authenticated account request.
  if(!(c.vast_configured&&c.whattomine_configured)) return '<span class="status-dot bad"></span><span>API setup incomplete</span>';
  if(b&&b.available) return '<span class="status-dot good"></span><span>Vast connected · WhatToMine key set</span>';
  if(b&&b.refreshing) return '<span class="status-dot idle"></span><span>Keys set · verifying Vast…</span>';
  return '<span class="status-dot bad"></span><span>Keys set · Vast account not verified</span>';
}
async function refreshProviderCardsWithoutDashboard(){
  // The Vast balance and Kryptex identity status are independent, in-memory endpoints.
  // Render them even if the PostgreSQL-backed /api/state request fails at first boot.
  if(providerFallbackInFlight)return;
  providerFallbackInFlight=true;
  try{
    const readStatuses=async()=>await Promise.allSettled([
      api('/api/vast/balance/status',{signal:AbortSignal.timeout(5000)}),
      api('/api/kryptex/status',{signal:AbortSignal.timeout(5000)})
    ]);
    let results=await readStatuses();
    const fallback=appState||{config:{},summary:{},funding:{},opportunities:[],instances:[],live_blockers:[],automation:{}};
    appState=fallback;
    try{
      // Database-independent: tells the user which keys are missing even when /api/state fails.
      const setup=await api('/api/setup/status',{signal:AbortSignal.timeout(5000)});
      appState.config=setup.config||appState.config||{}; appState.login=setup.login||appState.login;
      appState.live_blockers=setup.live_blockers||[]; appState.fx=setup.fx||appState.fx||{};
      appState.profiles_total=setup.profiles_total; appState.profiles_enabled=setup.profiles_enabled;
      const c=appState.config, ok=c.vast_configured&&c.whattomine_configured;
      $('apiStatus').innerHTML=providerBadgeHtml(c,results[0].status==='fulfilled'?results[0].value.vast_balance:null);
      renderFx(appState.fx); renderSafety(); renderSetupGaps(setup.setup_gaps);
    }catch(_){ /* keep whatever the failed dashboard load already rendered */ }
    if(results[0].status==='fulfilled')appState.vast_balance=results[0].value.vast_balance||{};
    if(results[1].status==='fulfilled'){
      appState.kryptex=results[1].value.kryptex||{};
      appState.kryptex_gpu_market=results[1].value.kryptex_gpu_market||{};
    }
    renderFinance(appState);
    renderKryptex(appState.kryptex||{},appState.kryptex_gpu_market||{});
    // If the read-only Vast balance lookup is underway, follow the in-memory status
    // briefly. Never leave static HTML's "Checking…" label behind after a failure.
    for(let i=0;i<12&&(appState.vast_balance?.refreshing||appState.kryptex?.refreshing);i++){
      await new Promise(resolve=>setTimeout(resolve,750));
      const statuses=await Promise.allSettled([
        api('/api/vast/balance/status',{signal:AbortSignal.timeout(4000)}),
        api('/api/kryptex/status',{signal:AbortSignal.timeout(4000)})
      ]);
      if(statuses[0].status==='fulfilled')appState.vast_balance=statuses[0].value.vast_balance||appState.vast_balance;
      if(statuses[1].status==='fulfilled'){
        appState.kryptex=statuses[1].value.kryptex||appState.kryptex;
        appState.kryptex_gpu_market=statuses[1].value.kryptex_gpu_market||appState.kryptex_gpu_market;
      }
      renderFinance(appState);
      renderKryptex(appState.kryptex||{},appState.kryptex_gpu_market||{});
    }
  }catch(_){
    // The primary /api/state error remains the visible diagnostic if even lightweight
    // status endpoints are unavailable.
  }finally{providerFallbackInFlight=false;}
}
async function refresh(options={}){
  // The wallet/provider cards use small in-memory endpoints. Start them alongside the
  // heavy database-backed /api/state call so a slow or unreachable database can never
  // leave the balance stuck on \"Checking...\". /api/state itself is time-boxed: without a
  // client timeout a stalled request kept the whole page loading forever.
  if(!appState||!appState.vast_balance) refreshProviderCardsWithoutDashboard();
  try{
    let state;
    try{ state=await api('/api/state',{signal:AbortSignal.timeout(25000)}); }
    catch(e){
      if(e&&(e.name==='TimeoutError'||e.name==='AbortError')) throw new Error('The dashboard data request timed out after 25s (the database is probably slow or unreachable). Wallet and provider cards below are still live.');
      throw e;
    }
    renderState(state);
    if(state.database_status?.available===false){
      setNotice(state.database_status.error||'Dashboard data is stale; keeping the last saved view while refreshing in the background.','info');
      scheduleDashboardRetry();
    }else{
      cancelDashboardRetry();
      if(!options.keepNotice) clearNotice();
    }
    return state;
  }catch(err){
    setNotice(`Could not load dashboard: ${err.message}`,'error');
    scheduleDashboardRetry();
    await refreshProviderCardsWithoutDashboard();
    if(options.throwOnError) throw err;
    return null;
  }
}
async function scanMarket(){
  const btn=$('scanBtn');
  try { await runUiTask('Scan GPU market',btn,async()=>{
    const clickAt=Date.now();
    let startResult=null;
    let startErrorAt=0;
    let progressObserved=false;
    showTask('Scan GPU market','Starting background scan','Sending a bounded request; progress polling starts immediately so a slow acknowledgement cannot freeze the UI.',5,'running');
    // Do not await the start acknowledgement before polling. The previous flow could
    // remain at 5% forever if a proxy/DB connection stalled the POST response.
    const startPromise=api('/api/scan',{method:'POST',body:JSON.stringify({}),signal:AbortSignal.timeout(8000)})
      .then(data=>{startResult={ok:true,data};})
      .catch(err=>{startResult={ok:false,error:err};startErrorAt=Date.now();});
    const deadline=Date.now()+15*60*1000; let terminal=null; let lastPollError='';
    while(Date.now()<deadline){
      try{
        const data=await api('/api/progress',{signal:AbortSignal.timeout(5000)}); const p=data.progress||{};
        const progressAt=Date.parse(p.updated_at||'');
        const belongsToThisClick=Number.isFinite(progressAt)&&progressAt>=clickAt-2000;
        const alreadyRunning=Boolean(startResult?.ok&&startResult.data?.started===false);
        // If the POST attaches to an existing scan, its progress timestamp can predate
        // this click. Accept its terminal state too; otherwise a completed scan is ignored
        // after the first running poll and the UI can spin forever at an old percentage.
        const attachedTerminal=alreadyRunning&&(p.status==='complete'||p.status==='failed');
        if(String(p.action||'').toLowerCase().includes('market scan')&&(belongsToThisClick||(alreadyRunning&&p.status==='running')||attachedTerminal)){
          progressObserved=true; lastPollError='';
          showTask('Scan GPU market',p.stage||'Scanning',p.detail||'Waiting for provider response',p.percent??10,p.status||'running');
          if(p.status==='failed'){const failure=new Error(p.detail||'Market scan failed. Check Event log and Render service logs.');failure.scanTerminalFailure=true;throw failure;}
          if(p.status==='complete'){terminal=p;break;}
        }else if(!progressObserved){
          const elapsed=Math.round((Date.now()-clickAt)/1000);
          const stage=startResult?.ok?'Waiting for scan worker':'Waiting for server acknowledgement';
          const detail=startResult?.ok
            ? (startResult.data?.started===false?'Another scan was reported as running; waiting for its current progress state.':'The server acknowledged the request but has not yet published a new progress state.')
            : `The scan has not yet reported a new progress state (${elapsed}s). No rental is started by this request.`;
          showTask('Scan GPU market',stage,detail,Math.min(15,5+Math.floor(elapsed/3)),'running');
        }
      }catch(err){
        lastPollError=err?.message||'Progress request failed';
        if(err?.scanTerminalFailure) throw err;
        const elapsed=Math.round((Date.now()-clickAt)/1000);
        showTask('Scan GPU market','Checking server progress',`Progress polling failed temporarily (${elapsed}s): ${lastPollError}`,Math.min(15,5+Math.floor(elapsed/3)),'running');
      }
      if(startResult && !startResult.ok && !terminal && !startErrorAt) startErrorAt=Date.now();
      if(startResult && !startResult.ok && !terminal && !progressObserved && Date.now()-startErrorAt>15000){
        throw new Error(`The scan-start request did not complete: ${startResult.error?.message||'server did not acknowledge the request'}. Check Render request logs for POST /api/scan; no rental is started by the scan button.`);
      }
      if(startResult?.ok && !terminal && !progressObserved && Date.now()-clickAt>20000){
        const reason=startResult.data?.started===false
          ? 'The server reported another scan as already running but did not return its active progress state.'
          : 'The server acknowledged the scan but did not publish fresh progress.';
        throw new Error(`${reason} Check Render request/service logs and /api/health for the database-pool state; no rental is started by scanning.`);
      }
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    await startPromise;
    if(!terminal) throw new Error('The scan has not finished after 15 minutes. Check Event log and Render service logs; no rental is started by scanning.');
    showTask('Scan GPU market','Loading saved scan results','Fetching persisted opportunities and provider diagnostics',94,'running');
    await refresh({keepNotice:true,throwOnError:true});
    const summary=appState?.last_scan_summary||{}; const errors=summary.errors||[];
    setNotice(`Scan completed: ${summary.offers_seen??0} offers, ${summary.coins_seen??0} coins and ${summary.qualified??0} fully qualified opportunities.${errors.length?` ${errors.length} calculation/API issue(s) were logged; inspect the offer audit and Event log.`:''}`,'success');gotoSection('opportunities');
    showTask('Scan GPU market','Complete',`${summary.offers_seen??0} offers · ${summary.coins_seen??0} coins · ${summary.qualified??0} qualified`,100,'complete');
  },'Requesting provider data and calculating conservative profitability'); }
  catch(err){setNotice(err.message,'error');}
}
function showModal(title,text,action){$('modalTitle').textContent=title;$('modalText').textContent=text;$('confirmModal').classList.remove('hidden');pendingModalAction=action;}
function closeModal(){$('confirmModal').classList.add('hidden');pendingModalAction=null;}
$('modalCancel').addEventListener('click',closeModal);$('modalConfirm').addEventListener('click',async()=>{const action=pendingModalAction;closeModal();if(action)await action();});
$('scanBtn').addEventListener('click',scanMarket);$('heroScanBtn').addEventListener('click',scanMarket);
$('saveKryptexConfigBtn').addEventListener('click',configureKryptex);$('refreshInstancesBtn').addEventListener('click',()=>runUiTask('Refresh instances',$('refreshInstancesBtn'),async()=>{showTask('Refresh instances','Requesting current provider instances', 'Loading live Vast instance state and local managed history',30,'running'); await refresh({keepNotice:true,throwOnError:true}); showTask('Refresh instances','Complete','Instance table and counters updated',100,'complete');},'Loading provider and local instance history').catch(err=>setNotice(err.message,'error')));$('refreshEventsBtn').addEventListener('click',()=>runUiTask('Refresh event log',$('refreshEventsBtn'),async()=>{showTask('Refresh event log','Loading audit events','Fetching latest saved events and rendering the event table',35,'running'); await refresh({keepNotice:true,throwOnError:true}); showTask('Refresh event log','Complete','Audit event table updated',100,'complete');},'Loading the latest audit trail').catch(err=>setNotice(err.message,'error')));$('saveAutomationBtn').addEventListener('click',saveAutomation);$('deployTargetBtn').addEventListener('click',deployTarget);$('refreshKryptexBtn').addEventListener('click',refreshKryptex);
$('decisionFilter').addEventListener('change',renderOpportunities);$('searchFilter').addEventListener('input',renderOpportunities);
 
$('targetInstances').addEventListener('input',()=>{updateTargetPreview();if(appState)renderFinance(appState);});
$('targetInstances').addEventListener('change',()=>{updateTargetPreview();if(appState)renderFinance(appState);});
$('refreshFxBtn').addEventListener('click',async()=>{const b=$('refreshFxBtn');b.disabled=true;const prior=b.textContent;b.textContent='Refreshing USD/INR…';try{const data=await api('/api/fx/refresh',{method:'POST',body:JSON.stringify({})});if(appState)appState.fx=data.fx||{};renderFx(data.fx||{});document.querySelectorAll('.metric-fx').forEach(x=>{if(x.id==='metricNetInr'){const best=appState?.summary?.best_candidate;x.textContent=best?`${inrLabel(best.expected_net_day_usd)} /day · display conversion only`:'INR appears here when a qualified estimate exists';}else{const sm=appState?.summary||{};x.textContent=`${inrLabel(sm.current_hourly_cost_usd||0)}/hr · ${inrLabel(sm.projected_current_daily_rent_usd||0)}/day`;}});if(appState)renderState(appState);setNotice(data.fx?.refresh_throttled?`The FX provider was checked recently. Keeping the current ${data.fx?.available?'fresh':'unavailable'} quote; try again in about ${Math.ceil((data.fx?.retry_after_seconds||60)/60)} minute(s).`:data.fx?.available?'USD/INR reference refreshed. USD remains the accounting base; this does not alter profit gates.':(data.fx?.error||'A fresh USD/INR quote is unavailable.'),data.fx?.available&&!data.fx?.refresh_throttled?'success':'info');}catch(err){setNotice(err.message,'error');}finally{b.disabled=false;if(!appState?.fx?.available)b.textContent=prior;}});
$('refreshVastBalanceBtn').addEventListener('click',refreshVastBalance);
document.querySelectorAll('.nav[data-section]').forEach(b=>b.addEventListener('click',()=>gotoSection(b.dataset.section)));
document.querySelectorAll('[data-goto]').forEach(b=>b.addEventListener('click',()=>gotoSection(b.dataset.goto)));
refresh();
async function pollBackgroundScanProgress(){
  if(activeUiTask||!appState?.automation?.auto_scan_enabled)return;
  try{const result=await api('/api/progress');const p=result.progress||{};if(String(p.action||'').toLowerCase().includes('market scan')){appState.ui_progress=p;renderScanDiagnostics(appState);}}catch(_){}
}
setInterval(()=>{if(!activeUiTask) refresh();},30000);
setInterval(pollBackgroundScanProgress,4000);
