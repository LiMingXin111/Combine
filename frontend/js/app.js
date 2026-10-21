// 康柏鹦前端：路由、外壳、各页面视图。所有用户可见文案都经 t() 本地化。
import { $, $$, esc, ic, api, GET, POST, PATCH, PUT, DEL, toast, fail, errText, modal, closeModal, confirmBox, md, money, dur, hhmmss, ago, debounce, once } from './core.js';
import { t, L, LANG, setLang } from './i18n.js';
import { CanvasView, artKey } from './canvas.js';
import { mountDeliverables, fallbackCards } from './deliverables.js';
import { renderChart } from './chart.js';
import { renderMap } from './mapview.js';
import { mountLeafletMap, leafletAvailable } from './imap.js';
import { ART_KINDS, ART_ICON, ART_LBL, renderArt, bindArt, artExport } from './artview.js';

const S = { me: null, meta: null, settings: null, locked: [], models: null, tasks: [], unread: 0, view: null };
const deliveryLabel = (value) => t(({ complete: '完整交付', partial: '部分交付', empty: '未形成有效交付' })[value] || '成果');

// ======================= 启动与路由 =======================
let ROUTE = 0;                                          // 路由序号：异步视图返回时据此判断页面是否已切换
const stale = (my) => my !== ROUTE;
function resetSession() {
  S.view?.destroy?.(); S.view = null; notifES?.close(); notifES = null; closeModal();
  Object.assign(S, { me: null, settings: null, models: null, tasks: [], unread: 0 });
  $('#app').dataset.shell = '';
}
window.addEventListener('cb:unauth', () => { if (!S.me) return; resetSession(); location.hash = '#/login'; });
window.addEventListener('hashchange', route);
boot();

async function boot() {
  S.meta = await GET('/api/meta').catch(() => ({}));
  try { await loadMe(); } catch { /* 未登录 */ }
  route();
}
async function loadMe() {
  const r = await GET('/api/me', { silent401: true });
  S.me = r.user; S.quota = r.quota; S.weights = r.weights;
  const st = await GET('/api/settings'); S.settings = st.values; S.locked = st.locked;
  setLang(S.settings['general.ui_language']); applyTheme();
  notifStream();
}
function applyTheme() {
  const th = S.settings?.['general.theme'] || 'dark';
  const light = th === 'light' || (th === 'system' && matchMedia('(prefers-color-scheme: light)').matches);
  document.body.classList.toggle('light', light);
  document.body.classList.toggle('compact', S.settings?.['general.density'] === 'compact');
  document.body.classList.toggle('hide-cites', S.settings?.['knowledge.show_citations'] === false);
}

function route() {
  S.view?.destroy?.(); S.view = null; closePicker(); closeModal();
  const my = ++ROUTE;
  const h = location.hash.slice(1) || '/';
  if (!S.me && h !== '/login') { location.hash = '#/login'; return; }
  if (h === '/login') return viewLogin();
  shell();
  const m = h.match(/^\/task\/([\w-]+)(\/result)?/);
  const run = () => {
    if (m) return m[2] ? viewResult(m[1], my) : (S.view = new TaskView(m[1]));
    if (h.startsWith('/kb')) return viewKB(my);
    if (h.startsWith('/playbooks')) return viewPlaybooks(my);
    if (h.startsWith('/caps')) return viewCaps(my);
    if (h.startsWith('/agents')) return viewAgents(my);
    if (h.startsWith('/settings')) return viewSettings(h.split('/')[2] || 'general', my);
    return viewHome(new URLSearchParams(h.split('?')[1] || ''), my);
  };
  // 视图加载失败时给出可见的错误占位，而不是留下上一个页面的残留 DOM
  Promise.resolve().then(run).catch((e) => { if (stale(my)) return; fail(e); $('#view').innerHTML = `<div class="page"><div class="empty">${esc(e?.message || String(e))}<br><a data-go="/">${t('返回任务空间')}</a></div></div>`; });
}

// ======================= 外壳 =======================
function shell() {
  if (!$('#app').dataset.shell) {
    $('#app').dataset.shell = '1';
    $('#app').innerHTML = `<aside class="sidebar" id="sidebar"><div class="brand">${t('康柏鹦')}</div>
      <div class="nav"><button class="btn primary" data-go="/" style="justify-content:center;margin:4px 2px 8px">${ic('plus')}${t('发起新任务')}</button>
      <button class="nav-item" data-go="/">${ic('home')}${t('任务空间')}</button><button class="nav-item" data-go="/kb">${ic('book')}${t('我的知识库')}</button>
      <button class="nav-item" data-go="/playbooks">${ic('flow')}${t('个人工作流')}</button><button class="nav-item" data-go="/caps">${ic('layers')}${t('Agent 能力库')}</button><button class="nav-item" data-go="/agents">${ic('cpu')}${t('Agent 目录')}</button>
      <button class="nav-item" data-go="/settings">${ic('gear')}${t('设置')}</button></div>
      <div class="nav-label">${t('最近任务')}</div><div class="recent" id="recent"></div><div class="side-foot" id="sideFoot"></div></aside>
      <main class="main"><header class="topbar"><button class="icon-btn menu-btn" id="menuBtn">${ic('menu')}</button><div class="grow" id="topTitle"></div>
      <div class="acts" id="topActs"></div><button class="icon-btn" id="bell" title="${esc(t('通知'))}">${ic('bell')}<span class="badge hidden" id="bellN"></span></button></header>
      <section class="view" id="view"></section></main>`;
    $('#app').addEventListener('click', (e) => {
      const g = e.target.closest('[data-go]'); if (g) { location.hash = '#' + g.dataset.go; $('#sidebar').classList.remove('open'); }
    });
    $('#menuBtn').onclick = () => { $('#sidebar').classList.toggle('open'); };
    $('#bell').onclick = openNotifications;
  }
  const h = location.hash.slice(1) || '/';
  $$('.nav-item').forEach((b) => b.classList.toggle('on', b.dataset.go === '/' ? h === '/' || h.startsWith('/task') : h.startsWith(b.dataset.go)));
  $('#sideFoot').innerHTML = `${esc(S.me.display_name)} · ${t('本月内置额度')} ${money(S.quota?.used)} / ${money(S.quota?.monthly)}${S.meta?.fake_llm ? `<br><span class="pill ac" style="margin-top:6px">${t('离线演示模型')}</span>` : ''}`;
  refreshRecent();
}
async function refreshRecent() {
  try { S.tasks = (await GET('/api/tasks?limit=30')).items; } catch { return; }
  const cur = (location.hash.match(/task\/([\w-]+)/) || [])[1];
  $('#recent').innerHTML = S.tasks.slice(0, 15).map((x) => `<button class="nav-item ${x.id === cur ? 'on' : ''}" data-go="/task/${x.id}" title="${esc(x.goal)}">${statusDot(x)}<span>${esc(x.title)}</span></button>`).join('') || `<div class="muted small" style="padding:8px">${t('还没有任务')}</div>`;
}
const statusDot = (x) => `<span class="dot" style="width:7px;height:7px;border-radius:50%;flex:0 0 7px;background:${{ completed: 'var(--ok)', running: 'var(--run)', plan_review: 'var(--gate)', awaiting_approval: 'var(--gate)', failed: 'var(--err)' }[x.status] || 'var(--tx-3)'}"></span>`;
function top(title, sub = '', acts = '') { $('#topTitle').innerHTML = `<h1>${title}</h1>${sub ? `<div class="sub">${sub}</div>` : ''}`; $('#topActs').innerHTML = acts; }
const TASK_STATUS = { triaging: '理解任务中', planning: '生成计划中', plan_review: '正在开始', running: '执行中', completed: '已完成', failed: '失败', cancelled: '已取消',
  paused: '已暂停', awaiting_approval: '等待你批准' };

// ======================= 通知 =======================
let notifES;
function notifStream() {
  notifES?.close(); notifES = new EventSource('/api/stream/notifications');
  notifES.addEventListener('notification', (e) => { const n = JSON.parse(e.data); S.unread++; bell(); toast(t(n.title)); });
  GET('/api/notifications').then((r) => { S.unread = r.unread; bell(); }).catch(() => {});
}
function bell() { const b = $('#bellN'); if (!b) return; b.textContent = S.unread; b.classList.toggle('hidden', !S.unread); }
async function openNotifications() {
  let r; try { r = await GET('/api/notifications'); } catch (e) { return fail(e); }
  await modal(t('通知'), r.items.length ? r.items.map((n) => `<div class="ev ${n.read ? '' : 'run'}"><span class="k"></span><div class="grow">
    ${n.task_id ? `<a data-go="/task/${n.task_id}">${esc(t(n.title))}</a>` : esc(t(n.title))}<div class="muted small">${ago(n.created_at)}</div></div></div>`).join('') : `<div class="empty">${t('暂无通知')}</div>`,
  [{ label: t('全部标为已读'), onClick: async () => { await POST('/api/notifications/read-all'); S.unread = 0; bell(); } }]);
}

// ======================= 登录 =======================
let wxTimer = null;
const stopWx = () => { if (wxTimer) { clearInterval(wxTimer); wxTimer = null; } };

/** 用本地 vendor 的 qrcode-generator 把授权链接画成 SVG：无外域请求，CSP 无需放宽。 */
function renderQr(el, text) {
  if (!el || typeof window.qrcode !== 'function') return false;
  try {
    const q = window.qrcode(0, 'M');
    q.addData(text);
    q.make();
    el.innerHTML = q.createSvgTag({ cellSize: 4, margin: 8, scalable: true });
    return true;
  } catch { return false; }
}

const wxBlock = (inner) => `<div class="wx-div"><span>${t('或')}</span></div>${inner}`;

function wxExpired() {
  const box = $('#wxBox'); if (!box) return;
  box.innerHTML = wxBlock(`<div class="wx-qr wx-dead">${t('二维码已过期')}</div>
    <p style="text-align:center;margin:10px 0 0"><button class="btn sm" id="wxRe">${t('刷新二维码')}</button></p>`);
  $('#wxRe').onclick = () => { mountWechat(); };
}

/** 微信扫码登录：取二维码 → 每 2 秒轮询 → 手机确认后自动登录（会话由服务端下发）。 */
async function mountWechat() {
  stopWx();
  const box = $('#wxBox'); if (!box) return;
  let info;
  try { info = await GET('/api/auth/wechat/start?next=/'); } catch { return; }
  if (!$('#wxBox')) return;                                  // 加载期间页面已重绘
  if (!info.enabled) {
    box.innerHTML = `<div class="wx-off">${t('微信扫码登录未启用')}<br>
      <span class="muted">${t('需在服务端配置 WECHAT_APPID / WECHAT_SECRET / WECHAT_REDIRECT_URI')}</span></div>`;
    return;
  }
  box.innerHTML = wxBlock(`<div class="wx-qr" id="wxQr"></div>
    <p class="small muted" style="text-align:center;margin:10px 0 0">${t('请用微信扫一扫，在手机上确认登录')}</p>`);
  if (!renderQr($('#wxQr'), info.qr_url)) {
    box.innerHTML = `<div class="wx-off">${t('二维码组件未加载，请刷新页面重试')}</div>`;
    return;
  }
  const state = info.state;
  wxTimer = setInterval(() => {
    if (!$('#wxQr')) { stopWx(); return; }                    // 已离开登录页
    GET(`/api/auth/wechat/poll?id=${encodeURIComponent(state)}`, { silent401: true }).then(async (r) => {
      if (r.status === 'ok') { stopWx(); await loadMe(); location.hash = '#' + (r.next || '/'); }
      else if (r.status === 'expired' || r.status === 'consumed') { stopWx(); wxExpired(); }
    }).catch(() => { /* 网络抖动：下一轮继续轮询 */ });
  }, 2000);
}

function viewLogin() {
  $('#app').dataset.shell = '';
  let mode = 'login';
  const draw = () => {
    $('#app').innerHTML = `<div class="auth"><div class="card"><div class="brand" style="padding:0 0 14px">${t('康柏鹦')}</div>
      <div class="field"><label>${t('邮箱')}</label><input id="em" type="email" autocomplete="username"></div>
      ${mode === 'register' ? `<div class="field"><label>${t('昵称')}</label><input id="dn"></div>` : ''}
      <div class="field"><label>${t('密码')}</label><input id="pw" type="password" autocomplete="${mode === 'login' ? 'current-password' : 'new-password'}"></div>
      <button class="btn primary" id="go" style="width:100%;justify-content:center">${t(mode === 'login' ? '登录' : '注册并登录')}</button>
      <div id="wxBox"></div>
      <p class="small muted" style="text-align:center;margin:14px 0 0"><a id="sw">${t(mode === 'login' ? '没有账号？注册' : '已有账号？登录')}</a> · <a id="lg">${LANG() === 'en' ? 'Chinese' : 'English'}</a></p></div></div>`;
    $('#sw').onclick = () => { mode = mode === 'login' ? 'register' : 'login'; draw(); };
    $('#lg').onclick = () => { setLang(LANG() === 'en' ? 'zh-CN' : 'en-US'); draw(); };
    const go = () => once($('#go'), async () => {
      try {
        const d = { email: $('#em').value, password: $('#pw').value };
        if (mode === 'register') d.display_name = $('#dn').value;
        await POST(mode === 'login' ? '/api/auth/login' : '/api/auth/register', d);
        if (mode === 'register') await PATCH('/api/settings', { 'general.ui_language': LANG() === 'en' ? 'en-US' : 'zh-CN' });
        await loadMe();
        location.hash = '#/';
      } catch (e) { fail(e); }
    });
    // 注意：DOM0 处理器中箭头函数「表达体」若返回 false 会阻止默认行为，必须用块体
    $('#go').onclick = go;
    $('#pw').onkeydown = (e) => { if (e.key === 'Enter') go(); };
    mountWechat();
  };
  draw();
}

// ======================= 模型切换器（F28） =======================
const MP = { open: false, anchor: null, items: [], idx: -1, onPick: null, value: null, q: '' };
async function loadModels(force) { if (!S.models || force) S.models = await GET('/api/models'); return S.models; }
function modelLabel(ref) {
  if (!ref || ref === 'auto') return t('自动（按节点分档）');
  for (const g of S.models?.groups || []) for (const m of g.items) if (m.ref === ref) return t(m.display_name);
  return ref;
}
const CAPS = { json: 'JSON', tools: '工具调用', long_ctx: '长上下文', vision: '视觉' };
/** 在 el 中渲染模型按钮；onChange(ref) */
export async function mountPicker(el, value, onChange) {
  try { await loadModels(); } catch (e) { fail(e); return; }
  if (!el.isConnected) return;                           // 加载期间页面已重绘
  el.innerHTML = `<button class="mp-btn" type="button" title="${t('切换模型')} (Ctrl+Shift+M)">${ic('cpu')}<span>${esc(modelLabel(value))}</span>${ic('down')}</button>`;
  el.firstChild.onclick = (e) => { e.stopPropagation(); MP.open && MP.anchor === el.firstChild ? closePicker() : openPicker(el.firstChild, value, (r) => { value = r; el.querySelector('span').textContent = modelLabel(r); onChange(r); }); };
  el.dataset.picker = '1';
}
async function openPicker(anchor, value, onPick) {
  await loadModels(true);
  Object.assign(MP, { open: true, anchor, value, onPick, q: '', idx: -1 });
  const pop = $('#mpPop'); pop.classList.add('on');
  const r = anchor.getBoundingClientRect();
  pop.style.left = Math.min(innerWidth - 340, r.left) + 'px';
  pop.style.top = (r.top > 440 ? r.top - 428 : r.bottom + 6) + 'px';
  pop.innerHTML = `<input id="mpQ" placeholder="${esc(t('搜索模型'))}"><div class="mp-list" id="mpList"></div>`;
  $('#mpQ').oninput = (e) => { MP.q = e.target.value.toLowerCase(); MP.idx = -1; drawPicker(); };
  drawPicker(); $('#mpQ').focus();
}
function drawPicker() {
  const G = { auto: '推荐', builtin: '平台内置', byok: '我的外部服务', local: '本地模型' };
  MP.items = []; let h = '';
  for (const g of S.models.groups) {
    const items = g.items.filter((m) => !MP.q || m.display_name.toLowerCase().includes(MP.q));
    if (!items.length && g.key !== 'byok') continue;
    h += `<div class="mp-g">${t(G[g.key])}</div>`;
    if (!items.length) h += `<div class="small muted" style="padding:2px 8px 6px">${t('还没有，')}<a data-mpgo="1">${t('去设置添加')}</a></div>`;
    for (const m of items) {
      const dis = m.status !== 'ok', i = MP.items.push({ ref: m.ref, dis }) - 1;
      h += `<button class="mp-item ${m.ref === MP.value ? 'sel' : ''} ${dis ? 'dis' : ''} ${i === MP.idx ? 'kb' : ''}" data-i="${i}">
        <span class="grow">${esc(m.ref === 'auto' ? t('自动（按节点分档）') : t(m.display_name))}<br>${(m.capabilities || []).map((c) => `<span class="cap">${t(CAPS[c] || c)}</span>`).join(' ')}
        ${dis ? `<span class="cap" style="color:var(--err)">${t({ key_invalid: 'Key 无效', unavailable: '当前版本不可用', unreachable: '无法连接' }[m.status] || m.status)}</span>` : ''}</span>
        ${m.price_in_per_1m ? `<span class="small muted">¥${m.price_in_per_1m}/${m.price_out_per_1m}</span>` : ''}${m.ref === MP.value ? ic('check') : ''}</button>`;
    }
  }
  $('#mpList').innerHTML = h;
  $('#mpList').onclick = (e) => {
    if (e.target.closest('[data-mpgo]')) { closePicker(); location.hash = '#/settings/models'; return; }
    const b = e.target.closest('[data-i]'); if (b) pickModel(+b.dataset.i);
  };
}
function pickModel(i) {
  const it = MP.items[i]; if (!it) return;
  if (it.dis) { toast(t('该模型当前不可用，请在设置中检查服务商'), true); return; }
  const cb = MP.onPick; closePicker(); cb(it.ref);
}
function closePicker() { MP.open = false; $('#mpPop')?.classList.remove('on'); }
document.addEventListener('click', (e) => { if (MP.open && !e.target.closest('#mpPop') && !e.target.closest('.mp-btn')) closePicker(); });
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'm') {     // 键盘打开最近的模型切换器
    e.preventDefault(); const b = $$('[data-picker] .mp-btn').find((x) => x.offsetParent); if (b) b.click(); return;
  }
  if (!MP.open) return;
  const en = MP.items.map((x, i) => (x.dis ? -1 : i)).filter((i) => i >= 0);
  if (e.key === 'Escape') { e.stopPropagation(); closePicker(); MP.anchor?.focus(); }
  else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault(); if (!en.length) return; const c = en.indexOf(MP.idx);
    MP.idx = en[(c + (e.key === 'ArrowDown' ? 1 : -1) + en.length) % en.length]; drawPicker();
  } else if (e.key === 'Enter') { e.preventDefault(); pickModel(MP.idx >= 0 ? MP.idx : en[0]); }
}, true);

