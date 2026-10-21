// 成果卡片（v1.5）：把 artifact.cards 渲染成「地图 / 图 / 文 / 文件」四类卡片。
// v1.5.2 起：点击卡片在卡片所在分区「就地展开」详情（图放大 / 文档全文 / 文件预览）——不打开任何独立弹窗。
// v1.7 起：画布成果节点改为内容直显（图内联 / 正文内联 / 文件下载），不再用本文件的详情渲染；本文件只服务成果页。
// v1.9：新增「地图」卡片（旅行/路线类任务的攻略图，renderMap full 640×420，就地展开放大）。
import { esc, ic, md, modal, GET, POST, toast, fail } from './core.js';
import { t } from './i18n.js';
import { renderChart } from './chart.js';
import { renderMap } from './mapview.js';
import { mountLeafletMap, leafletAvailable } from './imap.js';

const TEXT_EXT = new Set(['md', 'markdown', 'txt', 'csv', 'json', 'html', 'htm', 'svg', 'py', 'js', 'css', 'log', 'yaml', 'yml']);
const IMG_EXT = new Set(['svg', 'png', 'jpg', 'jpeg', 'gif', 'webp']);

export function fmtBytes(n) {
  n = Number(n) || 0;
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}

const dl = (path) => `/api/workspace/file/download?path=${encodeURIComponent(path)}`;

/** 无 cards 的旧成果兜底：从 sections 构造文卡。 */
export function fallbackCards(sections = []) {
  return sections.map((s) => ({ kind: 'doc', title: s.title, excerpt: '', section_key: s.section_key, node_key: s.node_key, chars: (s.content || '').length, citations: (s.citations || []).length }));
}

function mapCard(c, i, ctx) {
  const n = (c.map?.markers || []).length;
  return `<div class="dv-card map" data-dvk="map:${i}" role="button" tabindex="0">
    <div class="dv-h">${ic('pin')}<span class="dv-t">${esc(c.title || t('地图'))}</span><span class="dv-act">${ic('zin')}${t('放大')}</span></div>
    <div class="dv-map">${renderMap(c.map)}</div>
    <div class="dv-meta">${esc(t('{n} 个地点', { n }))}${c.node_key ? ' · ' + t('来自节点：') + esc(ctx.nodeTitle?.(c.node_key) || c.node_key) : ''}</div></div>`;
}

function chartCard(c, i, ctx) {
  return `<div class="dv-card chart" data-dvk="chart:${i}" role="button" tabindex="0">
    <div class="dv-h"><span class="dv-t">${esc(c.title || t('图表'))}</span><span class="dv-act">${ic('zin')}${t('放大')}</span></div>
    <div class="dv-chart">${renderChart(c.chart)}</div>
    ${c.node_key ? `<div class="dv-meta">${t('来自节点：')}${esc(ctx.nodeTitle?.(c.node_key) || c.node_key)}</div>` : ''}</div>`;
}

function docCard(c, i, ctx) {
  const meta = [`${t('共 {n} 字', { n: c.chars || 0 })}`, c.citations ? t('{n} 条引用', { n: c.citations }) : ''].filter(Boolean).join(' · ');
  return `<div class="dv-card doc" data-dvk="doc:${i}" role="button" tabindex="0">
    <div class="dv-h">${ic('doc')}<span class="dv-t">${esc(c.title)}</span></div>
    <div class="dv-ex">${esc(c.excerpt || '')}</div>
    <div class="dv-meta">${esc(meta)}${c.node_key ? ' · ' + t('来自节点：') + esc(ctx.nodeTitle?.(c.node_key) || c.node_key) : ''}<span class="dv-act">${t('点击阅读全文')}</span></div></div>`;
}

function fileCard(c, i, ctx) {
  const ext = (c.ext || '').toUpperCase();
  return `<div class="dv-card file" data-dvk="file:${i}" role="button" tabindex="0" title="${esc(c.path)}">
    <span class="dv-fic">${ic(IMG_EXT.has(c.ext) ? 'image' : 'file')}</span>
    <div class="dv-fb"><div class="dv-t">${esc(c.title)}</div>
      <div class="dv-meta">${[ext, fmtBytes(c.bytes)].filter(Boolean).join(' · ')}<span class="dv-act">${t('点击预览')}</span></div></div>
    <a class="btn sm" href="${dl(c.path)}" data-dvd title="${esc(t('下载'))}">${ic('down')}${t('下载')}</a></div>`;
}

