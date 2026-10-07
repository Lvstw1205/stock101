'use strict';
// All charts read the primary PAPER ledger. No trading action is exposed here.
const PortfolioCharts=(()=>{
 const COLORS=['#3867d6','#14a69c','#9474df','#e3a239','#e07882','#43a0d1','#74934d','#ad6b42','#a3adbd'];
 const finite=v=>v!==null&&v!==undefined&&v!==''&&Number.isFinite(Number(v));
 const money=v=>finite(v)?Number(v).toLocaleString('en-CA',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
 const stamp=v=>v?new Date(v*1000).toLocaleString('ko-KR',{timeZone:'UTC',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'기록 없음';
 const date=v=>new Date(v*1000).toLocaleString('ko-KR',{timeZone:'UTC',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
 const node=id=>document.getElementById(id);
 let range='1',series='equity',includeCash=false,data=null,lastPortfolio=null,error='',loadedAt=0;
 const pending=new Map();
 function holdings(p,cash=false){
  if(!p||!Array.isArray(p.positions))return {available:false,rows:[],total:0};
  const coins=new Map();let invalid=0;
  for(const row of p.positions){
   if(!finite(row.quantity)){invalid++;continue;}
   if(Number(row.quantity)<=0)continue;
   if(!finite(row.market_value)||Number(row.market_value)<0){invalid++;continue;}
   const name=String(row.product||'이름 없음').split('-')[0];
   coins.set(name,(coins.get(name)||0)+Number(row.market_value));
  }
  let rows=[...coins].map(([name,value])=>({name,value})).filter(r=>r.value>0).sort((a,b)=>b.value-a.value||a.name.localeCompare(b.name));
  const total=rows.reduce((sum,r)=>sum+r.value,0)+(cash&&finite(p.cash)?Number(p.cash):0);
  if(cash){if(!finite(p.cash)||Number(p.cash)<0)invalid++;else if(Number(p.cash)>0)rows.push({name:'CAD 현금',value:Number(p.cash),cash:true});}
  rows.sort((a,b)=>b.value-a.value||a.name.localeCompare(b.name));
  const all=rows.map(r=>({...r,ratio:total>0?r.value/total:0}));
  if(rows.length>8)rows=[...rows.slice(0,7),{name:'기타 '+(rows.length-7)+'개',value:rows.slice(7).reduce((s,r)=>s+r.value,0),other:true}];
  let offset=0;
  rows=rows.map((r,i)=>{const ratio=total>0?r.value/total:0,item={...r,ratio,offset,color:r.cash?'#a3adbd':COLORS[i]};offset+=ratio;return item;});
  return {available:invalid===0,invalid,rows,all,total,coinCount:coins.size};
 }
 function signed(v){return finite(v)?profitMoney(v):'—';}
 function region(id,html){const el=node(id);if(!el)return;const opened=[...el.querySelectorAll('details[open]')].map(n=>n.dataset.chartDetails);el.innerHTML=html;for(const d of el.querySelectorAll('details'))if(opened.includes(d.dataset.chartDetails))d.open=true;}
 function donut(p){
  const m=holdings(p,includeCash);
  if(!m.available)return '<p class="chart-empty">보유 평가액을 확인한 뒤 비중을 표시합니다. 일부 값이 없으면 비중을 추정하지 않습니다.</p>';
  if(m.total<=0)return '<p class="chart-empty">현재 보유한 코인이 없습니다. ‘현금 포함’을 켜면 가상 현금 비중을 볼 수 있습니다.</p>';
  const arcs=m.rows.map(r=>{const text=`${r.name}: ${money(r.value)} CAD · ${(r.ratio*100).toFixed(1)}%`;return `<circle class="allocation-arc" cx="110" cy="110" r="79" pathLength="100" stroke="${r.color}" stroke-dasharray="${r.ratio*100} ${100-r.ratio*100}" stroke-dashoffset="${-r.offset*100}" transform="rotate(-90 110 110)" fill="none" stroke-width="30" tabindex="0" role="img" aria-label="${esc(text)}"><title>${esc(text)}</title></circle>`;}).join('');
  const legend=m.rows.map(r=>`<li><span class="chart-dot" style="background:${r.color}"></span><span>${esc(r.name)}<small>${money(r.value)} CAD</small></span><strong>${(r.ratio*100).toFixed(1)}%</strong></li>`).join('');
  return `<div class="allocation-layout"><svg class="allocation-donut" viewBox="0 0 220 220" role="group" aria-label="${includeCash?'현금 포함 총자산':'보유 코인 평가액'} 비중 원형 그래프">${arcs}<text x="110" y="98" text-anchor="middle" class="donut-caption">${includeCash?'현금 포함 총자산':'보유 코인 평가액'}</text><text x="110" y="125" text-anchor="middle" class="donut-value">${money(m.total)}</text><text x="110" y="144" text-anchor="middle" class="donut-caption">CAD</text></svg><ul class="allocation-legend">${legend}</ul></div><p class="small">${includeCash?'현금 + 보유 코인 평가액':'보유 코인 평가액만'}을 100%로 계산합니다. ${m.coinCount}종 보유 · 비중은 반올림 표시합니다.</p><details data-chart-details="allocation"><summary>모든 코인 비중 보기</summary><div class="tablewrap"><table><thead><tr><th>자산</th><th>현재 평가액 (CAD)</th><th>비중</th></tr></thead><tbody>${m.all.map(r=>`<tr><td>${esc(r.name)}</td><td>${money(r.value)}</td><td>${(r.ratio*100).toFixed(2)}%</td></tr>`).join('')}</tbody></table></div></details>`;
 }
 function historyModel(d,key){
  const rows=(d?.history||[]).filter(r=>finite(r.timestamp)&&finite(r[key])&&finite(r.initial));
  if(!rows.length)return {rows,paths:[],funding:[],ticks:[]};
  const values=rows.flatMap(r=>key==='equity'?[Number(r[key]),Number(r.initial)]:[Number(r[key]),0]);
  let lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.12,.5);lo-=pad;hi+=pad;
  const first=Number(rows[0].timestamp),last=Number(rows.at(-1).timestamp),left=64,right=610,top=16,bottom=215;
  const x=t=>left+(Number(t)-first)/(last-first||1)*(right-left),y=v=>bottom-(Number(v)-lo)/(hi-lo)*(bottom-top);
  const path=field=>rows.map((r,i)=>`${!i||r.gap_before?'M':'L'}${x(r.timestamp).toFixed(2)},${y(r[field]).toFixed(2)}`).join(' ');
  return {rows,path:path(key),funding:key==='equity'?path('initial'):null,x,y,lo,hi,first,last,ticks:[lo,(lo+hi)/2,hi]};
 }
 function historyView(d){
  const m=historyModel(d,series);
  if(!m.rows.length)return '<p class="chart-empty">선택한 기간에 저장된 자산 평가 기록이 없습니다.</p>';
  const last=m.rows.at(-1),pnl=series==='net_profit_cad';
  const grid=m.ticks.map(v=>`<line x1="64" x2="610" y1="${m.y(v)}" y2="${m.y(v)}" class="chart-gridline"/><text x="57" y="${m.y(v)+4}" text-anchor="end" class="chart-axis">${money(v)}</text>`).join('');
  const tips=m.rows.map(r=>{const info=`${stamp(r.timestamp)} · ${pnl?'계좌 전체 평가손익':'총자산'} ${pnl?signed(r.net_profit_cad):money(r.equity)} CAD · 누적 가상 자금 ${money(r.initial)} CAD`;return `<circle cx="${m.x(r.timestamp)}" cy="${m.y(r[series])}" r="3" class="history-point" tabindex="0" role="img" aria-label="${esc(info)}"><title>${esc(info)}</title></circle>`;}).join('');
  return `<div class="chart-latest"><strong class="${pnl?profitTone(last.net_profit_cad):''}">${pnl?signed(last.net_profit_cad):money(last.equity)} <small>CAD</small></strong><span>${pnl?'총자산 − 누적 가상 자금':'마지막 저장된 총자산'}</span></div><svg class="portfolio-line" viewBox="0 0 640 255" role="group" aria-label="${pnl?'계좌 전체 평가손익':'총자산과 누적 가상 자금'} 변화 그래프"><text x="10" y="12" class="chart-axis">CAD</text>${grid}${pnl?`<line x1="64" x2="610" y1="${m.y(0)}" y2="${m.y(0)}" class="chart-zero"/>`:''}${m.funding?`<path d="${m.funding}" fill="none" class="funding-line"/>`:''}<path d="${m.path}" fill="none" class="equity-line"/>${tips}<text x="64" y="243" class="chart-axis">${esc(date(m.first))}</text><text x="610" y="243" text-anchor="end" class="chart-axis">${esc(date(m.last))}</text></svg><div class="chart-key"><span><i class="key-equity"></i>${pnl?'계좌 전체 평가손익':'총자산'}</span>${pnl?'':'<span><i class="key-funding"></i>누적 가상 자금</span>'}</div><p class="small">저장된 시점 기준 · UTC 시간. ${pnl?'추가 자금·보너스는 수익에서 제외합니다.':'총자산 상승에는 추가 자금이 포함될 수 있습니다. 손익 탭에서 수익을 구분하세요.'} 추가 매도 비용 전 평가이며, 기록 간 15분 넘는 공백은 선으로 잇지 않습니다.</p><p class="small">${esc(stamp(d.first_at))} ~ ${esc(stamp(d.last_at))} · ${Number(d.record_count).toLocaleString()}건에서 추린 관측값${d.truncated?' · 최근 20만 건만 표시':''}${d.invalid_marks?' · 유효하지 않은 평가 '+Number(d.invalid_marks)+'건 제외':''}</p>`;
 }
 function dailyView(d){
  const rows=d?.daily||[],valid=rows.filter(r=>finite(r.net_profit_cad)),max=Math.max(.01,...valid.map(r=>Math.abs(Number(r.net_profit_cad))));
  if(!valid.length)return '<p class="chart-empty">해당 기간에 기록된 모의 체결 내역이 없습니다.</p>';
  const width=540/rows.length,zero=120,scale=85/max;
  const bars=rows.map((r,i)=>{const x=60+i*width,available=finite(r.net_profit_cad),n=available?Number(r.net_profit_cad):0,h=Math.abs(n)*scale;
   const caption=available?`${r.date} · 매도 확정 순손익 ${signed(n)} CAD · 매도 ${r.sell_count}회 · 매도 원가 대비 ${profitPercent(r.return_ratio)}${r.status==='today'?' · 오늘 집계 중':''}`:`${r.date} · 기록 전`;
   return `<g tabindex="0" role="img" aria-label="${esc(caption)}"><title>${esc(caption)}</title>${available?`<rect x="${x+width*.17}" y="${n>=0?zero-h:zero}" width="${width*.66}" height="${Math.max(h,1.5)}" rx="2" fill="${n>0?'#18866a':n<0?'#c95762':'#a8b4c8'}"/>`:`<text x="${x+width/2}" y="${zero+4}" text-anchor="middle" class="chart-axis">—</text>`}${rows.length<=7||i%5===0||i===rows.length-1?`<text x="${x+width/2}" y="235" text-anchor="middle" class="chart-axis">${esc(r.date.slice(5).replace('-','/'))}</text>`:''}</g>`;
  }).join('');
  const total=valid.reduce((s,r)=>s+Number(r.net_profit_cad),0),unknown=rows.some(r=>r.verified===false);
  return `<div class="chart-latest"><strong class="${unknown?'':profitTone(total)}">${unknown?'확인 필요':signed(total)} <small>CAD</small></strong><span>최근 ${d.daily_days}일 매도 확정 손익 합계</span></div><svg class="portfolio-line" viewBox="0 0 640 255" role="group" aria-label="날짜별 매도 확정 순손익 막대 그래프"><text x="10" y="12" class="chart-axis">CAD</text>${[max,0,-max].map(v=>`<line x1="60" x2="600" y1="${zero-v*scale}" y2="${zero-v*scale}" class="${v?'chart-gridline':'chart-zero'}"/><text x="53" y="${zero-v*scale+4}" text-anchor="end" class="chart-axis">${money(v)}</text>`).join('')}${bars}</svg><p class="small">매수·매도 및 환전 비용이 반영된 완료 매도의 손익입니다. 보유 평가손익은 제외하며, 오늘은 집계 중입니다. %는 매도한 코인의 원가 대비입니다.</p><details data-chart-details="daily"><summary>날짜별 금액과 % 보기</summary><div class="tablewrap"><table><thead><tr><th>날짜 (UTC)</th><th>확정 순손익 (CAD)</th><th>매도 원가 대비</th><th>매도 횟수</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.date)}${r.status==='today'?' · 집계 중':''}</td><td class="${profitTone(r.net_profit_cad)}">${signed(r.net_profit_cad)}</td><td>${profitPercent(r.return_ratio)}</td><td>${r.status==='before_records'?'기록 전':Number(r.sell_count)}</td></tr>`).join('')}</tbody></table></div></details>`;
 }
 function coinPnl(p){
  if(!p)return '<p class="chart-empty">현재 보유 코인 평가를 기다립니다.</p>';
  const rows=(p.positions||[]).filter(r=>Number(r.quantity)>0&&finite(r.unrealized)).sort((a,b)=>Math.abs(Number(b.unrealized))-Math.abs(Number(a.unrealized)));
  if(!rows.length)return '<p class="chart-empty">보유 중인 코인의 평가손익이 없습니다.</p>';
  const shown=rows.slice(0,10),max=Math.max(.01,...shown.map(r=>Math.abs(Number(r.unrealized))));
  return `<div class="coin-pnl-bars" role="list" aria-label="코인별 보유 평가손익">${shown.map(r=>{const n=Number(r.unrealized),width=Math.abs(n)/max*50;return `<div class="coin-pnl-row" role="listitem"><span>${esc(r.product)}</span><div class="coin-pnl-track" role="img" aria-label="${esc(r.product)} 보유 평가손익 ${signed(n)} CAD"><i style="left:${n<0?50-width:50}%;width:${width}%;background:${n<0?'#c95762':'#18866a'}"></i></div><strong class="${profitTone(n)}">${signed(n)}</strong></div>`;}).join('')}</div><p class="small">단위 CAD · 0을 중심으로 왼쪽은 손실, 오른쪽은 이익. ${rows.length>10?'손익 절댓값이 큰 10개 표시. ':''}매수 비용은 반영됐고 추가 매도 비용은 아직 빼지 않은 값입니다.</p>`;
 }
 function shell(){return `<section class="portfolio-charts" aria-label="자산과 손익 그래프"><div class="section-top portfolio-chart-heading"><div><span class="overline">MY PAPER PORTFOLIO</span><h2>내 자산, 한눈에 보기</h2><p class="small">이 모의계좌만 표시 · 전략 연구실의 별도 실험 자금 제외</p></div><button id="portfolio-charts-refresh">그래프 새로고침</button></div><p id="portfolio-chart-status" class="small" role="status">기록을 불러오는 중…</p><div class="portfolio-chart-grid"><section class="card"><div class="section-top"><h3>AIRI가 보유한 코인 비중</h3><label class="chart-switch"><input id="portfolio-include-cash" type="checkbox" ${includeCash?'checked':''}>현금 포함</label></div><p id="portfolio-holdings-time" class="small"></p><div id="portfolio-composition"></div></section><section class="card"><div class="section-top"><h3>자산·손익 변화</h3><label>기간<select id="portfolio-chart-range"><option value="1" ${range==='1'?'selected':''}>최근 24시간</option><option value="7" ${range==='7'?'selected':''}>최근 7일</option><option value="30" ${range==='30'?'selected':''}>최근 30일</option></select></label></div><div class="chart-tabs" role="group" aria-label="변화 그래프 기준"><button data-portfolio-series="equity" aria-pressed="${series==='equity'}">총자산</button><button data-portfolio-series="net_profit_cad" aria-pressed="${series==='net_profit_cad'}">전체 평가손익</button></div><div id="portfolio-history"></div></section><section class="card"><h3>날짜별 매도 확정 순손익</h3><div id="portfolio-daily"></div><button id="portfolio-open-calendar" class="chart-calendar-link">수익 달력 열기 ↗</button></section><section class="card"><h3>코인별 보유 평가손익</h3><div id="portfolio-coin-pnl"></div><a href="#paper-holdings-card">전체 보유 코인 표 보기 ↓</a></section></div></section>`;}
 function update(p){
  lastPortfolio=p;
  if(!node('portfolio-composition'))return;
  const stale=p?.mark_status==='STALE';
  node('portfolio-holdings-time').textContent=p?`${p.mark_status==='PARTIAL'?'일부 지난 시세 포함 · 참고값':stale?'지난 평가값 · 시세 갱신 대기':'평가 시각'} ${stamp(p.mark_status==='PARTIAL'?p.fresh_mark_as_of:p.mark_as_of)} · UTC`: '평가 시각 확인 중';
  region('portfolio-composition',donut(p));region('portfolio-coin-pnl',coinPnl(p));
 }
 function renderHistory(){
  const current=data&&String(data.days)===range;
  region('portfolio-history',current?historyView(data):'<p class="chart-empty">선택한 기간의 기록을 불러오는 중…</p>');
  region('portfolio-daily',current?dailyView(data):'<p class="chart-empty">매도 기록을 불러오는 중…</p>');
  const status=node('portfolio-chart-status');if(status)status.textContent=error?'그래프 갱신 실패 · '+(current?'마지막 조회 자료 유지 · '+stamp(data.observed_at):'아직 표시할 자료 없음'):current?'보유 비중은 약 5초마다, 과거 기록은 약 30초마다 갱신 · 기록 조회 '+stamp(data.observed_at):'과거 자산·매도 기록 조회 중…';
 }
 async function refresh(force=false){
  if(page!=='overview'||!node('portfolio-history'))return;
  if(!force&&data&&String(data.days)===range&&Date.now()-loadedAt<30000)return;
  const selectedRange=range,gen=generation,key=`${gen}:${selectedRange}`;
  if(pending.has(key))return pending.get(key);
  const task=(async()=>{try{
   const response=await api('portfolio-charts?days='+selectedRange);
   if(gen!==generation||page!=='overview'||range!==selectedRange)return;
   if(response.mode!=='paper'||response.source!=='primary_paper_ledger')throw Error('다른 계좌 자료');
   data=response;loadedAt=Date.now();error='';renderHistory();
  }catch(e){if(gen===generation&&page==='overview'&&range===selectedRange){error=e.message;renderHistory();}}
  finally{pending.delete(key);}})();pending.set(key,task);return task;
 }
 function mount(p){
  update(p);renderHistory();
  const cash=node('portfolio-include-cash');if(cash)cash.onchange=()=>{includeCash=cash.checked;update(lastPortfolio);};
  const picker=node('portfolio-chart-range');if(picker)picker.onchange=()=>{range=picker.value;error='';renderHistory();void refresh(true);};
  document.querySelectorAll('[data-portfolio-series]').forEach(b=>b.onclick=()=>{series=b.dataset.portfolioSeries;document.querySelectorAll('[data-portfolio-series]').forEach(n=>n.setAttribute('aria-pressed',String(n===b)));renderHistory();});
  const refreshButton=node('portfolio-charts-refresh');if(refreshButton)refreshButton.onclick=async()=>{refreshButton.disabled=true;try{await Promise.all([refresh(true),refreshPaperPortfolio()]);}finally{refreshButton.disabled=false;}};
  const calendar=node('portfolio-open-calendar');if(calendar)calendar.onclick=()=>navigate('calendar');
  void refresh();
 }
 return {shell,mount,update,refresh,holdings,historyModel,historyView,dailyView,coinPnl};
})();