// ======================= 首页 =======================
async function viewHome(qs, my = ROUTE) {
  top(t('任务空间'), t('说出任意职业的任意任务，康柏鹦按需组建 Agent 团队'));
  const s = S.settings;
  let model = s['models.default_model'], playbook = qs.get('playbook') || null, atts = [], outOverride = null;
  $('#view').innerHTML = `<div class="page"><div class="hero"><h2>${t('今天想让康柏鹦帮你做什么？')}</h2><p class="muted">${t('简单问题直接回答；复杂任务先给你看计划，再由多个 Agent 并行协作完成')}</p></div>
    <div class="composer"><textarea id="goal" placeholder="${esc(t('例如：帮我规划一个轻松的杭州周末，同行有老人'))}"></textarea>
      <div id="attList" class="row wrap"></div>
      <div class="composer-bar"><label class="chip" title="${esc(t('添加附件'))}">${ic('upload')}${t('附件')}<input type="file" id="file" class="hidden" multiple accept=".txt,.md,.markdown,.csv,.json,.html,.htm"></label>
        <span id="picker"></span>
        <select id="mode" style="width:auto;padding:4px 8px;font-size:12px"><option value="auto">${t('自动判断复杂度')}</option><option value="quick">${t('仅快速回答')}</option><option value="team">${t('团队协作')}</option></select>
        <span class="small muted">${t('预算')}</span><input id="budget" type="number" step="0.1" min="0.05" max="100" style="width:80px;padding:4px 8px;font-size:12px" value="${s['execution.task_budget_cny']}">
        <span class="grow"></span><button class="btn primary" id="send">${ic('sparkle')}${t('开始')}</button></div>
      <div class="row wrap small" style="margin-top:8px"><span id="langHint" class="muted"></span><span id="pbHint"></span></div></div>
    <div class="row wrap" style="margin:12px 0 26px;gap:6px">${['帮我规划一个杭州两日游，同行有老人，少走路', '调研并对比 Notion、飞书、语雀、石墨四款笔记工具', '帮我写一份关于远程办公效率的分析报告', '帮我制定一个两周的期末复习计划']
      .map((x) => `<button class="chip" data-fill="${esc(t(x))}">${esc(t(x))}</button>`).join('')}</div>
    <h3 style="font-size:14px">${t('最近任务')}</h3><div class="grid2" id="taskGrid"></div></div>`;
  $('#mode').value = s['execution.default_mode'];
  mountPicker($('#picker'), model, (r) => { model = r; });
  const goal = $('#goal');
  const hint = () => {
    const v = goal.value, out = outOverride || (s['general.output_language'] !== 'follow_input' ? s['general.output_language'] : (/[\u4e00-\u9fff]/.test(v) && (v.match(/[\u4e00-\u9fff]/g).length / Math.max(1, v.replace(/\s/g, '').length)) > 0.15 ? 'zh' : (v.trim() ? 'en' : (LANG()))));
    const name = out === 'zh' ? t('中文') : t('英文');
    $('#langHint').innerHTML = v.trim() ? `${t('成果将使用：')}<b>${name}</b> · <a id="olang">${t('更改')}</a>` : '';
    if ($('#olang')) $('#olang').onclick = () => { outOverride = out === 'zh' ? 'en' : 'zh'; hint(); };
    return out;
  };
  const matchPb = debounce(async () => {
    if (playbook || goal.value.trim().length < 6 || !goal.isConnected) return;
    const r = await POST('/api/playbooks/match', { text: goal.value }).catch(() => ({}));
    if (!$('#pbHint')) return;                       // 已离开首页
    $('#pbHint').innerHTML = r.match ? ` · ${t('可复用工作流')}「${esc(r.match.name)}」 <a id="usePb">${t('使用')}</a>` : '';
    if ($('#usePb')) $('#usePb').onclick = () => { playbook = r.match.id; $('#pbHint').innerHTML = ` · <span class="pill ac">${t('将使用工作流')}「${esc(r.match.name)}」</span>`; };
  }, 500);
  goal.oninput = () => { hint(); matchPb(); };
  if (playbook) GET('/api/playbooks/' + playbook).then((p) => { goal.value = p.sample_goal; hint(); $('#pbHint').innerHTML = ` · <span class="pill ac">${t('将使用工作流')}「${esc(p.name)}」</span>`; }).catch(() => { playbook = null; });
  $$('[data-fill]').forEach((b) => (b.onclick = () => { goal.value = b.dataset.fill; hint(); matchPb(); goal.focus(); }));
  $('#file').onchange = async (e) => {
    for (const f of e.target.files) {
      const fd = new FormData(); fd.append('file', f);
      try { const r = await api('POST', '/api/uploads', fd); atts.push(r); drawAtts(); } catch (err) { fail(err); }
    }
    e.target.value = '';
  };
  const drawAtts = () => { $('#attList').innerHTML = atts.map((a, i) => `<span class="chip">${ic('doc')}${esc(a.filename)}<a data-rm="${i}">×</a></span>`).join(''); $$('[data-rm]').forEach((x) => (x.onclick = () => { atts.splice(+x.dataset.rm, 1); drawAtts(); })); };
  const send = () => once($('#send'), async () => {      // 防止双击 / 连按 Ctrl+Enter 创建两个任务
    if (!goal.value.trim()) return toast(t('请先描述目标'), true);
    try {
      const b = parseFloat($('#budget').value);
      const r = await POST('/api/tasks', { goal: goal.value, mode: $('#mode').value, model_choice: model, budget_cny: b > 0 ? b : undefined, attachments: atts.map((a) => a.attachment_id), playbook_id: playbook, output_language: hint() });
      location.hash = '#/task/' + r.task.id;
    } catch (e) { fail(e); }
  });
  $('#send').onclick = send; goal.onkeydown = (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send(); };
  goal.focus();
  await refreshRecent();
  if (stale(my) || !$('#taskGrid')) return;
  $('#taskGrid').innerHTML = S.tasks.map((x) => `<div class="card task-card" data-go="/task/${x.id}"><div class="row">${statusDot(x)}<b class="grow" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.title)}</b><span class="pill">${t(TASK_STATUS[x.status] || x.status)}</span></div>
    <div class="small muted" style="margin:6px 0">${esc(x.goal.slice(0, 70))}</div><div class="progress"><i style="width:${x.progress.total ? (x.progress.done / x.progress.total) * 100 : x.status === 'completed' ? 100 : 0}%"></i></div>
    <div class="row small muted" style="margin-top:6px"><span>${x.lane === 'quick' ? t('快速回答') : t('{d}/{n} 个节点', { d: x.progress.done, n: x.progress.total }) + (x.progress.issues ? ' · ' + t('{n} 个未完成', { n: x.progress.issues }) : '')}</span><span class="grow"></span><span>${money(x.cost_used)}</span><span>${ago(x.updated_at)}</span></div></div>`).join('') || `<div class="empty">${t('还没有任务，试试上面的示例')}</div>`;
}

// ======================= 任务工作区 =======================
const EV_TEXT = {
  'task.created': '任务已创建', 'task.triaged': '复杂度判断：{lane}', 'plan.generating': '正在生成计划', 'plan.invalid': '计划结构无效（{code}），重新规划',
  'plan.drafted': '计划草稿已生成：{nodes} 个节点', 'plan.draft_edited': '计划草稿已修改', 'plan.optimization_applied': '已应用并行优化建议',
  'plan.confirmed': '计划 v{version} 已确认，开始执行', 'plan.version_created': '新建计划 v{version}：{na} 个节点重算，{nr} 个沿用',
  'node.started': '「{title}」开始 · {agent} · {model}', 'node.succeeded': '「{title}」完成',
  'node.failed': '「{title}」失败：{code}', 'node.skipped': '「{title}」跳过（上游未完成）', 'node.retrying': '「{title}」重试中（{code}）', 'node.reused': '「{title}」输入未变，直接沿用已有结果',
  'node.empty_retry': '「{title}」返回空白，已要求补齐正文后重试',
  'node.length_retry': '「{title}」输出超限被截断，已加大预算重试',
  'node.speculative_started': '「{title}」在等待批准期间预执行（只读）', 'node.speculative_done': '「{title}」预执行完成，待批准后生效',
  'node.speculative_committed': '「{title}」预执行结果已提交，无需再次调用模型', 'node.speculative_discarded': '「{title}」预执行结果已作废',
  'node.retry_requested': '已请求重试「{title}」', 'tool.called': '「{title}」调用工具 {tool}', 'tool.result': '工具 {tool} 返回 {items} 条结果',
  'tool.denied': '工具 {tool} 不在白名单内，已拒绝', 'tool.approval_required': '「{title}」要执行危险操作，等你批准：{summary}',
  'tool.approval_denied': '你拒绝了 {tool} 的这个操作，该节点已停止',
  'approval.approved': '你已批准「{title}」', 'attention.created': '关注项：{atitle}', 'model.substituted': '「{title}」模型替换：{requested} → {used}',
  'scheduler.concurrency_changed': '{provider} 并发许可调整为 {permits}（{reason}）', 'run.paused': '已暂停：不再派发新节点，进行中的会完成',
  'run.resumed': '已继续执行', 'run.step': '单节点执行：只派发一个节点', 'budget.exceeded': '平台内置模型额度不足，该节点未执行',
  'task.completed': '任务完成', 'task.failed': '任务失败：{code}', 'task.cancelled': '任务已取消', 'comment.added': '新评论：{body}',
  'quick.started': '快速回答 · {model}', 'artifact.archived': '成果已沉淀到知识库', 'artifact.edited': '你修改了成果「{title}」（{chars} 字）',
  'run.recovered': '服务重启后恢复执行', 'task.model_changed': '任务模型改为 {model_choice}',
};
const EV_KIND = (ty) => ty.includes('failed') || ty.includes('exceeded') || ty.includes('denied') ? 'err' : ty.includes('approval') || ty.includes('attention') || ty.includes('substituted') || ty.includes('skipped') ? 'gate'
  : ty.endsWith('succeeded') || ty.endsWith('completed') || ty.endsWith('committed') || ty.endsWith('reused') ? 'ok' : ty.includes('started') || ty.includes('running') ? 'run' : '';

// 画布成果节点（v1.6）：交付物直接以节点形态进入任务图，节点状态（固定/隐藏）由 CanvasView 持久化。
// 「节点管理」模块管理当前工作流与历史工作流的全部节点与成果。

class TaskView {
  constructor(id) {
    this.id = id; this.events = []; this.sel = null; this.tab = 'overview'; this.dock = 'events'; this.detail = null; this.replay = null;
    this.refresh = debounce(() => this.load(), 250); this.alive = true; this.mount();
    this.mg = { open: false, kind: 'all', q: '', ver: {} };   // 节点管理模块状态
    this.selArtKey = null;                                    // v1.8：右侧面板展示的成果节点（点成果节点即打开）
    this.artEdit = null;                                      // v1.8：成果编辑器状态 {key, draft, orig}
  }
  destroy() { this.alive = false; this.es?.close(); this.es = null; clearTimeout(this._rt); this.refresh.cancel?.(); this.cv?.destroy(); }
  remount() { this.destroy(); S.view = new TaskView(this.id); }
  get T() { return this.snap?.task; }
  titleOf(k) { return this.snap?.nodes.find((n) => n.node_key === k)?.title || k || ''; }

  async mount() {
    $('#view').innerHTML = `<div class="work" id="work"><div class="work-main"><div class="canvastop" id="ctop"></div>
      <nav id="mobileViews" class="mobile-views"><button data-mobile="result">${t('成果')}</button><button data-mobile="steps">${t('节点')}</button><button data-mobile="canvas">${t('画布')}</button></nav><section id="mobileRead" class="mobile-read"></section><div id="cvHost" style="flex:1;display:flex;min-height:0"></div>
      <div class="dock" id="dock"><div class="tabs" id="dockTabs"></div><div class="dock-body" id="dockBody"></div></div><div class="steer" id="steer"></div></div>
      <aside class="insp" id="insp"></aside>
      <div class="insp-resz" id="inspResz" title="${esc(t('左右拖动调整面板宽度（双击重置）'))}"></div>
      <aside class="mgmt hidden" id="mgmt"><div class="mgmt-h"><b>${t('节点管理')}</b><span class="small muted" id="mgmtSum"></span><span class="grow"></span>
        <button class="btn sm" data-mgc="tidy">${ic('layers')}${t('整理布局')}</button>
        <button class="icon-btn" data-mgc="close" title="${esc(t('关闭'))}">${ic('x')}</button></div>
        <div class="mgmt-b" id="mgmtBody"></div></aside></div>`;
    this.cv = new CanvasView($('#cvHost'), {
      onSelect: (k) => this.select(k), onZoom: (z) => { const zv = $('#zv'); if (zv) zv.textContent = Math.round(z * 100) + '%'; },
      onSelectionClear: () => { this.sel = null; this.detail = null; this.drawInsp(); },
      onLayout: (l) => { if (!this.replay) PUT(`/api/tasks/${this.id}/layout`, l).catch(() => {}); },
      onOpenGantt: () => { this.dock = 'gantt'; this.drawDock(); }, isDraft: () => this.T?.status === 'plan_review',
      onEditToggle: () => this.toggleEdit(), onEditCancel: () => this.cancelEdit(), onEditApply: (ops) => this.applyEdit(ops),
      onAddRequest: (at) => this.addStep(at), onEditChange: () => this.drawCtop(),
      onArtZoom: (c) => this.zoomArt(c), onArtState: () => { this.drawMgmt(); if (this.selArtKey) this.drawInsp(); },
      onArtSelect: (c, key) => this.onArtSelect(c, key), onArtEdit: (c, key) => this.onArtEdit(c, key),
    });
    this.bindMgmt();
    $('#mobileViews').onclick = (e) => { const mode = e.target.closest('[data-mobile]')?.dataset.mobile; if (mode) { this.mobileMode = mode; this.drawMobile(); } };
    this.bindInspResize();
    this.layout = await GET(`/api/tasks/${this.id}/layout`).catch(() => ({}));
    if (!this.alive) return;                               // 加载期间已离开页面：不要再建立实时连接
    if (this.layout?.collapsed && 'minimap' in this.layout.collapsed) this.cv.mmCol = this.layout.collapsed.minimap;
    this.cv.setArtState(this.layout?.art);
    try { this.events = (await GET(`/api/tasks/${this.id}/events?limit=2000`)).items; } catch (e) { if (this.alive) fail(e); return; }
    if (!this.alive) return;
    await this.load();
    if (this.alive) this.connect();
  }
  connect() {
    if (!this.alive) return;
    const last = this.events.at(-1)?.seq || 0;
    this.es?.close(); const es = this.es = new EventSource(`/api/tasks/${this.id}/stream?since_seq=${last}`);
    const on = (e) => {
      if (!this.alive || es !== this.es) return;           // 已销毁或已被新连接取代
      let ev; try { ev = JSON.parse(e.data); } catch { return; }
      if (ev.seq <= (this.events.at(-1)?.seq || 0)) return;
      this.events.push(ev); if (this.events.length > 3000) this.events.splice(0, this.events.length - 2000);
      this.refresh(); if (this.dock === 'events' && !this.replay) this.drawDock();
    };
    Object.keys(EV_TEXT).concat(['scheduler.error', 'attention.resolved', 'node.feedback', 'task.escalated']).forEach((ty) => es.addEventListener(ty, on));
    es.onopen = () => { this._retry = 0; };
    es.onerror = () => {
      // 网络抖动时浏览器会自动重连；但服务端返回非 200（如 429 / 401）时连接会被永久关闭，需要自己退避重连
      if (!this.alive || es !== this.es || es.readyState !== EventSource.CLOSED) return;
      this._retry = Math.min(6, (this._retry || 0) + 1);
      clearTimeout(this._rt); this._rt = setTimeout(() => { if (this.alive) { this.load(); this.connect(); } }, 1000 * 2 ** this._retry);
    };
  }
  async load() {
    if (!this.alive) return;
    const seq = (this._loadSeq = (this._loadSeq || 0) + 1);   // 只采用最后一次发出的快照请求，避免旧快照覆盖新快照
    let snap;
    try { snap = await GET(`/api/tasks/${this.id}/state`); } catch (e) {
      if (!this.alive || seq !== this._loadSeq) return;
      if (e.status === 404) { toast(t('任务不存在'), true); location.hash = '#/'; }
      else if (e.status !== 401 && Date.now() - (this._failAt || 0) > 10000) { this._failAt = Date.now(); fail(e); }   // 节流，避免刷屏
      return;
    }
    if (!this.alive || seq !== this._loadSeq) return;
    this.snap = snap;
    this.drawMobile();
    const st = this.T.status;
    if (['triaging', 'planning'].includes(st)) { this.drawWaiting(); setTimeout(() => this.alive && this.refresh(), 800); return; }
    if (!this.alive) return;
    $('#work').classList.remove('noinsp');
    if (this.drawTop() === false) return;           // 已重新挂载为新的视图实例，旧实例停止渲染
    this.drawCtop();
    if (this.T.lane === 'quick' && st === 'completed') return this.drawQuick();
    if (!this.replay) this.cv.setData(this.snap.nodes, this.snap.estimate, this.layout);
    if (this.sel && !this.snap.nodes.some((n) => n.node_key === this.sel)) { this.sel = null; this.detail = null; }
    if (!this.sel && !this.cv.selectedKeys.size && st === 'running') {
      const g = this.snap.nodes.find((n) => n.status === 'awaiting_approval');
      if (g) { this.sel = g.node_key; this.cv.select(this.sel); }
    }
    this.drawInsp(); this.drawDock(); this.drawSteer();
    if (this.sel && this.tab !== 'overview') this.loadDetail();
    this.syncArts(st);                                     // 成果以节点形态进入任务图（v1.6：不再是悬浮面板）
    refreshRecent();
  }
  /** 成果节点（v1.6）：任务有成果时把交付物（图 / 文 / 文件）作为节点挂进任务图；修订重算期间标注旧版本。 */
  async syncArts(st) {
    if (this.replay || this.T.lane === 'quick' || !this.snap.artifact) return this.cv.setArtifacts([], null);
    const aid = this.snap.artifact.id;
    if (this._art?.id === aid) return this.renderArts();
    if (this._artFetch === aid) return;                    // 同一份成果的请求已在途
    this._artFetch = aid;
    try {
      const a = await GET(`/api/tasks/${this.id}/artifact`);
      if (this.alive && a) { this._art = a; if (this.snap?.artifact?.id === aid) this.renderArts(); }
    } catch { /* 忽略：下一次快照刷新会再取 */ } finally { if (this._artFetch === aid) this._artFetch = null; }
    void st;
  }
  renderArts() {
    const a = this._art; if (!a || !this.alive) return;
    const cards = a.cards?.length ? a.cards : fallbackCards(a.sections);
    const active = this.snap?.plan_version;
    // 正文随成果一并传给画布：文档节点里直接显示全文（限高滚动），不再点开弹层
    this.cv.setArtifacts(cards, { id: a.id, version: a.plan_version, stale: active != null && a.plan_version < active, sections: a.sections || [] });
    this.drawMgmt();
    this.drawMobile();
  }
  drawMobile() {
    if (!this.snap || !$('#mobileRead')) return;
    const mode = this.mobileMode || (this.T.status === 'completed' ? 'result' : 'steps');
    $('#work').classList.toggle('mobile-reading', mode !== 'canvas');
    $$('#mobileViews button').forEach((b) => b.classList.toggle('on', b.dataset.mobile === mode));
    if (mode === 'canvas') return;
    if (mode === 'steps') {
      $('#mobileRead').innerHTML = `<h2>${t('节点')}</h2>${this.snap.nodes.map((n) => `<article class="card"><b class="user-content">${esc(n.title)}</b><p>${t(({ succeeded: '已完成', running: '执行中', failed: '失败', skipped: '已跳过', pending: '等待中' })[n.status] || n.status)}</p></article>`).join('') || `<p>${t(TASK_STATUS[this.T.status] || this.T.status)}</p>`}`;
    } else {
      const art = this._art;
      $('#mobileRead').innerHTML = `<h2>${deliveryLabel(this.T.completion_quality)}</h2>${art ? art.sections.map((s) => `<article class="card"><h3 class="user-content">${esc(s.title)}</h3>${md(s.content, s.citations)}</article>`).join('') : `<p>${t('成果生成后显示在这里')}</p>`}`;
    }
  }
  /** 成果节点上的「放大查看」：把内容放大到弹窗里看（内容平时就直显在节点里；v2.0 地图弹窗为可操控地图；
   *  v3.0 十类功能性节点在弹窗里展开完整形态——表格带搜索 / 排序 / 导出 CSV，代码不截断，图片灯光箱）。 */
  zoomArt(c) {
    if (c?.kind === 'map' && c.map) {
      const live = leafletAvailable();
      modal(c.title || t('地图'), live ? '<div class="dv-zoom dv-mapfull"><div class="im-map im-big"></div></div>'
        : `<div class="dv-zoom dv-mapfull">${renderMap(c.map)}</div>`, [], 720);
      if (live) { const box = document.querySelector('#overlay .im-map'); if (box) mountLeafletMap(box, c.map, { key: 'modal:' + artKey(c) }); }
      return;
    }
    if (c?.kind === 'chart' && c.chart) { modal(c.title || t('图表'), `<div class="dv-zoom">${renderChart(c.chart)}</div>`, [], 720); return; }
    const d = c?.[c?.kind];
    if (!ART_KINDS.includes(c?.kind) || !d) return;
    const wide = c.kind === 'table' || c.kind === 'calendar' || c.kind === 'code' || c.kind === 'chat';
    modal(c.title || t(ART_LBL[c.kind] || '成果'), `<div class="dv-zoom av-full-host">${renderArt(c.kind, d, { compact: false })}</div>`, [], wide ? 780 : 640);
    const host = document.querySelector('#overlay .av-full-host');   // modal 同步写入 #overlay，可立即取到容器
    if (host) bindArt(host, c.kind, d, { localSort: true });   // 弹窗内的排序 / 搜索 / 导出是本地状态，不写回布局
  }