const GROUPS = [
  { kind: 'map', icon: 'pin', label: () => t('地图') },
  { kind: 'chart', icon: 'chart', label: () => t('图表') },
  { kind: 'doc', icon: 'doc', label: () => t('文档') },
  { kind: 'file', icon: 'save', label: () => t('文件') },
];

/** 渲染交付物分区（含分区标题）；cards 为空时返回空串。 */
export function deliverablesHtml(cards, ctx = {}) {
  if (!cards?.length) return '';
  const secs = GROUPS.map((g) => {
    const items = cards.filter((c) => c.kind === g.kind);
    if (!items.length) return '';
    const body = items.map((c, i) => (g.kind === 'map' ? mapCard(c, i, ctx) : g.kind === 'chart' ? chartCard(c, i, ctx) : g.kind === 'doc' ? docCard(c, i, ctx) : fileCard(c, i, ctx))).join('');
    return `<section class="dv-sec"><h4 class="dv-gh">${ic(g.icon)}${g.label()}<span class="pill">${items.length}</span></h4>
      <div class="dv-grid ${g.kind}">${body}</div></section>`;
  }).join('');
  return `<div class="dv">${secs}</div>`;
}

/** 给已渲染的交付物 DOM 绑定交互（就地展开 / 下载）。 */
export function bindDeliverables(root, cards, ctx = {}) {
  if (!root) return;
  root.querySelectorAll('[data-dvk]').forEach((el) => {
    el.onclick = (e) => {
      if (e.target.closest('[data-dvd]')) return;              // 下载按钮走 <a> 默认行为
      const [kind, i] = el.dataset.dvk.split(':');
      const c = cards.filter((x) => x.kind === kind)[+i];
      if (!c) return;
      openDetail(root, el, kind, c, ctx);
    };
  });
  root.onclick = (e) => {                                      // 详情区关闭按钮（就地收起）
    const x = e.target.closest('[data-dvc]');
    if (!x) return;
    const d = x.closest('.dv-detail'); if (!d) return;
    d.hidden = true; d.dataset.for = '';
    d.closest('.dv-sec')?.querySelectorAll('.dv-card.on').forEach((y) => y.classList.remove('on'));
  };
}

/** 渲染到 host 并绑定交互（就地展开 / 下载）。 */
export function mountDeliverables(host, cards, ctx = {}) {
  host.innerHTML = deliverablesHtml(cards, ctx);
  bindDeliverables(host, cards, ctx);
  return cards.length;
}

// ---------- 就地展开的详情 ----------
function detailHead(c, extra = '') {
  return `<div class="dv-detail-h"><span class="dv-detail-t">${esc(c.title || '')}</span><span class="grow"></span>${extra}
    <button class="icon-btn" data-dvc title="${esc(t('收起'))}">${ic('x')}</button></div>`;
}

// ---------- 详情渲染（成果页就地展开 / 画布成果节点详情 共用） ----------
/** 成果详情 HTML：地图放大 / 图放大 / 文档全文（可重写本节）/ 文件预览 + 下载。 */
export function artDetailHtml(kind, c, ctx = {}) {
  return kind === 'map' ? mapDetail(c) : kind === 'chart' ? chartDetail(c) : kind === 'doc' ? docDetail(c, ctx) : fileDetail(c);
}
/** 给详情 DOM 绑定交互：重写本节 / 文件内容水合 / 地图挂载（可操控）/ 关闭按钮（ctx.onClose）。 */
export function bindArtDetail(el, kind, c, ctx = {}) {
  if (kind === 'doc') {
    const rw = el.querySelector('[data-dvrw]');
    if (rw) rw.onclick = () => {
      if (ctx.isReplay?.()) return toast(t('回放为只读模式，退出后可重写'));
      rewriteSection(ctx.taskId, c.section_key);
    };
  }
  if (kind === 'file') hydrateFile(el, c);
  if (kind === 'map') hydrateMap(el, c);
  const x = el.querySelector('[data-dvc]');
  if (x) x.onclick = (e) => { e.stopPropagation(); ctx.onClose?.(); };
}

/** v2.0：成果页详情里的地图挂为可操控交互地图（Leaflet 未就绪则保持离线 SVG）。 */
function hydrateMap(el, c) {
  const box = el.querySelector('.im-map');
  if (box && !box._imap) mountLeafletMap(box, c.map, { key: 'dv:' + (c.node_key || c.title || '') });
}

