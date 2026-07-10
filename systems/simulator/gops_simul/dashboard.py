from __future__ import annotations


def render_control_dashboard() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GOPS Demo Control</title>
  <style>
    :root { color-scheme: dark; --ink:#e9f2ee; --muted:#85948e; --panel:#111816; --line:#28332f; --mint:#77f2b1; --amber:#ffbd59; --red:#ff6b63; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; color:var(--ink); background:radial-gradient(circle at 88% 8%,#17352b 0,transparent 31%),#080d0b; font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }
    main { width:min(1180px,calc(100% - 32px)); margin:0 auto; padding:28px 0 50px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:20px; margin-bottom:24px; }
    .eyebrow { color:var(--mint); letter-spacing:.14em; text-transform:uppercase; font-size:11px; }
    h1 { margin:5px 0 0; font:600 clamp(27px,4vw,48px)/1.05 system-ui,sans-serif; letter-spacing:-.045em; }
    .mode { display:flex; align-items:center; gap:11px; padding:8px 8px 8px 14px; border:1px solid var(--line); border-radius:999px; background:#0c1210cc; }
    .mode b { min-width:83px; }
    .switch { position:relative; width:52px; height:29px; border:0; border-radius:99px; background:#39423f; cursor:pointer; }
    .switch::after { content:""; position:absolute; top:4px; left:4px; width:21px; height:21px; border-radius:50%; background:white; transition:.2s; }
    .switch.on { background:var(--mint); }
    .switch.on::after { transform:translateX(23px); background:#092117; }
    .grid { display:grid; grid-template-columns:1.45fr .55fr; gap:16px; }
    .panel { border:1px solid var(--line); border-radius:18px; padding:18px; background:linear-gradient(145deg,#131b18e8,#0c1210e8); box-shadow:0 22px 60px #0004; }
    .panel h2 { margin:0 0 14px; font:600 15px/1.2 system-ui,sans-serif; }
    .hero { grid-column:1/-1; padding:22px; }
    .clock { display:flex; align-items:baseline; justify-content:space-between; gap:16px; }
    .clock strong { font-size:38px; letter-spacing:-.05em; }
    .clock span { color:var(--muted); }
    .track { position:relative; height:14px; margin:24px 0 12px; border-radius:99px; background:#25302c; overflow:hidden; }
    .progress { width:0; height:100%; background:linear-gradient(90deg,var(--mint),var(--amber)); transition:width .2s linear; }
    .news-mark { position:absolute; top:0; bottom:0; left:1.666%; width:3px; background:var(--red); box-shadow:0 0 12px var(--red); }
    .track-labels { display:flex; justify-content:space-between; color:var(--muted); font-size:11px; }
    .phase { display:inline-flex; align-items:center; gap:7px; margin-top:16px; padding:6px 10px; border-radius:7px; color:var(--mint); background:#77f2b116; }
    .phase::before { content:""; width:7px; height:7px; border-radius:50%; background:currentColor; box-shadow:0 0 10px currentColor; }
    .symbols { display:grid; grid-template-columns:repeat(4,1fr); gap:9px; }
    .quote { padding:11px; border:1px solid var(--line); border-radius:11px; background:#090e0c; }
    .quote div { display:flex; justify-content:space-between; gap:6px; }
    .quote b { font-size:13px; }
    .quote small { color:var(--muted); }
    .quote .up { color:var(--mint); } .quote .down { color:var(--red); }
    .actions { display:grid; gap:8px; }
    .actions button { width:100%; border:1px solid var(--line); color:var(--ink); padding:11px; border-radius:10px; background:#18211e; cursor:pointer; font:inherit; }
    .actions button:hover { border-color:var(--mint); }
    .actions .danger { color:#ffd6d3; border-color:#663532; }
    .account { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-bottom:14px; }
    .metric { padding:10px; border-left:2px solid var(--mint); background:#0b110f; }
    .metric small { display:block; color:var(--muted); font-size:10px; text-transform:uppercase; }
    .metric b { font-size:15px; }
    .positions { display:flex; flex-wrap:wrap; gap:6px; }
    .positions span { padding:5px 8px; color:#dbe6e1; background:#202b27; border-radius:6px; }
    .log { max-height:245px; overflow:auto; }
    .entry { display:grid; grid-template-columns:68px 1fr; gap:10px; padding:9px 0; border-bottom:1px solid #202925; }
    .entry time { color:var(--muted); } .entry.breaking { color:#ffd0cd; }
    .empty { color:var(--muted); padding:18px 0; }
    @media (max-width:800px) { .grid{grid-template-columns:1fr}.symbols{grid-template-columns:repeat(2,1fr)}header{align-items:flex-start;flex-direction:column}.account{grid-template-columns:1fr} }
  </style>
</head>
<body>
<main>
  <header>
    <div><div class="eyebrow">Saturday demo simulator</div><h1>GOPS Demo Control</h1></div>
    <div class="mode"><b id="modeText">LIVE</b><button id="toggle" class="switch" aria-label="LIVE SIM 전환"></button></div>
  </header>
  <section class="grid">
    <article class="panel hero">
      <div class="clock"><div><div class="eyebrow">Scenario clock</div><strong id="clock">00:00.0</strong></div><span>총 05:00 · 속보 T+00:05</span></div>
      <div class="track"><div id="progress" class="progress"></div><i class="news-mark"></i></div>
      <div class="track-labels"><span>시뮬레이션 시작</span><span>이란 속보</span><span>시장 영향 구간</span><span>종료</span></div>
      <div id="phase" class="phase">LIVE 대기</div>
    </article>
    <article class="panel">
      <h2>8종목 틱 상태</h2><div id="symbols" class="symbols"></div>
    </article>
    <aside class="panel">
      <h2>운영 제어</h2>
      <div class="actions"><button id="pause">일시정지</button><button id="restart" class="danger">처음부터 다시 시작</button></div>
    </aside>
    <article class="panel">
      <h2>더미 계좌 · demo-user</h2><div id="account" class="empty">SIM 전환 시 반도체 포트폴리오를 준비합니다.</div>
    </article>
    <article class="panel">
      <h2>이벤트 로그</h2><div id="log" class="log"><div class="empty">아직 발생한 이벤트가 없습니다.</div></div>
    </article>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id); let lastRun = null, newsLogged = false;
const money = (n) => new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:0}).format(n||0);
function stamp(sec){ const m=Math.floor(sec/60),s=sec-m*60; return `${String(m).padStart(2,'0')}:${s.toFixed(1).padStart(4,'0')}`; }
function log(message, breaking=false){ const box=$('log'); if(box.querySelector('.empty')) box.innerHTML=''; const row=document.createElement('div'); row.className='entry'+(breaking?' breaking':''); row.innerHTML=`<time>T+${$('clock').textContent}</time><span>${message}</span>`; box.prepend(row); }
async function json(url, options){ const res=await fetch(url,{headers:{'content-type':'application/json'},...options}); if(!res.ok) throw new Error((await res.json()).detail||res.statusText); return res.json(); }
async function refresh(){
  try {
    const s=await json('/api/control/status'); const sim=s.mode==='simulation';
    $('toggle').classList.toggle('on',sim); $('modeText').textContent=sim?'SIMULATION':'LIVE';
    $('clock').textContent=stamp(s.elapsedSeconds); $('progress').style.width=`${(s.elapsedSeconds/s.durationSeconds)*100}%`;
    $('phase').textContent=sim?(s.phase==='pre-war'?'속보 전 · 기준 틱 송신':s.phase==='complete'?'시나리오 종료':'속보 후 · 충격 틱 송신'):'LIVE 대기';
    $('pause').textContent=s.state==='paused'?'계속 재생':'일시정지';
    $('symbols').innerHTML=s.symbols.map(q=>`<div class="quote"><div><b>${q.symbol}</b><small>${q.price?.toFixed(2)??'—'}</small></div><small class="${q.changePercent>=0?'up':'down'}">${q.changePercent==null?'—':(q.changePercent>=0?'+':'')+q.changePercent.toFixed(2)+'%'}</small></div>`).join('');
    if(s.runId!==lastRun){ if(lastRun!==null) log('새 시나리오 실행이 시작되었습니다.'); lastRun=s.runId; newsLogged=false; }
    if(s.breakingNewsReleased&&!newsLogged){ log('[속보] 이란 휴전 붕괴 — 시장 충격 구간 진입',true); newsLogged=true; }
    if(sim){ const a=await json('/api/control/account?userId=demo-user'); const p=a.account; $('account').innerHTML=`<div class="account"><div class="metric"><small>총 평가</small><b>${money(p.totalValueForeign)}</b></div><div class="metric"><small>현금</small><b>${money(p.cashForeign)}</b></div><div class="metric"><small>미실현</small><b>${money(p.unrealizedPnlForeign)}</b></div></div><div class="positions">${Object.values(a.positions).map(v=>`<span>${v.symbol} ${v.quantity}주</span>`).join('')||'<span>보유 종목 없음</span>'}</div>`; }
  } catch(e){ console.error(e); }
}
$('toggle').onclick=async()=>{ const next=$('toggle').classList.contains('on')?'live':'simulation'; await json('/api/control/mode',{method:'PUT',body:JSON.stringify({mode:next})}); log(next==='simulation'?'SIMULATION 시작 — 5초 카운트다운':'LIVE 모드로 복귀'); refresh(); };
$('pause').onclick=async()=>{ const action=$('pause').textContent.includes('계속')?'resume':'pause'; await json('/api/control/action',{method:'POST',body:JSON.stringify({action})}); refresh(); };
$('restart').onclick=async()=>{ await json('/api/control/action',{method:'POST',body:JSON.stringify({action:'restart'})}); log('계좌와 시나리오를 초기화했습니다.'); refresh(); };
refresh(); setInterval(refresh,250);
</script>
</body></html>"""