  // ---------- 成果详情面板（v1.8）：点击画布成果节点 → 右侧展示，可直接修改 ----------
  artCard() { return (this._art?.cards || []).find((x) => artKey(x) === this.selArtKey) || null; }
  /** 画布上点选 / 取消选中成果节点：右侧面板同步展示这一份成果。 */
  onArtSelect(c, key) {
    if (this.artEdit?.key && this.artEdit.key !== key && this.artEdit.draft !== this.artEdit.orig) toast(t('未保存的修改已丢弃'));
    this.artEdit = null;
    this.selArtKey = key || null;
    if (key && c) $('#insp').classList.add('open');
    this.drawInsp();
  }
  /** 「⋯」菜单里的「编辑内容」：打开右侧面板并直接进入编辑态。 */
  onArtEdit(c, key) { this.onArtSelect(c, key); this.artEditStart(); }
  artEditStart() {
    const c = this.artCard();
    if (!c || c.kind !== 'doc' || this.replay) return;
    const sec = (this._art?.sections || []).find((s) => s.section_key === c.section_key);
    const src = String(sec?.content ?? c.excerpt ?? '');
    this.artEdit = { key: this.selArtKey, draft: src, orig: src, focused: false };
    this._drawInsp();                                      // 编辑态要立即出现（drawInsp 在编辑中会跳过重绘）
    $('#artTa')?.focus();
  }
  artEditCancel() { this.artEdit = null; this.drawInsp(); }
  /** 保存人工修改：PATCH 后立即重建成果——画布节点、导出与打包文件同步更新。 */
  async artEditSave() {
    const e = this.artEdit; if (!e) return;
    const c = this.artCard(); if (!c || c.kind !== 'doc') return;
    if (String(e.draft).trim() === String(e.orig).trim()) return this.artEditCancel();   // 没变化就当作取消
    const content = e.draft;
    this.wrap(async () => {
      await PATCH(`/api/tasks/${this.id}/sections/${encodeURIComponent(c.section_key)}`, { content });
      this.artEdit = null;
      await this.reloadArt();
      toast(t('已保存：画布与交付文件已更新；之后的新对话会让 AI 以你的修改为基础继续'));
    });
  }
  async reloadArt() {
    try { const a = await GET(`/api/tasks/${this.id}/artifact`); if (this.alive && a) { this._art = a; this.renderArts(); this.drawInsp(); } } catch { /* 下一次快照刷新会再取 */ }
  }
  _drawArtInsp() {
    const el = $('#insp');
    const c = this.artCard();
    if (!c) { this.selArtKey = null; return (el.innerHTML = this.summaryPanel(), this.bindInsp()); }
    const a = this._art || {}, KIND = { map: '地图', chart: '图表', doc: '文档', file: '文件', ...ART_LBL };
    const sec = c.kind === 'doc' ? (a.sections || []).find((s) => s.section_key === c.section_key) : null;
    const editing = !!this.artEdit && this.artEdit.key === this.selArtKey;
    const ro = !!this.replay || this.T?.status === 'cancelled';
    let body = '';
    if (c.kind === 'map') {
      const live = leafletAvailable();
      body = `<div class="field"><div class="label">${t('地图')}</div><div class="artp-chart">${live ? '<div class="im-map im-insp"></div>' : (renderMap(c.map) || '')}</div>
        <p class="small muted">${t('这张路线图由「{s}」节点产出；可直接在地图上拖动、缩放；如需调整行程，可在下方对话里说明。', { s: this.titleOf(c.node_key) || c.node_key || '—' })}</p></div>`;
    } else if (c.kind === 'chart') {
      body = `<div class="field"><div class="label">${t('图表')}</div><div class="artp-chart">${renderChart(c.chart) || ''}</div>
        <p class="small muted">${t('这张图由「{s}」节点产出；如需改动，可在下方对话里说明。', { s: this.titleOf(c.node_key) || c.node_key || '—' })}</p></div>`;
    } else if (c.kind === 'doc') {
      const src = editing ? this.artEdit.draft : String(sec?.content ?? '');
      const meta = [t('共 {n} 字', { n: src.length }), sec?.citations?.length ? t('{n} 条引用', { n: sec.citations.length }) : '',
        c.user_edited ? t('已被你修改') : ''].filter(Boolean).join(' · ');
      body = editing
        ? `<textarea id="artTa" class="artp-ta" spellcheck="false">${esc(this.artEdit.draft)}</textarea>
           <p class="small muted" style="margin:6px 0 0">${t('保存后：画布与交付文件立即更新；之后在对话里提出新要求时，AI 会以你的修改为基础继续。')}</p>`
        : `<div class="row small muted" style="margin-bottom:6px"><span>${esc(meta)}</span></div><div class="artp-body">${md(src)}</div>`;
    } else if (ART_KINDS.includes(c.kind) && c[c.kind]) {
      // v3.0：十类功能性成果在右栏展开完整形态（与画布共用同一份 L2 视图状态：勾选 / 排序）
      body = `<div class="field"><div class="label">${t(ART_LBL[c.kind])}</div>
        <div class="artp-art av-insp-host">${renderArt(c.kind, c[c.kind], { compact: false, key: artKey(c), checks: this.cv.art.checks, sorts: this.cv.art.sorts })}</div>
        <p class="small muted">${t('这份成果由「{s}」节点产出；如需改动，可在下方对话里说明。', { s: this.titleOf(c.node_key) || c.node_key || '—' })}</p></div>`;
    } else {
      body = `<div class="field"><div class="label">${t('文件')}</div><p class="small muted mono">${esc(c.path || '')}</p>
        <a class="btn" href="/api/workspace/file/download?path=${encodeURIComponent(c.path || '')}" draggable="false">${ic('down')}${t('下载文件')}</a></div>`;
    }
    const foot = [];
    if (!ro) {
      if (c.kind === 'doc') {
        if (editing) foot.push(`<button class="btn primary" data-i="artsave">${ic('check')}${t('保存修改')}</button><button class="btn" data-i="artcancel">${t('取消')}</button>`);
        else foot.push(`<button class="btn primary" data-i="artedit">${ic('pen')}${t('编辑内容')}</button><button class="btn" data-i="artrewrite">${ic('sparkle')}${t('让 AI 重写这一节')}</button>`);
      }
      if (c.kind === 'chart' || c.kind === 'map' || ART_KINDS.includes(c.kind)) foot.push(`<button class="btn" data-i="artzoom">${ic('zin')}${t('放大查看')}</button>`);
      if (ART_KINDS.includes(c.kind)) foot.push(`<button class="btn" data-i="artcsv">${ic('down')}${t('导出数据（CSV）')}</button>`);
      if (c.kind === 'file') foot.push(`<a class="btn" href="/api/workspace/file/download?path=${encodeURIComponent(c.path || '')}" draggable="false">${ic('down')}${t('下载文件')}</a>`);
      if (c.node_key) foot.push(`<span class="grow"></span><button class="btn sm" data-i="artlocate">${ic('eye')}${t('定位来源节点')}</button>`);
    }
    el.innerHTML = `<div class="insp-head"><div class="row small muted"><a data-i="artback">${t('← 任务概览')}</a><span class="grow"></span><span>${esc(t('成果'))}</span><button class="icon-btn" data-i="close" title="${esc(t('关闭'))}">${ic('x')}</button></div>
      <h2>${esc(c.title || t('成果'))}</h2>
      <div class="row small"><span class="pill">${t(KIND[c.kind] || '成果')}</span>${c.user_edited ? `<span class="pill gate">${ic('pen')}${t('已被你修改')}</span>` : ''}
      ${c.node_key ? `<span class="muted">${t('来自节点：')}${esc(this.titleOf(c.node_key) || c.node_key)}</span>` : ''}</div></div>
      <div class="insp-body">${body}</div>${foot.length ? `<div class="insp-foot">${foot.join('')}</div>` : ''}`;
    if (c.kind === 'map') { const box = el.querySelector('.im-map'); if (box) mountLeafletMap(box, c.map, { key: 'insp:' + artKey(c) }); }   // v2.0：右栏投影为可操控地图
    if (ART_KINDS.includes(c.kind)) {                      // v3.0：右栏的勾选 / 排序与画布共用同一份持久视图状态
      const host = el.querySelector('.av-insp-host'), key = artKey(c);
      if (host) bindArt(host, c.kind, c[c.kind], {
        onCheck: (i) => { this.cv.toggleArtCheck(key, i); this.drawInsp(); },
        onSort: (i) => { this.cv.toggleArtSort(key, i); this.drawInsp(); },
      });
    }
    this._bindArtInsp(c);
  }
  _bindArtInsp(c) {
    const el = $('#insp');
    el.onclick = (e) => {
      const a = e.target.closest('[data-i]')?.dataset.i; if (!a) return;
      if (a === 'artback') { this.sel = null; this.cv.selectArt(null); this.drawInsp(); return; }       // selectArt(null) 回调会清掉成果选中与编辑态
      if (a === 'close') { el.classList.remove('open'); this.sel = null; this.cv.selectArt(null); this.drawInsp(); return; }
      if (a === 'artedit') { this.artEditStart(); return; }
      if (a === 'artcancel') { this.artEditCancel(); return; }
      if (a === 'artsave') { this.artEditSave(); return; }
      if (a === 'artzoom') { this.zoomArt(c); return; }
      if (a === 'artcsv') { artExport(c.kind, c[c.kind]); return; }
      if (a === 'artlocate' && c.node_key) { this.cv.selectArt(null); this.select(c.node_key); this.cv.center(c.node_key); return; }
      if (a === 'artrewrite' && c.node_key) {
        this.revise({ instructions: { [c.node_key]: ['请重写这一节'] } },
          `/api/tasks/${this.id}/sections/${encodeURIComponent(c.section_key)}/rewrite`, { instructions: '请重写这一节' });
      }
    };
    const ta = $('#artTa');
    if (ta) {
      ta.oninput = () => { if (this.artEdit) this.artEdit.draft = ta.value; };
      if (this.artEdit && !this.artEdit.focused) { ta.focus(); this.artEdit.focused = true; }
    }
  }

  // ---------- 节点管理模块（v1.6） ----------
  bindMgmt() {
    const el = $('#mgmt'); if (!el) return;
    el.onclick = (e) => {
      const c = e.target.closest('[data-mgc]');
      if (c) {
        const a = c.dataset.mgc;
        if (a === 'close') this.toggleMgmt(false);
        else if (a === 'tidy') { this.cv.tidy(); toast(t('已整理布局，Ctrl+Z 可撤销')); }
        else if (a === 'focus') { this.cv.setArtFocus(!this.cv.art.focus); this.drawMgmt(); }
        else if (a === 'showall') this.cv.showAllArt();
        return;
      }
      const mf = e.target.closest('[data-mgf]');
      if (mf) { this.mg.kind = mf.dataset.mgf; this.drawMgmt(); return; }
      const vh = e.target.closest('[data-mgv]');
      if (vh) { this.toggleVersion(+vh.dataset.mgv); return; }
      const b = e.target.closest('[data-mg]');
      if (b) {
        const key = b.dataset.key;
        if (b.dataset.mg === 'focus') this.cv.focusArt(key);
        else if (b.dataset.mg === 'pin') this.cv.toggleArtPinned(key);
        else if (b.dataset.mg === 'hide') this.cv.setArtHidden(key, true);
        else if (b.dataset.mg === 'show') this.cv.setArtHidden(key, false);
        else if (b.dataset.mg === 'csv') { const row = this.cv.artList().find((x) => x.key === key); if (row) artExport(row.card.kind, row.card[row.card.kind]); }
        this.drawMgmt();
      }
    };
    el.oninput = (e) => {
      const q = e.target.closest('[data-mgq]');
      if (q) { this.mg.q = q.value.trim(); this.drawMgmt(); }
    };
  }
  // v2.1.1：右栏「任务概览 / 节点详情」面板宽度可拖动（拖左缘调宽、双击重置、按用户持久化）。
  // 面板内元素（地图 / 图表 / 文本）随宽度自适应：地图由 imap.js 的 ResizeObserver 自动 invalidateSize + 重取全貌。
  bindInspResize() {
    const work = $('#work'); const bar = $('#inspResz');
    if (!work || !bar) return;
    const KEY = 'combine.inspW';
    const MIN = 320; const DEF = 360;
    const max = () => Math.max(MIN, Math.min(760, Math.round(window.innerWidth * 0.62)));
    const clamp = (w) => Math.max(MIN, Math.min(max(), Math.round(w)));
    const stored = parseInt(localStorage.getItem(KEY) || '', 10);
    if (Number.isFinite(stored)) work.style.setProperty('--insp-w', clamp(stored) + 'px');
    const save = () => {
      const w = parseInt(getComputedStyle(work).getPropertyValue('--insp-w'), 10);
      if (Number.isFinite(w)) localStorage.setItem(KEY, String(w));
    };
    let dragging = false;
    bar.onpointerdown = (e) => {
      if (e.button !== 0) return;
      dragging = true; bar.classList.add('on');
      bar.setPointerCapture(e.pointerId); e.preventDefault();
    };
    bar.onpointermove = (e) => {
      if (!dragging) return;
      const w = clamp(work.getBoundingClientRect().right - e.clientX);
      work.style.setProperty('--insp-w', w + 'px');
    };
    const stop = () => {
      if (!dragging) return;
      dragging = false; bar.classList.remove('on'); save();
    };
    bar.onpointerup = stop;
    bar.onpointercancel = stop;
    bar.ondblclick = () => { work.style.setProperty('--insp-w', DEF + 'px'); save(); };
  }
  toggleMgmt(on) {
    this.mg.open = on === undefined ? !this.mg.open : !!on;
    const el = $('#mgmt'); if (!el) return;
    el.classList.toggle('hidden', !this.mg.open);
    if (this.mg.open) this.drawMgmt();
    this.drawCtop();
  }
  /** 当前工作流节点树 + 历史工作流 + 自动管理说明：所有节点（节点 / 成果）在这里集中管理。 */
  drawMgmt() {
    const body = $('#mgmtBody'); if (!body || !this.mg.open || !this.snap) return;
    const arts = this.cv.artList(), steps = this.snap.nodes;
    const sum = $('#mgmtSum');
    const hiddenN = arts.filter((a) => a.hidden).length;
    if (sum) sum.textContent = `${t('{n} 个节点', { n: steps.length })} · ${t('{n} 项成果', { n: arts.length })}${hiddenN ? ' · ' + t('{n} 项已隐藏', { n: hiddenN }) : ''}`;
    const kindF = this.mg.kind, q = this.mg.q.toLowerCase();
    // 已隐藏的成果始终留在模块里（这里才是恢复它们的入口），只是标记「已隐藏」并变灰
    const match = (a) => (kindF === 'all' || a.card.kind === kindF) && (!q || (a.card.title || '').toLowerCase().includes(q));
    const filtersActive = kindF !== 'all' || !!q;
    const byNode = {};
    for (const a of arts) (byNode[a.card.node_key || ''] ||= []).push(a);

    const ICON = { map: 'pin', chart: 'chart', doc: 'doc', file: 'file', ...ART_ICON };
    const LBL = { map: '地图', chart: '图表', doc: '文档', file: '文件', ...ART_LBL };
    const KIND_ORDER = ['map', 'table', 'metrics', 'chart', 'list', 'timeline', 'calendar', 'image', 'code', 'web', 'status', 'chat', 'doc', 'file'];
    const artRow = (a) => {
      const c = a.card;
      return `<div class="mg-art ${a.hidden ? 'off' : ''}">
        <span class="mg-ic k-${esc(c.kind)}">${ic(ICON[c.kind] || 'doc')}</span>
        <span class="mgmt-xt grow" title="${esc(c.title || '')}">${esc(c.title || '')}</span>
        ${a.pinned ? `<span class="an-tag">${t('已固定')}</span>` : ''}
        ${a.hidden ? `<span class="an-tag">${t('已隐藏')}</span>` : ''}
        <span class="mg-acts">
          <button class="icon-btn" data-mg="focus" data-key="${esc(a.key)}" title="${esc(t('定位'))}">${ic('eye')}</button>
          <button class="icon-btn" data-mg="pin" data-key="${esc(a.key)}" title="${esc(t(a.pinned ? '取消固定' : '固定在画布'))}">${ic('pin')}</button>
          ${a.hidden
            ? `<button class="icon-btn" data-mg="show" data-key="${esc(a.key)}" title="${esc(t('恢复显示'))}">${ic('check')}</button>`
            : `<button class="icon-btn" data-mg="hide" data-key="${esc(a.key)}" title="${esc(t('从画布隐藏'))}">${ic('eyeoff')}</button>`}
          ${c.kind === 'file' ? `<a class="icon-btn" href="/api/workspace/file/download?path=${encodeURIComponent(c.path)}" title="${esc(t('下载'))}">${ic('down')}</a>`
            : ART_KINDS.includes(c.kind) ? `<button class="icon-btn" data-mg="csv" data-key="${esc(a.key)}" title="${esc(t('导出数据（CSV）'))}">${ic('down')}</button>` : ''}
        </span></div>`;
    };
    const stepGroup = (n, list) => `<div class="mg-step"><span class="dot" style="background:${{ succeeded: 'var(--ok)', running: 'var(--run)', failed: 'var(--err)', awaiting_approval: 'var(--gate)' }[n.status] || 'var(--tx-3)'}"></span>
        <b class="mgmt-xt grow">${esc(n.title)}</b>${(byNode[n.node_key] || []).length ? `<span class="pill">${t('{n} 项成果', { n: byNode[n.node_key].length })}</span>` : ''}</div>
      ${list.map(artRow).join('')}`;
    const groups = [];
    for (const n of steps) {
      const list = (byNode[n.node_key] || []).filter(match);
      if (filtersActive && !list.length) continue;
      groups.push(stepGroup(n, list));
    }
    const orphans = (byNode[''] || []).filter(match);
    if (orphans.length) groups.push(`<div class="mg-step"><span class="dot"></span><b class="mgmt-xt grow">${t('任务级交付')}</b></div>${orphans.map(artRow).join('')}`);

    const reasons = { manual: '手动修订', steer: '补充要求', canvas_edit: '画布编辑', reassign: '更换 Agent', model_override: '更换模型', section_rewrite: '重写章节' };
    const verRow = (v) => `<div class="mg-ver ${v.status === 'active' ? 'on' : ''}">
      <div class="mg-vh" data-mgv="${v.version}"><span class="pill ${v.status === 'active' ? 'ok' : ''}">v${v.version}</span>
        <span class="mgmt-xt">${esc(t(reasons[v.reason] || v.reason))}</span><span class="grow"></span>
        <span class="small muted">${v.status === 'active' ? t('当前') : ago(v.created_at)}</span><span class="mg-caret">▸</span></div>
      <div class="mg-vb hidden" data-mgvb="${v.version}"></div></div>`;

    body.innerHTML = `<div class="mg-sec"><h4>${t('当前工作流')} ${this.snap.plan_version ? `<span class="pill ac">v${this.snap.plan_version}</span>` : ''}
        <span class="grow"></span>${hiddenN ? `<button class="btn sm" data-mgc="showall">${t('全部恢复')}</button>` : ''}</h4>
      <div class="mg-filter"><button class="chip ${kindF === 'all' ? 'on' : ''}" data-mgf="all">${t('全部')}</button>
        ${KIND_ORDER.filter((k) => arts.some((x) => x.card.kind === k)).map((k) => `<button class="chip ${kindF === k ? 'on' : ''}" data-mgf="${k}">${ic(ICON[k])}${t(LBL[k])}</button>`).join('')}</div>
      <input id="mgmtQ" data-mgq placeholder="${esc(t('搜索成果…'))}" value="${esc(this.mg.q)}">
      ${groups.length ? groups.join('') : `<div class="empty small">${arts.length ? t('没有匹配的成果') : t('任务完成后，成果会以节点形式出现在任务图中。')}</div>`}</div>
      <div class="mg-sec"><h4>${t('历史工作流')}<span class="small muted" style="font-weight:400">${t('旧版本成果自动归档，不混入当前画布')}</span></h4>
      ${this.snap.versions.map(verRow).join('') || `<div class="empty small">${t('还没有历史版本')}</div>`}</div>
      <div class="mg-sec"><h4>${t('自动管理')}</h4>
      <ul class="mg-auto">
        <li>${t('自动直显：成果内容直接长在节点里——图直接画、文直接读、文件直接下载，不用点开')}</li>
        <li>${t('自动归属：成果自动连线到产出它的节点')}</li>
        <li>${t('自动关联引用：下游答案自动连线到它引用的上游答案，关系一目了然')}</li>
        <li>${t('自动排版：成果排在任务图右侧、按产出节点分层，互不重叠')}</li>
        <li>${t('自动去重：同一章节 / 图表多次修订只保留最新一份')}</li>
        <li>${t('自动过期标记：修订重算期间旧成果标注版本')}</li>
        <li>${t('自动归档：历史版本成果收进这里，不混入当前画布')}</li>
      </ul>
      <div class="mg-auto-row"><span class="grow">${t('新成果出现时自动居中展示')}</span>
        <button class="switch ${this.cv.art.focus ? 'on' : ''}" data-mgc="focus" role="switch" aria-checked="${!!this.cv.art.focus}"><i></i></button></div></div>`;
    const box = $('#mgmt');
    const qi = $('#mgmtQ');
    if (qi && box && this.mg.q) { qi.focus(); qi.setSelectionRange(qi.value.length, qi.value.length); }   // 搜索中重绘：恢复焦点与光标
  }
  async toggleVersion(v) {
    const host = document.querySelector(`[data-mgvb="${v}"]`); if (!host) return;
    if (!host.classList.contains('hidden')) { host.classList.add('hidden'); host.closest('.mg-ver').classList.remove('open'); return; }
    host.classList.remove('hidden'); host.closest('.mg-ver').classList.add('open');
    if (this.mg.ver[v] === undefined) {
      host.innerHTML = `<div class="small muted">${t('正在读取…')}</div>`;
      try { this.mg.ver[v] = await GET(`/api/tasks/${this.id}/artifact?version=${v}`); } catch { this.mg.ver[v] = null; }
    }
    if (!this.alive || !host.isConnected) return;
    const a = this.mg.ver[v];
    host.innerHTML = a ? this.verBody(a) : `<div class="small muted">${t('该版本没有成果记录（可能被取消，或修订后尚未完成）。')}</div>`;
  }
  verBody(a) {
    const cards = a.cards?.length ? a.cards : fallbackCards(a.sections);
    const ICON = { map: 'pin', chart: 'chart', doc: 'doc', file: 'file', ...ART_ICON };
    return `<div class="row small" style="margin:4px 0 6px"><span class="muted">${t('{n} 项成果', { n: cards.length })} · ${hhmmss(a.created_at)}</span><span class="grow"></span>
      <a class="btn sm" href="/api/tasks/${this.id}/artifact/export?format=md&version=${a.plan_version}">${ic('doc')}${t('导出 Markdown')}</a></div>
      ${cards.map((c) => `<div class="mg-art ro"><span class="mg-ic k-${esc(c.kind)}">${ic(ICON[c.kind] || 'doc')}</span>
        <span class="mgmt-xt grow" title="${esc(c.title || '')}">${esc(c.title || '')}</span>
        ${c.kind === 'file' ? `<a class="icon-btn" href="/api/workspace/file/download?path=${encodeURIComponent(c.path)}" title="${esc(t('下载'))}">${ic('down')}</a>` : ''}</div>`).join('')}`;
  }
  drawWaiting() {
    top(esc(this.T.title), t(TASK_STATUS[this.T.status]));
    $('#work').classList.add('noinsp');
    $('#cvHost').innerHTML = `<div class="empty" style="margin:auto"><div class="pill run" style="font-size:13px;padding:6px 14px">${ic('sparkle')} ${t(this.T.status === 'triaging' ? '正在判断任务复杂度…' : '正在拆解任务、匹配 Agent…')}</div>
      <p class="muted small" style="margin-top:12px">${esc(this.T.goal)}</p></div>`;
    $('#dock').classList.add('hidden'); $('#steer').innerHTML = ''; $('#ctop').innerHTML = ''; this._needRemount = true;
  }
  drawTop() {
    if (this._needRemount) { this._needRemount = false; this.remount(); return false; }
    $('#dock').classList.remove('hidden');
    const T = this.T, ph = this.snap.phase, acts = [];
    const sub = `${t(TASK_STATUS[ph] || ph)}${T.status === 'completed' && T.completion_quality ? ' · ' + deliveryLabel(T.completion_quality) : ''} · ${T.lane === 'quick' ? t('快速回答') : t('团队模式')}${this.snap.plan_version ? ' · ' + t('计划 v{v}', { v: this.snap.plan_version }) : ''} · ${t('并行上限 {n}', { n: T.max_concurrency })}`;
    acts.push(`<span class="pill" title="${esc(t('费用仅作展示：不会因为预算停止任务'))}">${money(T.cost_used)} / ${money(T.budget_cny)}</span>`);
    if (T.status === 'running') {
      acts.push(T.paused ? `<button class="btn primary" data-a="resume">${ic('play')}${t('继续执行')}</button>` : `<button class="btn warn" data-a="pause">${ic('pause')}${t('暂停')}</button>`);
      acts.push(`<button class="btn" data-a="step" title="${esc(t('只派发一个就绪节点'))}">${ic('step')}${t('单节点')}</button><button class="btn" data-a="cancel">${ic('stop')}${t('取消')}</button>`);
    }
    if (T.status === 'failed') acts.push(`<button class="btn primary" data-a="replan">${ic('refresh')}${t('重新规划')}</button>`);
    if (this.snap.artifact) acts.push(`<button class="btn ${T.status === 'completed' ? 'primary' : ''}" data-a="result">${ic('doc')}${t('查看成果')}</button>`);
    acts.push(`<button class="btn" data-a="workspace" title="${esc(t('Agent 读写文件的工作区'))}">${ic('save')}${t('工作区')}</button>`);
    if (this.events.length && T.lane !== 'quick') acts.push(`<button class="btn" data-a="replay">${ic('clock')}${t('历史回放')}</button>`);
    top(esc(T.title), esc(sub) + (T.error_code ? ` · <span style="color:var(--err)">${esc(T.error_code)}</span>` : ''), acts.join(''));
    if (this.snap.deliverable?.assumptions?.length) $('#topTitle').title = this.snap.deliverable.assumptions.join('\n');
    $('#topActs').onclick = (e) => { const a = e.target.closest('[data-a]')?.dataset.a; if (a) this.act(a); };
  }
  drawCtop() {
    if (!this.snap || this.T.lane === 'quick') { $('#ctop').innerHTML = ''; return; }
    const att = this.snap.attention.length, ed = !!this.cv.edit, ro = !!this.replay || ['cancelled'].includes(this.T.status);
    $('#ctop').innerHTML = `<span class="small muted">${t('状态')}：<span class="pill ok">${t('已完成')}</span> <span class="pill run">${t('执行中')}</span> <span class="pill gate">${t('等你')}</span></span><span class="grow"></span>
      <button class="btn sm tool-btn ${this.cv.cp ? 'on' : ''}" data-c="cp" title="${esc(t('关键路径决定总时长'))}">${ic('flow')}${t('关键路径')}</button>
      ${ro ? '' : `<button class="btn sm tool-btn ${ed ? 'on' : ''}" data-c="edit" title="${esc(t('编辑工作流（E）'))}">${ic('pen')}${t('编辑工作流')}</button>`}
      <button class="btn sm tool-btn" data-c="tidy">${ic('layers')}${t('整理布局')}</button>
      ${ro || this.T.lane === 'quick' ? '' : `<button class="btn sm tool-btn ${this.mg.open ? 'on' : ''}" data-c="mgmt" title="${esc(t('集中管理节点与成果节点、历史工作流'))}">${ic('flow')}${t('节点管理')}</button>`}
      <button class="btn sm" data-c="att">${ic('alert')}${t('关注项')} ${att}</button>
      <div class="zoomer"><button data-c="zout" title="${esc(t('缩小（-）'))}">${ic('zout')}</button><b id="zv" data-c="z100" title="${esc(t('重置为 100%（0）'))}">${Math.round(this.cv.vp.z * 100)}%</b>
      <button data-c="zin" title="${esc(t('放大（+）'))}">${ic('zin')}</button><button data-c="fit" title="${esc(t('适应窗口（1）'))}">${ic('fit')}</button><button data-c="center" title="${esc(t('定位选中（F）'))}">${ic('eye')}</button></div>`;
    $('#ctop').onclick = (e) => {
      const c = e.target.closest('[data-c]')?.dataset.c; if (!c) return;
      ({ cp: () => { this.cv.cp = !this.cv.cp; this.cv.render(); this.drawCtop(); }, edit: () => this.toggleEdit(), tidy: () => { this.cv.tidy(); toast(t('已整理布局，Ctrl+Z 可撤销')); },
        mgmt: () => this.toggleMgmt(),
        att: () => this.attention(), zout: () => this.cv.zoomBy(1 / 1.2), zin: () => this.cv.zoomBy(1.2), z100: () => this.cv.zoomReset(), fit: () => this.cv.fit(),
        center: () => this.sel && this.cv.center(this.sel) })[c]();
    };
  }
  drawQuick() {
    $('#work').classList.add('noinsp'); $('#dock').classList.add('hidden'); $('#ctop').innerHTML = ''; $('#steer').innerHTML = '';
    this.cv.destroy();                                     // 快速回答不需要画布：移除它的键盘监听
    if (this._quickShown) return; this._quickShown = true;  // 后续事件刷新不重复请求、不重绘回答
    GET(`/api/tasks/${this.id}/artifact`).then((a) => {
      if (!this.alive) return;
      $('#cvHost').innerHTML = `<div class="page" style="width:100%"><div class="card"><div class="row"><span class="pill ac">${t('快速回答')}</span><span class="small muted">${esc(t(a.sections[0].model || ''))}</span></div>
        <div style="margin-top:10px">${md(a.sections[0].content)}</div></div><div class="row" style="margin-top:12px"><span class="small muted grow">${t('需要多节点协作？升级后会生成可编辑的计划')}</span>
        <button class="btn primary" id="esc">${ic('flow')}${t('升级为团队任务')}</button></div></div>`;
      $('#esc').onclick = () => once($('#esc'), async () => { try { await POST(`/api/tasks/${this.id}/escalate`); this.remount(); } catch (e) { fail(e); } });
    }).catch((e) => { this._quickShown = false; if (this.alive) fail(e); });
  }

