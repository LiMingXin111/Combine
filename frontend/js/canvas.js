// 可操控的任务画布（F31）：平移、以指针为中心缩放、拖动节点、小地图、关键路径高亮、编辑模式。
// 视图操作只改布局（INV-22），只有编辑模式中「应用修改」才会把结构变化提交给后端。
// v1.6：成果（图 / 文 / 文件）作为「成果节点」进入任务图——虚线连到产出节点，自动排版、可拖拽，
//       固定 / 隐藏等视图状态持久化在布局里（仍是纯视图数据）。
// v1.7：成果内容**直接长在节点里**（图直接画出来、文档正文直接可读、文件直接可下载），不再点开弹层；
//       答案之间若有引用（下游节点引用了上游成果）自动画引用连线，所有答案与关系一目了然。
// v1.7.1：节点内图表改用紧凑布局（renderChart compact 300×190，与节点内容宽 1:1，图内字号不再被二次压缩）。
// v1.7.2：鼠标按键分工——中键按住拖动 = 平移整个画布（落在节点上也一样，不改选中）；左键才拖节点 / 拉连线；空白处左键与空格+拖动仍可平移。
// v1.8：点选成果节点回调 onArtSelect（供右侧面板展示与编辑）、「⋯」菜单新增「编辑内容」（onArtEdit）、隐藏时清空当前展示。
// v1.9：新增「地图」成果节点（旅行/路线类任务的攻略图，renderMap compact 300×240 直显，双击或菜单放大）。
// v2.0：地图成果节点默认挂载**可操控交互地图**（开源 Leaflet + OpenStreetMap 瓦片，拖动/缩放/点标记都在画布里直接用；
//       滚轮缩放需先点击地图，否则滚轮继续滚动画布）；节点内可一键切回「离线示意图」（renderMap SVG，无网环境降级）。
// v3.0：十类功能性成果节点（表格 / 指标 / 清单 / 代码 / 图片 / 时间线 / 日历 / 状态 / 会话 / 网页）在节点内直显；
//       清单勾选与表格排序是 L2 本地操作——纯视图状态存布局的 art.checks / art.sorts，不改成果数据、不产生事件（INV-22）。
import { esc, ic, md } from './core.js';
import { t, L } from './i18n.js';
import { fmtBytes } from './deliverables.js';
import { renderChart } from './chart.js';
import { renderMap } from './mapview.js';
import { mountLeafletMap, leafletAvailable } from './imap.js';
import { ART_KINDS, ART_ICON, ART_LBL, ART_SCROLL, artSub, renderArt, artExport } from './artview.js';

const NW = 216, GX = 270, GY = 140, ZMIN = 0.25, ZMAX = 2;
const AW = 320, AGAP = 22;                                // 成果节点：宽 / 同列最小间距
// 成果节点高度估算（自动排版与包围盒在渲染前也要有稳定尺寸）：
// map/chart/doc/file 沿用实测值；v3.0 十类功能性节点按「内容上限 + 头部/脚注」估算。
const A_EST = { map: 300, chart: 254, doc: 356, file: 108,
  table: 348, metrics: 236, list: 348, code: 340, image: 268, timeline: 340, calendar: 300, status: 300, chat: 340, web: 212 };
/** 十类功能性节点的键前缀（同 chart/map 一样按 node_key 生成：修订之间位置与固定状态稳定复用）。 */
const ART_KEY_PREFIX = { map: 'a:m:', chart: 'a:c:', table: 'a:t:', metrics: 'a:k:', list: 'a:l:', code: 'a:o:',
  image: 'a:i:', timeline: 'a:tl:', calendar: 'a:cal:', status: 'a:st:', chat: 'a:ch:', web: 'a:w:' };
/** 双击放大的节点选择器：图表 / 地图 + 十类功能性节点。 */
const AZOOM_SEL = ['.anode.k-chart', '.anode.k-map', ...ART_KINDS.map((k) => `.anode.k-${k}`)].join(', ');
/** 节点内的本地操作控件（勾选 / 排序 / 复制 / 链接）：点击它们不启动节点拖动、不改选中。 */
const ACTRL_SEL = '[data-avc],[data-avs],[data-avcopy],a[href]';
const STATUS = { pending: '等待中', running: '执行中', succeeded: '已完成', awaiting_approval: '等待你批准', skipped: '已跳过', failed: '失败', cancelled: '已取消' };

const hash = (s) => { let x = 5381; for (const ch of String(s)) x = ((x << 5) + x + ch.charCodeAt(0)) >>> 0; return x.toString(36); };

/** 成果节点键：同一份成果（同章节 / 同图表 / 同文件）在多次修订之间保持稳定，位置与固定状态据此复用。 */
export function artKey(c) {
  const pre = ART_KEY_PREFIX[c.kind];
  if (pre) return pre + (c.node_key || hash(c.title || ''));
  if (c.kind === 'doc') return 'a:d:' + (c.section_key || hash(c.title || ''));
  return 'a:f:' + hash(c.path || c.title || '');
}

export class CanvasView {
  constructor(root, opts) {
    this.root = root; this.o = opts; this.nodes = []; this.pos = {}; this.vp = { x: 40, y: 40, z: 1 };
    this.sel = null; this.selectedKeys = new Set(); this.cp = false; this.edit = null; this.mmCol = true; this.selEdge = null; this.statusOverride = null;
    this.arts = []; this.artMeta = null; this._secBy = {}; // 成果节点（cards）、版本信息与章节正文索引
    this.art = { hidden: new Set(), pinned: new Set(), focus: true, checks: {}, sorts: {} };   // 持久化的成果视图状态（checks/sorts 为 L2 本地操作，仍是纯视图数据 INV-22）
    this.artSeen = new Set(); this.selArt = null; this.artMenu = null;
    this._refs = {}; this._refEdges = [];                  // 答案之间的关联引用（v1.7）
    this._mapMode = new Map(); this._imapActiveEl = null;  // v2.0 地图：逐节点视图模式（'live' 可操控 / 'svg' 示意图）与当前激活的容器
    root.innerHTML = `<div class="canvas" tabindex="0" aria-label="${t('任务图画布')}"><div class="cv-inner"><svg></svg><div class="cv-nodes"></div><div class="cv-ui"></div></div>
      <div class="cv-float par-card hidden"></div><div class="cv-float edit-banner hidden"></div>
      <div class="cv-float art-menu hidden"></div><div class="cv-selection-box hidden"></div>
      <div class="cv-float cv-selection-status hidden" role="status" aria-live="polite"></div>
      <div class="cv-float minimap col"></div></div>`;
    this.cv = root.querySelector('.canvas'); this.inner = root.querySelector('.cv-inner');
    this.svg = root.querySelector('svg'); this.layer = root.querySelector('.cv-nodes'); this.ui = root.querySelector('.cv-ui');
    this.par = root.querySelector('.par-card'); this.banner = root.querySelector('.edit-banner'); this.mm = root.querySelector('.minimap');
    this.artM = root.querySelector('.art-menu');
    this.selectionBox = root.querySelector('.cv-selection-box'); this.selectionStatus = root.querySelector('.cv-selection-status');
    this._ac = new AbortController();                      // 统一移除 document 级监听
    this._bind();
  }
  /** 视图销毁时调用：移除 document 上的键盘监听、断开尺寸观察、丢弃未保存的布局定时器。 */
  destroy() { this._ac.abort(); this._ro?.disconnect(); clearTimeout(this._lt); this._killMaps(); this.dead = true; }