function openDetail(root, cardEl, kind, c, ctx) {
  const sec = cardEl.closest('.dv-sec'); if (!sec) return;
  const key = cardEl.dataset.dvk;
  let det = sec.querySelector(':scope > .dv-detail');
  const sameOpen = det && !det.hidden && det.dataset.for === key;
  // 同时只保留一处展开：先收起其他分区/本分区已打开的详情
  root.querySelectorAll('.dv-detail').forEach((d) => { if (d !== det) { d.hidden = true; d.dataset.for = ''; } });
  sec.querySelectorAll('.dv-card.on').forEach((x) => { if (x !== cardEl) x.classList.remove('on'); });
  if (sameOpen) { det.hidden = true; det.dataset.for = ''; cardEl.classList.remove('on'); return; }
  if (!det) { det = document.createElement('div'); det.className = 'dv-detail'; sec.appendChild(det); }
  det.hidden = false; det.dataset.for = key; cardEl.classList.add('on');
  det.innerHTML = artDetailHtml(kind, c, ctx);
  bindArtDetail(det, kind, c, ctx);
  det.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function mapDetail(c) {
  const live = leafletAvailable();
  return `${detailHead(c)}<div class="dv-zoom dv-mapfull">${live ? '<div class="im-map im-big"></div>' : renderMap(c.map)}</div>`;
}

function chartDetail(c) {
  return `${detailHead(c)}<div class="dv-zoom">${renderChart(c.chart)}</div>`;
}

function docDetail(c, ctx) {
  const sec = (ctx.sections || []).find((s) => s.section_key === c.section_key);
  if (!sec) return `${detailHead(c)}<p class="small muted">${t('正文暂不可用')}</p>`;
  const cites = (sec.citations || []).length
    ? `<div class="small muted dv-cites">${sec.citations.map((x, i) => `<div><span class="cite">${i + 1}</span>${esc(x.doc_title)}</div>`).join('')}</div>` : '';
  const foot = [`${t('共 {n} 字', { n: (sec.content || '').length })}`, sec.model || '', c.node_key && ctx.nodeTitle ? t('来自节点：') + ctx.nodeTitle(c.node_key) : ''].filter(Boolean).join(' · ');
  const rw = ctx.taskId ? `<button class="btn sm" data-dvrw>${t('重写本节')}</button>` : '';
  return `${detailHead(c, rw)}<div class="dv-read">${md(sec.content, sec.citations)}</div>${cites}<div class="small muted" style="margin-top:10px">${esc(foot)}</div>`;
}

function fileDetail(c) {
  const extra = `<a class="btn sm" href="${dl(c.path)}">${ic('down')}${t('下载')}</a>`;
  const body = IMG_EXT.has(c.ext) ? `<img class="dv-img" src="${dl(c.path)}" alt="${esc(c.title)}">`
    : TEXT_EXT.has(c.ext) ? `<div class="code dv-prev" data-dvfc>${t('正在读取…')}</div>`
      : `<p class="small muted">${t('该格式不支持预览，请下载后查看。')}</p>`;
  return `${detailHead(c, extra)}${body}`;
}

async function hydrateFile(det, c) {
  const host = det.querySelector('[data-dvfc]'); if (!host) return;
  try {
    const f = await GET(`/api/workspace/file?path=${encodeURIComponent(c.path)}`);
    if (!host.isConnected) return;
    host.innerHTML = esc(f.content) + (f.truncated ? `\n\n…（${t('内容较长，只显示前一部分。')}）` : '');
  } catch (e) { if (host.isConnected) { host.textContent = ''; fail(e); } }
}

export async function rewriteSection(taskId, sectionKey) {
  const v = await modal(t('重写这一节'), `<textarea id="ri" placeholder="${esc(t('例如：改成表格形式，并加上每段的步行距离'))}"></textarea><p class="small muted">${t('只会重新生成这一节对应的节点，其他章节逐字保持不变。')}</p>`,
    [{ label: t('取消'), value: null }, { label: t('重写本节'), cls: 'primary', onClick: (ov) => ov.querySelector('#ri').value.trim() || false }]);
  if (v) { try { await POST(`/api/tasks/${taskId}/sections/${sectionKey}/rewrite`, { instructions: v }); toast(t('正在重写，完成后成果会更新')); location.hash = `#/task/${taskId}`; } catch (e) { fail(e); } }
}