  // ---------- 详情面板 ----------
  select(k) {
    this.sel = k; this.detail = null; this.cv.select(k);
    if (this.selArtKey) {                                  // 切到节点：右侧面板回到节点详情（成果编辑未保存则丢弃）
      if (this.artEdit && this.artEdit.draft !== this.artEdit.orig) toast(t('未保存的修改已丢弃'));
      this.artEdit = null; this.selArtKey = null;
    }
    this.drawInsp(); if (this.tab !== 'overview') this.loadDetail(); $('#insp').classList.add('open');
  }
  async loadDetail() {
    if (!this.sel) return;
    const k = this.sel;
    try { const d = await GET(`/api/tasks/${this.id}/nodes/${k}`); if (this.alive && this.sel === k) { this.detail = d; this.drawInsp(); } } catch { /* 节点已不存在 */ }
  }
  drawInsp() {
    if (!this.alive || !$('#insp')) return;
    if (this.artEdit) return;                              // v1.8：成果编辑中不重绘右侧面板（表单内容与光标保持稳定）
    // 重绘会替换整个面板：先记下评论框里尚未提交的文字与光标，重绘后恢复（执行中每秒都可能有事件触发重绘）
    const old = $('#cmt'), keep = old && { v: old.value, f: document.activeElement === old, s: old.selectionStart, e: old.selectionEnd, k: this.sel };
    this._drawInsp();
    const nw = $('#cmt');
    if (keep && nw && keep.k === this.sel) { nw.value = keep.v; if (keep.f) { nw.focus(); nw.setSelectionRange(keep.s, keep.e); } }
  }
  /** v3.0 M2：外部 Agent 决策徽章——「用了谁 / 为什么 / 备选 / 连接引导」，没调用时也说明依据（什么时候不调同样透明）。 */
  _extAgentField(n) {
    const g = n.agents, needs = n.agent_needs || [];
    if (!g) {                                              // 没有外部需求：说明为什么不用调（N1 纯语言智能 / 内置已覆盖）
      return needs.length ? `<div class="field"><div class="label">${t('外部 Agent 调用')}</div>
        <p class="small muted" style="margin:0">${t('未调用外部 Agent：这个节点的需求「{n}」由内置能力覆盖。', { n: needs.join('、') })}</p></div>` : '';
    }
    const MODE = { conservative: '保守', balanced: '平衡', aggressive: '激进' };
    const prim = g.primary || null, alts = g.alternates || [], attached = g.attached || [], gap = g.gap || [];
    const head = `<div class="field"><div class="label row">${t('外部 Agent 调用')}<span class="grow"></span><span class="small muted">${t('决策档位')}：${t(MODE[g.mode] || g.mode || '平衡')}</span></div>`;
    if (!prim) {
      const why = g.why === 'N4' ? t('外部 Agent 调用总开关已关闭，本节点由内置能力执行') : L(g.reason,g.reason_en) || t('没有匹配到合适的外部 Agent');
      return `${head}<p class="small muted" style="margin:0">${esc(why)}${needs.length ? `<br>${t('需求')}：${esc(needs.join('、'))}` : ''}</p></div>`;
    }
    const badge = `<div class="card" style="padding:10px">
      <div class="row small" style="flex-wrap:wrap;gap:4px"><b>${esc(L(prim.name, prim.name_en))}</b>
        <span class="pill ${prim.connected ? 'ok' : 'gate'}">${t(prim.connected ? '已连接' : '未连接')}</span>
        <span class="pill">${t('匹配分')} ${prim.score}</span>
        <span class="pill ${g.used ? 'ac' : ''}">${t(g.used ? '已挂载 {n} 个工具' : '未挂载工具', { n: attached.length })}</span></div>
      <p class="small muted" style="margin:6px 0">${esc(L(g.reason,g.reason_en) || '')}</p>
      ${attached.length ? `<div class="small mono" style="margin-bottom:4px">${attached.map((x) => esc(x)).join('<br>')}</div>` : ''}
      ${alts.length ? `<div class="small muted">${t('备选')}：${alts.map((x) => `${esc(L(x.name, x.name_en))}（${x.score}${x.connected ? ' · ' + t('已连接') : ''}）`).join('、')}</div>` : ''}
    </div>`;
    const guide = gap.length ? `<div class="banner info" style="margin-top:8px">${ic('cpu')}<span class="grow">${t('要用的 {n} 还没有连接；这个节点已由内置能力照常执行，连上后重跑即可用上它。', { n: gap.join('、') })} <a data-go="/agents">${t('去 Agent 目录')}</a></span></div>` : '';
    const needLine = needs.length ? `<div class="small muted" style="margin-top:6px">${t('这个节点需要')}：${esc(needs.join('、'))}</div>` : '';
    return `${head}${badge}${guide}${needLine}</div>`;
  }
  _drawInsp() {
    const el = $('#insp');
    if (this.selArtKey) return this._drawArtInsp();        // v1.8：点选的成果 → 右侧展示（可编辑）
    const T = this.T, n = this.snap.nodes.find((x) => x.node_key === this.sel);
    if (!n) return (el.innerHTML = this.summaryPanel(), this.bindInsp());
    const ro = !!this.replay;
    // 待审批记录（node=产出确认 / tool=执行前安全闸门）在正文与底部按钮都要用，必须声明在函数作用域
    const curAp = this.snap.approvals.find((x) => x.node_key === n.node_key);
    const toolAp = curAp && curAp.kind === 'tool' ? curAp : null;
    const tabs = [['overview', '概览'], ['io', '输入输出'], ['log', '执行日志'], ['comments', '评论']];
    let b = '';
    if (this.tab === 'overview') {
      const a = n.agent || {}, bd = a.breakdown || {};
      if (n.status === 'awaiting_approval') {
        b += toolAp
          ? `<div class="banner warn" style="margin-bottom:12px">${ic('alert')}<span>${t('这个节点要执行危险操作，批准后才会真正执行；拒绝则这个节点失败。')}</span></div>
            <div class="field"><div class="label">${t('待批准的操作')}</div><div class="code">${esc(toolAp.tool)} ${esc(JSON.stringify(toolAp.args || {}, null, 2))}</div>
            <p class="small muted">${t('批准只对「这个工具 + 这组参数」生效；换成别的操作会再问你一次。')}</p></div>`
          : `<div class="banner warn" style="margin-bottom:12px">${ic('alert')}<span>${t('这个节点在等待你的批准。')}</span></div>`;
      }
      if (n.status === 'failed') b += `<div class="banner err" style="margin-bottom:12px">${ic('alert')}<span>${esc(n.error_code || '')} ${esc(errText(n.error_code,n.error))}</span></div>`;
      if (n.spec_state === 'ready') b += `<div class="banner info" style="margin-bottom:12px">${ic('sparkle')}<span>${t('已在等待批准期间预执行（只读），批准后直接生效；拒绝则作废。')}</span></div>`;
      b += `<div class="field"><div class="label">${t('这个节点在做什么')}</div><p style="margin:0">${esc(n.description || n.title)}</p>
        ${n.acceptance?.length ? `<ul class="small muted">${n.acceptance.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}</div>
        <div class="field"><div class="label row">${t('匹配到的 Agent')}<span class="grow"></span>${ro ? '' : `<a data-i="agent">${t('更换')}</a>`}</div>
        <div class="card" style="padding:10px"><b>${esc(L(a.name, a.name_en))}</b> <span class="pill ${a.source === 'composed' ? 'gate' : 'ac'}">${t(a.source === 'composed' ? '临时组合' : '能力库')}</span>
        <span class="pill">${t('匹配分')} ${a.score ?? '—'}</span><p class="small muted" style="margin:6px 0">${esc(L(a.reason, a.reason_en))}</p>
        <div class="score">${[['tag', '能力标签重合'], ['tool', '工具覆盖'], ['perm', '权限合规'], ['cost', '成本档惩罚']].map(([k, lb]) => `<span>${t(lb)}</span><div class="bar"><i style="width:${(bd[k] || 0) * 100}%"></i></div><span>${(bd[k] ?? 0).toFixed(2)}</span>`).join('')}</div></div></div>
        ${this._extAgentField(n)}
        <div class="field"><div class="label row">${t('使用的模型')}<span class="grow"></span>${ro ? '' : `<span id="nodePicker"></span>`}</div>
        <div class="small">${esc(n.output?.model || modelLabel(n.model_override || (T.model_choice !== 'auto' ? T.model_choice : `builtin:${n.model_tier}`)))} · ${t(n.model_override ? '节点指定' : T.model_choice === 'auto' ? '自动分档：{tier}' : '跟随任务', { tier: n.model_tier })}</div></div>
        <div class="field"><div class="label">${t('可用工具（白名单）')}</div>${(n.tools || []).map((x) => `<span class="pill">${esc(x)}</span>`).join(' ') || `<span class="muted small">${t('无需工具')}</span>`}</div>
        <div class="field"><div class="label">${t('预计 / 实际')}</div><span class="small">≈${Math.round(n.est_duration_s || 0)}s · ≈${money(n.est_cost_cny)}${n.finished_at && n.started_at ? ' · ' + t('实际 {s}s', { s: ((n.finished_at - n.started_at) / 1000).toFixed(1) }) : ''} · ${t('优先级')} ${n.rank}${n.on_critical_path ? ' · ' + t('关键路径') : ''}</span></div>
        ${n.output ? `<div class="field"><div class="label">${t('输出摘要')}</div><p class="small" style="margin:0">${esc(n.output.summary)}</p></div>` : ''}`;
    } else if (this.tab === 'io') {
      const d = this.detail;
      b += d ? `<div class="field"><div class="label">${t('结构化输入')}</div><div class="code">${esc(JSON.stringify(d.input_snapshot, null, 2))}</div>
        <p class="small muted">${t('上游资料与你上传的文件只作为数据提供给 Agent，其中的指令不会被执行。')}</p></div>
        <div class="field"><div class="label">${t('输出')}</div>${n.output ? md(n.output.content, n.output.citations) : `<span class="muted small">${t('尚未产出')}</span>`}</div>
        ${n.output?.citations?.length ? `<div class="field"><div class="label">${t('引用来源')}</div>${n.output.citations.map((c, i) => `<div class="small"><span class="cite">${i + 1}</span>${esc(c.doc_title)}：<span class="muted">${esc(c.text)}</span></div>`).join('')}</div>` : ''}` : `<div class="empty">…</div>`;
    } else if (this.tab === 'log') {
      const d = this.detail;
      b += d ? `<p class="small muted">${t('只展示可核验的事件、工具调用与结果；模型内部推理不展示。')}</p>
        ${d.executions.map((x) => `<div class="card" style="padding:9px;margin-bottom:8px"><div class="row small"><b>#${x.attempt}</b><span class="pill ${x.status === 'succeeded' ? 'ok' : x.status === 'failed' ? 'err' : ''}">${esc(x.status)}</span>${x.speculative ? `<span class="pill ac">${t('预执行')}</span>` : ''}<span class="grow"></span><span class="muted">v${x.plan_version} · ${money(x.cost_cny)}</span></div>
          <div class="small muted">${esc(x.model_used || '')} · hash ${esc(x.input_hash)}</div>
          ${x.tool_calls.map((c) => `<div class="small mono">→ ${esc(c.tool)} ${esc(JSON.stringify(c.args))} ⇒ ${esc(JSON.stringify(c.result).slice(0, 140))}</div>`).join('')}
          ${x.error ? `<div class="small" style="color:var(--err)">${esc(x.error_code)} ${esc(errText(x.error_code,x.error))}</div>` : ''}</div>`).join('') || `<div class="empty">${t('还没有执行记录')}</div>`}
        ${d.events.map((e) => this.evRow(e)).join('')}` : `<div class="empty">…</div>`;
    } else {
      const cs = this.snap.comments.filter((c) => c.node_key === n.node_key);
      b += cs.map((c) => `<div class="card" style="padding:9px;margin-bottom:8px"><div class="row small"><b>${esc(S.me.display_name)}</b><span class="muted">${ago(c.created_at)}</span><span class="grow"></span>
        <span class="pill ${c.status === 'applied' ? 'ok' : ''}">${t(c.status === 'applied' ? '已应用' : '待应用')}</span>${c.status === 'open' ? `<a data-delc="${c.id}">${t('删除')}</a>` : ''}</div><p style="margin:6px 0 0">${esc(c.body)}</p></div>`).join('')
        || `<p class="small muted">${t('在节点上直接写修改意见：应用后只有这个节点和它的下游会重算。')}</p>`;
      if (!ro) b += `<textarea id="cmt" placeholder="${esc(t('例如：第二天下午太赶了，只安排一个景点'))}"></textarea>
        <div class="row" style="margin-top:8px"><button class="btn sm" data-i="addc">${t('添加评论')}</button><span class="grow"></span>
        <button class="btn sm primary" data-i="applyc" ${cs.some((c) => c.status === 'open') ? '' : 'disabled'}>${t('应用并重算')}</button></div>`;
    }
    const foot = [];
    if (!ro) {
      if (n.status === 'awaiting_approval' && toolAp) foot.push(`<button class="btn ok" data-i="approve">${ic('check')}${t('批准执行')}</button><button class="btn danger" data-i="reject">${t('拒绝执行')}</button>`);
      if (['failed', 'cancelled'].includes(n.status) && T.status !== 'cancelled') foot.push(`<button class="btn primary" data-i="retry">${ic('refresh')}${t('重试')}</button>`);
      if (n.status === 'succeeded' && T.status !== 'cancelled') foot.push(`<button class="btn" data-i="rerun">${ic('refresh')}${t('重跑此节点')}</button>`);
      if (n.status !== 'running' && T.status !== 'cancelled') foot.push(`<button class="btn" data-i="edit">${ic('pen')}${t('编辑节点')}</button>`);
      if (n.status === 'succeeded') foot.push(`<span class="grow"></span><button class="icon-btn" data-i="up" title="${esc(t('有帮助'))}">👍</button><button class="icon-btn" data-i="down" title="${esc(t('没帮助'))}">👎</button>`);
    }
    el.innerHTML = `<div class="insp-head"><div class="row small muted"><a data-i="back">${t('← 任务概览')}</a><span class="grow"></span><span>${esc(n.node_key)}</span><button class="icon-btn" data-i="close" title="${esc(t('关闭'))}">${ic('x')}</button></div>
      <h2>${esc(n.title)}</h2><div class="row small"><span class="pill ${{ succeeded: 'ok', running: 'run', awaiting_approval: 'gate', skipped: 'gate', failed: 'err' }[n.status] || ''}">${t({ pending: '等待中', running: '执行中', succeeded: '已完成', awaiting_approval: '等待你批准', skipped: '已跳过', failed: '失败', cancelled: '已取消' }[n.status] || n.status)}</span>
      <span class="muted">${t('执行次数')} ${n.attempts}</span></div></div>
      <div class="tabs">${tabs.map(([k, lb]) => `<button class="tab ${this.tab === k ? 'on' : ''}" data-tab="${k}">${t(lb)}</button>`).join('')}</div>
      <div class="insp-body">${b}</div>${foot.length ? `<div class="insp-foot">${foot.join('')}</div>` : ''}`;
    this.bindInsp(n);
    if ($('#nodePicker')) mountPicker($('#nodePicker'), n.model_override || 'auto', (r) => this.nodeModel(n, r));
  }
  summaryPanel() {
    const T = this.T, e = this.snap.estimate || {}, done = this.snap.nodes.filter((n) => n.status === 'succeeded').length;
    const ap = this.snap.nodes.filter((n) => n.status === 'awaiting_approval');
    const bad = this.snap.nodes.filter((n) => ['failed', 'skipped'].includes(n.status));
    return `<div class="insp-head"><h2>${t('任务概览')}</h2><div class="small muted">${esc(T.goal)}</div></div><div class="insp-body">
      ${ap.map((n) => `<div class="banner warn" style="margin-bottom:8px">${ic('alert')}<span class="grow">${t('需要你批准：')}「${esc(n.title)}」</span><a data-sel="${n.node_key}">${t('查看')}</a></div>`).join('')}
      ${bad.length ? `<div class="banner warn" style="margin-bottom:8px">${ic('alert')}<span class="grow">${t('{n} 个节点未完成（失败或跳过），成果不含这部分内容。', { n: bad.length })}</span></div>` : ''}
      <div class="card"><div class="row"><b class="grow">${t('进度')}</b><span>${done}/${this.snap.nodes.length}</span></div><div class="progress" style="margin:8px 0"><i style="width:${(done / Math.max(1, this.snap.nodes.length)) * 100}%"></i></div>
      <div class="small muted">${t('预计总时长 {a}，串行需要 {b}，并行节省 {p}%', { a: dur(e.makespan_s), b: dur(e.serial_s), p: Math.round((e.parallel_saving || 0) * 100) })}${T.actual_makespan_s ? ' · ' + t('实际 {s}', { s: dur(T.actual_makespan_s) }) : ''}</div></div>
      <div class="field" style="margin-top:14px"><div class="label">${t('成果语言')}</div>${T.output_language === 'zh' ? t('中文') : t('英文')}</div>
      <div class="field"><div class="label">${t('版本')}</div>${this.snap.versions.map((v) => `<div class="small">v${v.version} · ${esc(v.reason)} · ${t(v.status === 'active' ? '当前' : v.status === 'draft' ? '草稿' : '已被取代')}</div>`).join('')}</div>
      <p class="small muted">${t('点击画布中的节点查看匹配依据、输入输出、执行日志并写评论。')}</p></div>`;
  }
  bindInsp(n) {
    $('#insp').onclick = async (e) => {
      const tb = e.target.closest('[data-tab]'); if (tb) { this.tab = tb.dataset.tab; this.drawInsp(); if (this.tab !== 'overview') this.loadDetail(); return; }
      const sl = e.target.closest('[data-sel]'); if (sl) return this.select(sl.dataset.sel);
      const dc = e.target.closest('[data-delc]'); if (dc) return this.wrap(() => DEL('/api/comments/' + dc.dataset.delc));
      const a = e.target.closest('[data-i]')?.dataset.i; if (!a) return;
      const map = {
        back: () => { this.sel = null; this.cv.select(null); this.drawInsp(); }, close: () => { $('#insp').classList.remove('open'); this.sel = null; this.cv.select(null); this.drawInsp(); },
        approve: () => this.wrap(async () => { const ap = this.snap.approvals.find((x) => x.node_key === n.node_key); if (!ap) throw new Error(t('审批已失效，请刷新')); await POST(`/api/approvals/${ap.id}/approve`); toast(t('已批准，这个节点重新执行')); }),
        reject: () => this.reject(n), retry: () => this.wrap(() => POST(`/api/tasks/${this.id}/nodes/${n.node_key}/retry`)),
        rerun: () => this.revise({ instructions: { [n.node_key]: ['重新生成'] } }, `/api/tasks/${this.id}/nodes/${n.node_key}/rerun`, {}),
        agent: () => this.changeAgent(n), edit: () => this.editStep(n),
        addc: () => { const v = $('#cmt').value.trim(); if (!v) return; this.wrap(async () => { await POST(`/api/tasks/${this.id}/comments`, { node_key: n.node_key, body: v }); if ($('#cmt')) $('#cmt').value = ''; }); },
        applyc: () => { const ids = this.snap.comments.filter((c) => c.node_key === n.node_key && c.status === 'open'); this.revise({ instructions: { [n.node_key]: ids.map((c) => c.body) } }, `/api/tasks/${this.id}/comments/apply`, { comment_ids: ids.map((c) => c.id) }); },
        up: () => POST(`/api/tasks/${this.id}/nodes/${n.node_key}/feedback`, { signal: 'thumb_up' }).then(() => toast(t('谢谢反馈'))),
        down: () => POST(`/api/tasks/${this.id}/nodes/${n.node_key}/feedback`, { signal: 'thumb_down' }).then(() => toast(t('谢谢反馈，可以在评论里写明哪里不好'))),
      };
      map[a]?.();
    };
  }
  async wrap(fn) {
    if (this._busy) return;                                // 同一时间只处理一个操作，防止连点批准 / 重复评论
    this._busy = true;
    try { await fn(); await this.load(); } catch (e) { fail(e); } finally { this._busy = false; }
  }

  // ---------- 修订：先预览影响范围，确认后提交（F11） ----------
  async revise(change, url, payload) {
    try {
      const p = await POST(`/api/tasks/${this.id}/revisions/preview`, change);
      const row = (k, lab, col) => `<tr><td>${esc(p.titles[k] || k)}</td><td style="color:${col}">${t(lab)}</td></tr>`;
      const html = `<table class="tbl"><thead><tr><th>${t('节点')}</th><th>${t('处理方式')}</th></tr></thead><tbody>
        ${p.affected.map((k) => row(k, p.added.includes(k) ? '新增' : '重新执行', 'var(--accent-2)')).join('')}${p.removed.map((k) => row(k, '删除', 'var(--err)')).join('')}
        ${p.reused.map((k) => row(k, '沿用结果', 'var(--ok)')).join('')}${p.carried.map((k) => row(k, '继续执行（结果沿用）', 'var(--run)')).join('')}</tbody></table>
        <p class="small muted">${t('会创建一个新的计划版本，旧版本结果不会混入。预计新增费用约 {c}。', { c: money(p.estimate_cost_cny) })}</p>`;
      const ok = await modal(t('这次修改会影响哪些节点？'), html, [{ label: t('取消'), value: false }, { label: t('确认并重算'), cls: 'primary', value: true }]);
      if (!ok) return false;
      const r = await POST(url, payload ?? change);
      toast(t('已创建 v{v}，只重算受影响的 {n} 个节点', { v: r.version, n: r.affected.length }));
      await this.load(); return true;
    } catch (e) { fail(e); return false; }
  }
  async reject(n) {
    const ap = this.snap.approvals.find((x) => x.node_key === n.node_key);
    if (!ap) return toast(t('审批已失效，请刷新'), true);
    // 安全防护：拒绝 = 这个危险操作不做，该节点失败（其余节点不受影响）
    const ok = await confirmBox(t('拒绝执行这个操作？'), t('拒绝后「{tool}」不会执行，这个节点会标记为失败；其余节点不受影响，你可以改完指令再重试。', { tool: ap.tool }), t('拒绝执行'), true);
    if (ok) this.wrap(() => POST(`/api/approvals/${ap.id}/reject`, {}));
  }
  // ---------- 工作区（Agent 真正读写的沙箱目录） ----------
  async workspaceModal() {
    let r; try { r = await GET('/api/workspace'); } catch (e) { return fail(e); }
    const items = r.items || [];
    const head = `<p class="small muted">${t('Agent 只能在你自己的任务工作区里读写文件；开启「危险操作先问我」后，删除与控制台操作会先等你批准。')}${r.root ? `<br><span class="mono">${esc(r.root)}</span>` : ''}</p>`;
    const list = items.map((f) => `<div class="row card" style="padding:8px;margin-bottom:6px"><span class="grow small mono">${esc(f.path)}</span><span class="small muted">${f.bytes} B</span><a data-wv="${esc(f.path)}">${t('查看')}</a><a data-wd="${esc(f.path)}">${t('删除')}</a></div>`).join('') || `<div class="empty">${t('工作区还没有文件')}</div>`;
    await modal(t('工作区'), head + list, [], 560);
  }
  async wsShow(path) {
    const host = $('#overlay')?.querySelector('.modal-body');
    if (!host) return;
    try {
      const f = await GET(`/api/workspace/file?path=${encodeURIComponent(path)}`);
      host.innerHTML = `<p class="row small"><a data-wb="1">${t('← 返回列表')}</a><span class="grow"></span><span class="mono muted">${esc(f.path)}</span></p>
        <div class="code" style="white-space:pre-wrap">${esc(f.content)}</div>${f.truncated ? `<p class="small muted">${t('内容较长，只显示前一部分。')}</p>` : ''}`;
    } catch (e) { fail(e); }
  }
  async wsDelete(path) {
    if (!(await confirmBox(t('删除文件？'), t('将从你的工作区删除 {p}，不可恢复。', { p: path }), t('删除'), true))) return;
    try { await DEL(`/api/workspace/file?path=${encodeURIComponent(path)}`); toast(t('已删除')); this.workspaceModal(); } catch (e) { fail(e); }
  }
  async changeAgent(n) {
    const alts = n.agent?.alternatives || [];
    const v = await modal(t('更换「{t}」的 Agent', { t: n.title }), `<p class="small muted">${t('更换后只重算这个节点及其下游；你的选择会用于微调匹配权重。')}</p>
      ${alts.map((a) => `<label class="check"><input type="radio" name="ag" value="${a.key}" style="width:auto" ${(n.agent.capability_keys || [])[0] === a.key ? 'checked' : ''}> <span class="grow">${esc(L(a.name, a.name_en))}</span><span class="pill">${a.score}</span></label>`).join('')}`,
      [{ label: t('取消'), value: null }, { label: t('确认更换'), cls: 'primary', onClick: (ov) => $('input[name=ag]:checked', ov)?.value || false }]);
    if (!v) return;
    const op = { op: 'set_agent', node_key: n.node_key, capability_keys: [v] };
    this.revise({ ops: [op] }, `/api/tasks/${this.id}/revisions`, { ops: [op], reason: 'reassign' });
  }
  nodeModel(n, ref) {
    const op = { op: 'set_model', node_key: n.node_key, model_ref: ref === 'auto' ? null : ref };
    this.revise({ ops: [op] }, `/api/tasks/${this.id}/revisions`, { ops: [op], reason: 'model_override' }).then((ok) => !ok && this.drawInsp());
  }
  async editStep(n) {
    const v = await modal(t('编辑节点'), `<div class="field"><label>${t('标题')}</label><input id="et" value="${esc(n.title)}"></div>
      <div class="field"><label>${t('描述')}</label><textarea id="ed">${esc(n.description)}</textarea></div>
      <div class="field"><label>${t('模型档位')}</label><select id="etier">${['fast', 'standard', 'strong'].map((x) => `<option value="${x}" ${n.model_tier === x ? 'selected' : ''}>${t({ fast: '快速', standard: '标准', strong: '强力' }[x])}</option>`).join('')}</select></div>`,
      [{ label: t('取消'), value: null }, { label: t('保存'), cls: 'primary', onClick: (ov) => ({ title: $('#et', ov).value.trim(), description: $('#ed', ov).value, tier: $('#etier', ov).value }) }]);
    if (!v) return;
    const ops = [{ op: 'update_node', node_key: n.node_key, patch: { title: v.title || n.title, description: v.description } },
                 { op: 'set_model_tier', node_key: n.node_key, tier: v.tier }];
    this.revise({ ops }, `/api/tasks/${this.id}/revisions`, { ops, reason: 'manual' });
  }

  // ---------- 编辑模式 ----------
  toggleEdit() { if (this.replay || this.T.status === 'cancelled') return; if (this.cv.edit) this.cancelEdit(); else this.cv.toggleEdit(true); }
  async cancelEdit() { if (this.cv.edit?.ops.length && !(await confirmBox(t('放弃修改？'), t('草稿中的 {n} 处修改将被丢弃。', { n: this.cv.edit.ops.length }), t('放弃'), true))) return; this.cv.toggleEdit(false); this.load(); }
  async applyEdit(ops) {
    if (await this.revise({ ops }, `/api/tasks/${this.id}/revisions`, { ops, reason: 'canvas_edit' })) this.cv.toggleEdit(false);
  }
  async addStep(at) {
    const v = await modal(t('添加节点'), `<div class="field"><label>${t('标题')}</label><input id="at" placeholder="${esc(t('例如：补充雨天备选方案'))}"></div>
      <div class="field"><label>${t('所需能力（逗号分隔，可选）')}</label><input id="ag" placeholder="plan, write"></div><p class="small muted">${t('保存后由服务端按能力需求匹配 Agent。')}</p>`,
      [{ label: t('取消'), value: null }, { label: t('添加'), cls: 'primary', onClick: (ov) => $('#at', ov).value.trim() ? { title: $('#at', ov).value.trim(), tags: $('#ag', ov).value.split(/[,，\s]+/).filter(Boolean) } : false }]);
    if (v) this.cv.addNode(v.title, v.tags.length ? v.tags : ['write'], at && { x: at.x - 108, y: at.y - 40 });
  }

  // ---------- 底部记录 / 并行时间线 ----------
  evRow(e) {
    const p = e.payload || {}, vars = { ...p, title: this.titleOf(e.node_key), lane: t(p.lane === 'quick' ? '快速回答' : '团队协作'), na: (p.affected || []).length, nr: (p.reused || []).length, atitle: p.title || '' };
    const node = this.snap.nodes.find(item => item.node_key === e.node_key);
    if (p.agent) vars.agent = L(node?.agent?.name, node?.agent?.name_en) || p.agent;
    for (const key of ['model', 'provider', 'reason', 'atitle']) if (vars[key]) vars[key] = t(vars[key]);
    const tx = EV_TEXT[e.type] ? t(EV_TEXT[e.type], vars) : e.type;
    return `<div class="ev ${EV_KIND(e.type)}"><time>${hhmmss(e.created_at)}</time><span class="k"></span><span class="grow">${esc(tx)}${S.settings['advanced.show_raw_events'] ? ` <span class="mono muted">${esc(e.type)}</span>` : ''}</span></div>`;
  }
  drawDock() {
    if (!this.snap || this.T.lane === 'quick') return;
    const tabs = [['events', '执行记录'], ['tools', '工具调用'], ['costs', '费用明细'], ['gantt', '并行时间线']];
    $('#dockTabs').innerHTML = tabs.map(([k, lb]) => `<button class="tab ${this.dock === k ? 'on' : ''}" data-d="${k}">${t(lb)}</button>`).join('') + (this.replay ? `<span class="grow"></span>` : '');
    $('#dockTabs').onclick = (e) => { const d = e.target.closest('[data-d]'); if (d) { this.dock = d.dataset.d; this.drawDock(); } };
    const body = $('#dockBody');
    if (this.replay) return this.drawReplay();
    if (this.dock === 'events') body.innerHTML = this.events.slice(-300).reverse().map((e) => this.evRow(e)).join('');
    else if (this.dock === 'tools') body.innerHTML = this.events.filter((e) => e.type.startsWith('tool.')).reverse().map((e) => this.evRow(e)).join('') || `<div class="empty">${t('还没有工具调用')}</div>`;
    else if (this.dock === 'costs') body.innerHTML = `<table class="tbl"><thead><tr><th>${t('时间')}</th><th>${t('节点')}</th><th>${t('模型')}</th><th>tokens</th><th>${t('费用')}</th><th>ms</th></tr></thead><tbody>
      ${this.snap.costs.map((c) => `<tr><td>${hhmmss(c.created_at)}</td><td>${esc(this.titleOf(c.node_key) || '—')}</td><td class="small">${esc(c.model_id.split('|')[1] || c.model_id)}${c.ok ? '' : ` <span class="pill err">${esc(c.error_code)}</span>`}</td><td>${c.tokens_in}/${c.tokens_out}</td><td>${money(c.cost_cny)}</td><td>${c.latency_ms}</td></tr>`).join('')}</tbody></table>`;
    else this.drawGantt(body);
  }
  drawGantt(body) {
    const T = this.T, nodes = this.snap.nodes, e = this.snap.estimate || {}, t0 = T.run_started_at || Date.now(), now = (Date.now() - t0) / 1000;
    const estBy = Object.fromEntries((e.timeline || []).map((x) => [x.node_key, x]));
    const bars = nodes.map((n) => {
      if (n.started_at) { const s = (n.started_at - t0) / 1000, f = n.finished_at ? (n.finished_at - t0) / 1000 : now;
        return { n, s, f, k: n.status === 'running' ? 'run' : n.status === 'failed' ? 'fail' : n.spec_state === 'committed' ? 'spec' : 'done' }; }
      const x = estBy[n.node_key]; return x ? { n, s: Math.max(now, x.est_start_s), f: Math.max(now, x.est_start_s) + (x.est_end_s - x.est_start_s), k: 'est' } : null;
    }).filter(Boolean);
    nodes.filter((n) => n.status === 'awaiting_approval').forEach((n) => bars.push({ n, s: ((n.started_at || t0) - t0) / 1000, f: now, k: 'gate', gate: true }));
    const lanes = []; bars.sort((a, b) => a.s - b.s).forEach((b) => { let i = lanes.findIndex((end) => end <= b.s + 0.01); if (i < 0) i = lanes.push(0) - 1; lanes[i] = b.f; b.lane = i; });
    const max = Math.max(1, now, ...bars.map((b) => b.f)) * 1.05, cp = new Set(e.critical_path || []);
    body.innerHTML = `<div class="gantt"><div class="row small wrap" style="gap:14px;margin:4px 0 8px"><span>${t('预计总时长')} <b>${dur(e.makespan_s)}</b></span><span>${t('串行需要')} <b>${dur(e.serial_s)}</b></span>
      <span style="color:var(--ok)">${t('并行节省')} ${Math.round((e.parallel_saving || 0) * 100)}%</span>${T.actual_makespan_s ? `<span>${t('实际')} <b>${dur(T.actual_makespan_s)}</b></span>` : ''}<span class="muted">${t('虚线为预计，斜纹为等待批准或预执行')}</span></div>
      ${lanes.map((_, i) => `<div class="g-row"><span class="g-lab">${t('泳道')} ${i + 1}</span><div class="g-track">${bars.filter((b) => b.lane === i).map((b) => `<div class="g-bar k-${b.k} ${cp.has(b.n.node_key) ? 'cp' : ''}" data-sel="${b.n.node_key}" title="${esc(b.n.title)} · ${(b.f - b.s).toFixed(1)}s" style="left:${(b.s / max) * 100}%;width:${Math.max(0.8, ((b.f - b.s) / max) * 100)}%">${b.gate ? t('等待批准') : esc(b.n.title)}</div>`).join('')}
      ${T.status === 'running' ? `<div class="g-now" style="left:${(now / max) * 100}%"></div>` : ''}</div></div>`).join('') || `<div class="empty">${t('计划就绪后开始记录')}</div>`}</div>`;
    body.onclick = (ev) => { const s = ev.target.closest('[data-sel]'); if (s) this.select(s.dataset.sel); };
  }
  drawSteer() {
    const T = this.T;
    if (T.lane === 'quick' || ['plan_review', 'cancelled'].includes(T.status) || this.replay) { $('#steer').innerHTML = ''; return; }
    if ($('#steerIn')) return;                             // 已存在：保留用户正在输入的内容与焦点
    $('#steer').innerHTML = `<span id="steerPicker"></span><input id="steerIn" placeholder="${esc(t('补充要求，例如「同行有老人，减少步行」——只有受影响的节点会重算'))}"><button class="btn primary" id="steerGo">${t('预览影响')}</button>`;
    mountPicker($('#steerPicker'), T.model_choice, async (r) => {
      try { const x = await PATCH(`/api/tasks/${this.id}/model-choice`, { model_choice: r }); toast(t('任务模型已更换，影响 {n} 个未开始的节点', { n: x.affects_pending.length })); this.load(); } catch (e) { fail(e); }
    });
    const go = () => {
      const v = $('#steerIn').value.trim(); if (!v) return;
      // 补充要求作用于「无需工具的起始节点」（通常是需求分析），由依赖闭包决定哪些下游需要重算
      const roots = this.snap.nodes.filter((n) => !n.depends_on.length), tgt = roots.filter((n) => !n.tools.length);
      const ins = Object.fromEntries((tgt.length ? tgt : roots).map((n) => [n.node_key, [v]]));
      return this.revise({ instructions: ins }, `/api/tasks/${this.id}/revisions`, { instructions: ins, reason: 'steer' }).then((ok) => { if (ok && $('#steerIn')) $('#steerIn').value = ''; });
    };
    $('#steerGo').onclick = () => once($('#steerGo'), go);
    $('#steerIn').onkeydown = (e) => { if (e.key === 'Enter' && !e.isComposing) once($('#steerGo'), go); };
  }

  // ---------- 关注项 / 回放 / 顶栏动作 ----------
  async attention() {
    const items = this.snap.attention;
    const sev = { block: ['err', '阻断'], warn: ['gate', '需检查'], info: ['', '提示'] };
    await modal(t('关注项（{n}）', { n: items.length }), `<p class="small muted">${t('系统把需要你判断的地方集中起来，不必逐个点开节点。')}</p>` + (items.map((a) => `<div class="card" style="padding:10px;margin-bottom:8px">
      <div class="row"><span class="pill ${sev[a.severity][0]}">${t(sev[a.severity][1])}</span><b class="grow small">${esc(t(a.title))}</b><a data-loc="${a.node_key}">${t('定位')}</a></div>
      ${a.detail ? `<div class="small muted" style="white-space:pre-wrap;margin-top:4px">${esc(a.detail)}</div>` : ''}
      <div class="row" style="margin-top:8px">${a.kind === 'open_question' ? `<input data-ansin="${a.id}" placeholder="${esc(t('你的回答'))}"><button class="btn sm primary" data-ans="${a.id}">${t('回答')}</button>`
        : `<button class="btn sm" data-res="${a.id}">${t('已处理')}</button><button class="btn sm ghost" data-dis="${a.id}">${t('忽略')}</button>`}</div></div>`).join('') || `<div class="empty">${t('没有需要处理的事项')}</div>`), [], 600);
  }
  async act(a) {
    const T = this.T, id = this.id;
    const map = {
      replan: async () => { const fb = await modal(t('重新规划'), `<textarea id="fb" placeholder="${esc(t('可选：说说哪里不满意'))}"></textarea>`, [{ label: t('取消'), value: null }, { label: t('重新规划'), cls: 'primary', onClick: (ov) => $('#fb', ov).value || ' ' }]);
        if (fb) { await POST(`/api/tasks/${id}/plan/replan`, { feedback: fb.trim() || null }); this.remount(); } },
      pause: () => POST(`/api/tasks/${id}/pause`), resume: () => POST(`/api/tasks/${id}/resume`), step: () => POST(`/api/tasks/${id}/step`),
      cancel: async () => { if (await confirmBox(t('取消任务？'), t('进行中的调用会被中止，已完成的结果会保留。'), t('取消任务'), true)) await POST(`/api/tasks/${id}/cancel`); },
      workspace: () => this.workspaceModal(),
      result: () => { location.hash = `#/task/${id}/result`; }, replay: () => this.startReplay(),
    };
    try { await map[a]?.(); if (!['result', 'replay', 'replan'].includes(a)) await this.load(); } catch (e) { fail(e); }
    void T;
  }
  startReplay() { this.replay = { seq: this.events.at(-1).seq }; if (this.cv.edit) this.cv.toggleEdit(false); this.dock = 'events'; this.load(); this.drawReplay(); }
  async drawReplay() {
    const seqs = this.events.map((e) => e.seq), r = this.replay;
    if (!r) return;
    if ($('#rps')) { $('#rps').max = String(seqs.length - 1); return; }   // 新事件到达时只更新范围，不重建滑块
    $('#dockBody').innerHTML = `<div class="banner info" style="margin:6px 0">${ic('clock')}<span class="grow">${t('回放为只读模式：按事件重建当时的状态，不会执行任何工具、不产生费用，也不修改当前任务。')}</span><button class="btn sm" id="rpx">${t('退出回放')}</button></div>
      <input type="range" id="rps" min="0" max="${seqs.length - 1}" value="${Math.max(0, seqs.indexOf(r.seq))}" style="width:100%">
      <div class="small muted" id="rpi"></div>`;
    const apply = async (i) => {
      r.seq = seqs[i]; const tok = (r.tok = (r.tok || 0) + 1);
      let s; try { s = await GET(`/api/tasks/${this.id}/replay?seq=${r.seq}`); } catch (e) { return fail(e); }
      if (!this.alive || this.replay !== r || tok !== r.tok || !$('#rpi')) return;
      this.cv.statusOverride = s.status; this.cv.render();
      $('#rpi').innerHTML = `${hhmmss(s.at)} · v${s.plan_version ?? '—'} · ${this.evRow(this.events[i])}`;
    };
    $('#rps').oninput = debounce((e) => apply(+e.target.value), 80);
    $('#rpx').onclick = () => { this.replay = null; this.cv.statusOverride = null; this.load(); };
    apply(+$('#rps').value);
    $('#steer').innerHTML = ''; this.drawCtop();
  }
}
document.addEventListener('click', async (e) => {      // 关注项 / 工作区弹窗内的操作
  const v = S.view; if (!(v instanceof TaskView)) return;
  const wv = e.target.closest('[data-wv]'), wd = e.target.closest('[data-wd]'), wb = e.target.closest('[data-wb]');
  if (wv) return v.wsShow(wv.dataset.wv);
  if (wd) return v.wsDelete(wd.dataset.wd);
  if (wb) return v.workspaceModal();
  const loc = e.target.closest('[data-loc]'), res = e.target.closest('[data-res]'), dis = e.target.closest('[data-dis]'), ans = e.target.closest('[data-ans]');
  try {
    if (loc) { closeModal(); v.select(loc.dataset.loc); v.cv.center(loc.dataset.loc); }
    if (res) { await POST(`/api/attention/${res.dataset.res}/resolve`); res.closest('.card').remove(); v.load(); }
    if (dis) { await POST(`/api/attention/${dis.dataset.dis}/dismiss`); dis.closest('.card').remove(); v.load(); }
    if (ans) { const val = $(`[data-ansin="${ans.dataset.ans}"]`).value.trim(); if (!val) return;
      const r = await POST(`/api/attention/${ans.dataset.ans}/answer`, { answer: val }); closeModal(); toast(t('已创建 v{v}，按你的回答重算', { v: r.version })); v.load(); }
  } catch (err) { fail(err); }
});