  // ---------- 数据 ----------
  setData(nodes, est, layout) {
    const first = !this.nodes.length;
    const keysChanged = nodes.map((n) => n.node_key).join() !== this.nodes.map((n) => n.node_key).join();
    this.nodes = nodes; this.est = est;
    if (layout?.positions) for (const [k, p] of Object.entries(layout.positions)) if (!this.pos[k]) this.pos[k] = p;
    this._autoPlace(false);
    if (this.edit) return this.render();          // 编辑中：服务端状态照常刷新，但不打断草稿
    this.render();
    if (first) { if (layout?.viewport?.z) { this.vp = layout.viewport; this._apply(); } else this.fit(); }
    else if (keysChanged && !this.userMoved) this.fit();
  }
  current() { return this.edit ? this.edit.nodes : this.nodes; }

  // ---------- 成果节点（v1.6） ----------
  /** 从布局恢复成果视图状态（hidden / pinned / focus，以及 L2 勾选与排序）。 */
  setArtState(a) {
    if (!a) return;
    this.art.hidden = new Set(a.hidden || []);
    this.art.pinned = new Set(a.pinned || []);
    this.art.focus = a.focus !== false;
    this.art.checks = a.checks && typeof a.checks === 'object' ? a.checks : {};
    this.art.sorts = a.sorts && typeof a.sorts === 'object' ? a.sorts : {};
  }
  /** 更新成果节点集合。meta：{ id, version, stale, sections }——首次见到这份成果时自动聚焦。 */
  setArtifacts(cards, meta) {
    this.arts = cards || []; this.artMeta = meta || null;
    this._secBy = {}; for (const s of meta?.sections || []) this._secBy[s.section_key] = s;   // 节点内直显正文用
    if (this.selArt && !this.arts.some((c) => artKey(c) === this.selArt)) { this.selArt = null; this.o.onArtSelect?.(null, null); }
    this._autoPlace(false);                                // 新成果自动排进右侧泳道（只补空缺，不覆盖用户摆好的位置）
    this.render();
    const id = meta?.id;
    if (id && this.arts.length && !this.artSeen.has(id)) {
      this.artSeen.add(id);
      if (!this.userMoved && this.art.focus !== false) requestAnimationFrame(() => { if (!this.dead && this.cv.isConnected) this.fit(); });
    }
  }
  _visibleArts() { return this.edit ? [] : this.arts.filter((c) => !this.art.hidden.has(artKey(c))); }
  artList() { return this.arts.map((c) => { const k = artKey(c); return { card: c, key: k, hidden: this.art.hidden.has(k), pinned: this.art.pinned.has(k) }; }); }
  titleOfKey(k) { return this.nodes.find((n) => n.node_key === k)?.title || ''; }

  _depths(nodes) {
    const by = Object.fromEntries(nodes.map((n) => [n.node_key, n])), d = {};
    const f = (k, seen = new Set()) => { if (d[k] != null) return d[k]; if (seen.has(k)) return 0; seen.add(k);
      const n = by[k]; return (d[k] = n && n.depends_on.length ? 1 + Math.max(...n.depends_on.filter((x) => by[x]).map((x) => f(x, seen))) : 0); };
    nodes.forEach((n) => f(n.node_key)); return d;
  }
  _autoPlace(all) {
    const nodes = this.current(), d = this._depths(nodes), cols = {};
    nodes.forEach((n) => (cols[d[n.node_key]] ||= []).push(n));
    const maxH = Math.max(1, ...Object.values(cols).map((c) => c.length));
    for (const [c, list] of Object.entries(cols)) {
      list.sort((a, b) => (b.rank || 0) - (a.rank || 0));
      list.forEach((n, i) => { if (all || !this.pos[n.node_key]) this.pos[n.node_key] = { x: 40 + c * GX, y: 40 + (i + (maxH - list.length) / 2) * GY }; });
    }
    // 成果泳道（v1.6）：排在任务图右侧，按「产出节点所在层」分列；不与任务列抢位，任务节点的既有位置不动
    // 注意：隐藏的成果也要分配位置——恢复显示时立刻有落点，不会堆到原点
    const byDepth = {};
    for (const c of this.arts) {
      const dd = c.node_key != null && d[c.node_key] != null ? d[c.node_key] : 'end';
      (byDepth[dd] ||= []).push(c);
    }
    const laneKeys = Object.keys(byDepth).sort((a, b) => (a === 'end' ? 1e9 : +a) - (b === 'end' ? 1e9 : +b));
    if (!laneKeys.length) return;
    let right = 0;
    for (const n of nodes) { const p = this.pos[n.node_key]; if (p) right = Math.max(right, p.x + NW); }
    const laneX = {}; let lx = right + 70;
    for (const k of laneKeys) { laneX[k] = lx; lx += AW + 64; }
    for (const k of laneKeys) {
      const seq = {}, entries = byDepth[k].map((c) => {
        const pk = c.node_key != null && this.pos[c.node_key] ? c.node_key : '';
        const i = (seq[pk] = (seq[pk] || 0)); seq[pk]++;
        return { c, py: pk ? this.pos[pk].y : 40, i };
      });
      entries.sort((a, b) => (a.py - b.py) || (a.i - b.i));
      let floor = -1e9;
      for (const e of entries) {
        const y = Math.max(e.py, floor + AGAP);
        floor = y + this._artH(e.c);                       // 按节点实际高度（估算）顺排，防止不同高度的成果重叠
        const key = artKey(e.c);
        if (all || !this.pos[key]) this.pos[key] = { x: laneX[k], y: Math.round(y / 8) * 8 };
      }
    }
  }
  /** 成果节点高度估算：自动排版与包围盒在渲染前也要有稳定尺寸。 */
  _artH(c) { return A_EST[c?.kind] || 200; }
  tidy() { const old = JSON.stringify(this.pos); this.lastPos = old; this._autoPlace(true); this.layer.classList.add('anim'); this.render(); this._saveLayout(); setTimeout(() => this.layer.classList.remove('anim'), 260); }
  undoTidy() { if (this.lastPos) { this.pos = JSON.parse(this.lastPos); this.lastPos = null; this.render(); this._saveLayout(); return true; } return false; }

