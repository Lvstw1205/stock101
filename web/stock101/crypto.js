'use strict';
const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token='',state={},products=[],page=['overview','live','learning'].includes(location.hash.slice(1))?location.hash.slice(1):'overview',generation=0,latest=null,paperPortfolio=null;
const fmt=(v,n=2)=>v==null?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:n}),pct=v=>v==null?'—':fmt(Number(v)*100)+'%';
const selected=()=>$('#product').value,version=()=>$('#version').value||'multi_strategy_v5';
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;setTimeout(()=>$('#toast').hidden=true,6000);}
async function api(path,body){
 const controller=body?null:new AbortController(),timer=controller?setTimeout(()=>controller.abort(),18000):null;
 try{const r=await fetch(body?'/api/stock101':'/api/stock101/crypto/'+path,{method:body?'POST':'GET',headers:{'X-AIRI-Token':token,'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{signal:controller.signal})});const d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;}
 catch(e){if(e.name==='AbortError')throw Error('상태 조회가 지연되어 다시 연결합니다.');throw e;}
 finally{if(timer!==null)clearTimeout(timer);}
}
async function act(action,extra={}){return api('',{action:'crypto_'+action,...extra});}
function metric(label,value,note=''){return `<div class="card"><small>${esc(label)}</small><div class="metric">${esc(value)}</div><small>${esc(note)}</small></div>`;}
function paperMarkLabel(p){
 if(!p)return '평가 조회 중';
 if(p.mark_status==='PARTIAL')return `일부 지난 시세 포함 · 나머지 시세 ${paperStamp(p.fresh_mark_as_of)}`;
 return `${p.mark_status==='STALE'?'지난 평가값':'평가 시각'} · ${paperStamp(p.mark_as_of)}`;
}
function paperSuspensionNote(){
 const rows=state.cad_plan?.activity?.suspended_products||[];if(!rows.length)return '';
 return `<p class="notice warn"><strong>${rows.map(r=>esc(r.product)).join(', ')} · 거래소 시세 없음</strong><br>보유 수량과 원가는 유지합니다. 총자산·평가손익·그래프에 해당 코인의 지난 평가액이 포함됩니다. 다른 종목의 분석·매도 관리는 실행 중이며, 전체 자산의 시세를 확인할 때까지 신규 매수는 대기합니다.</p>`;
}
function paperBalanceCards(p,d){
 const total=p?Number(p.equity)-Number(p.initial):null,ratio=p&&Number(p.initial)>0?total/Number(p.initial):null;
 const stamp=paperMarkLabel(p);
 return `<div class="card account-total-pnl"><small>계좌 전체 평가손익 · ${p?.mark_status==='PARTIAL'?'일부 지난 시세 포함':'누적'}</small><div class="metric ${profitTone(total)}">${p?profitMoney(total)+' CAD':'조회 중'}</div><strong class="${profitTone(total)}">${p?profitPercent(ratio):'—'}</strong><small>누적 가상 자금 대비 · 추가 매도 비용 전</small></div>`
  +metric(p?.mark_status==='PARTIAL'?'총 가상 자산 · 참고값':'총 가상 자산',p?fmt(p.equity)+' CAD':'조회 중','현금 + 보유 코인 평가액 · '+stamp)
  +metric('누적 가상 자금',fmt(p?.initial??d.account.initial)+' CAD','원금 + 지급된 보너스 · 수익과 구분')
  +metric('사용 가능한 가상 현금',p?fmt(p.cash)+' CAD':'조회 중','총자산에 이미 포함된 현금');
}
function paperPnlExplanation(p){
 if(!p)return '<p class="small">총자산과 손익을 같은 시점의 기록으로 확인하고 있습니다.</p>';
 const total=Number(p.equity)-Number(p.initial);
 return `<section class="card account-reconciliation" aria-label="전체 손익 계산"><div class="section-top"><h2>확정 손익과 전체 손익은 달라요</h2><span class="pill">모두 CAD · 누적</span></div><div class="pnl-equation"><div><small>매도 확정 순손익</small><strong class="${profitTone(p.realized)}">${profitMoney(p.realized)}</strong><small>지금까지 판 코인 · 매수·매도 비용 반영</small></div><span aria-hidden="true">+</span><div><small>보유 코인 평가손익</small><strong class="${profitTone(p.unrealized)}">${profitMoney(p.unrealized)}</strong><small>아직 보유한 코인 · 추가 매도 비용 전</small></div><span aria-hidden="true">=</span><div><small>계좌 전체 평가손익</small><strong class="${profitTone(total)}">${profitMoney(total)}</strong><small>두 손익의 합계</small></div></div><p class="pnl-balance-equation">누적 가상 자금 ${fmt(p.initial)} + 전체 평가손익 (${profitMoney(total)}) = 총 가상 자산 <strong>${fmt(p.equity)} CAD</strong></p><p class="small">매도에서 이익이 나도 보유 코인의 손실이 더 크면 계좌 전체는 손실입니다. ${p.mark_status==='PARTIAL'?'거래소 시세가 없는 코인은 지난 평가액을 포함한 참고값이며 현재 청산 가능 금액이 아닙니다.':'보유 평가액은 현재 매도 호가를 CAD로 환산한 값입니다.'} 이미 반영된 거래·환전 비용을 다시 빼지 않으며, 앞으로 팔 때의 추가 수수료·체결 비용은 아직 포함하지 않습니다. 표시액은 소수 둘째 자리로 반올림합니다.</p><p class="small">${esc(paperMarkLabel(p))}</p></section>`;
}
function jsonDialog(title,data){$('#detail-title').textContent=title;$('#detail-text').textContent=typeof data==='string'?data:JSON.stringify(data,null,2);$('#detail').showModal();}
function bind(id,fn){const node=$(id);if(node)node.onclick=async()=>{node.disabled=true;try{await fn();}catch(e){toast(liveReason(e.message));}finally{node.disabled=false;}};}
function updateHeader(){
 const connected=state.health?.state==='HEALTHY'&&Date.now()/1000-(state.health.checked_at||0)<60;
 document.body.classList.toggle('live-mode',!!state.live?.running);if($('#live-global-label'))$('#live-global-label').textContent=state.live?.running?'실제매매 · 내 Coinbase 자금 사용':page==='live'?'실제매매 · 시작 전 확인':state.mode==='BACKTEST'?'과거 데이터 테스트 · 가상 자금':'모의매매 · 가상 자금 사용';
 const modeName=state.live?.running?'LIVE · 실제 돈으로 매매 중':page==='live'?'LIVE · 시작 전 확인':state.mode==='BACKTEST'?'과거 데이터 테스트':'모의매매 · 가상 자금';
 $('#status').innerHTML=`<span class="pill">${modeName}</span><span class="pill ${connected?'ok':'warn'}">시세 ${connected?'연결됨':'확인 중'}</span><span class="pill">${state.live?.running?'실제매매: 실행 중':'실제매매: 미실행'}</span>`;
 document.querySelectorAll('[data-mode]').forEach(x=>x.classList.toggle('selected',page==='live'?x.dataset.mode==='LIVE':x.dataset.mode===state.mode));
 const previous=version();$('#version').innerHTML=(state.versions||[]).filter(v=>page!=='research'||!['historical_plan_v2','multi_strategy_v3','multi_strategy_v4','multi_strategy_v5'].includes(v.id)).map(v=>`<option value="${esc(v.id)}">${esc(v.id)}</option>`).join('');if([...$('#version').options].some(v=>v.value===previous))$('#version').value=previous;
}
let refreshInFlight=null,portfolioInFlight=null;
async function refreshPaperPortfolio(){
 if(portfolioInFlight)return portfolioInFlight;
 portfolioInFlight=(async()=>{
  try{paperPortfolio=(await api('portfolio')).portfolios.find(p=>p.currency==='CAD')||null;}catch{paperPortfolio=null;}
  if(page==='overview')updateExecutionStatus();
 })();
 try{return await portfolioInFlight;}finally{portfolioInFlight=null;}
}
async function refreshState(){
 if(refreshInFlight)return refreshInFlight;
 refreshInFlight=(async()=>{
  state=await api('state?view=summary');if(page==='live')updateLiveRuntime();updateHeader();updateExecutionStatus();
  if(page==='overview')void refreshPaperPortfolio();
 })();
 try{return await refreshInFlight;}finally{refreshInFlight=null;}
}
function fillProducts(){const old=selected(),q=$('#filter').value.toUpperCase();const filtered=products.filter(p=>p.product_id.includes(q));$('#product').innerHTML='<option value="">거래쌍 선택</option>'+filtered.map(p=>`<option value="${esc(p.product_id)}">${esc(p.product_id)}${p.tradable?'':' · unavailable'}</option>`).join('');if(filtered.some(p=>p.product_id===old))$('#product').value=old;}
async function navigate(next=page){page=next;const gen=++generation;updatePageTitle();document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===page));$('#content').innerHTML='<div class="card empty">현재 기록을 불러오는 중…</div>';try{await refreshState();if(gen!==generation)return;if(page==='overview')await overview(gen);else if(page==='market')await market(gen);else if(page==='journal')journal();else if(page==='agents')await agentsPage(gen);else if(page==='learning')await learningPage(gen);else if(page==='quant')await quantPage(gen);else if(page==='calendar')await refreshProfitCalendar(gen);else if(page==='research')await research(gen);else if(page==='analytics')await analytics(gen);else if(page==='live')livePage();else if(page==='connection')connectionPage();else riskPage();}catch(e){if(gen===generation)$('#content').innerHTML=`<div class="notice warn">${esc(e.message)} · 주문을 실행하지 않았습니다.</div>`;}}
async function overview(gen){
 const d=state.cad_plan;if(!d){$('#content').innerHTML='<div class="notice">예산 계획을 불러오지 못했습니다. 새로고침해 주세요.</div>';return;}
 const labels={STOPPED:'확인 중지',SCANNING:'여러 코인 확인 중',WAITING_FOR_VERIFIED_FX:'환전 비용을 확인할 수 없어 대기',WAITING_FOR_NET_PROFIT_EVIDENCE:'수익 기준 대기',RUNNING:'여러 코인을 자동으로 확인하고 있어요',NO_ELIGIBLE_PRODUCTS:'거래 가능한 코인 없음'};
 $('#content').innerHTML=`<div id="account-run-strip"></div><div class="grid account-summary" id="plan-balances">${paperBalanceCards(paperPortfolio,d)}</div><div id="paper-pnl-explanation">${paperPnlExplanation(paperPortfolio)}</div>${typeof PortfolioCharts!=='undefined'?PortfolioCharts.shell():''}<section class="card" id="paper-holdings-card"><h2>보유 중인 가상 코인</h2><div class="tablewrap" id="paper-positions"></div><p class="small">매수 직후에는 수수료와 매수·매도 가격 차이 때문에 평가손익이 마이너스일 수 있습니다. AIRI가 추세·저항선·되돌림을 확인해 보유, 일부 익절, 전량 매도를 판단합니다. 플러스라는 이유만으로 바로 팔지는 않습니다. 평가손익은 매수 비용을 포함하며, 추가 매도 비용은 체결 때 반영됩니다. 확정 손익은 매도가 체결된 뒤 기록됩니다.</p></section><div id="execution-status" aria-live="polite"></div><div class="practice-card"><div class="section-top"><div><span class="overline">여러 코인 · 하나의 CAD 예산</span><h2>내 자동매매 계획</h2></div><span class="pill">모의매매 전용</span></div><p id="paper-next-funding"><strong>총 원금 1,000 CAD · 정기 추가 납입 없음</strong></p><p class="small">원금은 한 번만 총 1,000 CAD로 배정됩니다. 기존 보유 코인과 거래 기록을 유지합니다. 누적 실현 순이익 200 CAD 이상이면 가상 현금 20 CAD를 한 번만 추가합니다.</p><p>${d.execution_strategies?.length||0}개 전략이 여러 코인을 비교합니다. 주간 BTC 정기 매수와 가격 조건에 따른 매수가 같은 예산을 사용합니다.</p><div class="notice"><strong>실제 시세로 작동하는 모의매매입니다.</strong><br>매수·매도 수수료 각 0.6%, 체결 가격 차이 각 0.05%, USD/CAD 환전 비용 각 0.25%를 가정합니다. 환율은 캐나다 중앙은행의 일일 기준값입니다. 실제 수수료·체결 환율과 다를 수 있습니다. 전략 목표는 실제 수익 예측이나 수익 보장이 아닙니다. 손실 방어 매도는 손해가 나더라도 실행될 수 있습니다.</div><div class="actions"><button id="plan-refresh">상태 새로고침</button></div><details><summary>현재 확인 범위와 안전 한도</summary><p>현재 자동 탐색 대상: ${d.scan.eligible}개 · 환전 경로 미지원 제외 표시: ${d.scan.excluded_fx}개</p><p>확인 시각: ${d.scan.checked_at?new Date(d.scan.checked_at*1000).toLocaleString():'아직 확인 전'}</p><p>모든 코인이 하나의 CAD 가상 계좌를 공유합니다. 원금 1,000 CAD 기준 매수액은 근거에 따라 유동적으로 계산하며 신규 주문당 최대 60 CAD, 코인별 최대 100 CAD, 전체 투자 최대 600 CAD, 현금 최소 400 CAD입니다. 실제 한도는 운용 중인 총자산에 따라 조정됩니다. 거래소 최소 주문액보다 작으면 거래하지 않습니다.</p><p>정기 매수를 제외한 신규 매수는 목표 도달 시 비용 차감 수익 0.5% 이상을 요구합니다. 시간봉과 별도로 최대 350일의 완료 일봉을 수집해 200일선과 메이어 배수를 계산합니다. 거래 가능한 USD·CAD 코인 전체를 순환 분석하며 보유·진입 근접 코인을 우선 재평가합니다. 공개 목록과 실제 계정의 거래 가능 목록은 다를 수 있습니다. 이전 10,000 단위 단일 코인 연습 기록은 별도 보관됩니다.</p></details></div><div class="card"><h2>가상 자금 추가 기록</h2><div class="tablewrap"><table><thead><tr><th>반영 시각</th><th>추가 금액</th><th>종류</th></tr></thead><tbody>${d.contributions.map(x=>`<tr><td>${new Date(x.created*1000).toLocaleString()}</td><td>+${fmt(x.amount)} CAD</td><td>${x.code==='FIXED_PRINCIPAL_SET'?'원금 1,000 CAD로 변경':x.period===0?'이전 시작 자금':'이전 가상 자금'}</td></tr>`).join('')}</tbody></table></div></div>`;
 updateExecutionStatus();
 if(typeof PortfolioCharts!=='undefined')PortfolioCharts.mount(paperPortfolio);
 bind('#multi-toggle',async()=>{await act(state.cad_plan.running||state.cad_plan.activity?.recovery?.requested?'multi_stop':'multi_start');await navigate('overview');});bind('#plan-refresh',()=>navigate('overview'));
}
function positionsTable(rows){if(!rows.length)return '<p class="small">현재 보유한 가상 코인이 없습니다. 조건에 맞는 코인을 찾으면 여기에 표시됩니다.</p>';return `<table><thead><tr><th>코인</th><th>수량</th><th>평균 매입원가 (CAD)<small>매수 비용 포함</small></th><th>평가 단가 (CAD)<small>현재 매도 호가 환산</small></th><th>평가액 (CAD)</th><th>보유 평가손익 (CAD)<small>추가 매도 비용 전</small></th></tr></thead><tbody>${rows.map(p=>`<tr><td>${esc(p.product)}<small>${esc(p.entry_plan?.strategy_name||'기존 매매 계획')}</small></td><td>${fmt(p.quantity,8)}</td><td>${fmt(Number(p.cost)/Number(p.quantity),6)}</td><td>${fmt(p.price,6)}${p.mark_status==='STALE'?`<small>지난 시세 · ${esc(paperStamp(p.mark_as_of))}</small>`:''}</td><td>${fmt(p.market_value)}</td><td class="${profitTone(p.unrealized)}">${profitMoney(p.unrealized)}</td></tr>`).join('')}</tbody></table>`;}
function chart(points,key,label='완료 1시간봉'){if(!points?.length)return '';const values=points.map(p=>Number(p[key])),lo=Math.min(...values),hi=Math.max(...values),path=values.map((v,i)=>`${i?'L':'M'}${(i/(values.length-1||1)*800).toFixed(1)},${(160-(v-lo)/(hi-lo||1)*145).toFixed(1)}`).join(' ');return `<svg class="chart" viewBox="0 0 800 180" role="img" aria-label="Historical ${esc(key)} chart"><path d="${path}" fill="none" stroke="#6389d8" stroke-width="2"/><text x="5" y="177" fill="#7c8ba2" font-size="11">${esc(fmt(lo))} – ${esc(fmt(hi))} · ${esc(label)}</text></svg>`;}
async function market(gen){if(!selected()){$('#content').innerHTML='<div class="card empty">분석할 Coinbase 거래쌍을 선택하세요.</div>';return;}const a=await api('analysis?product='+encodeURIComponent(selected())+'&version='+encodeURIComponent(version()));if(gen!==generation)return;latest=a;const s=a.analysis;
 $('#content').innerHTML=`<div class="grid">${metric(a.product.product_id,fmt(a.book.mid),'CAD 환산 가격 · 모의 FX 기준')}${metric('Market regime',s.regime.name,'휴리스틱 confidence '+pct(s.regime.confidence))}${metric('Strategy synthesis',s.decision,s.conflict?'전략 충돌 · 진입 보류':'다중 근거 평가')}${metric('Spread',fmt(a.book.spread_bps,4)+' bps','Exchange time: '+new Date(a.book.timestamp*1000).toLocaleTimeString())}</div><div class="card"><h2>완료된 1시간 봉 · ${esc(selected())}</h2>${chart(a.bars,'close')}</div><div class="split"><div class="card"><h2>전략별 판단</h2>${s.strategy_candidates?strategyCandidates(s.strategy_candidates):''}${s.signals.map(x=>`<div class="kv"><div><strong>${esc(x.strategy)}</strong><div class="small">${esc(x.reason)}</div></div><span>${esc(x.decision)} · ${pct(x.strength)}</span></div>`).join('')}</div><div class="card"><h2>판단 근거</h2>${Object.entries(s.indicators||{}).map(([k,v])=>`<div class="kv"><span>${esc(k)}</span><span>${fmt(v,5)}</span></div>`).join('')}<p>${esc(s.reason_codes.join(' · '))}</p><div class="notice">분석은 주문이 아닙니다. 아래 기능은 같은 위험 검증 경로를 거쳐 PAPER 결정을 기록하며, 조건이 맞으면 가상 주문이 발생할 수 있습니다.</div><div class="actions"><button id="evaluate" class="primary">전략 평가 및 PAPER 결정 기록</button></div></div></div>`;bind('#evaluate',async()=>{const r=await act('evaluate',{product:selected(),version:version()});jsonDialog(r.decision_id||'Paper result',r);await refreshState();});}
function journal(){const rows=state.journal||[];$('#content').innerHTML=`<div class="card"><h2>Trade Journal · immutable decision chain</h2><div class="tablewrap"><table><thead><tr><th>DECISION / TIME</th><th>PRODUCT</th><th>DECISION</th><th>STRATEGY</th><th>REGIME</th><th>REQUESTED</th><th>APPROVED</th><th>RISK</th><th>STATUS</th></tr></thead><tbody>${rows.map(r=>`<tr><td><button data-explain="${esc(r.decision_id)}" data-record-id="${esc(r.id)}">${esc(r.decision_id)}</button><br><small>${new Date(r.timestamp*1000).toLocaleString()}</small></td><td>${esc(r.product)}</td><td>${esc(r.analysis.decision)}</td><td>${esc(r.analysis.plan?.strategy_name||r.strategy_version)}</td><td>${esc(r.analysis.regime.name)}</td><td>${fmt(r.requested_order.notional)}</td><td>${fmt(r.risk.approved_notional)}</td><td>${esc(r.risk.status)}</td><td>${esc(r.status)}</td></tr>`).join('')}</tbody></table></div>${rows.length?'':'<p class="empty">아직 결정 기록이 없습니다. 과거 거래나 설명을 만들어 표시하지 않습니다.</p>'}</div>`;document.querySelectorAll('[data-explain]').forEach(b=>b.onclick=async()=>{try{const d=await api('explain?id='+encodeURIComponent(b.dataset.explain)+'&record_id='+encodeURIComponent(b.dataset.recordId));jsonDialog('AIRI · '+b.dataset.explain,d.explanation+'\n\n'+JSON.stringify(d.record,null,2));}catch(e){toast(liveReason(e.message));}});}
async function legacyResearch(gen){const r=await api('research');if(gen!==generation)return;$('#content').innerHTML=`<div class="split"><div class="card"><h2>Backtest · current discovered product</h2><p class="small">최대 300개의 최근 완료된 1시간 봉. 종가 판단 → 다음 시가 체결. 수수료 60 bps, 슬리피지 5 bps, 가정 스프레드 10 bps. 생존편향과 실제 호가 이력은 해결되지 않았습니다.</p><div class="actions"><button id="run-backtest" class="primary">BACKTEST 실행</button></div><div id="backtest-result"></div></div><div class="card"><h2>Immutable strategy experiment</h2><p class="small">새 버전을 만들며 기존 버전은 변경하지 않습니다. LIVE 승격 기능은 없습니다.</p><div class="formgrid"><label>이름<input id="candidate-name" value="balanced_candidate" maxlength="50"></label><label>진입 RSI<input id="rsi-entry" type="number" min="10" max="45" value="32"></label><label>부분 익절 수익률<input id="take-profit" type="number" min="0.02" max="1" step="0.01" value="0.15"></label></div><div class="actions"><button id="candidate">후보 버전 저장</button><button id="posttrade">완료된 거래의 사후 분석</button></div><p class="small">1h / 24h / 7d / 30d 관찰. 기간이 지나고 충분한 실제 봉이 있는 경우만 기록합니다.</p>${r.experiments.map(x=>`<div class="kv"><span>${esc(x.version)}</span><small>${esc(x.status)}</small></div>`).join('')}</div></div><div class="card tablewrap"><h2>Version comparison · research runs</h2><table><thead><tr><th>VERSION / PRODUCT</th><th>RETURN</th><th>DRAWDOWN</th><th>SHARPE</th><th>WIN RATE</th><th>PROFIT FACTOR</th><th>FEES</th><th>TURNOVER</th><th>TRADES</th></tr></thead><tbody>${r.backtests.map(x=>`<tr><td>${esc(x.result.strategy_version)} / ${esc(x.product)}</td><td>${pct(x.result.total_return)}</td><td>${pct(x.result.max_drawdown)}</td><td>${fmt(x.result.sharpe)}</td><td>${pct(x.result.win_rate)}</td><td>${fmt(x.result.profit_factor)}</td><td>${fmt(x.result.fees)}</td><td>${fmt(x.result.turnover)}</td><td>${x.result.trade_count}</td></tr>`).join('')}</tbody></table></div><div class="card"><h2>Post-trade observations</h2>${r.post_trade.length?'<pre>'+esc(JSON.stringify(r.post_trade,null,2))+'</pre>':'<p class="empty">완료된 관찰 구간이 없습니다.</p>'}</div>`;
 bind('#run-backtest',async()=>{const d=await act('backtest',{product:selected(),version:version()});$('#backtest-result').innerHTML=chart(d.equity,'equity')+`<div class="kv"><span>Return</span><strong>${pct(d.total_return)}</strong></div><div class="kv"><span>Max drawdown</span><strong>${pct(d.max_drawdown)}</strong></div><div class="kv"><span>Benchmark (buy & hold)</span><strong>${pct(d.benchmark_return)}</strong></div>`;const {equity,...summary}=d;jsonDialog('Backtest result',summary);});bind('#candidate',async()=>{await act('experiment',{config:{name:$('#candidate-name').value,rsi_entry:Number($('#rsi-entry').value),profit_take:Number($('#take-profit').value)}});await navigate();});bind('#posttrade',async()=>{const d=await act('post_trade');jsonDialog('Post-trade observations',d);});}
async function analytics(gen){const d=await api('analytics');if(gen!==generation)return;$('#content').innerHTML=`<div class="grid">${Object.entries(d.risk_interventions).map(([k,v])=>metric('Risk Engine · '+k,String(v))).join('')}</div><div class="card"><h2>Strategy × regime × currency</h2><p class="small">${esc(d.note)} · ${d.sample_size} simulated fills</p>${Object.entries(d.strategy_regime_performance).map(([k,v])=>`<div class="kv"><span>${esc(k)}</span><span>P&L ${fmt(v.realized)} · Fees ${fmt(v.fees)} · ${v.fills} fills</span></div>`).join('')||'<p class="empty">표시할 체결 데이터가 없습니다.</p>'}</div><div class="card"><h2>Portfolio equity observations</h2>${[...new Set(d.portfolio_snapshots.map(x=>x.currency))].map(currency=>`<h3>${esc(currency)}</h3>${chart(d.portfolio_snapshots.filter(x=>x.currency===currency).reverse(),'equity')}`).join('')}</div>`;}
function riskPage(){$('#content').innerHTML=`<div class="split"><div class="card"><h2>손실 관찰 기준과 주문 한도</h2><p class="small">모의매매는 손실률·연속 손실을 경고로 기록하며 계속 검증합니다. 주문 금액·현금·보유 한도는 계속 적용됩니다. 실제매매는 손실 한도 도달 시 중지합니다.</p>${Object.entries(state.limits).map(([k,v])=>`<div class="kv"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}</div><div class="card"><h2>System health</h2><pre>${esc(JSON.stringify({health:state.health,websocket:state.websocket,halted:state.halted,halt_reason:state.halt_reason,transport:state.transport},null,2))}</pre><h3>실거래 준비 상태</h3><p>실제 자동매매 화면에서 계정 권한, 실제 잔액, 거래 가능한 코인과 수수료를 확인하세요. 모의·실제 주문은 서로 다른 원장을 사용합니다.</p><p class="small">실거래 키는 Mac 연결 창에서 등록합니다. 출금·송금은 지원하지 않습니다. 이 Mac에서 AIRI가 실행되고 인터넷이 연결돼 있어야 감시와 주문 처리가 작동합니다. 모의매매는 이전 실행 요청이 있으면 재시작 후 원장·시세 검증을 거쳐 자동 복구합니다. 직접 중지한 경우에는 재개하지 않습니다. 실제매매는 로컬 시작 확인이 필요합니다.</p></div></div><div class="card"><h2>System events</h2><pre>${esc(JSON.stringify(state.events,null,2))}</pre></div>`;}
$('#close').onclick=()=>$('#detail').close();$('#filter').oninput=fillProducts;$('#product').onchange=()=>navigate();$('#version').onchange=()=>navigate();bind('#refresh',async()=>{products=(await api('products')).products;fillProducts();await navigate();});bind('#halt',async()=>{await act('halt');await navigate();toast('거래를 중지했습니다. 시세·기록 조회는 계속 사용할 수 있습니다.');});document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>navigate(b.dataset.page));document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=async()=>{try{if(b.dataset.mode==='LIVE'){await act('disarm');await navigate('live');return;}await act('mode',{mode:b.dataset.mode});await navigate(b.dataset.mode==='BACKTEST'?'research':'overview');}catch(e){toast(liveReason(e.message));}});
(async()=>{try{token=(await (await fetch('/api/bootstrap')).json()).token;await refreshState();await navigate();try{products=(await api('products')).products;fillProducts();await refreshState();}catch(e){toast('Coinbase 공개 시세 연결 실패: '+e.message);fillProducts();}}catch(e){$('#content').innerHTML='<div class="notice warn">'+esc(e.message)+'</div>';}})();
setInterval(async()=>{if(document.hidden||!token)return;try{await refreshState();if(page==='research'&&$('#validation-results'))await refreshValidation();if(page==='calendar')await refreshProfitCalendar(generation);if(page==='agents')await refreshAgents();if(page==='quant')await refreshQuant();if(page==='learning')await learningPage(generation);if(page==='overview'&&typeof PortfolioCharts!=='undefined')void PortfolioCharts.refresh();}catch{updateExecutionStatus(true);updateLiveRuntime(true);}},5000);

setInterval(async()=>{if(document.hidden||page!=='market'||!selected()||$('#detail').open)return;const gen=generation;try{await market(gen);}catch(e){if(gen===generation)toast('시세 업데이트 실패: '+e.message);}},30000);

function connectionPage(){
 $('#content').innerHTML=`<div class="card"><h2>내 Coinbase 계정 연결</h2><p>키를 Mac 연결 창에 직접 붙여넣거나 JSON 파일을 선택하면 됩니다. 키를 채팅에 보내거나 이 화면에 붙여넣지 마세요.</p><div class="stack"><div class="notice"><strong>1. 키 준비</strong><br>Coinbase의 API Key Name과 Private Key를 준비하세요. JSON 파일은 없어도 됩니다.</div><div class="notice"><strong>2. Mac 연결 창에서 입력</strong><br>아래 버튼을 누른 뒤 키 이름과 비밀키를 붙여넣으세요. 키는 Mac 키체인에 저장됩니다.</div><div class="notice"><strong>3. 연결 확인</strong><br>작은 연결 창에 ‘연결 완료’가 표시되면 이 화면에서 확인 버튼을 누르세요.</div></div><div class="actions"><button id="setup-connection" class="primary">Coinbase 연결하기</button><button id="connection-check">연결 확인</button></div><div id="connection-result" class="notice">내 계정 연결은 아직 확인하지 않았습니다.</div><p class="small">조회 전용 연결입니다. 실제 돈을 사용하거나 실제 매매를 켜지 않습니다. 모의매매 자금은 따로 유지됩니다.</p></div>`;
 bind('#setup-connection',async()=>{await act('connection_setup');$('#connection-result').textContent='Mac에 열린 연결 창에 키 이름과 비밀키를 붙여넣으세요. JSON 파일을 선택해도 됩니다.';});
 bind('#connection-check',async()=>{try{const d=await act('connection_check');$('#connection-result').textContent='✓ 내 Coinbase 계정이 연결됐습니다. 조회 전용 연결입니다. 자동매매 상태는 화면 상단에 표시됩니다.';}catch(e){const errors={READ_ONLY_CREDENTIALS_NOT_CONFIGURED:'먼저 ‘Coinbase 연결하기’을 눌러 키를 등록해 주세요.',KEYCHAIN_ACCESS_DENIED_OR_LOCKED:'Mac 키체인이 잠겨 있거나 접근을 허용하지 않았습니다. Mac의 안내를 확인해 주세요.',VIEW_ONLY_KEY_REQUIRED:'조회 전용 키가 필요합니다. View만 켠 키를 등록해 주세요.',AUTHENTICATION_FAILED:'Coinbase가 키를 확인하지 못했습니다. 등록한 키가 유효한지 확인해 주세요.',ACCESS_DENIED:'Coinbase 접근이 거절됐습니다. 키의 권한과 IP 허용 목록을 확인해 주세요.'};$('#connection-result').textContent=errors[e.message]||'아직 연결을 확인하지 못했습니다. 파일 등록과 인터넷 연결을 확인한 뒤 다시 눌러주세요.';}});
}

function livePage(){
 const live=state.live||{},cfg=live.config||{},ready=live.readiness||{},summary=live.summary;
 $('#content').innerHTML=`<section class="live-card"><span class="overline">LIVE · 내 실제 돈</span><h2 id="live-runtime-state">${live.running?'실제 자동매매 실행 중':'실제 자동매매 꺼짐'}</h2><p id="live-next-step" class="notice"></p><p>${live.running?'지정가 IOC 주문으로 거래하며, 체결은 Coinbase에서 확인해 기록합니다.':'설정 저장과 연결 확인만으로 주문이 나가지는 않습니다. 아래 시작 확인을 직접 완료해야 실행됩니다.'}</p><div class="grid" id="live-balances">${metric('현재 총 예산 한도',fmt(live.budget_cap_cad??1000)+' CAD')}${metric('실제 체결 기록',String(live.fill_count||0)+'건')}${metric('정기 추가 납입','없음','원금 1,000 CAD · 기존 잔액 범위 내 사용')}${metric('확정 손익 · CAD 참고값',summary?fmt(summary.realized_cad)+' CAD':'체결 없음','실제 환전 체결액과 다를 수 있음')}</div><div class="actions"><button id="live-preflight" class="primary">연결·잔액·수수료 확인</button><button id="live-stop" class="danger">실제매매 중지</button><button id="live-reconcile">미확정 주문 확인</button></div><div id="live-check-result">${liveChecks(ready)}</div><p class="small" id="live-current-reason"></p><p class="small" id="live-heartbeat"></p></section>
 <div id="live-performance-bonus">${performanceBonusPanel(live.performance_bonus,'LIVE')}</div>${executionStrategyPanel(live,'LIVE')}<div class="split"><section class="card"><h2>1. 계정과 거래 자금</h2><p>실거래용 API 키에는 <strong>View + Trade</strong>가 필요합니다. <strong>Transfer는 꺼두세요.</strong></p><button id="live-key">Mac에서 실거래 키 등록</button><p class="small">열리는 Mac 창에서 ‘실거래용 키 등록’을 선택하고 입력하세요. 키 등록으로 실제매매가 켜지지는 않습니다.</p><p>Coinbase에 이미 입금된 자금만 사용합니다. 은행 자동이체나 환전 주문은 실행하지 않습니다.</p><div class="notice">총 원금 1,000 CAD · 추가 납입 없음. 실제매매의 비용 차감 누적 실현이익이 200 CAD 이상이면 보너스 한도 20 CAD를 한 번 추가합니다. 실제 보너스 자금도 Coinbase에 입금돼 있어야 사용할 수 있습니다. 지원되는 거래 통화가 있어야 주문할 수 있습니다. 계정별 거래 가능 여부를 확인하고, 다른 코인은 예산을 공유합니다.</div></section>
 <section class="card"><h2>2. 예산과 비용 기준</h2><label>실제로 입금한 거래 통화<select id="live-quote"><option value="CAD">CAD</option><option value="USD">USD</option><option value="USDC">USDC</option></select></label><label>목표 도달 시 최소 비용 차감 수익 (%) · 정기 매수 제외<input id="live-net" type="number" min="0.5" max="20" step="0.1" value="${Number(cfg.minimum_net||'.005')*100}"></label><label>환전 비용 여유 · 편도 (%)<input id="live-fx" type="number" min="0" max="5" step="0.05" value="${Number(cfg.fx_cost_bps||'25')/100}"></label><details><summary>위험 한도 조정 · 선택 사항</summary><p class="small">기본 한도보다 보수적으로 조정할 수 있습니다. 현금 보유 비율은 높일 수 있습니다.</p><div class="formgrid">${liveRiskFields(cfg)}</div></details><label class="check"><input id="live-cost-confirm" type="checkbox">실제 계좌 수수료 외에 위 환전 비용을 반영하며, 전략 목표는 수익 보장이 아니며, 주간 BTC 정기 매수는 별도 비용 상한을 적용함을 확인했습니다.</label><button id="live-save">설정 저장 · 아직 주문하지 않음</button><p class="small">매수액 유동 조절 · 주문·코인당 최대 10%, 전체 투자·하루 신규 매수 최대 60%, 현금 최소 40%, 일일 손실 중지 3%, 최대 낙폭 중지 10%. 잔액·거래소 최소 주문액도 다시 검사합니다.</p></section></div>
 <section class="card live-start"><h2>3. 실제 자동매매 시작</h2><p>시작하면 모의매매를 중지하고 실제 자금으로 자동 주문합니다. 손실이 발생할 수 있으며 손실 방어 매도도 실행됩니다.</p><label class="check"><input id="live-confirm" type="checkbox">실제 돈으로 주문하며 손실 가능성과 위 예산·비용·위험 한도를 확인했습니다.</label><label>아래 문구를 직접 입력하세요<input id="live-phrase" autocomplete="off" placeholder="실제 돈으로 자동매매 시작"></label><div class="actions"><button id="live-start" class="danger" ${live.running?'disabled':''}>실제 자동매매 시작</button></div></section>
 <section class="card tablewrap"><h2>실제 주문·체결 기록</h2><p class="small">모의 기록은 이 표에 포함되지 않습니다. 주문 결과가 불확실하면 신규 주문을 멈추고 거래소와 대조합니다.</p><table><thead><tr><th>시간 / 코인</th><th>구분</th><th>체결 수량</th><th>수수료</th><th>상태</th></tr></thead><tbody id="live-orders">${liveOrderRows(live)}</tbody></table></section><section class="card"><h2>이 프로그램이 실제로 보유한 코인</h2><div id="live-positions" class="tablewrap"></div><p class="small">원래 계좌에 있던 다른 자산은 이 프로그램의 보유량에 포함하지 않습니다. CAD는 참고 환산액입니다.</p></section><section class="card"><h2>지금 확인 중인 코인</h2><p class="small">새 주문이 없을 때도 판단 시각과 대기 이유를 확인할 수 있습니다.</p><div id="live-decisions"></div></section>`;
 $('#live-quote').value=cfg.quote||'USDC';updateLiveRuntime();
 bind('#live-preflight',async()=>{const d=await act('live_status');state.live.readiness=d;updateLiveRuntime();});
 bind('#live-key',async()=>{await act('connection_setup');toast('Mac 창에서 ‘실거래용 키 등록’을 선택하세요.');});
 bind('#live-save',async()=>{await act('live_configure',{quote:$('#live-quote').value,minimum_net:String(Number($('#live-net').value)/100),fx_cost_bps:String(Number($('#live-fx').value)*100),costs_confirmed:$('#live-cost-confirm').checked,enable_live:true,risk_limits:Object.fromEntries([...document.querySelectorAll('[data-live-risk]')].map(x=>[x.dataset.liveRisk,String(Number(x.value)/100)]))});toast('설정을 저장했습니다. 실제매매는 아직 꺼져 있습니다.');await navigate('live');});
 bind('#live-start',async()=>{try{await act('live_start',{confirmation:$('#live-phrase').value,confirmed:$('#live-confirm').checked});await navigate('live');}catch(e){await refreshState();throw e;}});
 bind('#live-stop',async()=>{await act('live_stop');await navigate('live');});
 bind('#live-reconcile',async()=>{await act('live_reconcile');await navigate('live');});
}
function liveRiskFields(cfg){
 const limits=cfg.risk_limits||{},fields=[['single_order','주문당 최대',10],['asset_exposure','코인당 최대',10],['total_exposure','전체 투자 최대',60],['daily_deployment','하루 신규 매수 최대',60],['daily_loss','하루 손실 중지',3],['drawdown','최대 낙폭 중지',10],['cash_reserve','현금 보유 최소',40]];
 return fields.map(([key,label,base])=>`<label>${label} (%)<input type="number" data-live-risk="${key}" value="${Number(limits[key]??base/100)*100}" min="${key==='cash_reserve'?40:0.1}" max="${key==='cash_reserve'?99:base}" step="0.1"></label>`).join('');
}
function livePositionRows(live){
 const rows=live.summary?.positions||live.positions||[];
 return `<table><thead><tr><th>코인</th><th>보유 수량</th><th>평균 매수 비용</th><th>CAD 평가액</th></tr></thead><tbody>${rows.filter(p=>Number(p.quantity)>0).map(p=>`<tr><td>${esc(p.product)}<small>${esc(p.entry_plan?.strategy_name||'기존 매매 계획')}</small></td><td>${fmt(p.quantity,8)}</td><td>${fmt(Number(p.cost)/Number(p.quantity),8)} ${esc(p.quote)}</td><td>${fmt(p.value_cad)} CAD</td></tr>`).join('')||'<tr><td colspan="4">이 프로그램이 매수한 실제 코인이 없습니다.</td></tr>'}</tbody></table>`;
}
function liveOrderRows(live){
 const labels={PENDING:'체결 확인 중',SUBMITTING:'주문 결과 확인 중',SETTLED:'확정',REJECTED:'주문 거절',FILLED:'체결 완료',CANCELLED:'잔량 취소',EXPIRED:'만료',FAILED:'거절',OPEN:'거래소 처리 중'};
 return (live.orders||[]).map(x=>`<tr><td>${new Date(x.submitted_at*1000).toLocaleString()}<br>${esc(x.product)}</td><td>${x.side==='BUY'?'매수':'매도'}</td><td>${fmt(x.filled_size,8)}</td><td>${fmt(x.fees,8)} ${esc(x.quote)}</td><td>${esc(labels[x.exchange_status||x.state]||x.exchange_status||x.state)}</td></tr>`).join('')||'<tr><td colspan="5">아직 실제 주문이 없습니다.</td></tr>';
}
function updateLiveRuntime(offline=false){
 const heading=$('#live-runtime-state');if(!heading)return;const live=state.live||{},summary=live.summary;
 const fresh=live.last_completed&&Date.now()/1000-live.last_completed<150;
 heading.textContent=offline?'서버 연결 끊김 · 실행 여부 확인 필요':live.running?(fresh?'실제 자동매매 실행 중':'실제 자동매매 시작됨 · 상태 확인 중'):'실제 자동매매 꺼짐';
 const ready=live.readiness||{};$('#live-next-step').textContent=live.running?'실제 주문이 활성화됐습니다. 아래에서 처리 기록을 확인하세요.':!ready.checked_at?'연결 확인 버튼을 누르면 지금 필요한 준비를 안내합니다.':ready.error?liveReason(ready.error):!ready.permissions?.trade?'조회 연결은 완료됐습니다. 실거래용 View + Trade 키를 등록해야 합니다.':!ready.funded_currencies?.length?'실거래 키는 연결됐습니다. 사용 가능한 거래 자금이 아직 없습니다.':!ready.configured?'거래 통화와 비용 기준을 저장한 뒤 연결 상태를 다시 확인하세요.':ready.ready?'준비가 확인됐습니다. 아래에서 직접 실제 자동매매를 시작할 수 있습니다.':'아래 준비 항목 중 확인이 필요한 내용을 완료하세요.';
 const values=document.querySelectorAll('#live-balances .metric');if(values.length===4){values[0].textContent=fmt(live.budget_cap_cad??1000)+' CAD';values[1].textContent=String(live.fill_count||0)+'건';values[2].textContent='없음';values[3].textContent=summary?fmt(summary.realized_cad)+' CAD':'체결 없음';}
 if($('#live-performance-bonus')){const open=$('#bonus-details-live')?.open;$('#live-performance-bonus').innerHTML=performanceBonusPanel(live.performance_bonus,'LIVE');if(open&&$('#bonus-details-live'))$('#bonus-details-live').open=true;}
 $('#live-current-reason').textContent=live.reason?'상태: '+liveReason(live.reason):'';
 $('#live-heartbeat').textContent=live.last_completed?'마지막 처리 완료: '+new Date(live.last_completed*1000).toLocaleString()+' · 화면은 15초마다 갱신됩니다.':'실행 전입니다. 시작한 뒤 판단·주문·체결 시각이 여기에 표시됩니다.';
 $('#live-check-result').innerHTML=liveChecks(live.readiness||{});$('#live-orders').innerHTML=liveOrderRows(live);$('#live-positions').innerHTML=livePositionRows(live);
 $('#live-decisions').innerHTML=(live.decisions||[]).slice(0,6).map(d=>`<div class="kv"><span><button data-live-explain="${esc(d.decision_id)}">${esc(d.product)} · 상세</button><br><small>${new Date(d.timestamp*1000).toLocaleString()}</small></span><span>${esc(d.status==='SUBMITTED'?((live.orders||[]).find(o=>o.decision_id===d.decision_id)?.state==='SETTLED'?'거래소 결과 확정':'실제 주문 전송 · 체결 확인 중'):d.status==='REJECTED'?'거래소 주문 거절':liveReason(d.reason||d.signal?.reason_codes?.[0]||d.risk?.reason||d.status))}</span></div>`).join('')||'<p class="empty">아직 실거래 판단 기록이 없습니다.</p>';
 document.querySelectorAll('[data-live-explain]').forEach(b=>b.onclick=async()=>{try{const d=await api('explain?id='+encodeURIComponent(b.dataset.liveExplain));jsonDialog('실제 거래 판단 · '+b.dataset.liveExplain,d);}catch(e){toast(liveReason(e.message));}});
 const start=$('#live-start');if(start)start.disabled=offline||!!live.running||!live.readiness?.ready;
 const save=$('#live-save');if(save)save.disabled=!!live.running;
}

function liveReason(code){if(strategyReason(code))return strategyReason(code);return ({SUSPENDED_ASSET_ENTRY_HOLD:'거래소 시세가 없는 보유 종목이 있어 신규 매수 대기 · 다른 종목의 분석·매도 관리 계속',PRODUCT_NOT_FOUND:'거래소에서 이 종목의 시세를 제공하지 않음',LIVE_LEDGER_UNAVAILABLE:'실거래 기록을 읽지 못해 실제 주문을 차단했습니다. 기록 복구와 거래소 상태 확인이 필요합니다.',RISK_LIMIT_CANNOT_BE_WEAKENED:'위험 한도는 기본값보다 보수적으로만 변경할 수 있습니다.',PREVIEW_FEE_TOO_HIGH:'주문 미리보기 수수료가 허용 범위를 넘어 거래하지 않습니다.',READ_ONLY_CREDENTIALS_NOT_CONFIGURED:'등록된 키가 없습니다. Mac 연결 창에서 먼저 등록하세요.',VIEW_WITHOUT_TRANSFER_REQUIRED:'View는 켜고 Transfer는 끈 키가 필요합니다.',KEYCHAIN_ACCESS_DENIED_OR_LOCKED:'Mac 키체인 잠금 또는 접근 허용을 확인하세요.',AUTHENTICATION_FAILED:'Coinbase가 API 키를 확인하지 못했습니다.',ACCESS_DENIED:'Coinbase에서 접근을 거절했습니다. 권한과 IP 허용 목록을 확인하세요.',NET_EDGE_TOO_SMALL:'모든 비용을 뺀 모델 수익이 기준보다 낮아 대기',NET_EDGE_LOST_AFTER_PREVIEW:'실제 주문 비용을 확인한 결과 수익 기준에 못 미쳐 대기',NET_EXIT_COST_UNVERIFIED:'매수 원가와 환전 비용을 확인할 수 없어 익절 보류',NET_EXIT_PROFIT_TOO_SMALL:'주문 비용을 다시 확인한 결과 익절 기준을 충족하지 않아 대기',INSUFFICIENT_CONFIRMATION:'매수 근거가 부족해 대기',STRATEGY_DISAGREEMENT:'전략의 판단이 달라 대기',VOLATILITY_VETO:'가격 변동이 커서 진입 보류',RETAIN_POSITION:'현재 보유 유지',ORDER_BELOW_EXCHANGE_MINIMUM_OR_TOO_LARGE:'예산·수량이 거래소 주문 한도에 맞지 않아 대기',EXPOSURE_OR_CASH_LIMIT:'예산 또는 보유 한도에 도달해 대기',LOSS_LIMIT:'손실 한도에 도달해 신규 주문 중지',LIVE_LEDGER_POSITION_MISMATCH:'체결 기록과 보유 원장이 달라 주문을 중지했습니다.',LIVE_POSITION_BALANCE_MISMATCH:'거래소 잔액과 이 프로그램의 보유량이 달라 주문을 중지했습니다.',ORDER_SETTLEMENT_DELAYED:'거래소 체결 확정이 지연돼 신규 주문을 중지했습니다.',STOP_AND_RECONCILE_FIRST:'실제매매 중지 후 미확정 주문 확인을 마치세요.',INVALID_COST_POLICY:'수익 기준은 0.5~20%, 환전 비용은 0~5%로 입력하세요.',STALE_MARKET_DATA:'최신 시세를 확인할 수 없어 중지했습니다.',EXCHANGE_REQUEST_UNCERTAIN:'거래소 응답을 확인하지 못했습니다. 신규 주문을 멈추고 기존 주문을 확인합니다.',STARTUP_CONFIRMATION_REQUIRED:'앱을 다시 시작해 직접 시작 확인이 필요합니다.',TRADE_PERMISSION_REQUIRED:'현재 키는 조회 전용입니다. 실거래용 View + Trade 키를 등록하세요.',LIVE_REQUIREMENTS_NOT_MET:'연결·잔액·수수료 확인에서 미완료 항목을 확인하세요.',LOCAL_LIVE_CONFIRMATION_REQUIRED:'체크박스와 시작 확인 문구를 입력하세요.',EXPLICIT_LIVE_CONFIGURATION_REQUIRED:'비용 확인 체크박스에 동의한 뒤 설정을 저장하세요.',USER_STOP:'사용자가 중지했습니다.',ORDER_STATUS_UNVERIFIED:'주문 결과 확인 중입니다. 중복 주문을 보내지 않습니다.',STOP_AND_RECONCILE_LIVE_FIRST:'실거래 중지와 주문 확인을 마친 뒤 모의매매를 시작하세요.',SELECT_FUNDING_CURRENCY:'실제 입금한 거래 통화를 선택하세요.',INSUFFICIENT_FUNDS:'거래에 필요한 실제 잔액이 부족합니다.'})[code]||code;}
function liveChecks(d){return `<div class="stack">${(d.checks||[]).map(x=>`<div class="kv"><span>${esc(x.name)}</span><strong>${x.ok?'✓ 확인됨':'확인 필요'}</strong></div>`).join('')||'<p>위 확인 버튼을 누르면 현재 계정 상태를 조회합니다.</p>'}</div>${d.fees?'<p>Coinbase 계정 수수료: 즉시 체결 '+pct(d.fees.taker)+' · 지정가 메이커 '+pct(d.fees.maker)+'</p>':''}${Array.isArray(d.funded_currencies)?'<p>사용 가능한 거래 자금: '+(d.funded_currencies.map(x=>fmt(x.available,6)+' '+esc(x.currency)).join(' · ')||'현재 없음')+'</p>':''}${d.eligible_currencies?'<p>계정에서 거래 가능한 통화: '+(Object.entries(d.eligible_currencies).filter(([,n])=>n>0).map(([q,n])=>esc(q)+' · '+n+'개 거래쌍').join(' / ')||'확인된 거래쌍 없음')+'</p>':''}${d.cad_markets?'<p>계정에서 확인된 CAD 직접 거래쌍: '+(d.cad_markets.map(esc).join(', ')||'없음')+'</p><p class="small">CAD 입금 지원과 코인별 CAD 주문장 지원은 다릅니다. 즉시 체결 지정가는 Taker 수수료로 계산합니다. 실제 보유한 USD·USDC를 쓰는 경우에도 설정한 왕복 환전 비용을 수익 기준에 반영합니다.</p>':''}${d.error?'<p class="notice warn">'+esc(liveReason(d.error))+'</p>':''}`;}

function updatePageTitle(){
 const titles={learning:['판단이 나아지는지, 기록으로 확인해요.','사전 예측과 실제 결과를 비교하고 매매 성적에 따라 배분을 조절합니다.'],quant:['새 전략을 만들고, 검증하고, 지켜봐요.','기존 자동매매와 함께 사용하는 전략 연구실입니다.'],agents:['AI 팀의 판단을 한눈에 확인해요.','차트·뉴스·심리 분석과 토론 과정을 실제 실행 기록으로 보여드립니다.'],calendar:['하루 수익을 달력으로 봐요.','매도가 확정된 순손익을 CAD와 %로 기록합니다.'],overview:['1,000 CAD로 시작하고, 계속 운용해요.','여러 코인을 하나의 예산으로 관리하는 모의매매 계획입니다.'],live:['실제 자동매매','계정 연결 → 설정 확인 → 직접 시작. 실제 주문은 별도 기록합니다.'],connection:['Coinbase 연결, 여기서 시작해요.','Mac 연결 창에 키를 입력한 뒤 연결 상태를 확인하세요.'],journal:['어떻게 매매했는지 확인해요.','아래 기록은 모두 가상 매매의 판단과 결과입니다.'],market:['코인의 현재 흐름을 살펴봐요.','분석 결과는 수익을 보장하지 않으며, 매매 조건이 맞지 않으면 기다립니다.'],research:['과거 시세로 전략을 시험해요.','과거 결과는 앞으로의 수익을 보장하지 않습니다.'],analytics:['연습 결과를 살펴봐요.','가상 매매의 성과와 위험 제한 내역입니다.'],risk:['안전 한도와 시스템 상태','현재 적용 중인 제한과 상세 진단 정보입니다.']};
 const t=titles[page]||titles.overview;document.querySelector('h1').textContent=t[0];$('.subtitle').textContent=t[1];$('#trade-settings').hidden=!['market'].includes(page);
}

let coverageQuery='';
const when=x=>x?new Date(x*1000).toLocaleTimeString():'아직 없음';
function strategyReason(code){return ({AGENTS_AVOID_ENTRY:'AI 팀의 비중 축소 판단 · 신규 매수 보류',AGENTS_NET_EXIT:'AI 팀의 매도 판단 · 비용 차감 순이익 확보',AGENTS_PARTIAL_NET_EXIT:'AI 팀의 비중 축소 판단 · 일부 순이익 확보',
 PROFIT_TREND_WEAKENED:'상승세 약화 · 비용 차감 이익을 현금으로 확보',PROFIT_PULLBACK_EXIT:'고점 대비 되돌림 · 남은 순이익 확보',PROFIT_RESISTANCE_PARTIAL:'저항선 부근 · 보유량 절반 익절 검토',
 VOLUME_BREAKOUT_BUY:'거래량과 추세가 확인된 고점 돌파',VOLUME_BREAKOUT_REQUIRED:'고점 돌파·거래량·ADX 조건 확인',VOLUME_HISTORY_REQUIRED:'거래량 이력 필요',BOLLINGER_REENTRY_BUY:'밴드 안 복귀와 RSI 회복 확인',BOLLINGER_REENTRY_REQUIRED:'횡보 구간에서 밴드·RSI 복귀 확인',FX_USDC_BUY:'미국 달러 할인과 비용 조건 충족',FX_DISCOUNT_REQUIRED:'USD/CAD 60일 평균 대비 2% 하락 조건 확인',FX_HISTORY_REQUIRED:'과거 환율 준비 중',CAD_FUNDING_ROUTE_REQUIRED:'CAD로 USDC를 살 수 있는 주문 경로 필요',USDC_PRICE_REFERENCE_REQUIRED:'USDC/USD 기준 가격을 확인 중',USDC_PEG_DEVIATION:'USDC의 달러 가격 이탈로 적립 보류',USDC_ONLY:'USDC 전용 전략',USDC_FX_STRATEGY_ONLY:'USDC는 환율 적립 전략으로 관리',

 SUPERTREND_BUY:'일봉 슈퍼트렌드 상승 전환 · 매수 검토',SUPERTREND_TURN_REQUIRED:'슈퍼트렌드가 하락에서 상승으로 바뀌는 일봉 마감 감시',
 HULL_BUY:'55일 Hull 평균 상승 · 추세 매수',HULL_UPTREND_REQUIRED:'55일 Hull 평균이 2일 전보다 높아지면 검토',
 ICHIMOKU_BUY:'전환선 > 기준선 · 종가 > 200 EMA 조건 충족',TK_TREND_REQUIRED:'전환선·기준선·200 EMA 상승 정렬 감시',
 MACD_TREND_BUY:'35 EMA > 195 EMA · MACD 0선 상향 돌파',MACD_ZERO_CROSS_REQUIRED:'상승 추세 안에서 MACD 0선 상향 돌파 감시',
 RSI_CROSS_BUY:'5 EMA 상향 교차 · 8시간 RSI 50 초과',RSI_EMA_CROSS_REQUIRED:'5·160 EMA 상향 교차와 8시간 RSI 50 초과를 함께 확인',RSI_8H_HISTORY_REQUIRED:'완료된 UTC 8시간봉 15개 이상 준비 중',
 RSI_REVERSION_BUY:'200 EMA 위 · RSI 25 미만 눌림목 매수',RSI_PULLBACK_REQUIRED:'상승 추세 안에서 RSI 25 미만 눌림목 감시',RSI_RECOVERY_EXIT:'RSI 70 회복 · 비용 차감 순익 확인 후 매도',
 REFERENCE_TREND_EXIT:'매수 전략의 추세 반전 · 보유분 청산',REFERENCE_PRICE_EXTENDED:'신호봉 기준 진입 범위를 벗어남 · 다음 조건 재평가',
 DAILY_REFERENCE_HISTORY_REQUIRED:'이 전략에 필요한 완료 일봉 수집 중',HOURLY_200_HISTORY_REQUIRED:'EMA 계산용 완료 시간봉 201개 이상 수집 중',
 ALT_ONLY:'BTC·ETH를 제외한 코인에 적용',BTC_REFERENCE_REQUIRED:'BTC의 최신 시세와 완료 일봉 확인 중',BTC_DIP_TRIGGER_REQUIRED:'BTC 급락·전일 하락·3일 연속 하락 감시',RED_DAY_WEEKLY_LIMIT:'이번 주차 BTC 하락 신호 매수 15회 완료',
 BTC_INTRADAY_DIP_BUY:'BTC 당일 급락 단계 도달 · 분할매수 검토',BTC_RED_DAY_BUY:'BTC 전일 시가 대비 3.3% 이상 하락 · 분할매수 검토',BTC_THREE_RED_BUY:'BTC 3일 연속 하락 · 분할매수 검토',
 DCA_WEEKLY_BUY:'이번 주 BTC 정기 매수 · 예산과 비용 확인',DCA_COST_LIMIT:'즉시 왕복 비용이 3%를 넘어 정기 매수 보류',
 INSTALLMENT_ALREADY_FILLED:'이번 회차 매수 완료 · 다음 회차 대기',BTC_ONLY:'BTC에만 적용하는 전략',
 DAILY_200_HISTORY_REQUIRED:'200일선 계산용 완료 일봉 수집 중',DAILY_HISTORY_REQUIRED:'전일 변동폭과 ATR 계산용 일봉 수집 중',HOURLY_HISTORY_REQUIRED:'완료 시간봉 수집 중',
 ABOVE_200MA_REQUIRED:'가격과 50일선이 200일선 위로 올라오면 검토',TREND_SUPPORT_BOUNCE_REQUIRED:'200일선 위 · 지지선 반등 또는 골든크로스 확인 중',MA200_SWING_BUY:'200일선 상승 추세와 반등 조건 충족',
 RANGE_NOT_ESTABLISHED:'횡보하는 지지·저항 구간 확인 중',RANGE_LOWER_BAND_REQUIRED:'박스권 하단 25% 구간 진입 감시',PRICE_RECOVERY_REQUIRED:'진입 구간에서 직전 가격 대비 반등 확인 중',RANGE_BUY:'박스권 하단 반등 · 상단을 목표로 매수',
 BREAKOUT_NET_SESSION_EXIT:'돌파 전략 다음 날 · 비용을 뺀 순이익 확인 후 청산',BREAKOUT_COST_RECOVERY_HOLD:'돌파 전략 비용 회수 대기 · 손실 방어선 감시',
 UTC_SESSION_OPEN_REQUIRED:'UTC 첫 1시간봉 마감 후 당일 시가 확정',BREAKOUT_PRICE_REQUIRED:'당일 시가 + 전일 변동폭의 절반 돌파 감시',BREAKOUT_BUY:'변동성 돌파 조건 충족',BREAKOUT_SESSION_EXIT:'UTC 날짜 변경 · 손익과 무관하게 돌파 포지션 청산',
 DIP_LIMIT_NOT_REACHED:'UTC 시가보다 10%·12.5%·15% 낮은 가격 감시',DIP_GRID_BUY:'급락 매수 단계 도달 · 지정 가격 이내 주문',
 MAYER_UNDER_08_REQUIRED:'가격 ÷ 200일선이 0.8 이하일 때 분할 매수',MAYER_ACCUMULATE:'메이어 배수 0.8 이하 · 이번 주 분할 매수',MAYER_PARTIAL_EXIT:'메이어 2.4 이상 · 보유량 절반 익절',MAYER_FINAL_EXIT:'메이어 2.4 이상 유지 · 나머지 보유량 익절',LONG_TERM_EXIT_TODAY_DONE:'오늘 분할 익절 완료 · 다음 UTC 날짜에 재평가',
 STRATEGY_HOLD:'매수한 전략의 청산 조건을 감시하며 보유',STRATEGY_STOP:'매수할 때 정한 손실 방어선 도달 · 매도',STRATEGY_TARGET:'목표 도달 · 비용을 뺀 이익 실현',PORTFOLIO_REBALANCE:'코인별 보유 한도 초과분 축소',
 OTHER_STRATEGY_OWNS_POSITION:'현재 보유분을 매수한 전략이 관리 중',TARGET_NET_TOO_SMALL:'목표에 도달해도 비용 차감 수익이 0.5% 미만',
 })[code];}
function strategyFacts(r){
 const f=r.facts||{},v=x=>fmt(x,4);
 if(r.strategy_id==='volume_breakout'&&f.adx!=null)return `직전 20봉 고점 ${v(f.breakout_high)} · 거래량 ${v(f.volume_ratio)}배 · ADX ${v(f.adx)}`;
 if(r.strategy_id==='bollinger_reentry'&&f.center!=null)return `밴드 하단 ${v(f.lower)} / 중심 ${v(f.center)} · RSI ${v(f.rsi)} · ADX ${v(f.adx)}`;
 if(r.strategy_id==='fx_usdc'&&f.rate!=null)return `1 USD = ${v(f.rate)} CAD · 60일 평균 ${v(f.mean60)} · 차이 ${pct(f.discount)} · ${esc(f.date)}`;
 if(r.strategy_id==='supertrend'&&f.supertrend!=null)return `슈퍼트렌드 ${v(f.supertrend)} · ${f.up?'상승':'하락'} · ATR 10 × 3`;
 if(r.strategy_id==='hull'&&f.hma!=null)return `HMA 55 ${v(f.hma)} · 2봉 전 ${v(f.hma_two_bars_ago)}`;
 if(r.strategy_id==='ichimoku'&&f.tenkan!=null)return `전환 ${v(f.tenkan)} · 기준 ${v(f.kijun)} · EMA 200 ${v(f.ema200)}`;
 if(r.strategy_id==='macd_trend'&&f.macd!=null)return `EMA 35 ${v(f.ema35)} / 195 ${v(f.ema195)} · MACD ${v(f.macd_previous)} → ${v(f.macd)}`;
 if(r.strategy_id==='rsi_cross'&&f.ema5!=null)return `EMA 5 ${v(f.ema5)} / 160 ${v(f.ema160)} · 완료 8시간 RSI ${f.rsi8h==null?'준비 중':v(f.rsi8h)}`;
 if(r.strategy_id==='rsi_reversion'&&f.rsi9!=null)return `RSI 9 ${v(f.rsi9)} · EMA 200 ${v(f.ema200)} · 진입 25 / 익절 검토 70`;
 if(r.strategy_id==='red_day'&&f.btc_price!=null)return `BTC 당일 ${f.btc_intraday_return==null?'시가 확인 중':pct(f.btc_intraday_return)} · 전일 ${pct(f.btc_daily_return)} · 3일 연속 하락 ${f.btc_three_red_days?'충족':'미충족'} · 주차 매수 ${f.weekly_events}/15회`;
 if(r.strategy_id==='dca_weekly')return `주 ${esc(f.weekly_cad||'2')} CAD · 수수료 포함 · 주문 한도 적용`;
 if(r.strategy_id==='ma200_swing'&&f.ma200)return `200일선 ${v(f.ma200)} · 50일선 ${v(f.ma50)} · ATR ${v(f.atr)} · 최근 골든크로스 ${f.recent_golden_cross?'있음':'없음'}`;
 if(r.strategy_id==='range'&&f.support)return `지지 ${v(f.support)} · 저항 ${v(f.resistance)} · 횡보 조건 ${f.flat?'충족':'미충족'}`;
 if(r.strategy_id==='breakout'&&f.trigger)return `UTC 시가 ${v(f.utc_open)} · 전일 고가−저가 ${v(f.prior_range)} · 돌파 가격 ${v(f.trigger)}`;
 if(r.strategy_id==='dip_grid'&&f.levels)return `기준 시가 ${v(f.anchor)} · 매수 가격 ${f.levels.map(v).join(' / ')}`;
 if(r.strategy_id==='mayer'&&f.multiple!=null)return `현재 배수 ${v(f.multiple)} · 200일선 ${v(f.ma200)}`;
 return '필요한 데이터가 준비되면 계산값이 표시됩니다.';
}
function strategyCandidates(rows){return `<div class="tablewrap"><table class="strategy-candidates"><thead><tr><th>전략</th><th>이번 판단과 계산 근거</th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>${esc(r.name)}</strong><small>${esc(({BUY:'매수 조건 충족',SELL:'매도 조건 충족',REDUCE:'보유 축소',HOLD:'보유',WAIT:'감시'})[r.decision]||r.decision)}</small></td><td>${esc(strategyReason(r.reason)||r.reason)}<small>${strategyFacts(r)}</small></td></tr>`).join('')}</tbody></table></div>`;}
function evaluationPanel(e,mode='PAPER'){
 if(!e)return '';
 const label=e.breached?'실패 기준 도달 · 폐기 검토':e.fresh?'손실 기준 이내':'최신 평가 대기';
 return `<section class="card" aria-label="운용 실패 기준"><div class="section-top"><h2>총손실 10% 이상이면 운용 실패</h2><span class="pill">${label}</span></div><p><strong>지금 전량 매도 시 예상 순손익 ${e.net_profit_cad==null?'확인 전':profitMoney(e.net_profit_cad)+' CAD'}</strong> · 기준 −100 CAD</p><p class="small">고정 원금 1,000 CAD 기준입니다. 보유 코인을 현재 호가에 처분할 때의 예상 수수료·환전 비용까지 반영하며, 보너스는 수익에서 뺍니다. ${e.valuation_at?'평가 시각 '+paperStamp(e.valuation_at)+(e.fresh?'':' · 지난 평가값'):''}</p>${e.breached?'<p class="notice warn">'+when(e.first_breach?.breached_at)+'에 실패 기준에 도달했습니다. 이후 회복돼도 이 기록은 남습니다.</p>':''}<p class="small">${mode==='PAPER'?'모의매매는 손실을 포함해 계속 기록합니다.':'실제매매의 기존 손실 한도와 시작 설정은 유지됩니다.'} 폐기 여부는 사용자가 결정합니다.</p></section>`;
}
function performanceBonusPanel(b,mode='PAPER'){
 if(!b)return '';
 const ratio=b.return_ratio==null?null:Number(b.return_ratio),progress=ratio==null?0:Math.max(0,Math.min(100,ratio/.2*100));
 const status=b.earned?'20 CAD 보너스 1회 적용':'목표 달성 전';
 return (mode==='LIVE'?dailyTargetPanel(b):'')+evaluationPanel(b.evaluation,mode)+`<section class="card performance-bonus" aria-label="성과 보너스"><div class="section-top"><div><span class="overline">${mode==='LIVE'?'실제매매':'모의매매'} 누적 매도 확정 손익으로 판단</span><h2>실현 순이익 200 CAD 달성 → 보너스 20 CAD</h2></div><span class="pill">${status}</span></div><p class="small">누적 매도 확정 순손익 · 보유 평가손익 제외 · 계좌 전체 손익과 다릅니다.</p><div class="bonus-numbers"><strong class="${profitTone(b.net_profit_cad)}">${profitMoney(b.net_profit_cad)} CAD <small>/ 200 CAD</small></strong><span>${pct(ratio)} / 원금의 20%</span></div><progress max="100" value="${progress}" aria-label="실현 순이익 목표 진행률"></progress><p class="small">${b.earned?'달성 시각 '+when(b.award.earned_at)+' · 한 번 적용 완료':'원금 1,000 CAD · 매주 추가 납입 없음 · 목표 미달 시 보너스 없음'}</p><details id="bonus-details-${mode.toLowerCase()}"><summary>보너스 계산 방식</summary><p>완료된 매도의 이익과 손실을 합산하고 매수·매도 수수료와 환전 비용을 차감합니다. 누적 실현 순이익이 200 CAD 이상일 때 20 CAD를 한 번만 추가합니다. 보유 코인의 평가이익, 입금, 보너스 자체는 실현이익으로 세지 않습니다.</p><p>${mode==='LIVE'?'실제 체결 기록에서 수수료와 설정한 환전 비용 여유를 차감한 CAD 기준입니다. 보너스는 사용 가능한 예산 한도이며, 은행 입금이나 환전은 자동 실행하지 않습니다.':'보너스는 가상 현금에 추가됩니다. 실제매매는 별도의 실제 실현이익으로 같은 조건을 충족해야 합니다.'}</p>${b.verified?'':'<p>체결 비용을 확인할 수 없어 보너스 판단을 보류하고 있습니다.</p>'}</details></section>`;
}
function strategySource(s){
 if(!s.source_url)return '';
 let url;try{url=new URL(s.source_url);if(url.protocol!=='https:'||!['www.youtube.com','koothaninfotech.com','www.bankofcanada.ca'].includes(url.hostname))return '';}catch{return '';}
 return `<details id="strategy-source-${esc(s.id)}"><summary>참고 자료와 적용 방식</summary><a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${url.hostname==='www.youtube.com'?'영상의 해당 구간 보기':'참고 원문 보기'} ↗</a><p class="small">${esc(s.adaptation||'')}</p></details>`;
}
function executionStrategyPanel(d,mode='PAPER'){
 const rules=d.execution_strategies||[];if(!rules.length)return '';
 return `<section class="card strategy-panel"><div class="section-top"><h2>적용된 실행 전략 · ${rules.length}개</h2><span class="pill ${d.running?'ok':''}">${d.running?'자동 판단에 적용 중':mode==='LIVE'?'실제매매 시작 시 적용':'시작하면 적용'}</span></div><p>거래량 돌파·볼린저 복귀·환율 USDC 적립을 포함합니다. 조건이 맞으면 같은 예산 안에서 매수하고, 매수한 전략의 보호 조건과 공통 익절 판단을 함께 적용합니다.</p>${profitPolicyCard()}<details id="strategy-catalog"><summary>${rules.length}개 전략의 조건·체결 보기</summary><div class="strategy-grid">${rules.map(s=>`<article><span class="overline">${esc(s.scope)} · ${esc(s.interval)}</span><h3>${esc(s.name)}</h3><p>${esc(s.rule)}</p>${strategySource(s)}${mode==='PAPER'?`<small>${s.scope==='BTC'?'BTC 조건 확인':'조건 확인 '+(s.assessed_assets||0)+'개'} · 매수 조건 충족 ${s.entry_matches||0}개<br>모의 체결 ${s.fills||0}회${s.last_fill_at?' · 최근 '+when(s.last_fill_at):''}</small>`:''}</article>`).join('')}</div></details><details id="strategy-policy"><summary>예산·비용·실행 방식</summary><p>신호가 겹치면 USDC 환율 → 메이어 → 거래량 돌파 → 볼린저 복귀 → 200일선 → 슈퍼트렌드 → 일목 → Hull → MACD → RSI 교차 → 눌림목 → 돌파 → 박스권 → BTC 하락 → 급락 → 정기 매수 순서입니다. 신선한 AI 후보 판단이 있으면 현재 매수 조건을 통과한 전략 중에서 진입 전략을 선택할 수 있습니다. 보유 후에는 매수 전략을 유지하며 공통 익절 판단이 현금 확보를 검토합니다.</p><p>영상 참고 전략의 목표는 신호봉 종가 + ATR 3배, 방어선은 −2배입니다. 추세가 반대로 바뀌면 손실이어도 청산할 수 있습니다. BTC 하락 신호 매수는 해당 코인의 최근 20일 고점을 목표로 하며, 20일 저점 아래 ATR 2배에서 방어합니다. 신호 하나를 여러 코인에서 중복 사용하지 않습니다.</p><p>총 원금 1,000 CAD를 ${rules.length}개 전략이 공유하며 추가 납입은 없습니다. BTC 정기 매수는 기존 예산 안에서 주 2 CAD를 사용합니다. 다른 매수액은 신호 근거·변동성·비용·검증 결과에 따라 매번 달라집니다. 주문·코인당 10%, 전체 투자·하루 신규 매수 60% 이내이며 현금 40%를 남깁니다. 목표 방어선까지 예상 손실은 주문당 총자산 0.5% 이내로 크기를 줄입니다. 갭·슬리피지로 실제 손실은 더 커질 수 있습니다. 정기 매수는 단기 수익 예측 대신 왕복 비용 3% 상한을 검사합니다. 나머지 신규 매수는 목표 가격 도달 시 비용 차감 수익이 0.5% 이상인지 검사합니다. 목표 가격은 수익 예측이나 보장이 아닙니다.</p><p>200일선은 완료 일봉으로 계산합니다. 급락 전략은 앱이 −10% / −12.5% / −15% 가격을 감시하다 IOC 주문을 보내며, 거래소에 상시 예약 주문을 걸어두지는 않습니다. 반등 목표는 7%, 최초 진입 기준 방어선은 −5%입니다. 방어선 도달 시 추가 매수보다 매도를 우선합니다.</p><p>BTC 적립·메이어 보유분은 메이어 2.4 이상이고 비용 차감 이익이 있을 때 절반, 다음 UTC 날짜에도 조건이 유지되면 나머지를 팝니다. 돌파 매매는 UTC 날짜 변경 시 손실이어도 청산합니다. 앱 중지·재시작·인터넷 단절 중에는 앱의 감시 주문이 실행되지 않습니다. 기존 위험 한도가 모든 전략보다 우선합니다.</p></details>${mode==='PAPER'?`<p class="small">일봉 준비 ${d.research?.daily_ready||0}개 · 200일선 계산 가능 ${d.research?.daily_ma200_ready||0}개 · 일봉 조회 재시도 ${Object.keys(d.research?.daily_errors||{}).length}개. 준비된 데이터가 있는 전략부터 판단합니다.</p>`:''}</section>`;
}
function strategyPlanView(data,pid){
 const r=data.assessment,p=r.plan,currency=esc(p.currency||'CAD'),rows=data.daily_chart||[],hourly=data.hourly_chart||[];
 const long=['dca_weekly','ma200_swing','mayer','supertrend','hull','ichimoku','red_day'].includes(p.strategy_id),history=long?rows:hourly,label=long?'완료 일봉':'완료 1시간봉';
 return `<button class="plan-close" aria-label="닫기">×</button><span class="overline">실행 전략 · ${esc(p.strategy_name)}</span><h2>${esc(pid)}의 매매 판단</h2><p>${esc(planReason(r))}</p><h3>${label} · ${history.length}개</h3>${history.length?chart(history,'close',label):'<p>차트를 수집 중입니다.</p>'}<div class="plan-levels"><div><small>현재 가격</small><strong>${fmt(p.current,6)} ${currency}</strong></div><div><small>전략 목표 가격</small><strong>${p.target==null?(p.strategy_id==='dca_weekly'?'메이어 2.4에서 검토':'조건 충족 후 설정'):fmt(p.target,6)+' '+currency}</strong></div><div><small>진입 당시 손실 방어선</small><strong>${p.invalidation==null?(['dca_weekly','mayer'].includes(p.strategy_id)?'장기 보유 · 전체 위험 한도 적용':'조건 충족 후 설정'):fmt(p.invalidation,6)+' '+currency}</strong></div></div><p>${p.net_center==null?(p.strategy_id==='dca_weekly'?'주 2 CAD 정기 매수: 단기 수익 예측 대신 예산과 비용 상한을 검사합니다.':'진입 조건이 충족되면 목표 가격과 비용 차감 수익을 계산합니다.'):`전략 목표에 도달한다면 비용 차감 수익 ${pct(p.net_center)}. 목표 도달을 예측한 확률이나 실제 손익이 아닙니다.`}</p>${p.position_net_exit!=null?`<p>지금 매도할 경우 추정 순손익률: <strong>${pct(p.position_net_exit)}</strong></p>`:''}${p.exit_at?`<p>정해진 청산 시각: ${new Date(p.exit_at*1000).toLocaleString()}</p>`:''}${profitManagementDetail(p.profit_management)}${sizingDetail(p.sizing,r)}<h3>${(r.strategies||[]).length}개 전략의 판단</h3>${strategyCandidates(r.strategies||[])}<p class="small">가격과 계산값 단위: ${currency}. 판단 시각 ${new Date(p.evaluated_at*1000).toLocaleString()}. 현재 보유분은 매수한 전략이 관리합니다. 신규 매수 조건이 충족돼도 예산·수수료·거래소 최소 주문액 검사를 통과해야 주문합니다.</p><h3>함께 확인한 뉴스</h3><div class="news-list">${(p.news?.articles||[]).map(a=>`<div>${newsLink(a)}</div>`).join('')||'<p>최근 연결된 뉴스가 없습니다.</p>'}</div>`;
}
function planReason(r){
 const p=r.plan;
 if(r.status==='MARKET_DATA_UNAVAILABLE')return '시세 조회 실패 · 해당 코인 재시도 대기';
 if(!['SIMULATED_FILLED','SIMULATED_PARTIAL_IOC'].includes(r.status)&&strategyReason((r.reason||'').split(', ')[0]))return strategyReason(r.reason.split(', ')[0]);
 if(!p)return ({ENTRY_PRICE_NOT_REACHED:'진입 가격 접근 감시',WAITING_FOR_PRICE_RECOVERY:'진입 구간에서 가격 회복 감시',REBUILD_AFTER_SUPPORT_BREAK:'지지선 재계산',REWARD_RISK_BELOW_1_2:'목표·방어선 거리 재검토',MANAGING_ENTRY_PLAN:'목표·손실 방어 가격 감시',HISTORY_REFRESH_PENDING:'새 시간봉 수집 중',ENTRY_COOLDOWN:'같은 코인 재진입 간격 적용',MARKET_DATA_UNAVAILABLE:'현재 주문장 확인 필요'})[r.reason||r.status]||liveReason(r.reason||r.status||'아직 분석 전');
 const money=n=>fmt(n,6)+' '+p.currency;
 if(['SIMULATED_FILLED','SIMULATED_PARTIAL_IOC'].includes(r.status))return '가상 주문 체결 · 거래 기록에 저장됨';
 const messages={
  NET_EDGE_TOO_SMALL:`비용 기준 가격 ${money(p.cost_entry_ceiling)} 이하에서 진입 재검토`,
  ENTRY_PRICE_NOT_REACHED:`진입 구간 ${money(p.entry_low)} ~ ${money(p.entry_high)} 접근 감시`,
  WAITING_FOR_PRICE_RECOVERY:'진입 구간 도달 · 직전 확인 가격보다 회복하면 진입 재검토',
  REBUILD_AFTER_SUPPORT_BREAK:`계획 기준선 ${money(p.invalidation)} 이탈 · 다음 차트로 지지선 재계산`,
  REWARD_RISK_BELOW_1_2:`목표까지 거리 / 손실 방어선까지 거리 ${fmt(p.reward_risk)}배 · 1.2배 이상 필요`,
  MANAGING_ENTRY_PLAN:`목표 ${money(p.target)} / 손실 방어선 ${money(p.invalidation)} 감시`,
  PLAN_ENTRY_REACHED:'진입 조건 도달 · 주문 비용과 예산 확인',
  PLAN_TARGET_REACHED:'목표 도달 · 비용 차감 후 이익 실현',
  PLAN_INVALIDATED:'진입 계획 무효화 · 손실 방어 매도',
  HISTORY_REFRESH_PENDING:'새 시간봉 수집 중 · 신규 진입 재계산',
  HARD_LIMIT_REDUCED:'예산 한도에 맞춰 주문 크기 축소',
  ENTRY_COOLDOWN:`같은 코인 재진입 가능 시각 ${when(p.next_entry_after)}`,
  QUOTE_SIZE_LIMIT:'계획 주문액이 거래소 최소 주문액보다 작음',
  BASE_SIZE_LIMIT:'계획 수량이 거래소 최소 수량보다 작음',
  EXPOSURE_OR_CASH_LIMIT:'공유 예산의 신규 매수 한도 소진',
  TRADE_FREQUENCY_LIMIT:'시간당 체결 한도 도달',
  WIDE_SPREAD:'매수·매도 호가 차이가 0.5% 이내로 좁혀지면 재검토'
 };
 return ({WAITING_FRESH_PRICE:'최근 가격 수신 대기',SCREEN_DATA_INVALID:'분석 자료 재확인',SCREEN_ANALYSIS_RETRY:'전략 계산 재시도',AGENTS_SELECTED_ENTRY:'AI가 진입 후보 선택'})[r.reason]||messages[r.reason]||liveReason(r.reason||r.status);
}
function newsLink(a){
 let url='';try{const u=new URL(a.url);if(u.protocol==='https:')url=u.href;}catch{}
 return `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a><small>${esc(a.source)} · ${new Date(a.published_at*1000).toLocaleString()}</small>`;
}
function coverageRows(){
 return (state.cad_plan?.coverage||[]).filter(r=>r.product.includes(coverageQuery.toUpperCase())).map(r=>`<tr><td>${esc(r.product)}</td><td>${r.history_ready?'시간봉 준비':r.error?'시간봉 확인 필요':'시간봉 수집 중'}<small>일봉 ${r.daily_bars||0}개</small></td><td>${r.assessed_at?when(r.assessed_at):'분석 대기'}<small>${r.screening_only?'후보 계산':'실행 호가 확인'}</small></td><td>${r.error?esc(({CANDLE_GAP_OR_DUPLICATE:'시간봉에 거래 공백 또는 중복 있음',STALE_CANDLES:'최근 완료 시간봉을 확인하지 못함',HISTORY_LESS_THAN_90_HOURS:'완료된 시간봉이 90개 미만',HISTORY_FETCH_FAILED:'차트 수집 재시도 중'})[r.error.reason]||r.error.reason):r.reason?esc(planReason({reason:r.reason})):''}</td></tr>`).join('')||'<tr><td colspan="4">해당 코인이 없습니다.</td></tr>';
}
function forecastChart(f,p){
 const rows=f.chart||[];if(rows.length<2)return '';
 const first=rows[0].at,last=f.as_of+f.horizon_hours*3600;
 const values=[...rows.map(x=>x.close),f.lower,f.upper,p?.invalidation,p?.target].filter(x=>Number.isFinite(x));
 const low=Math.min(...values)*.999,high=Math.max(...values)*1.001;
 const x=t=>30+(t-first)/(last-first)*630,y=v=>170-(v-low)/(high-low)*145;
 const points=rows.map(r=>`${x(r.at)},${y(r.close)}`).join(' '),right=x(last),origin=x(f.as_of);
 return `<svg class="forecast-chart" viewBox="0 0 700 210" role="img" aria-label="완료된 과거 차트와 앞으로 6시간의 예측 범위"><rect x="${origin}" y="10" width="${right-origin}" height="170" fill="#eef2fc"/><polyline points="${points}" fill="none" stroke="#2863df" stroke-width="2.5"/><polygon points="${origin},${y(f.reference)} ${right},${y(f.upper)} ${right},${y(f.lower)}" fill="#2863df" opacity=".16"/><line x1="${origin}" y1="${y(f.reference)}" x2="${right}" y2="${y(f.center)}" stroke="#2863df" stroke-dasharray="4 3"/><text x="30" y="200" fill="#52627a">과거 ${rows.length}시간</text><text x="${Math.max(430,origin-60)}" y="200" fill="#52627a">점선: 6시간 추정 / 음영: 오차 범위</text></svg>`;
}
async function showPlan(pid){
 try{
  const data=await api('plan?product='+encodeURIComponent(pid)),r=data.assessment,p=r?.plan,f=data.forecast;
  let dialog=$('#plan-dialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='plan-dialog';document.body.appendChild(dialog);}
  const currency=p?.currency||'CAD';
  if(data.strategy_bundle){dialog.innerHTML=strategyPlanView(data,pid);dialog.querySelector('.plan-close').onclick=()=>dialog.close();dialog.showModal();return;}
  dialog.innerHTML=`<button class="plan-close" aria-label="닫기">×</button><span class="overline">과거 차트 + 현재 가격 + 관련 뉴스</span><h2>${esc(pid)}의 매매 계획</h2><p>${esc(r?planReason(r):'차트 준비 완료 · 현재 호가 평가 대기')}</p>${forecastChart(f,p)}${p?`<div class="plan-levels"><div><small>현재 가격</small><strong>${fmt(p.current,6)} ${currency}</strong></div><div><small>목표 가격</small><strong>${fmt(p.target,6)} ${currency}</strong></div><div><small>손실 방어선</small><strong>${fmt(p.invalidation,6)} ${currency}</strong></div><div><small>비용 기준 진입 상한</small><strong>${fmt(p.cost_entry_ceiling,6)} ${currency}</strong></div></div><p>이 가격에서 진입해 추정 시나리오대로 움직일 경우 비용 차감 수익: <strong>${pct(p.net_center)}</strong><br><small>과거 예측 오차를 반영한 시나리오 범위 ${pct(p.net_low)} ~ ${pct(p.net_high)}. 실제 계좌 손익이 아닙니다.</small></p><p class="small">가격 재평가 ${when(p.evaluated_at)} · 과거 차트 기준 ${new Date(f.as_of*1000).toLocaleString()}</p><h3>이 코인 관련 뉴스</h3><div class="news-list">${(p.news?.articles||[]).map(a=>`<div>${newsLink(a)}${a.event_keywords.length?'<p class="small">사건 키워드 감지 · 신규 주문 크기 축소</p>':''}</div>`).join('')||'<p>최근 수집된 제목에서 연결되는 기사가 없습니다. 관련 뉴스가 없다는 뜻은 아닙니다.</p>'}</div><p class="small">뉴스 ${p.news?.status==='CURRENT'?'출처 최신 확인':'최신 수집 확인 필요'} · 제목의 키워드 분석이며 기사 내용의 사실 여부를 확정하지 않습니다.</p>`:''}<details><summary>예측 계산 근거</summary><p>${f.history_hours}개 완료 시간봉 · ${f.validation.samples}개 과거 6시간 구간을 순서대로 예측해 비교했습니다. 선택 모델: ${esc(f.model)}.</p><p>평균 예측 오차 ${pct(f.validation.mae_log_return)} · 가격 유지 가정의 오차 ${pct(f.validation.baseline_mae)}. 모델 선택에 쓴 기록이며 독립적인 수익 검증 결과는 아닙니다.</p></details>`;
  dialog.querySelector('.plan-close').onclick=()=>dialog.close();dialog.showModal();
 }catch(e){toast(e.message==='PLAN_NOT_READY'?'이 코인의 과거 차트를 수집하고 있습니다.':e.message);}
}
function paperDecisionRow(r){
 const p=r.plan;
 return `<tr><td><button class="plan-link" data-plan="${esc(r.product)}">${esc(r.product)} ↗</button><small>재평가 ${when(r.at)}</small></td><td>${p?fmt(p.current,6)+' '+esc(p.currency):'—'}</td><td class="plan-status"><strong>${esc(planReason(r))}</strong>${p?`<small>${esc(p.strategy_name||'기존 매매 계획')}${p.target!=null?' · 목표 '+fmt(p.target,6):''}${p.invalidation!=null?' / 방어 '+fmt(p.invalidation,6):''} ${esc(p.currency)}</small>${p.sizing?.fraction?`<small>매수 요청 비중 ${pct(p.sizing.fraction)} · 최종 예산 검사 적용</small>`:''}`:''}</td></tr>`;
}
function paperStopReason(code){
 if(code==='CONSECUTIVE_LOSSES')return '이전 연속 손실 중단 규칙이 작동했습니다. 기존 거래 기록은 보존됩니다.';
 if(code==='RISK_CALCULATION_FAILED')return '손실·예산 계산을 완료하지 못해 중지됐습니다. 계산 오류를 확인해야 합니다.';
 return ({USER_KILL_SWITCH:'중지 요청으로 멈췄습니다.',STARTUP_RECONCILIATION_REQUIRED:'앱 재시작 · 거래 기록과 시세 확인 중',STARTUP_HALTED:'앱 재시작 · 거래 기록과 시세 확인 중',PAPER_AUTO_RECOVERED:'거래 기록 확인 완료 · 모의매매 자동 재개',PAPER_STARTED:'자동 모의매매 시작',EXCHANGE_UNAVAILABLE:'Coinbase 시세 연결이 일시적으로 끊겼습니다.',EXECUTION_OR_DATABASE_FAILURE:'판단 또는 거래 기록 처리 중 프로그램 오류가 발생했습니다.',AUTOMATION_CYCLE_FAILED:'자동매매 처리 중 프로그램 오류가 발생했습니다.',PRODUCT_DISCOVERY_FAILED:'Coinbase의 코인 목록을 가져오지 못했습니다.',STALE_MARKET_DATA:'최신 호가를 확인하지 못해 중단했습니다.',PORTFOLIO_MARK_FAILED:'보유 자산의 현재 가치를 확인하지 못했습니다.',LOSS_LIMIT:'설정된 손실 한도에 도달했습니다.'})[code]||liveReason(code||'중단 이유 확인 필요');
}
const paperStamp=t=>t?new Date(t*1000).toLocaleString('ko-KR'):'기록 없음';
function paperRunState(d,offline=false){
 const a=d.activity||{},now=Date.now()/1000;
 if(offline)return {kind:'warning',title:'서버 연결 끊김 · 실행 여부 확인 필요',detail:'아래 기록은 마지막으로 받은 상태입니다.'};
 if(!d.running){
  const recovery=a.recovery;
  if(recovery?.requested&&recovery.status==='RECOVERING')return {kind:'warning',title:'모의매매 · 자동 복구 중',detail:'원장과 최신 시세를 확인한 뒤 자동 재개합니다. '+(recovery.next_retry_at?'다음 확인 '+paperStamp(recovery.next_retry_at):'복구 확인 중')};
  if(recovery?.status==='ACTION_REQUIRED'){
   const reason=a.halt_reason||recovery.reason;
   const title=({CONSECUTIVE_LOSSES:'모의매매 · 연속 손실로 중지',LOSS_LIMIT:'모의매매 · 손실 한도 도달로 중지',RISK_CALCULATION_FAILED:'모의매매 · 위험 계산 오류로 중지'})[reason]||'모의매매 · 중단 원인 확인 필요';
   return {kind:'error',title,detail:paperStopReason(reason)+' 자동 매수·매도는 중지된 상태입니다.'};
  }
  const error=a.halt_reason&&!['USER_KILL_SWITCH','STARTUP_RECONCILIATION_REQUIRED'].includes(a.halt_reason);
  return {kind:error?'error':'stopped',title:error?'모의매매 · 오류로 중단':'모의매매 · 중지됨',detail:paperStopReason(a.halt_reason)+(a.halt_at?' · '+paperStamp(a.halt_at):'')};
 }
 const progress=Math.max(a.last_cycle_completed_at||0,a.last_progress_at||0,a.last_assessment_at>=(a.started_at||0)?a.last_assessment_at:0);
 if(!progress)return {kind:'warning',title:'모의매매 · 시작 중',detail:'코인 목록과 현재 시세를 확인하고 있습니다.'};
 if(now-progress>90)return {kind:'warning',title:'모의매매 · 처리 지연',detail:'최근 판단과 시세 확인 기록이 90초 이상 갱신되지 않았습니다. 시세 연결과 실행 상태는 별개입니다.'};
 if(a.market_wait&&a.market_wait.at>=(a.last_assessment_at||0)){
  const missing=a.market_wait.unavailable_products||[],names=missing.slice(0,3).join(', ')+(missing.length>3?' 외 '+(missing.length-3)+'개':'');
  return {kind:'warning',title:'모의매매 · 시세 갱신 중',detail:(names?names+'의 최신 호가':'보유 자산의 최신 호가')+'를 자동으로 다시 확인하고 있습니다. 준비되면 매매 판단을 이어갑니다.'};
 }
 if(!a.last_assessment_at||a.last_assessment_at<(a.started_at||0)||now-a.last_assessment_at>300)return {kind:'warning',title:'모의매매 · 시세와 차트 준비 중',detail:'확인 가능한 데이터가 준비된 코인부터 매수·매도를 판단합니다.'};
 return {kind:'running',title:'자동 모의매매 실행 중',detail:'실제 시세로 가상 매매를 판단하고 있습니다. Coinbase 계정의 돈은 사용하지 않습니다.'};
}
function paperActivity(d,status){
 const a=d.activity||{},o=a.loss_observation;
 const lossNote=o?.mode==='CONTINUOUS_PAPER_OBSERVATION'?`<p class="${o.streak_warning||o.loss_warning?'notice warn':'small'}">모의 검증 계속 · 손실이 나도 중단하지 않고 기록합니다. 연속 손실 ${fmt(o.consecutive_losses||0,0)}회${o.loss_warning?' · 손실률 경고 기준 도달':''}. 주문 금액·현금 한도와 시세·원장 검사는 유지됩니다.</p>`:'';
 return `<div class="paper-activity ${status.kind}" role="status"><strong>${esc(status.detail)}</strong>${lossNote}<p>마지막 매매 판단: <b>${esc(paperStamp(a.last_assessment_at))}</b>${a.last_product?' · '+esc(a.last_product):''}${a.market_wait?.at?'<br>시세 재확인: <b>'+esc(paperStamp(a.market_wait.at))+'</b>':''}<br>누적 판단 ${fmt(a.assessment_count||0,0)}회 · 모의 체결 ${fmt(d.trade_count||0,0)}회</p><details id="paper-activity-events"><summary>시작·중단 기록 보기</summary>${(a.events||[]).map(e=>`<p><time>${esc(paperStamp(e.created))}</time> · ${esc(paperStopReason(e.code))}${e.diagnostic?`<br><small>진단: ${esc(e.diagnostic.type)} · ${esc((e.diagnostic.frames||[]).map(f=>f.file+':'+f.line+' '+f.function).join(' → '))}</small>`:''}</p>`).join('')||'<p>실행 기록이 아직 없습니다.</p>'}</details></div>`;
}
function rapidScanPanel(d,p=null){
 const r=d.rapid_scan;if(!r)return '';
 const ai=r.ai||{},labels={WARMING:'준비 중',RUNNING:'가동 중',STOPPED:'중지됨',RETRYING:'재시도 중',DELAYED:'분석 지연',ANALYZING:'후보 비교 중',WAITING_CANDIDATES:'다음 매수 후보 대기',DISABLED:'자동 AI 꺼짐',WAITING_DEEP_REPORT:'심층 보고서 완료 후 후보 비교'};
 const skipped=Object.values(r.skipped||{}).reduce((sum,n)=>sum+Number(n),0);
 const capacity=p?Math.max(0,Math.min(Number(p.equity)*.1,Number(p.equity)*.6-Number(p.invested),(Number(p.cash)-Number(p.equity)*.4)/1.006)):null;
 return `<section class="card" aria-label="전체 코인 빠른 분석"><div class="section-top"><h2>전체 코인 빠른 분석</h2><span class="pill">${esc(labels[r.status]||r.status)}</span></div><div class="execution-stats"><div><small>이번 순회 전략 계산</small><strong>${fmt(r.evaluated||0,0)} / ${fmt(r.universe||0,0)}개</strong></div><div><small>전체 계산에 걸린 시간</small><strong>${r.duration_seconds==null?'준비 중':fmt(r.duration_seconds,2)+'초'}</strong></div><div><small>우선 확인 후보</small><strong>매수 ${r.candidates||0} · 매도 ${r.exits||0}</strong></div></div><p>약 ${r.interval_seconds||10}초마다 저장된 차트와 실시간 가격을 함께 계산합니다. 매도 조건에 도달한 코인과 매수 후보부터 최신 호가를 확인하고 주문합니다.</p>${capacity!=null?`<p><strong>새 매수에 쓸 수 있는 여유: 약 ${fmt(capacity)} CAD</strong><br><small>최근 평가액에 현금 40%·전체 투자 60% 한도를 적용한 추정입니다. 매수 후보가 있어도 거래소 최소 주문액과 오늘의 매수 한도를 충족해야 체결됩니다.</small></p>`:''}<p class="small">최근 전체 분석 ${esc(paperStamp(r.completed_at))} · ${r.passes||0}회 완료 · 데이터 재확인 ${skipped}개. 후보 계산에는 예상 스프레드를 쓰며 실제 주문은 새 호가·수수료·환전 비용·예산 검사 후 결정합니다.</p><div class="kv"><span>AI 후보 묶음 비교 · ${esc(labels[ai.status]||ai.status||'준비 중')}</span><strong>${ai.reviewed||0}개 / ${ai.duration_seconds==null?'—':fmt(ai.duration_seconds,1)+'초'}</strong></div><p class="small">최근 AI 비교 ${esc(paperStamp(ai.completed_at))} · 선택 ${ai.selected||0}개 · 보류 ${ai.avoided||0}개. 전체 코인의 전략 계산과 별도로, 진입 조건을 충족한 후보를 최대 24개씩 비교합니다. 심층 TradingAgents 보고서는 AI 팀 화면에서 실행할 수 있습니다.</p></section>`;
}
function dailyTargetPanel(b){
 const d=b?.daily_target;if(!d)return '';
 const ready=d.return_ratio!=null;
 return `<section class="card" aria-label="하루 순수익 목표"><div class="section-top"><h2>하루 전체 순수익 목표 +10%</h2><span class="pill">목표 · 보장 아님</span></div><p>${ready?`오늘 측정 시작 이후 예상 순손익 <strong>${profitMoney(d.net_profit_cad)} CAD · ${profitPercent(d.return_ratio)}</strong><br>목표 수익 ${fmt(d.target_profit_cad)} CAD · ${d.reached?'현재 목표 이상':'목표 미달'}`:'최신 자산 평가를 기다리고 있습니다.'}</p><p class="small">UTC 날짜 ${esc(d.date)} 기준. 보유 코인까지 지금 매도한다고 가정한 금액에서 거래·환전 비용을 빼고 계산합니다. 입금·보너스는 수익에서 제외합니다. 확정 매도 수익과는 다릅니다.</p>${d.partial_day?`<p class="small">오늘은 ${esc(paperStamp(d.started_at))}부터 측정한 부분 기록입니다. 하루 전체 성과로 표시하지 않습니다.</p>`:''}<p class="small">평가 ${esc(paperStamp(d.valuation_at))}${d.fresh?'':' · 최신 평가 대기'} · 목표 미달도 실제 수치 그대로 기록합니다.</p></section>`;
}
function backgroundExecutionNote(){
 const b=state.background;if(!b)return '';
 const alive=b.supervisor_alive&&Date.now()/1000-(b.heartbeat_at||0)<45;
 const worker=b.workers?.['coinbase-paper'],storage=b.storage;
 const disk=storage?.location==='EXTERNAL_SSD'?(storage.available?`<small>저장 위치: 외장 SSD · 여유 ${fmt(storage.free_bytes/1e9,1)} GB · 사용 중에는 SSD를 연결해 두세요.</small>${storage.low_space?'<p class="notice warn">SSD 여유 공간이 10GB 미만입니다.</p>':''}`:`<p class="notice warn">${esc(storage.reason)}</p>`):'';
 return `<div class="background-execution-note"><strong>${alive?'● 백그라운드 자동 실행':'백그라운드 상태 확인 필요'}</strong><span>${b.login_autostart?'Mac 로그인 시 자동 시작 · ':''}서버가 켜져 있으면 브라우저를 닫아도 실행</span><small>${b.heartbeat_at?'서비스 확인 '+esc(paperStamp(b.heartbeat_at)):''}${worker?.last_completed_at?' · 엔진 확인 '+esc(paperStamp(worker.last_completed_at)):''} · 직접 중지한 모의매매는 자동 재개하지 않습니다.</small>${disk}${state.live?.ledger_available===false?'<p class="notice warn">실거래 기록을 읽지 못해 실제 주문을 차단했습니다. 거래소 주문 상태 확인이 필요합니다.</p>':''}</div>`;
}
function updateExecutionStatus(offline=false){
 const box=$('#execution-status'),d=state.cad_plan;if(!box||!d)return;
 const expanded=new Set([...box.querySelectorAll('details[open]')].map(n=>n.id));
 const focused=document.activeElement?.id==='coverage-search';
 const markDelayed=paperPortfolio?.mark_status==='STALE',markNote=markDelayed?'<p class="notice warn">시세 갱신 중 · 아래는 '+esc(paperStamp(paperPortfolio.mark_as_of))+'에 마지막으로 확인한 평가액입니다. 이 가격으로 주문하지 않습니다.</p>':'';
 if($('#paper-positions'))$('#paper-positions').innerHTML=paperPortfolio?markNote+positionsTable(paperPortfolio.positions||[]):'<p>현재 시세를 확인하지 못했습니다.</p>';
 if($('#plan-balances'))$('#plan-balances').innerHTML=paperBalanceCards(paperPortfolio,d);
 if($('#paper-pnl-explanation'))$('#paper-pnl-explanation').innerHTML=paperPnlExplanation(paperPortfolio);
  if(typeof PortfolioCharts!=='undefined')PortfolioCharts.update(paperPortfolio);
 if($('#paper-next-funding'))$('#paper-next-funding').textContent='총 원금 1,000 CAD · 정기 추가 납입 없음';
 const run=paperRunState(d,offline),fresh=run.kind==='running',research=d.research||{},ws=research.stream||{},news=research.news||{};
 const title=run.title;
 const strip=$('#account-run-strip');if(strip){strip.innerHTML=`${backgroundExecutionNote()}${paperSuspensionNote()}<div class="account-run-strip"><span><span class="pill ${fresh&&!offline?'ok':'warn'}">${esc(title)}</span><a href="#execution-status">실행 현황 보기 ↓</a></span><button id="account-multi-toggle" class="${d.running?'danger':'primary'}">${d.running||d.activity?.recovery?.requested?'■ 모의매매 중지':'▶ 자동 모의매매 시작'}</button></div>`;bind('#account-multi-toggle',async()=>{await act(state.cad_plan.running||state.cad_plan.activity?.recovery?.requested?'multi_stop':'multi_start');await navigate('overview');});}

 box.innerHTML=`<section class="execution-card ${d.running&&fresh&&!offline?'running':''}"><span class="overline">원금 1,000 CAD · 추가 납입 없음 · 여러 코인 자동 탐색</span><div class="execution-heading"><h2>${esc(title)}</h2><button id="multi-toggle" class="${d.running?'danger':'primary'}">${d.running||d.activity?.recovery?.requested?'■ 모의매매 중지':'▶ 자동 모의매매 시작'}</button></div>${paperActivity(d,run)}<p>전체 코인 탐색 → ${d.execution_strategies?.length||0}개 전략으로 판단 → 비용·예산 확인 → 주문·보유·청산</p><div class="execution-stats intelligence-stats"><div><small>자동 탐색 후보</small><strong>${fmt(d.scan.eligible||0,0)}개 코인</strong></div><div><small>과거 차트 준비 / 후보</small><strong>${fmt(research.history_ready||0,0)} / ${fmt(d.scan.eligible||0,0)}</strong><small>차트 확인 필요 ${Object.keys(research.history_errors||{}).length}개</small></div><div><small>실시간 가격 수신</small><strong>${ws.connected?'연결됨 · '+fmt(ws.received_products||0,0)+'개':'연결 준비 / 재연결 중'}</strong><small>${when(ws.last_message_at)}</small><small>보유 호가 ${fmt(d.activity?.book_stream?.ready||0,0)} / ${fmt(d.activity?.book_stream?.subscribed||0,0)}개 최신</small></div><div><small>모의 체결 기록</small><strong>${fmt(d.trade_count||0,0)}회</strong><small>최근 ${when(d.last_trade_at)}</small></div></div><p class="small">가격 수신은 모의매매가 중지돼도 계속될 수 있습니다. 매매 실행 여부는 위 상태와 마지막 판단 시각으로 확인하세요. 가격 변경은 WebSocket에서 약 5초 단위로 받습니다. 전체 후보를 함께 계산하고 매도·매수 조건을 충족한 코인의 호가부터 재확인합니다. 주문 전에는 현재 호가와 모든 비용을 다시 확인합니다.</p></section>${rapidScanPanel(d,paperPortfolio)}${dailyTargetPanel(d.plan.performance_bonus)}${allocationPanel(d)}${performanceBonusPanel(d.plan.performance_bonus)}${executionStrategyPanel(d)}<section class="card"><div class="section-top"><h2>${d.running?'지금 확인하는 매매 계획':'마지막으로 확인한 매매 계획'}</h2><span class="pill">${fmt(d.assessed_assets||0,0)}개 코인 평가됨</span></div><p class="small">코인을 누르면 차트·전략별 계산값·뉴스를 볼 수 있습니다. USD 코인 가격은 모의매매용 CAD 환산값입니다.</p><div class="tablewrap"><table class="plan-table"><thead><tr><th>코인 / 확인 시각</th><th>현재 가격</th><th>현재 계획</th></tr></thead><tbody>${(d.plans||[]).slice(0,8).map(paperDecisionRow).join('')||'<tr><td colspan="3">과거 차트를 수집 중입니다. 준비된 코인부터 계획이 표시됩니다.</td></tr>'}</tbody></table></div><details id="coverage-detail"><summary>전체 코인 확인 · ${fmt(d.scan.eligible||0,0)}개</summary><label>코인 찾기<input id="coverage-search" value="${esc(coverageQuery)}" placeholder="예: BTC, ETH, SOL"></label><p class="small">코인마다 데이터 수집·분석 순서가 다릅니다. 표에 마지막 평가 시각과 차트 누락 사유를 표시합니다. CAD·USD 현물에서 중복 코인을 합친 후보입니다.</p><div class="tablewrap coverage-scroll"><table><thead><tr><th>코인</th><th>차트</th><th>마지막 평가</th><th>상태</th></tr></thead><tbody id="coverage-rows">${coverageRows()}</tbody></table></div></details></section><section class="card"><div class="section-top"><h2>함께 확인하는 뉴스</h2><span class="pill">코인별 웹 검색 ${news.searched_assets||0}개</span></div><p class="small">CoinDesk · Cointelegraph · Coinbase 상태 공지 + 코인별 Google 뉴스 검색. 최신 24시간 제목을 수집하고 관련 사건은 주문 크기에 반영합니다.</p><div class="news-list">${(news.articles||[]).slice(0,4).map(a=>`<div>${newsLink(a)}</div>`).join('')||'<p>뉴스 출처를 확인 중입니다.</p>'}</div><details id="news-source-detail"><summary>수집 상태와 활용 방식</summary>${Object.entries(news.sources||{}).map(([name,s])=>`<p>${esc(name)} · ${s.ok?'수집됨':'접속 실패'} · ${when(s.checked_at)}</p>`).join('')}<p class="small">기본 피드는 10분마다, 코인별 검색은 5개씩 90초마다 순환합니다. 제목의 사건 키워드를 분류하는 보조 분석입니다. 뉴스만으로 매수하지 않으며, 수익을 높여 계산하지 않습니다.</p></details></section>`;
 for(const id of expanded){const node=document.getElementById(id);if(node)node.open=true;}
 const search=$('#coverage-search');search.oninput=()=>{coverageQuery=search.value;$('#coverage-rows').innerHTML=coverageRows();};if(focused){search.focus();search.setSelectionRange(search.value.length,search.value.length);}
 box.querySelectorAll('[data-plan]').forEach(b=>b.onclick=()=>showPlan(b.dataset.plan));
 bind('#open-validation',()=>navigate('research'));
 bind('#multi-toggle',async()=>{await act(state.cad_plan.running||state.cad_plan.activity?.recovery?.requested?'multi_stop':'multi_start');await navigate('overview');});
}

function evidenceLabel(code){return ({ROBUST_SAMPLE:'이 표본에서는 비용 스트레스 통과',WEAK_SAMPLE:'비용·손실 주의',INSUFFICIENT_SAMPLE:'검증 표본 부족',NOT_AVAILABLE:'검증 기록 없음'})[code]||'확인 중';}
function sizingDetail(s,r={}){
 if(!s)return '';
 return `<section class="notice"><strong>유동 매수 크기 · 총자산 ${pct(s.fraction)}</strong><p>근거 점수 ${fmt(s.score*100,0)}/100 · ${esc(evidenceLabel(s.validation))}. 점수는 수익 확률이 아닙니다.</p>${r.requested_cash!=null?`<p>이번 요청 ${fmt(r.requested_cash)} CAD · 예산 검사 허용 ${fmt(r.approved_cash)} CAD</p>`:''}<p class="small">서로 다른 유형의 신호·비용 여유·목표 대비 방어 손실·변동성으로 금액을 계산합니다. 같은 추세 지표 여러 개를 독립된 근거로 중복 계산하지 않습니다. 최종 주문은 현금·보유량·최소 주문액 검사 후 결정됩니다.</p></section>`;
}
function allocationPanel(d){
 const fx=d.research?.fx_reference;
 return `<section class="card"><div class="section-top"><div><span class="overline">근거가 강하면 늘리고 · 변동성이 크면 줄이기</span><h2>매수 금액을 유연하게</h2></div><span class="pill">현금 40% 유지</span></div><p>하나의 고정 금액으로 사지 않습니다. 투자 가능 범위는 총자산의 60%, 코인별 상한은 10%입니다. 모의매매는 학습 배분을 추가 적용해 신규 주문당 최대 6%로 제한합니다. 신호가 약하면 적게 사고, 조건이 맞지 않으면 현금을 유지합니다.</p><p class="small">하루 신규 매수 최대 60% · 시간당 신규 매수 최대 30회 · 매도는 매수 횟수 제한 제외 · 모의 손실은 경고로 기록하며 검증 계속. 이 비율은 주문 전 한도이며, 보유 중 시세 변화로 평가 비중은 달라질 수 있습니다. 주간 BTC 정기매수는 2 CAD입니다.</p><h3>달러가 저렴할 때 USDC 적립</h3><p>${fx?`1 USD = <strong>${fmt(fx.rate,4)} CAD</strong> · 60일 평균 ${fmt(fx.mean60,4)} CAD · 차이 ${pct(fx.discount)}<br><small>${esc(fx.date)} 캐나다 중앙은행 기준. 주문 체결 환율은 아닙니다.</small>`:'캐나다 중앙은행의 과거 환율을 준비하고 있습니다.'}</p><p class="small">평균보다 2% 이상 하락하고 모든 비용을 뺀 목표 여유가 있어야 분할 매수합니다. USDC도 투자 자산으로 계산하며 40% 현금에 포함하지 않습니다. 실제매매의 CAD 결제 경로가 없으면 주문하지 않습니다.</p><button id="open-validation">전략 검증 결과 보기</button></section>`;
}
let validationRefresh=null;
async function research(gen){const data=await api('validation');if(gen!==generation)return;$('#content').innerHTML='<div id="validation-results"></div>';renderValidation(data);}
async function refreshValidation(){
 if(validationRefresh)return validationRefresh;
 const gen=generation;validationRefresh=(async()=>{const data=await api('validation');if(page==='research'&&gen===generation&&$('#validation-results'))renderValidation(data);})();
 try{return await validationRefresh;}finally{validationRefresh=null;}
}
function validationView(data){
 const p=data.progress||{},r=data.result,run=p.status==='RUNNING',n=state.cad_plan?.execution_strategies?.length||16;
 const status=({NOT_RUN:'아직 검증 전',RUNNING:'검증 중',COMPLETE:'계산 완료',FAILED:'검증 실패',INTERRUPTED:'검증 작업 중단'})[p.status]||'확인 중';
 return `<section class="card"><div class="section-top"><h2>${n}개 전략을 과거 시세로 검증</h2><span class="pill">${status}</span></div><p>서로 다른 과거 구간 3개에서 전략을 실행하고, 수수료·환전 비용·체결 오차를 높여 다시 계산합니다. 모의매매는 검증 중에도 계속 작동합니다.</p><button id="run-validation" class="primary" ${run?'disabled':''}>${run?'계산 중…':r?'최신 시세로 다시 검증':'전략 검증 시작'}</button>${run?`<progress max="${p.total||153}" value="${p.completed||0}" aria-label="전략 검증 진행"></progress><p>${p.completed||0} / ${p.total||153} · ${esc(p.strategy_id||'준비')} · 구간 ${p.fold||'—'}</p>`:''}${p.reason?`<p class="notice warn">${esc(validationError(p.reason))}</p>`:''}</section>${r?`<section class="card"><h2>검증 범위</h2><p>${esc(r.products.join(' · '))} · 테스트 ${fmt(r.test_days,1)}일 · 각 구간 100 CAD 시작 · ${new Date(r.created_at*1000).toLocaleString()}</p><p class="small">준비 구간 201시간 이후를 3개로 나눕니다. 수치 최적화 없이 고정된 규칙으로 계산합니다. 종료 시 보유분은 매도 비용을 뺀 평가액이며, 실제 매도 체결로 세지 않습니다. 매주 추가 입금은 이 비교에서 제외합니다.</p>${r.outdated?'<p class="notice warn">현재 코드와 다른 버전의 결과입니다. 다시 검증해야 매수 크기에 참고할 수 있습니다.</p>':''}${r.missing_assets?.length?`<p>데이터 미준비: ${esc(r.missing_assets.join(', '))}. 이 코인의 전략 성과는 검증하지 못했습니다.</p>`:''}<p>${r.fx_history_available?'과거에 공개된 일일 USD/CAD 환율 반영':'과거 환율 미준비 · 고정 환율 가정. 환율 적립은 성과 검증 불가'}</p></section><section class="card"><h2>전략별 비용 차감 결과</h2><p class="small">표본 30일·완료된 매도 20건 미만은 표본 부족으로 표시합니다. 양수 결과도 미래 수익이나 검증 완료를 뜻하지 않습니다. 전체 조합과 각 전략을 같은 예산으로 따로 시험한 결과입니다.</p><div class="tablewrap"><table><thead><tr><th>전략</th><th>기본 수익률</th><th>비용 스트레스</th><th>체결 스트레스</th><th>최대 하락</th><th>완료 매도</th><th>판단</th></tr></thead><tbody>${r.entries.map(e=>`<tr><td><button data-validation-detail="${esc(e.strategy_id)}">${esc(e.name)} ↗</button></td><td>${pct(e.scenarios.base.return_ratio)}</td><td>${pct(e.scenarios.cost_stress.return_ratio)}</td><td>${pct(e.scenarios.execution_stress.return_ratio)}</td><td>${pct(e.scenarios.base.max_drawdown)}</td><td>${e.scenarios.base.closed_exits}</td><td>${evidenceLabel(e.status)}</td></tr>`).join('')}</tbody></table></div></section><details class="card" id="validation-method"><summary>계산 방법과 가정</summary><p>기본: 거래 수수료 편도 0.6%, 가격 오차 0.05%, 스프레드 0.1%, 환전 0.25%. 비용 스트레스: 0.9% / 0.2% / 0.3% / 0.5%. 체결 스트레스: 1.2% / 0.4% / 0.5% / 0.75%와 매수 신호 10% 누락.</p><p>완료 봉으로 판단한 뒤 다음 봉 시가로 체결합니다. 당시 호가 깊이·부분 체결·시간봉 안의 손절 움직임은 재현하지 못합니다. 각 구간은 현금으로 새로 시작하고, 표 수익률은 구간 수익률을 연결한 값입니다. 상장 폐지된 코인은 포함되지 않을 수 있습니다.</p><p>몬테카를로는 과거 시간별 수익을 24시간 묶음으로 200번 재조합합니다. 향후 손실 확률을 계산한 것이 아닙니다. 검증 결과가 실제매매를 자동으로 켜거나 전략을 자동 변경하지 않습니다.</p><p><a href="https://ftmo.com/fr/blog/a-big-test-of-algo-trading-does-it-work/" target="_blank" rel="noopener noreferrer">검증 방법 참고 · FTMO ↗</a></p></details>`:'<section class="card empty">검증을 시작하면 실제 저장된 시세로 계산한 결과가 표시됩니다.</section>'}`;
}
function validationError(code){return ({VALIDATION_HISTORY_NOT_READY:'검증할 코인의 시간봉과 일봉을 준비 중입니다.',VALIDATION_NEEDS_72_HOLDOUT_HOURS_AFTER_201_WARMUP:'준비 구간 201시간과 테스트 구간 72시간 이상이 필요합니다.',VALIDATION_WORKER_NO_PROGRESS:'작업 진행이 멈췄습니다. 다시 검증할 수 있습니다.',VALIDATION_HISTORY_GAP:'과거 시세에 빠진 구간이 있어 검증을 보류했습니다.'})[code]||code;}
function renderValidation(data){
 const box=$('#validation-results');if(!box)return;
 const open=$('#validation-method')?.open;box.innerHTML=validationView(data);if(open&&$('#validation-method'))$('#validation-method').open=true;
 bind('#run-validation',async()=>{try{renderValidation(await act('validate_strategies'));}catch(e){toast(validationError(e.message));}});
 box.querySelectorAll('[data-validation-detail]').forEach(b=>b.onclick=()=>{
  const e=data.result.entries.find(x=>x.strategy_id===b.dataset.validationDetail),m=e.monte_carlo;
  $('#detail-title').textContent=e.name+' · 과거 검증';
  $('#detail-text').textContent=`${evidenceLabel(e.status)}\n\n`+Object.entries(e.scenarios).map(([k,v])=>`${({base:'기본',cost_stress:'비용 스트레스',execution_stress:'체결 스트레스'})[k]}\n3개 구간 수익: ${v.fold_returns.map(pct).join(' / ')}\n수수료 합계 ${fmt(v.fees)} CAD · 가격·환전 비용 ${fmt(v.friction)} CAD\n체결 ${v.fills}회 · 완료 매도 ${v.closed_exits}회\n손실 제한 중단 구간 ${v.halted_folds}개`).join('\n\n')+`\n\n몬테카를로 ${m.iterations}회 · 24시간 묶음 재조합\n과거 재조합 수익률 하위 5% / 중앙 / 상위 5%: ${pct(m.return_p05)} / ${pct(m.return_p50)} / ${pct(m.return_p95)}\n재조합 최대 하락 상위 5%: ${pct(m.drawdown_p95)}\n미래 수익·손실 확률이 아닙니다.`;
  $('#detail').showModal();
 });
}


function profitPolicyCard(){return `<div class="notice profit-policy"><strong>AIRI가 판단해서 수익 확보</strong><p>거래·환전 비용을 모두 뺀 순이익이 있을 때 추세가 강하면 보유, 저항선 부근이면 일부 익절, 상승세 약화나 고점 대비 되돌림이면 전량 익절을 검토합니다.</p><p class="small">완료된 시간봉과 최신 호가로 계산하는 규칙 기반 판단입니다. 플러스라고 자동으로 모두 팔지 않습니다. 주문 직전 비용·최소 주문액·위험 한도를 다시 확인하며, 기존 손실 방어 조건은 유지합니다. 매도 대금은 거래 통화의 현금으로 확보합니다.</p></div>`;}
function profitManagementDetail(p){
 if(!p)return '';
 const reason=({HOLD_STRONG_TREND:'상승 추세가 이어져 보유',HOLD_FOR_MORE_EVIDENCE:'이익은 있지만 익절 근거가 아직 부족해 보유',NET_NOT_POSITIVE:'비용을 뺀 순이익이 없어 익절 대기',COSTS_UNVERIFIED:'매도 호가 깊이 또는 비용 확인 대기',HISTORY_REQUIRED:'익절 판단에 필요한 완료 시간봉 확인 중',STRATEGY_EXIT:'원래 전략의 청산 조건 적용'})[p.status]||strategyReason(p.status);
 return `<section class="notice"><strong>현금 확보 판단 · ${esc(reason)}</strong><p>지금 전량 매도 시 추정 순손익 <b>${profitMoney(p.net_profit_cad)} CAD · ${profitPercent(p.net_return)}</b></p><p class="small">매수 원가 ${fmt(p.cost_basis_cad,4)} CAD · 매도 비용 차감 후 ${fmt(p.liquidation_cad,4)} CAD. 추정값이며 확정 손익은 체결 후 달력에 기록됩니다.</p></section>`;
}
function profitMoney(value){
 if(value==null)return '—';const n=Number(value);
 if(n!==0&&Math.abs(n)<.01)return (n>0?'+':'−')+'0.01 미만';
 return (n>0?'+':n<0?'−':'')+Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
}
function profitPercent(value){if(value==null)return '—';const n=Number(value)*100;return (n>0?'+':n<0?'−':'')+(n!==0&&Math.abs(n)<.01?'0.01 미만':Math.abs(n).toLocaleString(undefined,{maximumFractionDigits:2}))+'%';}
function profitTone(value){return value==null?'':Number(value)>0?'profit-positive':Number(value)<0?'profit-negative':'';}
let profitMonth='',profitMode='paper',profitSelected='',profitData=null,profitRequest=0,profitPending=null;
function shiftedProfitMonth(month,step){const [y,m]=month.split('-').map(Number),d=new Date(Date.UTC(y,m-1+step,1));return d.toISOString().slice(0,7);}
async function refreshProfitCalendar(gen=generation){
 const key=profitMode+':'+profitMonth;
 if(profitPending?.key===key)return profitPending.promise;
 const seq=++profitRequest;
 const promise=(async()=>{
  try{
   const data=await api('calendar?mode='+encodeURIComponent(profitMode)+(profitMonth?'&month='+encodeURIComponent(profitMonth):''));
   if(gen!==generation||page!=='calendar'||seq!==profitRequest)return;
   profitData=data;profitMonth=data.month;
   if(!profitSelected.startsWith(data.month))profitSelected=data.today.startsWith(data.month)?data.today:data.days.find(d=>d.sell_count)?.date||data.days[0].date;
   renderProfitCalendar();
  }catch(error){
   if(gen!==generation||page!=='calendar'||seq!==profitRequest)return;
   const note=$('#profit-refresh');
   if(note){note.textContent='갱신 실패 · 마지막으로 받은 기록입니다. 다음 조회에서 다시 확인합니다.';note.classList.add('warn');}
   else throw error;
  }
 })();
 profitPending={key,promise};try{await promise;}finally{if(profitPending?.promise===promise)profitPending=null;}
}
function profitCalendarView(data,selected){
 const s=data.summary,mode=data.mode==='paper'?'모의매매':'실제매매';
 const cells='<div class="calendar-pad" aria-hidden="true"></div>'.repeat(data.weekday_offset)+data.days.map(day=>{
  const blank=['future','before_records'].includes(day.status),today=day.date===data.today;
  const amount=blank?'—':day.verified?profitMoney(day.net_profit_cad):'비용 미확인';
  const detail=day.sell_count?profitPercent(day.return_ratio):day.buy_count?'매수만 있음':blank?'':'매도 없음';
  return `<button class="calendar-day ${today?'is-today':''} ${day.date===selected?'is-selected':''} ${profitTone(day.net_profit_cad)}" data-profit-day="${day.date}" ${day.status==='future'?'disabled':''} aria-pressed="${day.date===selected}" aria-label="${day.date} ${amount} CAD, ${detail}"><span>${day.day}${today?'<small>오늘</small>':''}</span><strong>${amount}</strong><small>${detail}</small></button>`;
 }).join('');
 const row=data.days.find(d=>d.date===selected),trades=(row?.trades||[]).filter(t=>t.side==='SELL');
 return `<section class="card"><div class="section-top"><div class="profit-modes" role="group" aria-label="수익 기록 모드"><button data-profit-mode="paper" aria-pressed="${data.mode==='paper'}" class="${data.mode==='paper'?'primary':''}">모의매매</button><button data-profit-mode="live" aria-pressed="${data.mode==='live'}" class="${data.mode==='live'?'primary':''}">실제매매</button></div><span class="pill">${mode} · CAD 기준</span></div><p class="small">기록 조회만 바뀝니다. 매매 실행 모드는 바뀌지 않습니다.</p><div class="profit-summary"><div><small>이번 달 확정 순손익</small><strong class="${profitTone(s.net_profit_cad)}">${profitMoney(s.net_profit_cad)} CAD</strong></div><div><small>매도한 원가 대비 수익률</small><strong>${profitPercent(s.return_ratio)}</strong></div><div><small>확정 매도</small><strong>${s.sell_count}회</strong></div></div><p class="small">수익률 = 비용 차감 순손익 ÷ 매도한 수량의 매수 원가. 원가에 매수 비용을 포함합니다. 입금액·아직 팔지 않은 코인의 평가손익은 제외합니다.</p></section><section class="card"><div class="calendar-toolbar"><button id="profit-prev" aria-label="이전 달" ${data.month==='2000-01'?'disabled':''}>←</button><h2>${data.month.replace('-','년 ')}월</h2><button id="profit-next" aria-label="다음 달" ${data.month==='2100-12'?'disabled':''}>→</button><button id="profit-today">이번 달</button></div><div class="calendar-grid" aria-label="${data.month} 일별 확정 순손익">${['일','월','화','수','목','금','토'].map(w=>`<div class="calendar-weekday">${w}</div>`).join('')}${cells}</div><p class="small">칸의 금액은 CAD입니다. 날짜를 누르면 매도 내역을 볼 수 있어요. 오늘의 합계는 체결될 때마다 바뀝니다.</p><p class="small">${esc(data.cost_basis_label)} · 체결 확정 기록 시각을 UTC 날짜로 집계합니다.</p><p id="profit-refresh" class="small">마지막 조회 ${new Date(data.refreshed_at*1000).toLocaleTimeString()} · 5초마다 갱신</p></section><section class="card"><h2>${esc(selected)} · ${mode} 기록</h2>${row?.verified===false?'<p class="notice warn">일부 거래의 비용 자료가 없어 순손익 합계를 확정할 수 없습니다.</p>':''}${trades.length?`<div class="tablewrap"><table><thead><tr><th>코인 / 시각</th><th>매도 수량</th><th>배분 매수 원가</th><th>순매도 대금</th><th>순손익 / 수익률</th></tr></thead><tbody>${trades.map(t=>`<tr><td>${esc(t.product)}<br><small>${new Date(t.created*1000).toLocaleTimeString('ko-KR',{timeZone:data.timezone})}</small></td><td>${fmt(t.quantity,8)}</td><td>${fmt(t.cost_basis_cad,4)} CAD</td><td>${fmt(t.sell_proceeds_cad,4)} CAD</td><td class="${profitTone(t.net_profit_cad)}">${t.verified?profitMoney(t.net_profit_cad)+' CAD<br>'+profitPercent(t.return_ratio):'비용 미확인'}</td></tr>`).join('')}</tbody></table></div>`:`<p class="empty">${row?.buy_count?'매수 '+row.buy_count+'회 · 아직 매도해 확정된 손익이 없습니다.':'이 날짜에는 확정된 매도 기록이 없습니다.'}</p>`}<p class="small">부분 매도는 매도한 수량에 해당하는 원가만 배분합니다. 순매도 대금은 매도 비용 차감 후 금액입니다. ${data.mode==='live'?'USD·USDC 거래의 CAD 값은 환산값이며, 환전 비용은 설정한 준비금입니다. 실제 CAD 환전·출금 내역은 아닙니다.':'모의매매 비용은 가정값이며, 실제 Coinbase 계정 수수료와 다를 수 있습니다.'}</p></section>`;
}
function renderProfitCalendar(){
 const active=document.activeElement,focusDay=active?.dataset?.profitDay,focusMode=active?.dataset?.profitMode,focusId=active?.id;
 $('#content').innerHTML=profitCalendarView(profitData,profitSelected);
 const move=async(step)=>{profitMonth=shiftedProfitMonth(profitData.month,step);await refreshProfitCalendar();};
 bind('#profit-prev',()=>move(-1));bind('#profit-next',()=>move(1));bind('#profit-today',async()=>{profitMonth='';profitSelected='';await refreshProfitCalendar();});
 document.querySelectorAll('[data-profit-mode]').forEach(b=>b.onclick=async()=>{profitMode=b.dataset.profitMode;await refreshProfitCalendar();});
 document.querySelectorAll('[data-profit-day]').forEach(b=>b.onclick=()=>{profitSelected=b.dataset.profitDay;renderProfitCalendar();});
 const focus=focusDay?document.querySelector(`[data-profit-day="${focusDay}"]`):focusMode?document.querySelector(`[data-profit-mode="${focusMode}"]`):focusId?.startsWith('profit-')?document.getElementById(focusId):null;
 if(focus)focus.focus({preventScroll:true});
}

window.addEventListener('hashchange',()=>{const target=location.hash.slice(1);if(['overview','live'].includes(target)&&target!==page)navigate(target);});