// ======================= 成果页 =======================
async function viewResult(id, my = ROUTE) {
  let a, snap;
  try { [a, snap] = await Promise.all([GET(`/api/tasks/${id}/artifact`), GET(`/api/tasks/${id}/state`)]); } catch (e) { if (!stale(my)) { fail(e); location.hash = `#/task/${id}`; } return; }
  if (stale(my)) return;
  const titleOf = (k) => snap.nodes.find((n) => n.node_key === k)?.title || k;
  const cards = a.cards?.length ? a.cards : fallbackCards(a.sections);    // 旧成果无 cards 时从章节兜底
  const ctx = { taskId: id, sections: a.sections, nodeTitle: titleOf };
  top(esc(a.title), t('成果 · 计划 v{v} · 图可放大、文档可打开阅读、文件可下载', { v: a.plan_version }),
    `<button class="btn" data-go="/task/${id}">${ic('flow')}${t('返回任务图')}</button><button class="btn" id="pb">${ic('save')}${t('保存为工作流')}</button>
     <button class="btn" id="arch">${ic('book')}${t('沉淀到知识库')}</button><a class="btn primary" href="/api/tasks/${id}/artifact/export?format=md">${ic('doc')}${t('导出 Markdown')}</a>`);
  const pending = snap.attention.filter((x) => x.kind === 'unsupported_claim').length;
  const issues = snap.nodes.filter((n) => ['failed', 'skipped'].includes(n.status)).length;
  $('#view').innerHTML = `<div class="page">${snap.task.status !== 'completed' ? `<div class="banner warn" style="margin-bottom:12px">${t('任务有新的修订正在执行，这里展示的是上一次完成的成果。')}</div>` : ''}
    ${a.quality?.demo ? `<div class="banner info">${t('包含演示模型输出或演示数据。')}</div>` : ''}
    ${snap.deliverable?.assumptions?.length ? `<div class="banner info"><b>${t('采用的假设')}</b><span>${snap.deliverable.assumptions.map(esc).join(' · ')}</span></div>` : ''}
    ${a.quality?.stale_sections?.length ? `<div class="banner warn">${t('部分上游成果已修改，相关结论尚未重算，请核查。')}</div>` : ''}
    ${a.quality?.level ? `<div class="banner ${a.quality.level === 'complete' ? 'info' : 'warn'}" style="margin-bottom:12px">${deliveryLabel(a.quality.level)}</div>` : ''}
    ${issues ? `<div class="banner warn" style="margin-bottom:12px">${ic('alert')}<span class="grow">${t('{n} 个节点未完成（失败或跳过），成果不含这部分内容。', { n: issues })}</span><a data-go="/task/${id}">${t('查看')}</a></div>` : ''}
    ${pending ? `<div class="banner info" style="margin-bottom:12px">${t('{n} 个节点含缺少出处的结论，已列入「待核实」。', { n: pending })}</div>` : ''}
    <div class="row" style="margin:6px 0 16px"><b style="font-size:15px">${t('本任务的交付物')}</b><span class="pill ac">${cards.length}</span>
      <span class="small muted grow">${t('图可放大 · 文档可打开阅读 · 文件可下载')}</span></div>
    <div id="dvHost"></div></div>`;
  mountDeliverables($('#dvHost'), cards, ctx);
  $('#arch').onclick = async () => { if (await confirmBox(t('沉淀到知识库'), t('把这份成果存入你的知识库，之后的任务可以检索并引用它。'), t('沉淀'))) POST(`/api/tasks/${id}/artifact/archive`).then(() => toast(t('已存入知识库'))).catch(fail); };
  $('#pb').onclick = async () => {
    const v = await modal(t('保存为个人工作流'), `<div class="field"><label>${t('工作流名称')}</label><input id="pn" value="${esc(snap.task.title)}"></div>
      <p class="small muted">${t('保存任务图与依赖关系、每个节点的能力需求与审批设置。下次发起相似任务时会提示复用，你仍可修改。')}</p>`,
      [{ label: t('取消'), value: null }, { label: t('保存'), cls: 'primary', onClick: (ov) => $('#pn', ov).value.trim() || false }]);
    if (v) POST('/api/playbooks', { task_id: id, name: v }).then(() => toast(t('已保存为个人工作流'))).catch(fail);
  };
}

