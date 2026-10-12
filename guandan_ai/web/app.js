/* Browser UI owns only selection and presentation. The official table validates moves. */
"use strict";
const $ = id => document.getElementById(id);
const patterns = {Single:"单张",Pair:"对子",Trips:"三张",ThreeWithTwo:"三带二",Straight:"顺子",ThreePair:"三连对",TwoTrips:"钢板",Bomb:"炸弹",StraightFlush:"同花顺",FourKings:"四王炸",PASS:"不出",tribute:"进贡",back:"还贡"};
let names = ["你","右侧对手","对家队友","左侧对手"];
let state = null, selected = new Set(), busy = false, paused = false;
let shownResult = "", noticeTimer = null, socket = null, socketURL = "", reconnect = null, turnDeadline = 0;
const escapeHTML = value => String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const cardsOf = a => a && a[0] !== "PASS" && Array.isArray(a[2]) ? a[2] : [];
const label = a => !a ? "尚未出牌" : ["PASS","FourKings","tribute","back"].includes(a[0]) ? (patterns[a[0]] || a[0]) : `${patterns[a[0]] || a[0]} · ${String(a[1]).replace("T","10")}`;
const signature = cards => [...cards].sort().join(",");
const cardImage = (code, css="mini-card") => `<img class="${css}" src="/cards/${encodeURIComponent(code)}.svg" alt="${escapeHTML(code === "SB" ? "小王" : code === "HR" ? "大王" : ({S:"黑桃",H:"红桃",C:"梅花",D:"方块"}[code[0]] + code.slice(1).replace("T","10")))}" draggable="false">`;
const fan = cards => `<span class="mini-fan ${cards.length > 6 ? "many" : ""}">${cards.map(c => cardImage(c)).join("")}</span>`;
function notify(message, error=false) {
  clearTimeout(noticeTimer); $("notice").textContent=message; $("notice").classList.toggle("error",error); $("notice").hidden=false;
  noticeTimer=setTimeout(()=>$("notice").hidden=true,7000);
}
function matchingActions() {
  if (!state?.human_turn || selected.size===0) return [];
  const wanted=signature(state.hand.filter(c=>selected.has(c.id)).map(c=>c.code));
  return state.actions.map((action,index)=>({action,index})).filter(row=>row.action[0]!=="PASS" && signature(cardsOf(row.action))===wanted);
}
function renderPlayer(seat) {
  const p=state.players[seat], last=p.last;
  const element=$("seat-"+seat);
  element.classList.toggle("active",state.turn===seat);
  let action='<span class="empty-play">尚未出牌</span>';
  if(last) action=`<button class="action-box" data-seat="${seat}" aria-label="查看${escapeHTML(p.name)}最近出牌"><span class="action-caption">第 ${last.trick} 圈 · <strong>${escapeHTML(label(last.action))}</strong></span>${last.action[0]==="PASS" ? '<span class="pass-bubble">不出</span>' : fan(cardsOf(last.action))}</button>`;
  element.innerHTML=`<div class="player-head"><span class="avatar">${seat===2 ? "♣" : seat===1 ? "♦" : "♠"}</span><div class="player-meta"><span class="player-name">${escapeHTML(p.name)}${seat===2 ? " · 同队" : ""}</span><span class="player-rest">${p.place ? `第 <strong>${p.place}</strong> 名` : `剩 <strong>${p.rest}</strong> 张`}</span></div></div>${action}`;
}
function renderTable() {
  [1,2,3].forEach(renderPlayer);
  const leader=state.leading_action;
  const following=state.leader!==-1 && leader && leader[0]!=="PASS";
  if(state.round_over) $("leading").innerHTML='<div class="leading-label">本副结束</div><div class="free-lead">♠</div><div class="leading-tip">点击出牌可放大查看<br>各家最近一手仍保留在桌面</div>';
  else if(following) $("leading").innerHTML=`<div class="leading-label">当前要压的牌 · ${escapeHTML(names[state.leader])}</div><div class="leading-title">${escapeHTML(label(leader))}</div><button class="action-box" data-leading="1" aria-label="放大当前要压的牌">${fan(cardsOf(leader))}</button><div class="leading-tip">${state.human_turn ? "同型更大，或用更大的炸弹" : "等待下一位玩家"}</div>`;
  else $("leading").innerHTML=`<div class="leading-label">${state.stage==="play" ? "新一圈" : "牌局交接"}</div><div class="free-lead">♠</div><div class="leading-title">${state.stage==="tribute" ? "进贡" : state.stage==="back" ? "还贡" : state.human_turn ? "由你领出" : `${escapeHTML(names[state.turn])}领出`}</div><div class="leading-tip">${state.stage==="play" ? "可出任意合法牌型" : "按平台规则选择一张牌"}</div>`;
  const own=state.players[0].last;
  $("own-last").innerHTML=own ? `<span>你的最近一手 · 第 ${own.trick} 圈</span><button data-seat="0" aria-label="查看你的最近出牌">${escapeHTML(label(own.action))}${fan(cardsOf(own.action))}</button>` : '<span>你与上方对家同队 · 左右两家是对手</span>';
  $("round-info").textContent=`第 ${state.episode} 副 · 第 ${state.trick} 圈`;
  $("stage-info").textContent=state.round_over ? "本副结算" : state.stage==="tribute" ? "进贡阶段" : state.stage==="back" ? "还贡阶段" : "出牌阶段";
  $("score-info").textContent=`我方 ${state.ranks[0]} 级 / 对方 ${state.ranks[1]} 级`;
  $("trump-badge").textContent=`本副打 ${state.trump.replace("T","10")} · 百搭 ♥${state.trump.replace("T","10")}`;
  $("hand-count").textContent=state.hand.length;
}
function renderHand() {
  $("hand").innerHTML=state.hand.length ? `<div class="hand-grid">${state.hand.map(c=>`<button class="playing-card ${selected.has(c.id) ? "selected" : ""}" data-card="${c.id}" aria-pressed="${selected.has(c.id)}" aria-label="选择 ${escapeHTML(c.name)}，第 ${c.id+1} 张${c.wild ? "，百搭" : ""}" title="${escapeHTML(c.name)}${c.wild ? " · 百搭" : ""}" ${!state.human_turn ? "disabled" : ""}>${cardImage(c.code,"")}<span class="selection-check" aria-hidden="true">✓</span>${c.wild ? '<span class="wild-tag">百搭</span>' : ""}</button>`).join("")}</div>` : '<div class="history-empty">你已出完牌，继续看队友争取好名次。</div>';
  const ranks=[];
  state.hand.forEach(c=>{let group=ranks.find(r=>r.rank===c.code[1]);if(!group){group={rank:c.code[1],ids:[]};ranks.push(group);}group.ids.push(c.id);});
  $("rank-select").innerHTML=ranks.map(group=>`<button class="rank-chip" data-rank="${group.rank}" aria-label="选择全部${group.rank==="R" ? "大王" : group.rank==="B" ? "小王" : group.rank.replace("T","10")}，共 ${group.ids.length} 张" ${!state.human_turn ? "disabled" : ""}>${group.rank==="R" ? "大王" : group.rank==="B" ? "小王" : group.rank.replace("T","10")}<span>×${group.ids.length}</span></button>`).join("");
  renderSelection();
}
function renderSelection() {
  document.querySelectorAll("[data-card]").forEach(button=>{const chosen=selected.has(Number(button.dataset.card));button.classList.toggle("selected",chosen);button.setAttribute("aria-pressed",String(chosen));});
  document.querySelectorAll("[data-rank]").forEach(button=>{const ids=state.hand.filter(c=>c.code[1]===button.dataset.rank).map(c=>c.id);const all=ids.length>0 && ids.every(id=>selected.has(id));button.classList.toggle("selected",all);button.setAttribute("aria-pressed",String(all));});
  const picked=state.hand.filter(c=>selected.has(c.id));
  $("picked-cards").hidden=!picked.length;
  $("picked-cards").innerHTML=picked.map(c=>`<button class="picked-chip" data-unselect="${c.id}" aria-label="取消选择 ${escapeHTML(c.name)}，第 ${c.id+1} 张">${escapeHTML(c.name)} <span aria-hidden="true">×</span></button>`).join("");
  const matches=matchingActions(), status=$("selection-status"), previous=$("pattern").value;
  status.className=matches.length ? "valid" : selected.size ? "invalid" : "";
  status.textContent=selected.size ? `已选 ${selected.size} 张 · ${matches.length ? label(matches[0].action) : "不能按此组合出牌，可调整选牌或使用提示"}` : "点击整张牌选中；再次点击取消。也可按点数批量选择。";
  $("pattern-wrap").hidden=matches.length<=1;
  $("pattern").innerHTML=matches.map(row=>`<option value="${row.index}">${escapeHTML(label(row.action))}</option>`).join("");
  if(matches.some(row=>String(row.index)===previous)) $("pattern").value=previous;
  renderControls();
}
function renderControls() {
  const human=!!state?.human_turn && !state.round_over;
  document.querySelectorAll("[data-card],[data-rank],[data-unselect]").forEach(button=>button.disabled=!human || busy);
  $("play").disabled=!human || busy; $("hint").disabled=!human || busy;
  // Leave the lead-round button clickable so its reason is explained, not mistaken for a failure.
  $("pass").disabled=!human || busy;
  $("pass").textContent=state?.cannot_beat ? "要不起" : "不出 / 要不起";
  $("clear").disabled=!selected.size || busy;
  $("play").textContent=state?.stage==="tribute" ? "确认进贡" : state?.stage==="back" ? "确认还贡" : "出牌";
  $("restart").disabled=busy || !!state?.room && !state.room.owner; $("start").disabled=busy;
  $("create-room").disabled=busy;$("join-room").disabled=busy;
  $("ready").disabled=busy;
  $("room-start").disabled=busy || !!state?.room?.seats.some(s=>s.occupied&&!s.ready);
  $("autopilot").disabled=busy;$("leave-room").disabled=busy;$("room-settings").disabled=busy;
  document.querySelectorAll("[data-seat-kind]").forEach(select=>select.disabled=busy || !state?.room?.owner);
  $("pause").hidden=!state?.room?.owner || state.room.seats.filter(s=>s.occupied).length>1;
  $("speed").disabled=!state?.room?.owner;
  $("pause").textContent=paused ? "继续 AI" : "暂停 AI";
  $("step").hidden=!paused || human || !state?.active || state.round_over || !state?.room?.owner;
  $("step").disabled=busy;
  if(!state?.active) return;
  $("turn-status").textContent=state.round_over ? "本副已结束" : state.autopilot ? "你已托管 · 可随时取消" : human ? state.stage==="play" ? "轮到你了" : "轮到你交接牌" : paused ? `${names[state.turn]} · 已暂停` : state.thinking ? `${names[state.turn]}正在思考…` : `等待${names[state.turn]}出牌`;
  $("phase-tip").textContent=state.round_over ? "各家出牌会保留在桌面，可点开看详情" : !human ? "AI 依次出牌；轮到你时自动等待。可以切换慢速观察。" : state.stage==="tribute" ? "选一张平台允许的最大单牌进贡，红桃级牌不贡。" : state.stage==="back" ? "选一张不大于 10 且不是级牌的牌还贡。" : state.can_pass ? "你可以主动不出，也可以压牌；不出不需要选中任何牌。" : "本圈由你领出，必须出牌，不能不出。";
}
function renderHistory() {
  $("history").innerHTML=state.timeline.length ? [...state.timeline].reverse().map((e,index)=>`<button class="history-entry" data-history="${state.timeline.length-1-index}" aria-label="查看${escapeHTML(names[e.seat])}的${escapeHTML(e.label)}"><span class="history-name">${escapeHTML(names[e.seat])}<em>#${state.timeline.length-index}</em></span><span class="history-label">${escapeHTML(e.label)}</span>${cardsOf(e.action).length ? fan(cardsOf(e.action)) : ""}</button>`).join("") : '<div class="history-empty">出牌后，这里会留下记录</div>';
}
function showResult() {
  if(!state.round_over) return;
  const token=`${state.room.code}:${state.generation}:${state.episode}`;
  if(shownResult===token) return;
  shownResult=token;
  const won=state.result.winner===0;
  $("result-title").textContent=won ? "配合漂亮，我方获胜！" : "这副对方先走，下副再来";
  $("result-description").textContent=`${won ? "我方" : "对方"}升 ${state.result.upgrade} 级。${state.match_over ? "完整比赛已结束。" : "点击下一副，继续升级并完成进贡或还贡。"}`;
  $("result-order").innerHTML=state.result.order.map((seat,i)=>`<div class="result-place"><strong>${i+1}</strong>${escapeHTML(names[seat])}</div>`).join("");
  $("next").hidden=state.match_over || !state.room.owner;
  $("result-dialog").showModal();
}
function acceptState(next) {
  if(!next) return;
  if(state && next.session_version<state.session_version) return;
  const sameRoom=next.room?.code===state?.room?.code;
  const version=s=>s?.revision?.split(":").slice(1).map(Number) || [0,0];
  const [ng,nv]=version(next),[og,ov]=version(state);
  if(sameRoom && (ng<og || (ng===og && nv<ov))) return;
  const changed=!state || next.revision!==state.revision;
  const handChanged=!state || !sameRoom || next.generation!==state.generation || next.steps!==state.steps || next.human_turn!==state.human_turn;
  state=next;
  paused=state.room?.paused || false;
  $("lobby").hidden=!!state.room; $("game").hidden=!state.active;$("room-lobby").hidden=!state.room || state.active;
  $("restart").textContent=state.active ? "重新开局" : state.room ? "开始对局" : "开始游戏";
  turnDeadline=Date.now()+(state.turn_seconds || 0)*1000;
  if(changed || !sameRoom)renderRoom();
  if(!state.active){renderControls();return;}
  names=state.players.map((p,i)=>i===0 ? "你" : p.name);
  if(changed){renderTable();renderHistory();showResult();}
  if(handChanged){selected.clear();renderHand();}else renderSelection();
  renderControls();
}
async function command(name,data={}) {
  if(busy) return;
  busy=true;renderControls();
  try{
    const commandID=globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const options={method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...data,room_id:state?.room?.code,revision:state?.revision,command_id:commandID})};
    let response;
    try{response=await fetch(`/api/${name}`,options);}catch(error){response=await fetch(`/api/${name}`,options);}
    const payload=await response.json();
    acceptState(payload.state);
    if(!response.ok) {notify(payload.error || "操作未完成，请重试",true);return;}
    if(name==="hint" && payload.state.room?.code===state.room?.code && payload.state.steps===state.steps && state.human_turn){selected=new Set(payload.selected);renderSelection();notify(payload.hint);}
    else $("notice").hidden=true;
    if(name==="next") $("result-dialog").close();
  }catch(error){notify("连接暂时中断，牌局会保留。请稍后重试。",true);}
  finally{busy=false;renderControls();}
}
function play() {
  if(!state?.human_turn || busy) return;
  if(!selected.size){notify("请先选择要出的牌；不想压牌时可以直接点“不出 / 要不起”。",true);return;}
  const matches=matchingActions();
  if(!matches.length){notify("这些牌不能组成当前合法牌型，或不能压过上一手。请调整选牌。",true);return;}
  const index=matches.length>1 ? Number($("pattern").value) : matches[0].index;
  command("play",{selected:[...selected],action_index:index});
}
function pass() {
  if(!state?.human_turn || busy) return;
  if(!state.can_pass){notify(state.stage==="play" ? "本圈由你领出，必须出牌。跟牌时才能选择不出。" : "进贡和还贡阶段需要选择一张合法牌。",true);return;}
  command("pass");
}
function openCards(action,title,description="") {
  if(!action || action[0]==="PASS"){notify(`${title}：不出`);return;}
  $("cards-title").textContent=`${title} · ${label(action)}`;
  $("cards-detail").innerHTML=cardsOf(action).map(c=>cardImage(c,"")).join("");
  $("cards-description").textContent=description || `共 ${cardsOf(action).length} 张。这些牌已经公开，不是隐藏手牌。`;
  $("cards-dialog").showModal();
}
const nickname=()=>$("nickname").value.trim() || state?.nickname || "玩家";
$("start").onclick=$("restart").onclick=()=>{ $("result-dialog").close(); command("new",{strategy:$("difficulty").value,nickname:nickname()}); };
$("create-room").onclick=()=>command("create",{nickname:nickname()});
$("join-room").onclick=()=>{const data={nickname:nickname(),code:$("join-code").value.trim()};if(Number($("join-seat").value)>=0)data.seat=Number($("join-seat").value);command("join",data);};
$("ready").onclick=()=>{const self=state.room.seats.find(s=>s.you);command("ready",{ready:!self.ready});};
$("room-start").onclick=()=>command("start");
$("room-settings").onclick=()=>{ $("result-dialog").close();command("reset");};
$("leave-room").onclick=()=>{ $("result-dialog").close();command("leave");};
$("autopilot").onclick=()=>command("autopilot",{enabled:!state.autopilot});
$("copy-invite").onclick=async()=>{const link=`${location.origin}/?room=${state.room.code}`;try{await navigator.clipboard.writeText(link);notify("邀请链接已复制；朋友在同一网络打开即可加入。");}catch(error){notify(`邀请链接：${link}`);}};
$("play").onclick=play;$("pass").onclick=pass;$("hint").onclick=()=>command("hint");
$("clear").onclick=()=>{selected.clear();renderSelection();$("notice").hidden=true;};
$("pause").onclick=()=>command("pause",{paused:!paused});
$("step").onclick=()=>command("step");
$("speed").onchange=()=>command("speed",{delay:Number($("speed").value)/1000});
$("rules-button").onclick=()=>$("rules-dialog").showModal();
document.querySelectorAll(".close-dialog").forEach(button=>button.onclick=()=>button.closest("dialog").close());
$("result-close").onclick=()=>$("result-dialog").close();
$("next").onclick=()=>command("next");
$("hand").addEventListener("click",event=>{
  const button=event.target.closest("[data-card]");
  if(!button || !state.human_turn || busy) return;
  const id=Number(button.dataset.card);
  selected.has(id) ? selected.delete(id) : selected.add(id);
  renderSelection();
});
$("rank-select").addEventListener("click",event=>{
  const button=event.target.closest("[data-rank]");
  if(!button || !state.human_turn || busy) return;
  const ids=state.hand.filter(c=>c.code[1]===button.dataset.rank).map(c=>c.id);
  const remove=ids.every(id=>selected.has(id));
  ids.forEach(id=>remove ? selected.delete(id) : selected.add(id));
  renderSelection();
});
$("picked-cards").addEventListener("click",event=>{
  const button=event.target.closest("[data-unselect]");
  if(!button || !state.human_turn || busy) return;
  selected.delete(Number(button.dataset.unselect));renderSelection();
});
document.addEventListener("click",event=>{
  const button=event.target.closest("[data-seat],[data-history],[data-leading]");
  if(!button || !state) return;
  if(button.dataset.seat!==undefined){const seat=Number(button.dataset.seat);const last=state.players[seat].last;if(last) openCards(last.action,names[seat],`最近一手，出于第 ${last.trick} 圈。`);}
  else if(button.dataset.history!==undefined){const e=state.timeline[Number(button.dataset.history)];openCards(e.action,names[e.seat]);}
  else openCards(state.leading_action,`${names[state.leader]} · 当前要压的牌`);
});
document.addEventListener("keydown",event=>{
  if(document.querySelector("dialog[open]") || /INPUT|SELECT|TEXTAREA|BUTTON/.test(event.target.tagName)) return;
  if(event.key==="Enter"){event.preventDefault();play();}
  if(event.key===" "){event.preventDefault();pass();}
});
async function poll() {
  try{
    if(!busy && !document.hidden){
      const response=await fetch("/api/state",{cache:"no-store"});const payload=await response.json();
      if(response.ok){acceptState(payload.state);if(payload.ws_url)connectSocket(payload.ws_url);}
    }
  }catch(error){ /* A transient polling failure must not destroy a playable hand. */ }
  finally{setTimeout(poll,socket?.readyState===WebSocket.OPEN ? 5000 : 1500);}
}
function renderRoom(){
  const room=state.room;
  $("room-toolbar").hidden=!room;$("model-stats").hidden=!state.active;
  if(!$("nickname").value)$("nickname").value=state.nickname;
  if(!room)return;
  $("room-code").textContent=room.code;$("your-seat").textContent=room.your_seat;
  $("autopilot").hidden=!state.active;$("autopilot").textContent=state.autopilot ? "取消托管" : "托管";
  $("room-settings").hidden=!state.active || !room.owner;
  const kinds=[{id:"open",label:"等待真人",ready:true},{id:"greedy",label:"休闲 AI",ready:true},{id:"team",label:"团队 AI",ready:true},{id:"hybrid",label:"综合 AI",ready:true},...(state.providers || [])];
  if(!state.active){
    $("room-seats").innerHTML=room.seats.map(s=>`<article class="room-seat"><span class="seat-number">${s.seat}</span><strong>${escapeHTML(s.nickname)}${s.you ? " · 你" : ""}${s.owner ? " · 房主" : ""}</strong><small>${s.occupied ? `${s.online ? "在线" : "离线"} · ${s.ready ? "已准备" : "未准备"}` : s.kind==="open" ? "等待朋友入座" : "AI 已就位"}</small>${!s.occupied && room.owner ? `<select data-seat-kind="${s.seat}" aria-label="座位 ${s.seat} 类型">${kinds.map(k=>`<option value="${escapeHTML(k.id)}" ${s.kind===k.id ? "selected" : ""} ${!k.ready ? "disabled" : ""}>${escapeHTML(k.label)}${!k.ready ? "（未配置）" : ""}</option>`).join("")}</select>` : ""}</article>`).join("");
    const self=room.seats.find(s=>s.you);
    $("ready").textContent=self.ready ? "取消准备" : "准备";
    $("ready").disabled=busy;$("room-start").hidden=!room.owner;
    $("room-start").disabled=busy || room.seats.some(s=>s.occupied&&!s.ready);
    $("provider-note").textContent=(state.providers || []).map(p=>`${p.label}：${p.ready ? `已配置 · ${p.model}` : "服务器尚未配置密钥"}`).join("；")+"。模型失败或超时会使用本地 AI 回退。";
  }
  if(state.stats){const s=state.stats;$("model-stats-content").textContent=`总动作 ${s.decisions} · 回退 ${s.fallbacks} · 模型回合 ${s.model_decisions} · 已记录 tokens ${s.usage.total_tokens} · 模型平均 ${s.model_mean_ms===null ? "—" : Math.round(s.model_mean_ms)+"ms"} · P95 ${s.model_p95_ms===null ? "—" : Math.round(s.model_p95_ms)+"ms"} · 估算费用 ${s.estimated_cost===null ? "未配置价格" : s.estimated_cost.toFixed(6)}`;}
}
$("room-seats").addEventListener("change",event=>{const select=event.target.closest("[data-seat-kind]");if(select)command("configure",{seat:Number(select.dataset.seatKind),kind:select.value});});
function connectSocket(url){
  if(socket && socketURL===url && [WebSocket.OPEN,WebSocket.CONNECTING].includes(socket.readyState))return;
  socketURL=url;clearTimeout(reconnect);
  try{socket=new WebSocket(url);}catch(error){$("network-status").textContent="普通同步";return;}
  socket.onopen=()=>$("network-status").textContent="实时连接 · 可联机";
  socket.onmessage=event=>{try{const payload=JSON.parse(event.data);if(payload.state)acceptState(payload.state);if(payload.type==="error")notify(payload.error,true);}catch(error){}};
  socket.onclose=()=>{$("network-status").textContent="重连中 · 普通同步继续";reconnect=setTimeout(()=>connectSocket(socketURL),1500);};
  socket.onerror=()=>$("network-status").textContent="普通同步 · 正在重连";
}
const inviteCode=new URLSearchParams(location.search).get("room");if(inviteCode)$("join-code").value=inviteCode.slice(0,6);
setInterval(()=>{if(state?.active&&!state.round_over){$("turn-clock").textContent=paused ? "单人对局已暂停，回合倒计时冻结。" : state.human_turn ? `本回合剩 ${Math.max(0,Math.ceil((turnDeadline-Date.now())/1000))} 秒，超时后自动托管。` : "服务器正在推进回合，关闭其他玩家页面也会继续。";}else $("turn-clock").textContent="";},500);
poll();