  // ---------- 渲染 ----------
  render() {
    this._killMaps();                                      // v2.0：DOM 即将重建——先销毁其中挂载的交互地图实例
    const nodes = this.current(), cpSet = new Set(this.est?.critical_path || []), ed = this.edit;
    this._computeRefs();                                   // 先推导答案之间的关联引用（节点内也显示引用计数）
    this.cv.classList.toggle('cp-on', this.cp);
    this.layer.innerHTML = nodes.map((n) => {
      const st = this.statusOverride ? (this.statusOverride[n.node_key] || 'pending') : n.status || 'pending';
      const badges = [];
      if (n.spec_state === 'running') badges.push(`<span class="gb sp">${t('预执行中')}</span>`);
      if (n.spec_state === 'ready') badges.push(`<span class="gb sp">${t('已预执行，待批准生效')}</span>`);
      if (n.spec_state === 'committed') badges.push(`<span class="gb sp">${t('预执行已提交')}</span>`);
      if (n.model_override) badges.push(`<span class="gb">${t('节点指定模型')}</span>`);
      if (n.agent?.source === 'composed') badges.push(`<span class="gb">${t('临时 Agent')}</span>`);
      if (this.cp && cpSet.has(n.node_key)) badges.push(`<span class="gb cp">${t('关键路径')}</span>`);
      const time = n.finished_at && n.started_at ? Math.round((n.finished_at - n.started_at) / 100) / 10 + 's' : n.est_duration_s ? '≈' + Math.round(n.est_duration_s) + 's' : '';
      const p = this.pos[n.node_key] || { x: 0, y: 0 };
      const cls = ['gnode', 's-' + st, this.sel === n.node_key ? 'sel' : '', this.cp && !cpSet.has(n.node_key) ? 'dim' : '',
        ed?.added.has(n.node_key) ? 'added' : '', ed?.invalid?.has(n.node_key) ? 'invalid' : '', ed?.target === n.node_key ? 'target' : ''].join(' ');
      return `<div class="${cls}" data-key="${esc(n.node_key)}" style="left:${p.x}px;top:${p.y}px" tabindex="-1">
        ${ed ? `<button class="gn-del" data-del="${esc(n.node_key)}" title="${t('删除节点')}">×</button><span class="port" data-port="${esc(n.node_key)}"></span>` : ''}
        <div class="a">${esc(L(n.agent?.name, n.agent?.name_en) || t('应用后匹配 Agent'))}</div>
        <div class="t">${esc(n.title)}</div>
        <div class="st"><span class="dot"></span>${t(STATUS[st] || st)}<span class="grow"></span><span class="muted">${time}</span></div>
        ${badges.length ? `<div class="bd">${badges.join('')}</div>` : ''}</div>`;
    }).join('') + this._visibleArts().map((c) => this._artHtml(c)).join('');
    this._syncSelection(); this._edges(); this._minimap(); this._parCard(); this._banner(); this._syncMaps();
  }
  /** 成果节点（v1.7）：内容直显——图直接画出来、文档正文直接读、文件直接下载；不点开、不弹层。
   *  v3.0：十类功能性节点（表格 / 指标 / 清单 / 代码 / 图片 / 时间线 / 日历 / 状态 / 会话 / 网页）同样直显。 */
  _artHtml(c) {
    const key = artKey(c), meta = this.artMeta || {};
    const ICON = { map: 'pin', chart: 'chart', doc: 'doc', file: 'file', ...ART_ICON };
    const LABEL = { map: '地图', chart: '图表', doc: '文档', file: '文件', ...ART_LBL };
    const sub = c.kind === 'map' ? t('{n} 个地点', { n: (c.map?.markers || []).length })
      : c.kind === 'doc' ? t('共 {n} 字', { n: c.chars || 0 })
        : c.kind === 'file' ? [String(c.ext || '').toUpperCase(), fmtBytes(c.bytes)].filter(Boolean).join(' · ')
          : c[c.kind] ? artSub(c.kind, c[c.kind]) : '';      // 十类功能性节点：一句话说明规模（列×行 / 条目数 / 行数…）
    const rc = this._refs[key] || {};
    const rel = [rc.out ? t('依赖 {n}', { n: rc.out }) : '', rc.in ? t('被依赖 {n}', { n: rc.in }) : ''].filter(Boolean).join(' · ');
    const tags = [];
    if (c.user_edited) tags.push(`<span class="an-tag edit">${ic('pen')}${t('已修改')}</span>`);   // v1.8：你手工改过这一节
    if (meta.stale) tags.push(`<span class="an-tag warn">${t('旧版 v{v}', { v: meta.version })}</span>`);
    if (this.art.pinned.has(key)) tags.push(`<span class="an-tag">${ic('pin')}${t('已固定')}</span>`);
    const foot = [t(LABEL[c.kind] || ''), sub, c.node_key ? t('来自节点：') + (this.titleOfKey(c.node_key) || c.node_key) : '', rel].filter(Boolean).join(' · ');
    const p = this.pos[key] || { x: 0, y: 0 };
    return `<div class="anode k-${esc(c.kind)} ${this.selArt === key ? 'sel' : ''}" data-akey="${esc(key)}" style="left:${p.x}px;top:${p.y}px" tabindex="-1" title="${esc(c.title || '')}">
      <div class="an-h"><span class="an-i">${ic(ICON[c.kind] || 'doc')}</span><span class="an-t">${esc(c.title || t('成果'))}</span>
        <button class="an-more" data-amen="${esc(key)}" title="${esc(t('更多操作'))}">⋯</button></div>
      ${this._artBody(c)}
      <div class="an-m"><span class="an-mt">${esc(foot)}</span>${tags.join('')}</div></div>`;
  }
  /** 节点内直显的内容：地图（可操控交互地图 / 离线示意图）/ 图表 SVG / 文档正文（限高滚动）/ 文件下载。 */
  _artBody(c) {
    if (c.kind === 'map') {
      const key = artKey(c);
      const live = leafletAvailable() && this._mapMode.get(key) !== 'svg';   // v2.0：默认可操控；可一键切回离线示意图
      const body = live ? '<div class="im-map"></div>' : renderMap(c.map, { compact: true });
      if (!body) return '';
      return `<div class="an-c map${live ? ' live' : ''}">${body}<button class="im-sw" data-imsw="${esc(key)}" title="${esc(t(live ? '切换为离线示意图' : '切换为可操控地图'))}">${ic(live ? 'image' : 'pin')}${t(live ? '示意图' : '操控地图')}</button></div>`;
    }
    if (c.kind === 'chart') { const svg = renderChart(c.chart, { compact: true }); return svg ? `<div class="an-c chart">${svg}</div>` : ''; }
    if (c.kind === 'doc') {
      const sec = this._secBy?.[c.section_key];
      const src = String(sec?.content || c.excerpt || '').trim();
      return src ? `<div class="an-c an-scroll">${md(src)}</div>` : '';
    }
    if (ART_KINDS.includes(c.kind)) {                     // v3.0：十类功能性节点，内容直显（L1）
      const d = c[c.kind]; if (!d) return '';
      const key = artKey(c);
      const scroll = ART_SCROLL.has(c.kind) ? ' an-scroll' : '';
      return `<div class="an-c${scroll}">${renderArt(c.kind, d, { compact: true, key, checks: this.art.checks, sorts: this.art.sorts })}</div>`;
    }
    return `<div class="an-c an-file"><a class="btn sm" href="/api/workspace/file/download?path=${encodeURIComponent(c.path || '')}" draggable="false" title="${esc(t('下载'))}">${ic('down')}${t('下载')}</a></div>`;
  }
  /** 局部重绘一个成果节点（L2 勾选 / 排序后调用）：只替换这一个节点的 DOM，不惊动其它节点的滚动位置。 */
  _refreshArtNode(key) {
    const c = this.arts.find((x) => artKey(x) === key);
    const el = this.layer.querySelector(`[data-akey="${CSS.escape(key)}"]`);
    if (!c || !el) return;
    const tmp = document.createElement('div'); tmp.innerHTML = this._artHtml(c);
    const fresh = tmp.firstElementChild; if (!fresh) return;
    el.replaceWith(fresh);
    this._edges();                                        // 高度可能变化：重算连线锚点
    this._saveLayout();
    this.o.onArtState?.();                                // 右栏 / 节点管理同步（若正在展示这份成果）
  }
  /** L2（v3.0）：切换清单条目的本地勾选（纯视图状态，不改成果数据、不产生事件）。 */
  toggleArtCheck(key, i) {
    const set = new Set(this.art.checks[key] || []);
    if (set.has(i)) set.delete(i); else set.add(i);
    this.art.checks[key] = [...set].sort((x, y) => x - y);
    this._refreshArtNode(key);
    return this.art.checks[key];
  }
  /** L2（v3.0）：切换表格的本地排序列 / 方向（同样纯视图状态）。 */
  toggleArtSort(key, i) {
    const cur = this.art.sorts[key];
    this.art.sorts[key] = cur && cur[0] === i ? [i, cur[1] === 'asc' ? 'desc' : 'asc'] : [i, 'asc'];
    this._refreshArtNode(key);
    return this.art.sorts[key];
  }
  // ---------- 画布内可操控地图（v2.0，开源 Leaflet） ----------
  /** DOM 重建前销毁所有已挂载的交互地图实例（清监听、断开 Leaflet 内部引用）。 */
  _killMaps() {
    this.layer.querySelectorAll('.im-map').forEach((el) => el._imap?.destroy());
    this._imapActiveEl = null;
  }
  /** render 之后把地图容器挂载为可操控地图（视图状态按节点键记忆，重建恢复）。 */
  _syncMaps() {
    if (!leafletAvailable()) return;
    for (const el of this.layer.querySelectorAll('.im-map')) {
      if (el._imap) continue;
      const akey = el.closest('.anode')?.dataset.akey;
      const c = this.arts.find((x) => artKey(x) === akey);
      if (!c?.map) continue;
      mountLeafletMap(el, c.map, { key: akey, onActiveChange: (on) => this._onImapActive(el, on) });
    }
  }
  /** 某个地图进入 / 退出「滚轮缩放激活态」：同时只允许一个（激活新的会取消旧的）。 */
  _onImapActive(el, on) {
    if (on) {
      if (this._imapActiveEl && this._imapActiveEl !== el) this._imapActiveEl._imap?.setActive(false);
      this._imapActiveEl = el;
    } else if (this._imapActiveEl === el) this._imapActiveEl = null;
  }
  /** 退出当前地图激活态（点击画布空白 / 按 Esc 时调用）。 */
  _deactivateImap() { const el = this._imapActiveEl; if (el) { this._imapActiveEl = null; el._imap?.setActive(false); } }