// ======================= 知识库 =======================
async function viewKB(my = ROUTE) {
  top(t('我的知识库'), t('仅自己可见 · 任务执行时自动检索作为依据，成果确认后回流'),
    `<label class="btn">${ic('upload')}${t('上传资料')}<input type="file" id="kbf" class="hidden" multiple accept=".txt,.md,.markdown,.csv,.json,.html,.htm"></label>
     <button class="btn" id="kbn">${ic('pen')}${t('写笔记')}</button><button class="btn" id="kbc">${ic('link')}${t('剪藏网页')}</button>`);
  const draw = async () => {
    const [st, list] = await Promise.all([GET('/api/kb/stats'), GET('/api/kb/documents')]);
    if (stale(my)) return;
    const src = { upload: '上传', attachment: '任务附件', note: '笔记', clip: '剪藏', task_result: '任务成果' };
    $('#view').innerHTML = `<div class="page"><div class="grid2" style="grid-template-columns:repeat(4,1fr)">${[['资料总数', st.documents], ['已索引片段', st.chunks], ['被任务引用', st.cited], ['检索方式', 'BM25']].map(([k, v]) => `<div class="card"><div class="small muted">${t(k)}</div><b style="font-size:20px">${v}</b></div>`).join('')}</div>
      <div class="card" style="margin:14px 0"><div class="row"><input id="kq" placeholder="${esc(t('检索测试：输入问题，查看会被引用的片段'))}"><button class="btn" id="kqs">${ic('search')}${t('检索')}</button></div><div id="kqr"></div></div>
      <table class="tbl"><thead><tr><th>${t('标题')}</th><th>${t('来源')}</th><th>${t('片段')}</th><th>${t('被引用')}</th><th>${t('更新')}</th><th></th></tr></thead><tbody>
      ${list.items.map((d) => `<tr><td><a data-doc="${d.id}">${esc(d.title)}</a></td><td>${t(src[d.source] || d.source)}</td><td>${d.chunk_count}</td><td>${d.cited_count}</td><td>${ago(d.updated_at)}</td><td><a data-deld="${d.id}" style="color:var(--err)">${t('删除')}</a></td></tr>`).join('')}</tbody></table>
      ${list.items.length ? '' : `<div class="empty">${t('还没有资料。上传文件、写笔记或剪藏网页，任务执行时会自动检索它们。')}</div>`}</div>`;
    $('#kqs').onclick = async () => { const r = await POST('/api/kb/search', { query: $('#kq').value }).catch(fail); if (r) $('#kqr').innerHTML = r.items.map((i) => `<div class="small" style="margin-top:8px"><b>${esc(i.doc_title)}</b> <span class="muted">${i.score}</span><div class="muted">${esc(i.text.slice(0, 200))}</div></div>`).join('') || `<p class="small muted">${t('没有命中')}</p>`; };
    $('#view').onclick = async (e) => {
      const d = e.target.closest('[data-doc]'), x = e.target.closest('[data-deld]');
      if (x && (await confirmBox(t('删除资料？'), t('资料及其全部片段会被删除，已有成果中的引用将无法打开。'), t('删除'), true))) { await DEL('/api/kb/documents/' + x.dataset.deld).catch(fail); draw(); }
      if (d) { const r = await GET('/api/kb/documents/' + d.dataset.doc).catch(fail); if (!r) return; modal(r.document.title, r.chunks.map((c) => `<div class="card" style="padding:8px;margin-bottom:6px"><div class="small muted">#${c.idx} ${esc(c.heading)}</div><div class="small" style="white-space:pre-wrap">${esc(c.text)}</div></div>`).join(''), [], 680); }
    };
  };
  await draw();
  if (stale(my)) return;
  $('#kbf').onchange = async (e) => { for (const f of e.target.files) { const fd = new FormData(); fd.append('file', f); await api('POST', '/api/kb/documents/upload', fd).then(() => toast(t('已上传并索引：') + f.name)).catch(fail); } draw(); };
  $('#kbn').onclick = async () => { const v = await modal(t('写笔记'), `<div class="field"><label>${t('标题')}</label><input id="nt"></div><div class="field"><label>${t('内容（支持 Markdown）')}</label><textarea id="nx" style="min-height:180px"></textarea></div>`,
    [{ label: t('取消'), value: null }, { label: t('保存'), cls: 'primary', onClick: async (ov) => { await POST('/api/kb/documents/note', { title: $('#nt', ov).value, text: $('#nx', ov).value }); } }]); if (v) draw(); };
  $('#kbc').onclick = async () => { const v = await modal(t('剪藏网页'), `<div class="field"><label>URL</label><input id="cu" placeholder="https://"></div><div class="field"><label>${t('正文（可选，填写后不抓取网页）')}</label><textarea id="cx"></textarea></div>`,
    [{ label: t('取消'), value: null }, { label: t('剪藏'), cls: 'primary', onClick: async (ov) => { await POST('/api/kb/documents/clip', { url: $('#cu', ov).value, text: $('#cx', ov).value || undefined }); } }]); if (v) draw(); };
}

// ======================= 工作流与能力库 =======================
async function viewPlaybooks(my = ROUTE) {
  top(t('个人工作流'), t('把满意的任务流程保存下来，下次同类任务直接复用'));
  const draw = async () => {
    const r = await GET('/api/playbooks');
    if (stale(my)) return;
    $('#view').innerHTML = `<div class="page"><div class="grid2">${r.items.map((p) => `<div class="card"><div class="row"><b class="grow">${esc(p.name)}</b><span class="pill">${t('用过 {n} 次', { n: p.use_count })}</span></div>
      <div class="small muted" style="margin:6px 0">${esc(p.description)}</div><div class="small">${p.step_titles.map(esc).join(' → ')}</div>
      <div class="row" style="margin-top:10px"><span class="grow"></span><button class="btn sm" data-delp="${p.id}">${t('删除')}</button><button class="btn sm primary" data-go="/?playbook=${p.id}">${t('使用')}</button></div></div>`).join('')}</div>
      ${r.items.length ? '' : `<div class="empty">${t('还没有工作流。任务完成后在成果页点「保存为工作流」。')}</div>`}</div>`;
    $$('[data-delp]').forEach((b) => (b.onclick = async () => { if (await confirmBox(t('删除工作流？'), '', t('删除'), true)) { await DEL('/api/playbooks/' + b.dataset.delp); draw(); } }));
  };
  return draw();
}
async function viewCaps(my = ROUTE) {
  top(t('Agent 能力库'), t('可复用的基础能力；任务按能力需求确定性评分匹配，没有完全匹配时组合生成临时 Agent'));
  const r = await GET('/api/capabilities');
  if (stale(my)) return;
  $('#view').innerHTML = `<div class="page"><table class="tbl"><thead><tr><th>${t('能力')}</th><th>${t('能力标签')}</th><th>${t('可用工具')}</th><th>${t('权限')}</th><th>${t('成本档')}</th><th>${t('默认模型档')}</th></tr></thead><tbody>
    ${r.items.map((c) => `<tr><td><b>${esc(L(c.name, c.name_en))}</b><div class="small muted">${esc(t(c.description))}</div></td><td>${c.tags.map((x) => `<span class="pill">${esc(x)}</span>`).join(' ')}</td><td class="small mono">${c.tools.join('<br>')}</td><td>${t(c.permission === 'read' ? '只读' : '写入')}</td><td>${c.cost_tier}</td><td>${esc(t({fast:'快速档',standard:'标准档',strong:'强力档'}[c.default_tier] || c.default_tier))}</td></tr>`).join('')}</tbody></table>
    <div class="card" style="margin-top:16px"><h3>${t('匹配调试')}</h3><div class="row"><input id="mt" placeholder="${esc(t('能力标签，逗号分隔，例如 research, compare'))}"><input id="mtl" placeholder="${esc(t('工具，例如 kb.search'))}"><button class="btn" id="mgo">${t('匹配')}</button></div><div id="mres" class="small" style="margin-top:8px"></div></div>
    <p class="small muted">${t('所有工具调用都由服务端校验白名单与权限，模型不能自行扩大工具范围。临时 Agent 只在本任务内有效。')}</p></div>`;
  $('#mgo').onclick = async () => {
    const sp = (s) => s.split(/[,，\s]+/).filter(Boolean);
    const a = await POST('/api/capabilities/match', { capability_requirements: { tags: sp($('#mt').value), tools: sp($('#mtl').value), permission: 'read' } }).catch(fail);
    if (a) $('#mres').innerHTML = `<b>${esc(L(a.name, a.name_en))}</b> · ${t('匹配分')} ${a.score} · ${esc(L(a.reason, a.reason_en))}<br>${t('备选')}：${a.alternatives.map((x) => `${esc(L(x.name, x.name_en))}(${x.score})`).join('，')}`;
  };
}