  /** 推导答案之间的关联引用（v1.7）：下游成果 → 它直接依赖的产出节点的成果（跳过文件卡，文件是打包形态）。 */
  _computeRefs() {
    const vis = this._visibleArts(), by = Object.fromEntries(this.current().map((n) => [n.node_key, n]));
    const at = {}; for (const c of vis) { if (c.kind !== 'file' && c.node_key) (at[c.node_key] ||= []).push(c); }
    const refs = {}, edges = [], seen = new Set();
    for (const c of vis) {
      if (c.kind === 'file' || !c.node_key) continue;
      const n = by[c.node_key]; if (!n) continue;
      for (const u of n.depends_on) for (const o of at[u] || []) {
        const from = artKey(c), to = artKey(o);
        if (from === to || seen.has(from + '>' + to)) continue; seen.add(from + '>' + to);
        edges.push({ from, to });
        (refs[from] ||= { out: 0, in: 0 }).out++; (refs[to] ||= { out: 0, in: 0 }).in++;
      }
    }
    this._refEdges = edges; this._refs = refs;
  }
  _box(k) {
    const el = this.layer.querySelector(`[data-key="${CSS.escape(k)}"]`), p = this.pos[k] || { x: 0, y: 0 };
    return { x: p.x, y: p.y, w: NW, h: el ? el.offsetHeight : 90 };
  }
  _abox(key) {
    const el = this.layer.querySelector(`[data-akey="${CSS.escape(key)}"]`), p = this.pos[key] || { x: 0, y: 0 };
    const c = this.arts.find((x) => artKey(x) === key);
    return { x: p.x, y: p.y, w: AW, h: el ? el.offsetHeight : this._artH(c) };
  }
  _edges() {
    const nodes = this.current(), by = Object.fromEntries(nodes.map((n) => [n.node_key, n])), cp = this.est?.critical_path || [];
    const cpE = new Set(cp.slice(1).map((k, i) => cp[i] + '>' + k));
    let s = '';
    for (const v of nodes) for (const u of v.depends_on) {
      if (!by[u]) continue;
      const a = this._box(u), b = this._box(v.node_key), x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2, m = (x1 + x2) / 2;
      const d = x2 > x1 + 20 ? `M${x1},${y1} C${m},${y1} ${m},${y2} ${x2 - 4},${y2}` : `M${x1},${y1} C${x1 + 60},${y1} ${x2 - 60},${y2} ${x2 - 4},${y2}`;
      const su = this.statusOverride ? this.statusOverride[u] : by[u].status, sv = this.statusOverride ? this.statusOverride[v.node_key] : v.status;
      const cls = ['w', su === 'succeeded' && sv === 'succeeded' ? 'done' : '', su === 'succeeded' && sv === 'running' ? 'live' : '',
        su === 'awaiting_approval' ? 'gate' : '', (v.redundant_edges || []).includes(u) ? 'red' : '',
        this.cp && cpE.has(u + '>' + v.node_key) ? 'cp' : '', this.selEdge === u + '>' + v.node_key ? 'sel' : '',
        this.edit?.addedEdges.has(u + '>' + v.node_key) ? 'tmp' : ''].join(' ');
      s += `<path class="${cls}" d="${d}" marker-end="url(#arr)"/>` + (this.edit ? `<path class="w-hit" data-edge="${esc(u)}>${esc(v.node_key)}" d="${d}" style="pointer-events:stroke"/>` : '');
    }
    for (const c of this._visibleArts()) {                  // 成果边：产出节点 → 成果节点（虚线，类型着色）
      if (!c.node_key || !by[c.node_key]) continue;
      const key = artKey(c), a = this._box(c.node_key), b = this._abox(key);
      const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2, m = (x1 + x2) / 2;
      s += `<path class="w art k-${esc(c.kind)}${this.selArt === key ? ' sel' : ''}" d="M${x1},${y1} C${m},${y1} ${m},${y2} ${x2 - 4},${y2}" marker-end="url(#arr)"/>`;
    }
    for (const r of this._refEdges || []) {                 // 引用边（v1.7）：下游答案 → 它引用的上游答案（细虚线 + 实心箭头）
      const a = this._abox(r.from), b = this._abox(r.to);
      const x1 = a.x, y1 = a.y + a.h / 2, x2 = b.x + b.w, y2 = b.y + b.h / 2, m = Math.max(36, Math.abs(x1 - x2) / 2);
      const sel = this.selArt === r.from || this.selArt === r.to ? ' sel' : '';
      s += `<path class="w ref${sel}" d="M${x1},${y1} C${x1 - m},${y1} ${x2 + m},${y2} ${x2 + 4},${y2}" marker-end="url(#arr-ref)"/>`;
    }
    this.svg.innerHTML = `<defs><marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0L10,5L0,10z" fill="currentColor" style="color:var(--line-2)"/></marker>
      <marker id="arr-ref" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0L10,5L0,10z" fill="currentColor" style="color:var(--accent)"/></marker></defs>${s}<path class="w tmp hidden" id="tmpEdge"/>`;
    this.svg.style.pointerEvents = this.edit ? 'auto' : 'none';
    this.svg.setAttribute('width', 4000); this.svg.setAttribute('height', 3000);
    this.ui.innerHTML = '';
    if (this.selEdge && this.edit) {
      const [u, v] = this.selEdge.split('>'); const a = this._box(u), b = this._box(v);
      this.ui.innerHTML = `<button class="edge-del" data-edgedel="1" style="left:${(a.x + a.w + b.x) / 2}px;top:${(a.y + a.h / 2 + b.y + b.h / 2) / 2}px" title="${t('删除依赖')}">×</button>`;
    }
  }
  _parCard() {
    const e = this.est; if (!e || this.edit) { this.par.classList.add('hidden'); return; }
    this.par.classList.remove('hidden');
    const running = this.current().filter((n) => n.status === 'running').length;
    this.par.innerHTML = `<span>${t('预计')} <b>${Math.round(e.makespan_s)}s</b></span><span>${t('串行')} <b>${Math.round(e.serial_s)}s</b></span>
      <span class="sv">${t('并行节省')} ${Math.round((e.parallel_saving || 0) * 100)}%</span><span>${t('当前并行')} <b>${running}/${e.limit}</b></span>`;
    this.par.onclick = () => this.o.onOpenGantt?.();
  }
  _banner() {
    const ed = this.edit;
    this.banner.classList.toggle('hidden', !ed);
    if (!ed) return;
    this.banner.innerHTML = `<span>${t(this.o.isDraft?.() ? '编辑中 · 修改直接写入计划草稿' : '编辑中 · 修改会在应用后生成新版本，正在执行的节点不受影响')}</span>
      <button class="btn sm" data-eb="undo" ${ed.undo.length ? '' : 'disabled'}>${t('撤销')}</button>
      <button class="btn sm" data-eb="redo" ${ed.redo.length ? '' : 'disabled'}>${t('重做')}</button>
      <button class="btn sm" data-eb="add">${ic('plus')}${t('添加节点')}</button>
      <button class="btn sm" data-eb="cancel">${t('放弃')}</button>
      <button class="btn sm primary" data-eb="apply" ${ed.ops.length ? '' : 'disabled'}>${t('应用修改（{n} 处）', { n: ed.ops.length })}</button>`;
  }
  _minimap() {
    const nodes = this.current(); if (!nodes.length) { this.mm.innerHTML = ''; return; }
    const b = this._bounds(), W = 180, H = 110, s = Math.min(W / (b.w + 80), H / (b.h + 80));
    const ox = (W - b.w * s) / 2 - b.x * s, oy = (H - b.h * s) / 2 - b.y * s, r = this.cv.getBoundingClientRect();
    const col = { succeeded: 'var(--ok)', running: 'var(--run)', awaiting_approval: 'var(--gate)', failed: 'var(--err)' };
    const acol = { map: 'var(--accent)', chart: 'var(--accent-2)', doc: 'var(--ok)', file: 'var(--gate)',
      table: 'var(--accent)', metrics: 'var(--accent-2)', list: 'var(--accent-2)', code: 'var(--line-2)', image: 'var(--accent)',
      timeline: 'var(--ok)', calendar: 'var(--gate)', status: 'var(--ok)', chat: 'var(--accent-2)', web: 'var(--gate)' };
    const vx = (-this.vp.x / this.vp.z) * s + ox, vy = (-this.vp.y / this.vp.z) * s + oy;
    this.mm.className = 'cv-float minimap' + (this.mmCol ? ' col' : '');
    this.mm.innerHTML = `<div class="mm-h"><span>${t('小地图')}</span><button data-mm="toggle">${this.mmCol ? '▴' : '▾'}</button></div>
      <svg viewBox="0 0 ${W} ${H}">${nodes.map((n) => { const p = this.pos[n.node_key]; if (!p) return ''; return `<rect x="${p.x * s + ox}" y="${p.y * s + oy}" width="${NW * s}" height="${80 * s}" rx="2" fill="${col[n.status] || 'var(--line-2)'}"/>`; }).join('')}
      ${this._visibleArts().map((c) => { const p = this.pos[artKey(c)]; if (!p) return ''; return `<rect x="${p.x * s + ox}" y="${p.y * s + oy}" width="${AW * s}" height="${this._artH(c) * s}" rx="2" fill="${acol[c.kind] || 'var(--line-2)'}" opacity=".7"/>`; }).join('')}
      <rect x="${vx}" y="${vy}" width="${(r.width / this.vp.z) * s}" height="${(r.height / this.vp.z) * s}" fill="none" stroke="var(--accent)" stroke-width="1.2"/></svg>`;
    this._mm = { s, ox, oy };
  }
  _bounds() {
    const ks = this.current().map((n) => n.node_key); if (!ks.length) return { x: 0, y: 0, w: 1, h: 1 };
    const bx = ks.map((k) => this._box(k)).concat(this._visibleArts().map((c) => this._abox(artKey(c))));
    const x = Math.min(...bx.map((b) => b.x)), y = Math.min(...bx.map((b) => b.y));
    return { x, y, w: Math.max(...bx.map((b) => b.x + b.w)) - x, h: Math.max(...bx.map((b) => b.y + b.h)) - y };
  }

  // ---------- 视口 ----------
  _apply() { this.inner.style.transform = `translate(${this.vp.x}px,${this.vp.y}px) scale(${this.vp.z})`; this.o.onZoom?.(this.vp.z); this._minimap(); }
  zoomAt(z, px, py) {   // 以屏幕点 (px,py) 为中心缩放：保持该点下的画布坐标不变
    z = Math.min(ZMAX, Math.max(ZMIN, z));
    this.vp.x = px - (px - this.vp.x) * (z / this.vp.z); this.vp.y = py - (py - this.vp.y) * (z / this.vp.z); this.vp.z = z;
    this.userMoved = true; this._apply(); this._saveLayout();
  }
  zoomBy(f) { const r = this.cv.getBoundingClientRect(); this.zoomAt(this.vp.z * f, r.width / 2, r.height / 2); }
  zoomReset() { const r = this.cv.getBoundingClientRect(); this.zoomAt(1, r.width / 2, r.height / 2); }
  fit() {   // 把全部节点（含右侧成果泳道）放进可视区域并居中，留 48px 边距
    const r = this.cv.getBoundingClientRect(); if (!r.width) return;
    const b = this._bounds();
    const availH = Math.max(160, r.height - 96);
    const z = Math.min(1, Math.max(ZMIN, Math.min((r.width - 96) / b.w, availH / b.h)));
    this.vp = { z, x: (r.width - b.w * z) / 2 - b.x * z, y: (r.height - b.h * z) / 2 - b.y * z }; this._apply();
  }
  center(k) {
    const b = this._box(k), r = this.cv.getBoundingClientRect();
    this.vp.x = r.width / 2 - (b.x + b.w / 2) * this.vp.z; this.vp.y = r.height / 2 - (b.y + b.h / 2) * this.vp.z; this._apply();
  }
  /** 定位到一个成果节点（模块里点「定位」时用）：居中并选中高亮。 */
  focusArt(key) {
    const c = this.arts.find((x) => artKey(x) === key); if (!c) return;
    const b = this._abox(key), r = this.cv.getBoundingClientRect();
    this.vp.x = r.width / 2 - (b.x + b.w / 2) * this.vp.z; this.vp.y = r.height / 2 - (b.y + b.h / 2) * this.vp.z; this._apply();
    this.selectArt(key);
  }
  panBy(dx, dy) { this.vp.x += dx; this.vp.y += dy; this.userMoved = true; this._apply(); this._saveLayout(); }
  select(k) { this.sel = k; this.selectedKeys = new Set(k ? [k] : this.selArt ? [this.selArt] : []); this._syncSelection(); }
  // Selection is transient view state: never included in saved layouts or revision operations.
  _syncSelection() {
    const visible = new Set();
    this.layer.querySelectorAll('.gnode,.anode').forEach((el) => {
      const key = el.dataset.key || el.dataset.akey; visible.add(key);
      el.classList.toggle('sel', this.selectedKeys.has(key));
      el.setAttribute('aria-selected', String(this.selectedKeys.has(key)));
    });
    this.selectedKeys = new Set([...this.selectedKeys].filter((key) => visible.has(key)));
    if (this.sel && !visible.has(this.sel)) this.sel = null;
    this.selectionStatus.textContent = t('已选 {n} 个节点 · Shift 点击或框选 · Esc 清除', { n: this.selectedKeys.size });
    this.selectionStatus.classList.toggle('hidden', !this.selectedKeys.size);
  }
  _toggleSelection(key) {
    this.selectedKeys.has(key) ? this.selectedKeys.delete(key) : this.selectedKeys.add(key);
    this._syncSelection();
  }
  clearSelection() {
    this.sel = null; this.selectArt(null); this.selectedKeys.clear(); this._syncSelection();
    this.o.onSelectionClear?.();
  }
  _saveLayout() {
    if (this.dead) return;
    clearTimeout(this._lt);
    this._lt = setTimeout(() => this.o.onLayout?.({ positions: this.pos, viewport: this.vp, collapsed: { minimap: this.mmCol },
      art: { hidden: [...this.art.hidden], pinned: [...this.art.pinned], focus: this.art.focus,
             checks: this.art.checks, sorts: this.art.sorts } }), 800);   // v3.0：L2 勾选 / 排序一并持久化（纯视图）
  }
  _world(e) { const r = this.cv.getBoundingClientRect(); return { x: (e.clientX - r.left - this.vp.x) / this.vp.z, y: (e.clientY - r.top - this.vp.y) / this.vp.z }; }