// ======================= Agent 目录（v3.0 M1：外部 Agent 接入） =======================
const TRUST_TXT = { official: '官方', verified: '高星社区', community: '社区', local: '本地' };
async function viewAgents(my = ROUTE) {
  const state = { cat: null, mine: [], domain: '', q: '' };
  top(t('Agent 目录'), t('12 域 · 接入外部 Agent 扩展平台能力：连接即用（只读）；写操作默认关闭、走审批闸门；随时可断开'));
  $('#view').innerHTML = `<div class="page">
    <div class="row" style="margin-bottom:10px;flex-wrap:wrap;gap:8px">
      <input id="agQ" placeholder="${esc(t('搜索名称 / 描述 / 域'))}" style="max-width:240px">
      <button class="btn" id="agHealth">${ic('cpu')}<span>${t('一键体检')}</span></button>
      <button class="btn" id="agCustom">${ic('plus')}<span>${t('添加自定义')}</span></button>
      <span class="grow"></span><span class="small muted" id="agCount"></span>
    </div>
    <div class="row" id="agChips" style="flex-wrap:wrap;gap:6px;margin-bottom:12px"></div>
    <div id="agList" class="agt-grid"></div>
    <div class="banner info" style="margin-top:14px">${t('安全说明：凭据加密保存、只在本机使用，接口与日志永不回明文；社区级 Agent 的写操作逐次需要你批准；所有外部调用都会记入审计（工具、耗时、状态，不记参数明文）。')}</div>
  </div>`;

  const findItem = (k) => (state.cat?.items || []).find((x) => x.key === k) || (state.mine.find((r) => r.key === k) || { key: k, name: k, name_en: k, fields: [] });

  const doConnect = async (e, values) => {
    if (e.auth === 'oauth') { toast(t('OAuth 授权连接将在后续版本开放'), true); return; }
    toast(t('连接中…（首次可能要下载依赖，请稍候）'));
    const r = await POST(`/api/agents/${e.key}/connect`, { values: values || {} }).catch((err) => { fail(err); return null; });
    if (!r) return;
    if (r.ok) toast(r.message || t('已连接')); else toast(r.message || t('连接失败'), true);
    await load();
  };

  const askCredsAndConnect = async (e) => {
    const fields = e.fields || [];
    let values = {};
    if (fields.length) {
      const body = fields.map((f) => `<div class="field"><label>${esc(t(f.label || f.key))}${f.optional ? ` · ${t('可留空')}` : ''}</label>
        <input type="${f.secret ? 'password' : 'text'}" data-f="${esc(f.key)}" placeholder="${esc(t(f.hint || ''))}" autocomplete="off">
        ${f.url ? `<div class="small muted"><a href="${esc(f.url)}" target="_blank" rel="noopener">${t('获取凭据')} ↗</a></div>` : ''}</div>`).join('');
      const got = await modal(t('连接「{n}」', { n: L(e.name, e.name_en) }), body +
        `<div class="small muted">${t('凭据加密保存，仅用于本机调用；不会出现在日志里。')}</div>`,
        [{ label: t('取消'), value: false },
         { label: t('连接'), cls: 'primary', onClick: (ov) => {
             const out = {};
             fields.forEach((f) => { const el = $('[data-f="' + f.key + '"]', ov); const v = el ? el.value.trim() : ''; if (v) out[f.key] = v; });
             return out;
           } }], 520);
      if (got === false || got === null) return;
      values = got;
    }
    await doConnect(e, values);
  };

  const agTry = async (e, row) => {
    const tools = (row.tools || []).filter((x) => x.risk === 'read');
    if (!tools.length) { toast(t('没有可试用的只读工具'), true); return; }
    const body = `<div class="field"><label>${t('工具')}</label><select id="agTool">${tools.map((x) => `<option value="${esc(x.name)}">${esc(x.name)} — ${esc(x.desc || '')}</option>`).join('')}</select></div>
      <div class="field"><label>${t('参数（JSON 对象）')}</label><textarea id="agArgs" rows="3" placeholder="{}">{}</textarea></div>
      <div class="small muted">${t('只读工具可直接试跑；写 / 删除类工具会在任务执行时走审批闸门。')}</div>
      <pre class="agt-out" id="agOut"></pre>`;
    await modal(t('试跑「{n}」', { n: L(e.name, e.name_en) }), body, [
      { label: t('关闭'), value: false },
      { label: t('运行'), cls: 'primary', onClick: async (ov) => {
          const tool = $('#agTool', ov).value;
          let args = {};
          try { args = JSON.parse($('#agArgs', ov).value || '{}'); } catch (x) { toast(t('参数不是合法 JSON'), true); return false; }
          $('#agOut', ov).textContent = t('运行中…');
          const r = await POST(`/api/agents/${e.key}/call`, { tool, args }).catch((err) => { $('#agOut', ov).textContent = err?.message || String(err); return null; });
          if (r) $('#agOut', ov).textContent = JSON.stringify(r.result || {}, null, 2);
          return false;                                  // 留在弹窗里看完整结果
        } }], 560);
  };

  const card = (e, row) => {
    const nm = L(e.name, e.name_en);
    const st = row ? row.status : 'none';
    const stPill = st === 'connected' ? `<span class="pill ok">${t('已连接')}</span>`
      : st === 'error' ? `<span class="pill err">${t('连接出错')}</span>` : `<span class="pill">${t('未连接')}</span>`;
    const trust = `<span class="pill ac">${t(TRUST_TXT[e.trust] || e.trust || '社区')}</span>`;
    const dom = (state.cat?.domains || []).find((d) => d.key === e.domain);
    const domTxt = dom ? L(dom.zh, dom.en) : e.domain;
    const acts = [];
    if (!row) acts.push(`<button class="btn sm primary" data-act="install" data-k="${esc(e.key)}">${t('安装')}</button>`);
    else if (st === 'connected') {
      acts.push(`<button class="btn sm" data-act="try" data-k="${esc(e.key)}">${t('试跑')}</button>`);
      acts.push(`<button class="btn sm" data-act="refresh" data-k="${esc(e.key)}">${t('刷新工具')}</button>`);
      acts.push(`<button class="btn sm" data-act="disconnect" data-k="${esc(e.key)}">${t('断开')}</button>`);
    } else {
      acts.push(`<button class="btn sm primary" data-act="refresh" data-k="${esc(e.key)}">${t('重试连接')}</button>`);
      acts.push(`<button class="btn sm" data-act="connect" data-k="${esc(e.key)}">${t('重填凭据')}</button>`);
      acts.push(`<button class="btn sm" data-act="remove" data-k="${esc(e.key)}">${t('移除')}</button>`);
    }
    if (row && st === 'connected') acts.push(`<button class="switch ${row.enabled ? 'on' : ''}" data-act="toggle" data-k="${esc(e.key)}" role="switch" aria-label="${esc(t('启用'))}" aria-checked="${!!row.enabled}"><i></i></button>`);
    const err = row && row.error ? `<div class="small" style="color:var(--err)">${esc(errText('AGENT_CONNECT_FAILED', row.error))}</div>` : '';
    const note = row && row.note ? `<div class="small muted">${esc(t(row.note))}</div>` : '';
    const writeOn = row && row.allow_write ? `<span class="pill gate">${t('允许写操作')}</span>` : '';
    const kd = (row?.kind || e.kind || 'mcp');
    const kindPill = kd !== 'mcp' ? `<span class="pill ac">${t({ a2a: 'A2A 委派', cli: '本地 CLI' }[kd] || kd)}</span>` : '';   // v3.0 M4：接入类型
    return `<div class="agt" data-key="${esc(e.key)}">
      <div class="row"><b class="grow">${esc(nm)}${e.featured ? ` <span class="pill run">${t('首批')}</span>` : ''}</b>${stPill}</div>
      <div class="small muted">${esc(L(e.description, e.description_en))}</div>
      <div class="row small" style="margin-top:6px;flex-wrap:wrap;gap:4px">${trust}<span class="pill">${esc(domTxt)}</span>${kindPill}${writeOn}
        ${row ? `<span class="muted">${t('{n} 个工具', { n: row.tool_count })}</span>` : ''}
        ${e.auth === 'oauth' ? `<span class="pill gate">${t('OAuth · 稍后支持')}</span>` : ''}
      </div>
      ${err}${note}
      <div class="row agt-acts" style="margin-top:8px;gap:6px;flex-wrap:wrap">${acts.join('')}
        ${e.docs ? `<a class="small muted" href="${esc(e.docs)}" target="_blank" rel="noopener" style="margin-left:auto">${t('文档')} ↗</a>` : ''}</div>
    </div>`;
  };

  const chips = () => {
    const doms = state.cat?.domains || [];
    const mk = (k, label) => `<button class="ag-chip ${state.domain === k ? 'on' : ''}" data-dom="${esc(k)}">${esc(label)}</button>`;
    $('#agChips').innerHTML = mk('', t('全部')) + (state.mine.length ? mk('__mine', t('已连接') + ' ' + state.mine.length) : '') +
      doms.map((d) => mk(d.key, L(d.zh, d.en))).join('');
  };

  const draw = () => {
    const dom = state.domain, q = state.q.trim().toLowerCase();
    const mineMap = {}; state.mine.forEach((r) => (mineMap[r.key] = r));
    const items = (state.cat?.items || []).slice();
    state.mine.forEach((r) => { if (!items.find((i) => i.key === r.key)) items.push({ key: r.key, name: r.name, name_en: r.name_en, domain: r.domain, description: r.description, description_en: r.description_en, trust: r.trust, auth: r.auth, transport: r.transport, docs: r.docs, fields: [], installed: true }); });
    const shown = items.filter((e) => {
      if (dom === '__mine' && !mineMap[e.key]) return false;
      if (dom && dom !== '__mine' && e.domain !== dom) return false;
      if (q && !((e.name || '') + (e.name_en || '') + (e.description || '') + e.key).toLowerCase().includes(q)) return false;
      return true;
    }).sort((a, b) => (!!mineMap[b.key] - !!mineMap[a.key]) || ((b.featured ? 1 : 0) - (a.featured ? 1 : 0)) || String(a.key).localeCompare(String(b.key)));
    $('#agCount').textContent = t('{n} / {m} 个', { n: shown.length, m: items.length });
    $('#agList').innerHTML = shown.map((e) => card(e, mineMap[e.key])).join('') || `<div class="empty">${t('没有匹配的 Agent')}</div>`;
  };

  const load = async () => {
    const [cat, mine] = await Promise.all([GET('/api/agents/catalog'), GET('/api/agents')]);
    if (stale(my)) return;
    state.cat = cat; state.mine = mine.items || [];
    chips(); draw();
  };

  $('#agChips').onclick = (ev) => {
    const b = ev.target.closest('[data-dom]');
    if (!b) { return; }
    state.domain = b.dataset.dom;
    chips(); draw();
  };
  $('#agQ').oninput = () => { state.q = $('#agQ').value || ''; draw(); };

  $('#agList').onclick = async (ev) => {
    const b = ev.target.closest('[data-act]');
    if (!b) { return; }
    const act = b.dataset.act, k = b.dataset.k;
    const e = findItem(k);
    const row = state.mine.find((r) => r.key === k) || null;
    try {
      if (act === 'install') {
        await POST('/api/agents', { key: k });
        toast(t('已加入你的列表'));
        await load();
        const fresh = state.mine.find((r) => r.key === k);
        if (fresh && fresh.status !== 'connected') await askCredsAndConnect(findItem(k));
      } else if (act === 'connect') {
        await askCredsAndConnect(e);
      } else if (act === 'refresh') {
        if (e.auth === 'oauth') { toast(t('OAuth 授权连接将在后续版本开放'), true); return; }
        toast(t('连接中…（首次可能要下载依赖，请稍候）'));
        const r = await POST(`/api/agents/${k}/refresh`);
        r.ok ? toast(r.message || t('已刷新')) : toast(r.message || t('刷新失败'), true);
        await load();
      } else if (act === 'disconnect') {
        if (await confirmBox(t('断开「{n}」？', { n: L(e.name, e.name_en) }), t('凭据会被删除，工具从可用列表移除；历史任务不受影响。'), t('断开'), true)) {
          await DEL(`/api/agents/${k}`);
          toast(t('已断开'));
          await load();
        }
      } else if (act === 'remove') {
        if (await confirmBox(t('移除「{n}」？', { n: L(e.name, e.name_en) }), t('从你的列表删除这条接入记录。'), t('移除'), true)) {
          await DEL(`/api/agents/${k}`);
          toast(t('已移除'));
          await load();
        }
      } else if (act === 'toggle' && row) {
        await PATCH(`/api/agents/${k}`, { enabled: !row.enabled });
        await load();
      } else if (act === 'try' && row) {
        await agTry(e, row);
      }
    } catch (err) { fail(err); }
  };

  $('#agHealth').onclick = async () => {
    if (!state.mine.length) { toast(t('还没有已连接的 Agent')); return; }
    toast(t('体检中…'));
    const r = await POST('/api/agents/health').catch((err) => { fail(err); return null; });
    if (!r) return;
    const bad = (r.items || []).filter((x) => x.status !== 'connected').length;
    (bad ? toast(t('{n} 个 Agent 未连通，见卡片上的原因', { n: bad }), true) : toast(t('全部连通')));
    await load();
  };

  $('#agCustom').onclick = async () => {
    const selfHosted = S.meta.deploy_mode !== 'cloud';
    // v3.0 M4：自定义接入支持三种类型——MCP（stdio / http）、A2A（agent card 发现 + 委派）、CLI（本地命令，自托管专享）
    const body = `<div class="field"><label>${t('名称')}</label><input id="cuName" placeholder="${esc(t('例如：我们的内部知识库'))}"></div>
      <div class="field"><label>${t('接入类型')}</label><select id="cuKind">
        <option value="mcp">${t('MCP 工具服务器（stdio / http）')}</option>
        <option value="a2a">${t('A2A 外部 Agent（agent card 发现 + 委派）')}</option>
        ${selfHosted ? `<option value="cli">${t('本地 CLI 命令（自托管专享）')}</option>` : ''}
      </select></div>
      <div id="cuM1"><div class="field"><label>${t('传输方式')}</label><select id="cuTr"><option value="stdio">${t('stdio（本机进程 · 自托管）')}</option><option value="http">${t('http（远程服务）')}</option></select></div>
        <div class="field"><label>${t('命令（stdio）')}</label><input id="cuCmd" placeholder="${esc(t('npx / uvx / python / 可执行文件路径'))}"></div>
        <div class="field"><label>${t('参数（每行一个）')}</label><textarea id="cuArgs" rows="3" placeholder="-y&#10;@your/mcp-server"></textarea></div></div>
      <div id="cuM2" class="hidden"></div>
      <div id="cuM3" class="hidden"><div class="field"><label>${t('命令')}</label><input id="cuCli" placeholder="${esc(t('python / node / 可执行文件路径'))}"></div>
        <div class="field"><label>${t('参数（每行一个）')}</label><textarea id="cuCliArgs" rows="3" placeholder="scripts/agent.py&#10;--json"></textarea></div></div>
      <div class="field"><label>${t('Endpoint（http / A2A）')}</label><input id="cuUrl" placeholder="https://example.com/mcp  ·  https://agent.example.com"></div>
      <div class="field"><label>${t('环境变量 / 请求头（JSON，可留空）')}</label><textarea id="cuEnv" rows="2" placeholder='{"API_KEY": "…"}'></textarea></div>
      <div class="small muted">${t('自定义接入默认为社区级：写操作逐次需要你批准；凭据加密保存。A2A 会先读取 agent card 再调用；CLI 就在你的工作区内执行。')}</div>`;
    const specPromise = modal(t('添加自定义 Agent'), body, [
      { label: t('取消'), value: false },
      { label: t('添加'), cls: 'primary', onClick: (ov) => {
          const name = ($('#cuName', ov).value || '').trim();
          if (!name) { toast(t('请先填写名称'), true); return false; }
          const kind = $('#cuKind', ov).value;
          const out = { kind, name, transport: $('#cuTr', ov)?.value || 'stdio',
                        command: ($(kind === 'cli' ? '#cuCli' : '#cuCmd', ov).value || '').trim(),
                        args: ($(kind === 'cli' ? '#cuCliArgs' : '#cuArgs', ov).value || '').split('\n').map((s) => s.trim()).filter(Boolean),
                        endpoint: ($('#cuUrl', ov).value || '').trim() };
          const envRaw = ($('#cuEnv', ov).value || '').trim();
          if (envRaw) { try { out.env = JSON.parse(envRaw); } catch (x) { toast(t('环境变量不是合法 JSON'), true); return false; } }
          if (kind === 'mcp' && out.transport === 'http') out.transport = 'http'; else if (kind === 'mcp') out.transport = 'stdio';
          return out;
        } }], 580);
    // modal 同步写入 #overlay，所以**必须在 await 之前**绑定类型切换——
    // await 要等到用户点按钮才 resolve，那时弹窗内容已被清空，绑定会落在 null 上（真实缺陷）。
    const ov = $('#overlay');
    const kEl = ov.querySelector('#cuKind');
    if (kEl) {
      const sync = () => {
        const k = kEl.value;
        ov.querySelector('#cuM1').classList.toggle('hidden', k !== 'mcp');
        ov.querySelector('#cuM3').classList.toggle('hidden', k !== 'cli');
        const m2 = ov.querySelector('#cuM2');
        m2.classList.toggle('hidden', k !== 'a2a');
        if (k === 'a2a') m2.innerHTML = `<div class="small muted">${t('填上面的 Endpoint 即可：系统会先读 {p} 拿到这个 Agent 的能力清单，再把任务委派给它。', { p: '<code>/.well-known/agent.json</code>' })}</div>`;
      };
      kEl.onchange = sync; sync();
    }
    const spec = await specPromise;
    const r = await POST('/api/agents', { custom: spec }).catch((err) => { fail(err); return null; });
    if (!r) return;
    toast(t('已添加，点击「重试连接」开始'));
    await load();
  };

  await load();
}