  // ---------- 成果节点交互（v1.7 选中高亮；v1.8 点选后在右侧面板展示，可编辑） ----------
  /** 选中一个成果节点：高亮它及它的产出边与引用边，并通知右侧面板展示这一份成果。 */
  selectArt(key) {
    const changed = this.selArt !== key;
    this.selArt = key;
    if (key) { this.sel = null; this.selectedKeys = new Set([key]); }
    else this.selectedKeys = new Set(this.sel ? [this.sel] : []);
    this._syncSelection();
    this._edges();                                          // 引用边 / 产出边同步高亮
    if (changed) this.o.onArtSelect?.(key ? (this.arts.find((x) => artKey(x) === key) || null) : null, key || null);
  }
  _openArtMenu(btn, key) {
    this._closeArtMenu();
    const c = this.arts.find((x) => artKey(x) === key); if (!c) return;
    this.artMenu = { key };
    this.artM.innerHTML = [
      c.node_key ? `<button data-am="locate">${ic('eye')}${t('定位来源节点')}</button>` : '',
      c.kind === 'doc' ? `<button data-am="edit">${ic('pen')}${t('编辑内容')}</button>` : '',
      (c[c.kind] && (c.kind === 'chart' || c.kind === 'map' || ART_KINDS.includes(c.kind))) ? `<button data-am="zoom">${ic('zin')}${t('放大查看')}</button>` : '',
      `<button data-am="pin">${ic('pin')}${t(this.art.pinned.has(key) ? '取消固定' : '固定在画布')}</button>`,
      `<button data-am="hide">${ic('eyeoff')}${t('从画布隐藏')}</button>`,
      c.kind === 'file' ? `<button data-am="dl">${ic('down')}${t('下载文件')}</button>` : '',
      ART_KINDS.includes(c.kind) ? `<button data-am="csv">${ic('down')}${t('导出数据（CSV）')}</button>` : '',
    ].join('');
    this.artM.classList.remove('hidden');
    const r = this.root.getBoundingClientRect(), br = btn.getBoundingClientRect();
    const W = this.artM.offsetWidth || 180, H = this.artM.offsetHeight || 160;
    this.artM.style.left = Math.round(Math.min(Math.max(8, br.left - r.left), Math.max(8, r.width - W - 10))) + 'px';
    this.artM.style.top = Math.round(Math.min(Math.max(8, br.bottom - r.top + 6), Math.max(8, r.height - H - 10))) + 'px';
  }
  _closeArtMenu() { if (!this.artMenu) return; this.artMenu = null; this.artM.classList.add('hidden'); this.artM.innerHTML = ''; }
  _artAction(a) {
    const key = this.artMenu?.key; this._closeArtMenu();
    if (!key) return;
    const c = this.arts.find((x) => artKey(x) === key); if (!c) return;
    if (a === 'locate' && c.node_key) { this.selectArt(null); this.select(c.node_key); this.o.onSelect?.(c.node_key); this.center(c.node_key); }
    else if (a === 'edit') this.o.onArtEdit?.(c, key);
    else if (a === 'zoom') this.o.onArtZoom?.(c);
    else if (a === 'csv') artExport(c.kind, c[c.kind]);
    else if (a === 'pin') this.toggleArtPinned(key);
    else if (a === 'hide') this.setArtHidden(key, true);
    else if (a === 'dl') { const a2 = document.createElement('a'); a2.href = `/api/workspace/file/download?path=${encodeURIComponent(c.path)}`; a2.click(); }
  }
  /** 隐藏/恢复一个成果节点（隐藏后仍保留在「节点管理」里，可随时恢复）。 */
  setArtHidden(key, on = true) {
    if (on) { this.art.hidden.add(key); this.art.pinned.delete(key); } else this.art.hidden.delete(key);
    if (this.selArt === key) { this.selArt = null; this.o.onArtSelect?.(null, null); }
    this._autoPlace(false);                                // 恢复显示时立即回到泳道（不落在原点）
    this.render(); this._saveLayout(); this.o.onArtState?.();
  }
  toggleArtPinned(key) {
    if (this.art.pinned.has(key)) this.art.pinned.delete(key); else this.art.pinned.add(key);
    this.render(); this._saveLayout(); this.o.onArtState?.();
  }
  setArtFocus(on) { this.art.focus = !!on; this._saveLayout(); }
  showAllArt() { if (!this.art.hidden.size) return; this.art.hidden.clear(); this._autoPlace(false); this.render(); this._saveLayout(); this.o.onArtState?.(); }

  // ---------- 编辑模式（R31.8–R31.10） ----------
  toggleEdit(on = !this.edit) {
    if (on && !this.edit) this.edit = { nodes: JSON.parse(JSON.stringify(this.nodes)), ops: [], undo: [], redo: [], added: new Set(), addedEdges: new Set() };
    else if (!on) { this.edit = null; this.selEdge = null; }
    this.render(); this.o.onEditChange?.(!!this.edit);
  }
  _push(op, mutate) {
    const ed = this.edit;
    ed.undo.push({ nodes: JSON.parse(JSON.stringify(ed.nodes)), ops: ed.ops.length, added: new Set(ed.added), addedEdges: new Set(ed.addedEdges) });
    ed.redo = []; mutate(ed.nodes); ed.ops.push(op); this.render();
  }
  undo() { const ed = this.edit; if (!ed?.undo.length) return; const s = ed.undo.pop();
    ed.redo.push({ nodes: ed.nodes, op: ed.ops.slice(s.ops), added: ed.added, addedEdges: ed.addedEdges });
    Object.assign(ed, { nodes: s.nodes, added: s.added, addedEdges: s.addedEdges }); ed.ops = ed.ops.slice(0, s.ops); this.render(); }
  redo() { const ed = this.edit; if (!ed?.redo.length) return; const r = ed.redo.pop();
    ed.undo.push({ nodes: ed.nodes, ops: ed.ops.length, added: ed.added, addedEdges: ed.addedEdges });
    Object.assign(ed, { nodes: r.nodes, added: r.added, addedEdges: r.addedEdges }); ed.ops.push(...r.op); this.render(); }
  _reach(nodes, from) {      // from 的全部下游
    const ch = {}; nodes.forEach((n) => n.depends_on.forEach((d) => (ch[d] ||= []).push(n.node_key)));
    const seen = new Set(), st = [from]; while (st.length) for (const c of ch[st.pop()] || []) if (!seen.has(c)) { seen.add(c); st.push(c); }
    return seen;
  }
  addEdge(u, v) {
    const nodes = this.edit.nodes, nv = nodes.find((n) => n.node_key === v);
    if (u === v || nv.depends_on.includes(u) || this._reach(nodes, v).has(u)) return false;   // 成环：拒绝
    this._push({ op: 'add_edge', from: u, to: v }, (ns) => ns.find((n) => n.node_key === v).depends_on.push(u));
    this.edit.addedEdges.add(u + '>' + v); this.render(); return true;
  }
  removeEdge(u, v) { this.selEdge = null; this._push({ op: 'remove_edge', from: u, to: v }, (ns) => { const n = ns.find((x) => x.node_key === v); n.depends_on = n.depends_on.filter((d) => d !== u); }); }
  removeNode(k) {
    if (!this.edit || this.edit.nodes.length <= 1 || !this.edit.nodes.some((n) => n.node_key === k)) return;
    this._push({ op: 'remove_node', node_key: k }, (ns) => {
      const v = ns.find((n) => n.node_key === k);
      ns.forEach((n) => { if (n.depends_on.includes(k)) n.depends_on = [...new Set(n.depends_on.filter((d) => d !== k).concat(v.depends_on))]; });
      ns.splice(ns.indexOf(v), 1);
    });
  }
  addNode(title, tags, at) {
    const key = 's' + crypto.randomUUID().replaceAll('-', '');
    this.pos[key] = at || (() => { const b = this._bounds(); return { x: b.x + b.w / 2 - NW / 2, y: b.y + b.h + 40 }; })();
    this._push({ op: 'add_node', node: { node_key: key, title, description: title, depends_on: [], capability_requirements: { tags, tools: [], permission: 'read' }, produces_sections: [title] } },
      (ns) => ns.push({ node_key: key, title, depends_on: [], status: 'pending', agent: {} }));
    this.edit.added.add(key); this.render(); this._saveLayout();
  }

  // ---------- 交互绑定 ----------
  _bind() {
    const cv = this.cv, ptrs = new Map();
    let pan = null, drag = null, link = null, pinch = null, marquee = null;
    const endMarquee = (cancel = false) => {
      if (!marquee) return;
      if (cancel) this.selectedKeys = new Set(marquee.base);
      marquee = null; this.selectionBox.classList.add('hidden'); this._syncSelection();
    };
    const dragNodes = (el, key, e, art = false) => {
      const keys = this.selectedKeys.has(key) ? [...this.selectedKeys] : [key];
      const starts = keys.map((k) => ({ k, p: { ...(this.pos[k] || { x: 0, y: 0 }) },
        el: [...this.layer.querySelectorAll('.gnode,.anode')].find((n) => (n.dataset.key || n.dataset.akey) === k) }));
      drag = { k: key, art, sx: e.clientX, sy: e.clientY, starts, moved: false, el };
      cv.setPointerCapture(e.pointerId);
    };
    cv.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.cv-float') || e.target.closest('.edge-del')) return;
      if (e.target.closest('input,textarea,select,[contenteditable],.im-map')) return;
      cv.focus({ preventScroll: true });
      ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (ptrs.size === 2) { endMarquee(true); const [a, b] = [...ptrs.values()]; pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), z: this.vp.z }; pan = drag = null; return; }
      if (e.button === 1) {                                // 中键：无论落在哪里都平移整个画布（不拖节点、不触发菜单、不改选中）
        e.preventDefault();                                // 阻止浏览器中键自动滚动
        pan = { sx: e.clientX, sy: e.clientY, x: this.vp.x, y: this.vp.y };
        cv.classList.add('panning'); cv.setPointerCapture(e.pointerId); return;
      }
      if (e.button !== 0) return;                          // 只有左键才拖节点 / 连线 / 平移；其它键不参与画布交互（保留系统菜单）
      if (this._imapActiveEl && !e.target.closest('.im-map')) this._deactivateImap();   // v2.0：左键点画布其它位置 → 退出地图滚轮激活态
      if (e.target.closest('[data-imsw]')) return;         // 地图「示意图 / 操控地图」切换按钮：只走 click，不启动节点拖动
      if (e.target.closest(ACTRL_SEL)) return;             // v3.0：节点内本地操作控件（勾选 / 排序 / 复制 / 外链）：只走 click，不拖节点、不改选中
      const port = e.target.closest('[data-port]'), del = e.target.closest('[data-del]'), hit = e.target.closest('[data-edge]'), nodeEl = e.target.closest('.gnode');
      if (del) { this.removeNode(del.dataset.del); return; }
      if (hit) { this.selEdge = hit.dataset.edge; this._edges(); return; }
      const amen = e.target.closest('[data-amen]');
      if (amen) { this._openArtMenu(amen, amen.dataset.amen); return; }
      const aEl = e.target.closest('.anode');
      if (e.shiftKey && !port && !this.space) {
        e.preventDefault(); this.selEdge = null; this._edges();
        if (nodeEl || aEl) { this._toggleSelection(nodeEl?.dataset.key || aEl.dataset.akey); return; }
        marquee = { x: e.clientX, y: e.clientY, base: new Set(this.selectedKeys) };
        cv.setPointerCapture(e.pointerId); return;
      }
      if (aEl && !this.edit && !this.space) { dragNodes(aEl, aEl.dataset.akey, e, true); return; }
      if (port && this.edit) {
        const u = port.dataset.port, reach = this._reach(this.edit.nodes, u);
        // 会成环的目标：u 的上游（即可到达 u 的节点）——等价于从目标出发能到达 u
        this.edit.invalid = new Set(this.edit.nodes.filter((n) => n.node_key === u || this._reach(this.edit.nodes, n.node_key).has(u) || this.edit.nodes.find((x) => x.node_key === n.node_key).depends_on.includes(u)).map((n) => n.node_key));
        link = { u }; void reach; this.render(); cv.setPointerCapture(e.pointerId); return;
      }
      if (nodeEl && !this.space) { dragNodes(nodeEl, nodeEl.dataset.key, e); return; }
      if (this.selEdge) { this.selEdge = null; this._edges(); }
      if (!this.space) this.clearSelection();
      pan = { sx: e.clientX, sy: e.clientY, x: this.vp.x, y: this.vp.y }; cv.classList.add('panning'); cv.setPointerCapture(e.pointerId);
    });
    cv.addEventListener('pointermove', (e) => {
      if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pinch && ptrs.size === 2) { const [a, b] = [...ptrs.values()], r = cv.getBoundingClientRect();
        this.zoomAt(pinch.z * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top); return; }
      if (marquee) {
        const r = cv.getBoundingClientRect(), left = Math.min(marquee.x, e.clientX), top = Math.min(marquee.y, e.clientY),
          right = Math.max(marquee.x, e.clientX), bottom = Math.max(marquee.y, e.clientY);
        Object.assign(this.selectionBox.style, { left: left - r.left + 'px', top: top - r.top + 'px', width: right - left + 'px', height: bottom - top + 'px' });
        this.selectionBox.classList.remove('hidden'); this.selectedKeys = new Set(marquee.base);
        this.layer.querySelectorAll('.gnode,.anode').forEach((el) => {
          const b = el.getBoundingClientRect();
          if (b.right >= left && b.left <= right && b.bottom >= top && b.top <= bottom) this.selectedKeys.add(el.dataset.key || el.dataset.akey);
        });
        this._syncSelection(); return;
      }
      if (pan) { this.vp.x = pan.x + e.clientX - pan.sx; this.vp.y = pan.y + e.clientY - pan.sy; this.userMoved = true; this._apply(); }
      else if (drag) {
        const dx = (e.clientX - drag.sx) / this.vp.z, dy = (e.clientY - drag.sy) / this.vp.z;
        if (!drag.moved && Math.hypot(dx, dy) * this.vp.z < 4) return;      // 拖动阈值 4px，区分点击
        if (!drag.moved && !this.selectedKeys.has(drag.k)) {
          this.sel = null; this.selectArt(null); this.selectedKeys = new Set([drag.k]); this._syncSelection();
        }
        drag.moved = true;
        for (const s of drag.starts) {
          this.pos[s.k] = { x: s.p.x + Math.round(dx / 8) * 8, y: s.p.y + Math.round(dy / 8) * 8 };
          if (s.el) { s.el.classList.add('dragging'); s.el.style.left = this.pos[s.k].x + 'px'; s.el.style.top = this.pos[s.k].y + 'px'; }
        }
        this._edges();
      } else if (link) {
        const a = this._box(link.u), w = this._world(e), el = document.elementFromPoint(e.clientX, e.clientY)?.closest('.gnode');
        const tmp = this.svg.querySelector('#tmpEdge'); tmp.classList.remove('hidden');
        tmp.setAttribute('d', `M${a.x + a.w},${a.y + a.h / 2} L${w.x},${w.y}`);
        const tgt = el && !this.edit.invalid.has(el.dataset.key) ? el.dataset.key : null;
        if (tgt !== this.edit.target) { this.edit.target = tgt; this.layer.querySelectorAll('.gnode').forEach((n) => n.classList.toggle('target', n.dataset.key === tgt)); }
      }
    });
    const up = (e) => {
      endMarquee(e.type === 'pointercancel');
      ptrs.delete(e.pointerId); if (ptrs.size < 2) pinch = null;
      if (pan) { pan = null; cv.classList.remove('panning'); this._saveLayout(); }
      if (drag) { const d = drag; drag = null; d.starts.forEach((s) => s.el?.classList.remove('dragging'));
        if (d.moved) { this._minimap(); this._saveLayout(); }
        else if (d.art) { if (!d.skipSel) this.selectArt(this.selArt === d.k ? null : d.k); }   // 单击成果节点：选中高亮（再点取消）；点「下载」不改变选中
        else { this.selectArt(null); this.select(d.k); this.o.onSelect?.(d.k); } }
      if (link) { const tgt = this.edit.target; this.edit.invalid = null; this.edit.target = null; const u = link.u; link = null;
        if (tgt) this.addEdge(u, tgt); else this.render(); }
    };
    cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
    cv.addEventListener('mousedown', (e) => { if (e.button === 1) e.preventDefault(); });   // 中键拖画布：禁止浏览器「中键自动滚动」
    cv.addEventListener('wheel', (e) => {
      const sc = e.target.closest?.('.an-scroll');         // 文档节点的正文滚动优先；到边界后再交给画布平移
      if (sc && sc.scrollHeight > sc.clientHeight + 2) {
        const atTop = sc.scrollTop <= 0, atBottom = sc.scrollTop >= sc.scrollHeight - sc.clientHeight - 1;
        if (!(e.deltaY < 0 && atTop) && !(e.deltaY > 0 && atBottom)) return;
      }
      e.preventDefault();
      const r = cv.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) this.zoomAt(this.vp.z * Math.exp(-e.deltaY * 0.0022), e.clientX - r.left, e.clientY - r.top);
      else this.panBy(e.shiftKey ? -e.deltaY : -e.deltaX, e.shiftKey ? 0 : -e.deltaY);
    }, { passive: false });
    cv.addEventListener('dblclick', (e) => {
      if (e.shiftKey || e.target.closest('.cv-float,button,input,textarea,select,[contenteditable]')) return;
      const aEl = e.target.closest(AZOOM_SEL);              // 双击可放大的节点：放大查看（「⋯」菜单里也有）
      if (aEl && !this.edit) { const c = this.arts.find((x) => artKey(x) === aEl.dataset.akey); if (c) this.o.onArtZoom?.(c); return; }
      if (this.edit && !e.target.closest('.gnode')) this.o.onAddRequest?.(this._world(e));
    });
    this.root.addEventListener('click', (e) => {
      const eb = e.target.closest('[data-eb]'), mm = e.target.closest('.minimap'), am = e.target.closest('[data-am]');
      const isw = e.target.closest('[data-imsw]');
      if (isw) { const k = isw.dataset.imsw; this._mapMode.set(k, this._mapMode.get(k) === 'svg' ? 'live' : 'svg'); this.render(); return; }   // v2.0：地图节点「可操控 / 离线示意图」切换
      if (am) { this._artAction(am.dataset.am); return; }
      const avc = e.target.closest('[data-avc]');           // v3.0 L2：清单勾选（纯视图，不改成果数据、不产生事件 INV-22）
      if (avc) {
        const key = avc.closest('.anode')?.dataset.akey;
        if (key) this.toggleArtCheck(key, +avc.dataset.avc);
        return;
      }
      const avs = e.target.closest('[data-avs]');           // v3.0 L2：表格排序（同样纯视图）
      if (avs) {
        const key = avs.closest('.anode')?.dataset.akey;
        if (key) this.toggleArtSort(key, +avs.dataset.avs);
        return;
      }
      const avcp = e.target.closest('[data-avcopy]');       // v3.0 L2：复制代码（只读剪贴板，不改变任何状态）
      if (avcp) {
        const pre = avcp.closest('.av')?.querySelector('.av-code');
        if (pre) navigator.clipboard?.writeText(pre.textContent).then(() => { avcp.innerHTML = `${ic('check')}${t('已复制')}`; }).catch(() => {});
        return;
      }
      if (e.target.closest('[data-edgedel]')) { const [u, v] = this.selEdge.split('>'); this.removeEdge(u, v); return; }
      if (eb) { const a = eb.dataset.eb;
        if (a === 'undo') this.undo(); else if (a === 'redo') this.redo(); else if (a === 'add') this.o.onAddRequest?.(null);
        else if (a === 'cancel') this.o.onEditCancel?.(); else if (a === 'apply') this.o.onEditApply?.(this.edit.ops); return; }
      if (e.target.closest('[data-mm]')) { this.mmCol = !this.mmCol; this._minimap(); this._saveLayout(); return; }
      if (mm && this._mm && e.target.closest('svg')) {     // 点击小地图定位
        const r = mm.querySelector('svg').getBoundingClientRect(), c = this.cv.getBoundingClientRect();
        const wx = (e.clientX - r.left - this._mm.ox) / this._mm.s, wy = (e.clientY - r.top - this._mm.oy) / this._mm.s;
        this.vp.x = c.width / 2 - wx * this.vp.z; this.vp.y = c.height / 2 - wy * this.vp.z; this._apply(); this._saveLayout();
      }
    });
    const sig = { signal: this._ac.signal };
    document.addEventListener('keydown', (e) => {
      // 只在画布本身可见时响应（快速回答页等替换了画布的场景不应吞掉空格、方向键）；成果详情 / 菜单 / 管理模块内的按键不劫持
      if (!this.cv.isConnected || !this.cv.offsetParent || e.target.closest('input,textarea,select,[contenteditable],.art-menu,.mgmt') || document.querySelector('.overlay.on')) return;
      const k = e.key;
      if (k === ' ' && e.target.closest('button,a')) return;           // 焦点在按钮上时空格用于触发按钮
      if (k === ' ' && !this.space) { this.space = true; cv.classList.add('space'); e.preventDefault(); return; }
      if (this.edit && (e.ctrlKey || e.metaKey) && k.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? this.redo() : this.undo(); return; }
      if (!this.edit && (e.ctrlKey || e.metaKey) && !e.shiftKey && k.toLowerCase() === 'z' && this.undoTidy()) { e.preventDefault(); return; }
      if (this.edit && (k === 'Delete' || k === 'Backspace') && this.selEdge) { const [u, v] = this.selEdge.split('>'); this.removeEdge(u, v); return; }
      if (k === 'Escape') {                                            // Esc 依次：成果菜单 → 地图操控 → 取消成果选中 → 退出编辑
        if (this.artMenu) { this._closeArtMenu(); return; }
        if (this._imapActiveEl) { this._deactivateImap(); return; }
        if (marquee) { endMarquee(true); e.preventDefault(); return; }
        if (this.selectedKeys.size) { this.clearSelection(); e.preventDefault(); return; }
        if (this.selArt) { this.selectArt(null); return; }
        if (this.edit) { this.o.onEditCancel?.(); return; }
      }
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const map = { '+': () => this.zoomBy(1.2), '=': () => this.zoomBy(1.2), '-': () => this.zoomBy(1 / 1.2), '0': () => this.zoomReset(), '1': () => this.fit(),
        f: () => this.sel && this.center(this.sel), e: () => this.o.onEditToggle?.(),
        ArrowLeft: () => this.panBy(64, 0), ArrowRight: () => this.panBy(-64, 0), ArrowUp: () => this.panBy(0, 64), ArrowDown: () => this.panBy(0, -64) };
      if (map[k]) { e.preventDefault(); map[k](); }
    }, sig);
    document.addEventListener('keyup', (e) => { if (e.key === ' ') { this.space = false; cv.classList.remove('space'); } }, sig);
    document.addEventListener('click', (e) => {            // 点击别处关闭成果菜单
      if (!this.artMenu) return;
      if (e.target.closest('.art-menu') || e.target.closest('[data-amen]')) return;
      this._closeArtMenu();
    }, sig);
    this._ro = new ResizeObserver(() => this._minimap()); this._ro.observe(cv);
  }
}