// ======================= 设置（F26–F29） =======================
const SECTIONS = [['general', '通用'], ['models', '模型服务'], ['execution', '执行与协作'], ['knowledge', '知识库与偏好'], ['tools', '工具与权限'], ['notifications', '通知'], ['privacy', '隐私与数据'], ['account', '账户与用量'], ['advanced', '高级']];
const ROWS = {
  general: [['general.ui_language', '界面语言', '切换立即生效，不会打断正在执行的任务', 'enum', [['zh-CN', '简体中文'], ['en-US', 'English'], ['system', '跟随系统']]],
    ['general.output_language', '成果语言', '默认跟随你输入任务时用的语言', 'enum', [['follow_input', '跟随输入语言'], ['zh', '中文'], ['en', '英文']]],
    ['general.theme', '主题', '', 'enum', [['dark', '深色'], ['light', '浅色'], ['system', '跟随系统']]], ['general.density', '界面密度', '', 'enum', [['comfortable', '舒适'], ['compact', '紧凑']]]],
  execution: [['execution.default_mode', '默认任务模式', '自动判断复杂度：简单问题快速回答，复杂任务组建团队', 'enum', [['auto', '自动判断复杂度'], ['quick', '仅快速回答'], ['team', '团队协作']]],
    ['execution.max_concurrency', '并行上限', '同时执行的节点数（1–8）；服务商限流时会自动降低', 'int', 1, 8],
    ['execution.max_retries', '失败自动重试次数', '重试仍失败就标记该节点、继续跑其余部分，最后把问题清单随成果一起给你', 'int', 0, 3], ['execution.task_budget_cny', '单任务参考预算（元）', '只用于费用展示与估算，不会因为超预算暂停或拦截任务', 'float', 0.05, 100],
    ['execution.speculative', '等待批准期间预执行只读节点', '开启安全防护时，等你批准的同时先跑后面的只读节点；批准后直接生效，拒绝则作废', 'bool'], ['execution.speculative_max_cny', '单节点预执行费用上限（元）', '', 'float', 0, 2],
    ['safety.replay_readonly', '历史回放只读', '回放不会执行工具、不会产生费用', 'lock']],
  knowledge: [['knowledge.auto_search', '执行时自动检索知识库', '', 'bool'], ['knowledge.show_citations', '在成果中显示引用角标', '', 'bool'],
    ['knowledge.archive_mode', '成果回流知识库', '', 'enum', [['ask', '每次询问'], ['never', '从不']]], ['safety.uploads_are_data', '上传内容只作为资料', '其中的任何指令都不会被执行（防提示词注入）', 'lock']],
  tools: [['tools.file_write', '允许 Agent 写文件', '在你的任务工作区内新建或覆盖文件', 'bool'],
    ['tools.file_delete', '允许 Agent 删除文件', '删除工作区内的文件（不可恢复）', 'bool'],
    ['tools.shell_exec', '允许 Agent 执行命令', '在你的任务工作区内执行控制台命令；云端版不开放', 'bool'],
    ['tools.web_search', '外网检索', '多引擎聚合：网页（搜狗 / 360 / Bing）、新闻与公众号、学术文献、代码社区，并可抓取网页正文；关闭后 AI 不再联网检索', 'bool'],
    ['agents.enabled', '外部 Agent 调用', '在「Agent 目录」里连接的外部 Agent 是否参与任务；关闭后一律不调用（连接与试跑不受影响）', 'bool'],
    ['agents.mode', '外部调用决策档位', '保守：必须同时满足全部调用判据（时效事实 / 能力缺口 / 私域数据 / 真实执行 / 权威可验证）；平衡：满足任一判据即可；激进：主动推荐并预取', 'enum', [['conservative', '保守'], ['balanced', '平衡'], ['aggressive', '激进']]],
    ['agents.kb_share', '允许把知识库片段发给外部 Agent', '默认允许（调用会留痕）；关闭后，用到知识库片段的节点不再挂载外部工具，避免资料外发', 'bool'],
    ['approval.ask_dangerous', '危险操作先问我', '开启后，删除与控制台操作执行前逐次停下等你批准；默认关闭，一路直跑', 'bool'],
    ['safety.danger_ops_logged', '危险操作全程留痕', '每次工具调用与批准都写进事件流与执行日志（不可关闭）', 'lock']],
  notifications: [['notifications.approval', '需要我批准时', '保护规则：不可关闭', 'lock'], ['notifications.budget', '额度告警', '保护规则：不可关闭', 'lock'], ['notifications.completed', '任务完成', '', 'bool'], ['notifications.failed', '节点失败', '', 'bool']],
  privacy: [['privacy.event_retention_days', '执行记录保留天数', '', 'int', 7, 3650], ['privacy.learn_from_feedback', '用我的反馈优化 Agent 匹配', '只调整你自己的匹配权重，不训练模型', 'bool']],
  advanced: [['advanced.show_raw_events', '显示原始事件类型', '', 'bool'], ['canvas.highlight_critical', '默认高亮关键路径', '', 'bool']],
};
async function viewSettings(sec, my = ROUTE) {
  top(t('设置'), '');
  $('#view').innerHTML = `<div class="settings"><nav class="set-nav">${SECTIONS.map(([k, lb]) => `<button class="nav-item ${k === sec ? 'on' : ''}" data-go="/settings/${k}">${t(lb)}</button>`).join('')}</nav><div id="setBody"></div></div>`;
  const body = $('#setBody'), v = S.settings;
  const row = ([k, lb, help, kind, a, b, dep]) => {
    let ctl;
    if (kind === 'lock') ctl = `<span class="lock">${ic('lock')}${t('始终开启')}</span>`;
    else if (kind === 'bool') ctl = `<button class="switch ${v[k] ? 'on' : ''}" data-k="${k}" data-kind="bool" role="switch" aria-checked="${!!v[k]}"><i></i></button>`;
    else if (kind === 'enum') ctl = `<select data-k="${k}" data-kind="enum">${a.map(([x, l]) => `<option value="${x}" ${v[k] === x ? 'selected' : ''}>${x === 'en-US' ? 'English' : t(l)}</option>`).join('')}</select>`;
    else ctl = `<input type="number" data-k="${k}" data-kind="${kind}" value="${v[k]}" min="${a}" max="${b}" step="${kind === 'int' ? 1 : 0.05}">`;
    return `<div class="set-row"${dep ? ` data-dep="${dep}"` : ''}><div class="lab"><b>${t(lb)}</b>${help ? `<small>${t(help)}</small>` : ''}</div><div class="ctl">${ctl}</div></div>`;
  };
  if (ROWS[sec]) body.innerHTML = `<h2 style="font-size:16px">${t(SECTIONS.find((x) => x[0] === sec)[1])}</h2>${ROWS[sec].map(row).join('')}`;
  if (sec === 'tools' && S.meta.deploy_mode === 'cloud') body.insertAdjacentHTML('afterbegin', `<div class="banner info">${t('云端版不开放本机文件与命令：写文件、删除、控制台执行在这里始终不可用。')}</div>`);
  if (sec === 'models') await settingsModels(body);
  if (sec === 'knowledge') await settingsPrefs(body);
  if (sec === 'account') await settingsAccount(body);
  if (stale(my) || !body.isConnected) return;
  if (sec === 'advanced') body.innerHTML += `<div class="set-row"><div class="lab"><b>${t('部署形态')}</b><small>${t('由服务器配置 DEPLOY_MODE 决定：云端版不能访问你电脑上的本地模型')}</small></div><div class="ctl"><span class="pill ac">${S.meta.deploy_mode === 'cloud' ? t('云端版') : t('自托管版')}</span></div></div>`
    + (S.me.is_admin ? `<div class="card" style="margin-top:16px" id="adm"><h3>${t('管理：用量')}</h3></div>` : '');
  if (S.me.is_admin && sec === 'advanced') GET('/api/admin/usage').then((u) => { if (stale(my) || !$('#adm')) return; $('#adm').innerHTML += `<table class="tbl"><tr><th>${t('日期')}</th><th>${t('调用')}</th><th>${t('费用')}</th><th>${t('错误')}</th></tr>${u.by_day.map((d) => `<tr><td>${d.day}</td><td>${d.calls}</td><td>${money(d.cost)}</td><td>${d.errors}</td></tr>`).join('')}</table>`; });
  body.onchange = async (e) => {
    const el = e.target.closest('[data-k]'); if (!el || el.dataset.kind === 'bool') return;
    const val = el.dataset.kind === 'int' ? parseInt(el.value) : el.dataset.kind === 'float' ? parseFloat(el.value) : el.value;
    saveSetting(el.dataset.k, val);
  };
  body.addEventListener('click', (e) => { const sw = e.target.closest('.switch[data-k]'); if (sw) saveSetting(sw.dataset.k, !v[sw.dataset.k]); });
}
async function saveSetting(k, val) {
  try {
    S.settings = (await PATCH('/api/settings', { [k]: val })).values; applyTheme();
    if (k === 'general.ui_language') { setLang(val); $('#app').dataset.shell = ''; }
    toast(t('已保存')); route();
  } catch (e) { fail(e); route(); }
}
async function settingsModels(body) {
  const [mods, prov, cat] = await Promise.all([loadModels(true), GET('/api/providers'), GET('/api/providers/catalog')]);
  if (!body.isConnected) return;
  const opts = (cur, withAuto) => (withAuto ? `<option value="auto" ${cur === 'auto' ? 'selected' : ''}>${t('自动（按节点分档）')}</option>` : '') + mods.groups.filter((g) => g.key !== 'auto').flatMap((g) => g.items).map((m) => `<option value="${m.ref}" ${m.ref === cur ? 'selected' : ''} ${m.status !== 'ok' ? 'disabled' : ''}>${esc(t(m.display_name))}</option>`).join('');
  const v = S.settings;
  body.innerHTML = `<h2 style="font-size:16px">${t('模型服务')}</h2>${mods.fake_llm ? `<div class="banner info">${t('未配置平台模型 Key，平台内置模型当前由离线演示模型提供（结果为模拟内容）。可以添加你自己的服务商。')}</div>` : ''}
    <div class="set-row"><div class="lab"><b>${t('默认模型')}</b><small>${t('新建任务时输入框旁默认选中的模型')}</small></div><div class="ctl"><select data-k="models.default_model" data-kind="enum">${opts(v['models.default_model'], true)}</select></div></div>
    ${['fast', 'standard', 'strong'].map((x) => `<div class="set-row"><div class="lab"><b>${t({ fast: '快速档', standard: '标准档', strong: '强力档' }[x])}</b><small>${t('选择「自动」时，这一档的节点使用的模型')}</small></div><div class="ctl"><select data-k="models.tier_${x}" data-kind="enum">${opts(v['models.tier_' + x])}</select></div></div>`).join('')}
    <div class="set-row"><div class="lab"><b>${t('能力不足时改用平台内置模型')}</b><small>${t('例如所选模型不支持工具调用；替换会标注在节点上')}</small></div><div class="ctl"><button class="switch ${v['models.allow_builtin_fallback'] ? 'on' : ''}" data-k="models.allow_builtin_fallback" data-kind="bool"><i></i></button></div></div>
    <div class="row" style="margin:20px 0 8px"><h3 class="grow" style="margin:0;font-size:14px">${t('我的服务商')}</h3><button class="btn primary" id="addp">${ic('plus')}${t('添加服务商')}</button></div>
    ${prov.items.map((p) => `<div class="card" style="margin-bottom:8px"><div class="row"><b class="grow">${esc(p.display_name)}</b><span class="pill ${p.status === 'ok' ? 'ok' : p.status === 'untested' ? '' : 'err'}">${t({ ok: '连接正常', untested: '未测试', key_invalid: 'Key 无效', unreachable: '无法连接', limited: '能力受限' }[p.status] || p.status)}</span>
      ${p.latency_ms ? `<span class="small muted">${p.latency_ms} ms</span>` : ''}<button class="btn sm" data-pt="${p.id}">${t('测试连接')}</button><button class="btn sm" data-pm="${p.id}">${t('添加模型')}</button><button class="btn sm danger" data-pd="${p.id}">${t('删除')}</button></div>
      <div class="small muted">${esc(p.base_url)} · Key ${esc(p.api_key_hint || t('无'))} · ${t(p.source === 'local' ? '本地' : '外部')}</div>
      <div class="small" style="margin-top:4px">${p.models.map((m) => `${esc(m.model_name)} ${m.capabilities.map((c) => `<span class="cap">${t(CAPS[c] || c)}</span>`).join('')}`).join(' · ')}</div></div>`).join('') || `<p class="small muted">${t('还没有服务商。外部 Key 的费用由你自己的账户承担，平台只负责估算与限额。')}</p>`}`;
  $('#addp').onclick = () => addProvider(cat.items);
  body.onclick = async (e) => {
    const tb = e.target.closest('[data-pt]'), dl = e.target.closest('[data-pd]'), am = e.target.closest('[data-pm]');
    try {
      if (tb) { tb.disabled = true; tb.textContent = t('测试中…'); const r = await POST(`/api/providers/${tb.dataset.pt}/test`); toast(r.ok ? t('连接正常 · {ms} ms', { ms: r.latency_ms }) : t('测试未通过：{e}', { e: r.error || r.status }), !r.ok); S.models = null; viewSettings('models'); }
      if (dl && (await confirmBox(t('删除服务商？'), t('将删除它的模型列表和服务端保存的加密 Key。未开始的节点若指定了这些模型，会改用其他可用模型。'), t('删除'), true))) { await DEL('/api/providers/' + dl.dataset.pd); S.models = null; viewSettings('models'); }
      if (am) { const n = await modal(t('添加模型'), `<input id="mn" placeholder="${esc(t('模型名，例如 gpt-4o-mini'))}">`, [{ label: t('取消'), value: null }, { label: t('添加'), cls: 'primary', onClick: (ov) => $('#mn', ov).value.trim() || false }]);
        if (n) { await POST(`/api/providers/${am.dataset.pm}/models`, { model_name: n }); S.models = null; viewSettings('models'); } }
    } catch (err) { fail(err); }
  };
}
async function addProvider(kinds) {
  const st = { kind: null };
  const html = () => !st.kind ? `<p class="small muted">${t('选择服务类型')}</p><div class="kind-grid">${kinds.map((k) => `<button class="kind ${k.available ? '' : 'dis'}" data-kind="${k.kind}" ${k.available ? '' : `title="${esc(t('云端版无法访问你电脑上的模型，请使用自托管版'))}"`}><b>${esc(t(k.name))}</b><div class="small muted">${t(k.source === 'local' ? '本地模型' : '外部服务')}</div></button>`).join('')}</div>`
    : (() => { const k = kinds.find((x) => x.kind === st.kind); return `<div class="field"><label>${t('显示名称')}</label><input id="pn" value="${esc(t(k.name))}"></div>
      <div class="field"><label>${t('服务地址')}</label><input id="pu" value="${esc(k.default_base_url)}" placeholder="https://.../v1"></div>
      ${k.needs_key ? `<div class="field"><label>API Key</label><input id="pk" type="password" autocomplete="off" placeholder="sk-..."><div class="small muted">${t('只保存在服务端（加密），之后只显示末 4 位；不会出现在日志、执行记录与导出文件中。')}</div></div>` : ''}
      <div class="field"><label>${t('模型（逗号分隔）')}</label><input id="pm" value="${esc(k.suggested_models.join(', '))}"></div>`; })();
  const ov = $('#overlay');
  const p = modal(t('添加服务商'), `<div id="pv">${html()}</div>`, [{ label: t('取消'), value: null }, { label: t('保存并测试连接'), cls: 'primary', onClick: async (o) => {
    if (!st.kind) return toast(t('请先选择服务类型'), true), false;
    const key = $('#pk', o)?.value || '';
    const r = await POST('/api/providers', { kind: st.kind, display_name: $('#pn', o).value, base_url: $('#pu', o).value, api_key: key || undefined, models: $('#pm', o).value.split(/[,，]/).map((x) => x.trim()).filter(Boolean) });
    if ($('#pk', o)) $('#pk', o).value = '';                // 提交后立即清空输入框中的 Key
    const tr = await POST(`/api/providers/${r.provider.id}/test`);
    toast(tr.ok ? t('已添加，连接正常（JSON {j} · 工具调用 {tl}）', { j: tr.json ? '✓' : '✗', tl: tr.tools ? '✓' : '✗' }) : t('已添加，但测试未通过：{e}', { e: tr.error || tr.status }), !tr.ok);
  } }]);
  ov.querySelector('#pv').onclick = (e) => { const k = e.target.closest('[data-kind]'); if (!k) return; if (k.classList.contains('dis')) return toast(t('云端版无法访问你电脑上的模型，请使用自托管版'), true); st.kind = k.dataset.kind; ov.querySelector('#pv').innerHTML = html(); };
  await p; S.models = null; viewSettings('models');
}
async function settingsPrefs(body) {
  const r = await GET('/api/preferences');
  body.innerHTML += `<h3 style="font-size:14px;margin-top:22px">${t('个人偏好')}</h3><p class="small muted">${t('确认生效的偏好会作为要求提供给每个 Agent；系统从你的评论中发现的偏好需要你确认后才生效。')}</p>
    ${r.items.filter((p) => p.status !== 'dismissed').map((p) => `<div class="row card" style="padding:8px;margin-bottom:6px"><span class="grow small">${esc(p.statement)}</span><span class="pill ${p.status === 'active' ? 'ok' : 'gate'}">${t(p.status === 'active' ? '已生效' : '建议')}</span>
      ${p.status === 'suggested' ? `<button class="btn sm primary" data-pa="${p.id}">${t('确认')}</button><button class="btn sm" data-px="${p.id}">${t('忽略')}</button>` : `<button class="btn sm" data-pdel="${p.id}">${t('删除')}</button>`}</div>`).join('')}
    <div class="row"><input id="prefIn" placeholder="${esc(t('例如：成果尽量用表格呈现'))}"><button class="btn" id="prefAdd">${t('添加')}</button></div>`;
  $('#prefAdd').onclick = async () => { const v = $('#prefIn').value.trim(); if (v) { await POST('/api/preferences', { statement: v }).catch(fail); viewSettings('knowledge'); } };
  body.addEventListener('click', async (e) => {
    const a = e.target.closest('[data-pa]'), x = e.target.closest('[data-px]'), d = e.target.closest('[data-pdel]');
    if (a) await POST(`/api/preferences/${a.dataset.pa}/activate`); if (x) await POST(`/api/preferences/${x.dataset.px}/dismiss`); if (d) await DEL('/api/preferences/' + d.dataset.pdel);
    if (a || x || d) viewSettings('knowledge');
  });
}
async function settingsAccount(body) {
  const [me, ds] = await Promise.all([GET('/api/me'), GET('/api/me/data-summary')]);
  body.innerHTML = `<h2 style="font-size:16px">${t('账户与用量')}</h2>
    <div class="set-row"><div class="lab"><b>${t('邮箱')}</b></div><div class="ctl">${esc(me.user.email)}</div></div>
    <div class="set-row"><div class="lab"><b>${t('昵称')}</b></div><div class="ctl row"><input id="dn" value="${esc(me.user.display_name)}"><button class="btn sm" id="dns">${t('保存')}</button></div></div>
    <div class="set-row"><div class="lab"><b>${t('本月平台内置模型用量')}</b><small>${t('外部 Key 与本地模型不计入')}</small></div><div class="ctl">${money(me.quota.used)} / ${money(me.quota.monthly)}</div></div>
    <div class="set-row"><div class="lab"><b>${t('匹配权重')}</b><small>${t('根据你更换 Agent 的选择微调（不训练模型）')}</small></div><div class="ctl small mono">tag ${me.weights.w_tag} · tool ${me.weights.w_tool} · perm ${me.weights.w_perm} · cost ${me.weights.w_cost} · n=${me.weights.sample_count}</div></div>
    <div class="set-row"><div class="lab"><b>${t('修改密码')}</b></div><div class="ctl col"><input id="pw0" type="password" placeholder="${esc(t('当前密码'))}"><input id="pw1" type="password" placeholder="${esc(t('新密码（至少 8 位）'))}"><button class="btn sm" id="pws">${t('修改')}</button></div></div>
    <div class="set-row"><div class="lab"><b>${t('我的数据')}</b><small>${Object.entries(ds).map(([k, n]) => `${k} ${n}`).join(' · ')}</small></div><div class="ctl row"><a class="btn sm" href="/api/me/export">${t('导出全部数据')}</a></div></div>
    <div class="set-row"><div class="lab"><b style="color:var(--err)">${t('删除账户')}</b><small>${t('删除全部任务、知识库、工作流与服务商配置，不可恢复')}</small></div><div class="ctl"><button class="btn sm danger" id="delme">${t('删除账户')}</button></div></div>
    <div class="set-row"><div class="lab"><b>${t('退出登录')}</b></div><div class="ctl"><button class="btn sm" id="logout">${t('退出')}</button></div></div>`;
  $('#dns').onclick = () => PATCH('/api/me', { display_name: $('#dn').value }).then(async () => { await loadMe(); toast(t('已保存')); shell(); }).catch(fail);
  $('#pws').onclick = () => POST('/api/me/password', { current: $('#pw0').value, next: $('#pw1').value }).then(() => toast(t('密码已修改'))).catch(fail);
  $('#logout').onclick = async () => { await POST('/api/auth/logout').catch(() => {}); resetSession(); location.hash = '#/login'; };
  $('#delme').onclick = async () => {
    let tk; try { tk = (await GET('/api/me/delete-token')).confirm_token; } catch (e) { return fail(e); }
    const v = await modal(t('删除账户'), `<div class="banner err">${t('此操作不可恢复。')}</div><p>${t('请输入确认码 {c}', { c: `<b class="mono">${tk}</b>` })}</p><input id="dc">`,
      [{ label: t('取消'), value: null }, { label: t('永久删除'), cls: 'danger', onClick: (o) => $('#dc', o).value.trim() === tk || (toast(t('确认码不正确'), true), false) }]);
    if (!v) return;
    try { await POST('/api/me/delete', { confirm_token: tk }); } catch (e) { return fail(e); }   // 失败时不能当作已删除
    resetSession(); location.hash = '#/login';
  };
}
