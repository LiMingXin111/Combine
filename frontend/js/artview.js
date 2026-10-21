// 功能性成果节点渲染器（v3.0 M3/M4，prd/08 §6）：表格 / 指标 / 清单 / 代码 / 图片 / 时间线 / 日历 / 状态 / 会话 / 网页。
// 与 chart.js / mapview.js 同规约：纯函数返回 HTML 字符串、不依赖框架、不二次防御（数据已由服务端 clean_<kind> 校验）。
// 「功能性」三级定义（prd/08 §6.3）：
//   L1 直显——内容长在节点里（compact 模式，节点内 300px 宽）；
//   L2 本地操作——勾选 / 排序等纯视图操作（不产生事件、不改计划，INV-22）：
//      状态由调用方（canvas）持久化到布局的 art.checks / art.sorts，本模块只按传入状态渲染；
//   L3 回写——写回外部系统（本期不开放）。
import { esc, ic } from './core.js';
import { t } from './i18n.js';

/** 十类功能性节点（顺序与后端 CARD_ORDER 的数据类一致；map/chart/doc/file 由 canvas/deliverables 各自渲染）。 */
export const ART_KINDS = ['table', 'metrics', 'list', 'code', 'image', 'timeline', 'calendar', 'status', 'chat', 'web'];
export const ART_ICON = { table: 'table', metrics: 'gauge', list: 'list', code: 'code', image: 'image',
  timeline: 'clock', calendar: 'cal', status: 'check', chat: 'msg', web: 'globe' };
export const ART_LBL = { table: '表格', metrics: '指标', list: '清单', code: '代码', image: '图片',
  timeline: '时间线', calendar: '日历', status: '状态', chat: '会话', web: '网页' };
/** compact 下内容限高内滚（滚轮优先滚正文，见 canvas 的 wheel 处理）。指标卡片与图片本身很短，不套滚动容器。 */
export const ART_SCROLL = new Set(['table', 'list', 'code', 'timeline', 'calendar', 'status', 'chat', 'web']);

const DIR_ARROW = { up: '▲', down: '▼', flat: '—' };

function cell(v) {
  if (v === null || v === undefined) return '<span class="muted">—</span>';
  if (v === true) return '✓';
  if (v === false) return '✗';
  return esc(String(v));
}

/** 节点脚注的补充说明（比 LABEL 更有信息量的一句话）。 */
export function artSub(kind, d) {
  if (!d) return '';
  if (kind === 'table') return t('{c} 列 · {n} 行', { c: (d.columns || []).length, n: d.total_rows || (d.rows || []).length });
  if (kind === 'metrics') return t('{n} 项指标', { n: (d.items || []).length });
  if (kind === 'list') return d.mode === 'kanban' ? t('{n} 列', { n: (d.columns || []).length }) : t('{n} 条', { n: (d.items || []).length });
  if (kind === 'code') return [String(d.language || '').toUpperCase(), t('{n} 行', { n: String(d.code || '').split('\n').length })].filter(Boolean).join(' · ');
  if (kind === 'image') return (String(d.path || '').split('.').pop() || '').toUpperCase();
  if (kind === 'timeline') return t('{n} 个节点', { n: (d.items || []).length });
  if (kind === 'calendar') return t('{n} 个日程', { n: (d.events || []).length });
  if (kind === 'status') { const bad = (d.items || []).filter((x) => x.state !== 'ok').length; return bad ? t('{n} 项需关注', { n: bad }) : t('全部正常'); }
  if (kind === 'chat') return t('{n} 条消息', { n: (d.messages || []).length });
  if (kind === 'web') { try { return new URL(d.url).host; } catch { return ''; } }
  return '';
}

// ---------- 表格 ----------
function tableRows(d, sort) {
  const rows = (d.rows || []).map((r, i) => [r, i]);
  if (!sort) return rows.map((x) => x[0]);
  const [ci, dir] = sort;
  const mul = dir === 'desc' ? -1 : 1;
  return rows.slice().sort((a, b) => {
    const x = a[0][ci], y = b[0][ci];
    if (x === null || x === undefined) return 1;              // 空值恒排最后
    if (y === null || y === undefined) return -1;
    if (typeof x === 'number' && typeof y === 'number') return (x - y) * mul;
    return String(x).localeCompare(String(y)) * mul;
  }).map((x) => x[0]);
}

function tableHtml(d, o) {
  const full = !o.compact;
  // 排序来源优先级：弹窗的本地排序（临时视图）→ 画布 / 右栏共用的持久状态 sorts[key]。
  // 注意：不能只在 compact 分支读 sorts——右栏是 full 形态且共用持久状态，漏读会导致
  // 「点表头后状态已变、界面毫无反应」（真实缺陷，v3.0 冒烟首轮暴露）。
  const sort = (o.localSort && o._sort) || ((o.sorts || {})[o.key] || null);
  const rows = tableRows(d, sort);
  const head = `<div class="av-h">${d.title ? `<b class="av-tt">${esc(d.title)}</b>` : ''}<span class="grow"></span>
    ${full ? `<input class="av-q" data-avq placeholder="${esc(t('搜索…'))}"><button class="btn sm" data-avcsv>${ic('down')}${t('导出 CSV')}</button>` : `<span class="av-n">${esc(t('{n} 行', { n: d.total_rows || rows.length }))}</span>`}</div>`;
  const cols = (d.columns || []).map((c, i) => `<th class="${c.align === 'right' ? 'r' : ''}${sort && sort[0] === i ? ' on' : ''}" data-avs="${i}" title="${esc(t('点击排序'))}">${esc(c.label)}${sort && sort[0] === i ? `<span class="av-so">${sort[1] === 'desc' ? '▾' : '▴'}</span>` : ''}</th>`).join('');
  const body = rows.map((r) => `<tr>${(d.columns || []).map((c, i) => `<td class="${c.align === 'right' ? 'r' : ''}">${cell(r[i])}</td>`).join('')}</tr>`).join('');
  return `${head}<div class="av-tw"><table class="av-tbl"><thead><tr>${cols}</tr></thead><tbody>${body}</tbody></table></div>
    ${d.note ? `<div class="av-note">${esc(d.note)}</div>` : ''}`;
}

// ---------- 指标 ----------
function metricsHtml(d, o) {
  const items = o.compact ? (d.items || []).slice(0, 4) : (d.items || []);
  return `<div class="av-mt">${items.map((x) => `<div class="av-mc st-${esc(x.status || 'good')}">
      <div class="av-ml">${esc(x.label)}</div>
      <div class="av-mv">${esc(x.value)}${x.delta ? `<span class="av-md d-${esc(x.dir || 'flat')}">${DIR_ARROW[x.dir] || ''}${esc(x.delta)}</span>` : ''}</div>
    </div>`).join('')}</div>${d.note ? `<div class="av-note">${esc(d.note)}</div>` : ''}`;
}

// ---------- 清单（checklist / kanban） ----------
function listHtml(d, o) {
  const key = o.key, chk = new Set(((o.checks || {})[key]) || []);
  if (d.mode === 'kanban') {
    const cols = (d.columns || []);
    const items = o.compact ? (d.items || []).slice(0, 12) : (d.items || []);
    return `<div class="av-h">${d.title ? `<b class="av-tt">${esc(d.title)}</b>` : ''}<span class="grow"></span><span class="av-n">${esc(t('{n} 列', { n: cols.length }))}</span></div>
      <div class="av-kb">${cols.map((c) => `<div class="av-kc"><div class="av-kh">${esc(c)}<span class="pill">${items.filter((x) => x.column === c).length}</span></div>
        ${items.filter((x) => x.column === c).map((x) => `<div class="av-ki">${esc(x.text)}${x.meta ? `<span class="av-me">${esc(x.meta)}</span>` : ''}</div>`).join('')}</div>`).join('')}</div>`;
  }
  const items = o.compact ? (d.items || []).slice(0, 12) : (d.items || []);
  const on = (it, i) => (chk.has(i) ? !it.done : it.done);
  const n = (d.items || []).filter((it, i) => (chk.has(i) ? !it.done : it.done)).length;
  return `<div class="av-h">${d.title ? `<b class="av-tt">${esc(d.title)}</b>` : ''}<span class="grow"></span><span class="av-n">${esc(t('{a}/{b} 已完成', { a: n, b: (d.items || []).length }))}</span></div>
    <ul class="av-ls">${items.map((it, i) => `<li class="av-li${on(it, i) ? ' on' : ''}">
      <button class="av-ck" data-avc="${i}" role="checkbox" aria-checked="${on(it, i)}" title="${esc(t('勾选（本地视图，不影响成果数据）'))}">${ic('check')}</button>
      <span class="av-tx">${esc(it.text)}</span>${it.meta ? `<span class="av-me">${esc(it.meta)}</span>` : ''}</li>`).join('')}</ul>`;
}

// ---------- 代码 / 差异 ----------
function codeHtml(d, o) {
  const lines = String(d.code || '').split('\n');
  const shown = o.compact ? lines.slice(0, 18) : lines;
  const more = lines.length > shown.length;
  return `<div class="av-h"><span class="av-lang">${esc(String(d.language || 'plain').toUpperCase())}</span>${d.path ? `<span class="av-path" title="${esc(d.path)}">${esc(d.path)}</span>` : ''}<span class="grow"></span>
      <button class="btn sm" data-avcopy title="${esc(t('复制'))}">${ic('doc')}</button></div>
    <pre class="av-code">${shown.map((l) => `<span class="av-ln${d.mode === 'diff' && l[0] === '+' ? ' add' : d.mode === 'diff' && l[0] === '-' ? ' del' : ''}">${esc(l) || ' '}</span>`).join('\n')}</pre>
    ${more ? `<div class="av-note">${esc(t('已截断，双击节点放大查看完整内容'))}</div>` : ''}${d.note ? `<div class="av-note">${esc(d.note)}</div>` : ''}`;
}

// ---------- 图片 ----------
function imageHtml(d) {
  const src = `/api/workspace/file/download?path=${encodeURIComponent(d.path || '')}&inline=1`;
  return `<div class="av-im"><img src="${src}" alt="${esc(d.title || '')}" loading="lazy" draggable="false"></div>
    ${d.caption ? `<div class="av-cap">${esc(d.caption)}</div>` : ''}
    ${d.source_url ? `<div class="av-note"><a href="${esc(d.source_url)}" target="_blank" rel="noopener">${t('来源')} ↗</a></div>` : ''}`;
}

// ---------- 时间线 ----------
function timelineHtml(d, o) {
  const items = o.compact ? (d.items || []).slice(0, 10) : (d.items || []);
  return `<ol class="av-tl">${items.map((x) => `<li><span class="av-tm">${esc(x.time || '')}</span><span class="av-dot"></span>
      <div class="av-tlb"><div class="av-tx">${esc(x.text)}</div>${x.note ? `<div class="av-me">${esc(x.note)}</div>` : ''}</div></li>`).join('')}</ol>`;
}

// ---------- 日历（7 天视图） ----------
function calendarHtml(d, o) {
  const by = {}; (d.events || []).forEach((e) => (by[e.day] ||= []).push(e));
  const days = [1, 2, 3, 4, 5, 6, 7].filter((x) => o.compact ? (by[x] || []).length : true);
  const ev = (e) => `<div class="av-ce"><b>${esc(e.start || '')}${e.end ? '–' + esc(e.end) : ''}</b><span class="av-ct">${esc(e.title)}</span>${e.note ? `<span class="av-me">${esc(e.note)}</span>` : ''}</div>`;
  if (o.compact) {                                            // 节点内：按天纵向分组（320px 宽放 7 列不可读）
    return `<div class="av-h">${d.week_start ? `<span class="av-n">${esc(d.week_start)}</span>` : ''}<span class="grow"></span><span class="av-n">${esc(t('{n} 个日程', { n: (d.events || []).length }))}</span></div>
      <div class="av-cl">${days.map((x) => `<div class="av-cd"><div class="av-cdh">${esc(t('第 {n} 天', { n: x }))}</div>${(by[x] || []).map(ev).join('')}</div>`).join('')}</div>`;
  }
  return `<div class="av-h">${d.week_start ? `<span class="av-n">${esc(d.week_start)}</span>` : ''}<span class="grow"></span><span class="av-n">${esc(t('{n} 个日程', { n: (d.events || []).length }))}</span></div>
    <div class="av-cg">${days.map((x) => `<div class="av-cd"><div class="av-cdh">${esc(t('第 {n} 天', { n: x }))}</div>${(by[x] || []).map(ev).join('')}</div>`).join('')}</div>`;
}

// ---------- 状态检查 ----------
function statusHtml(d, o) {
  const items = o.compact ? (d.items || []).slice(0, 10) : (d.items || []);
  return `<ul class="av-st">${items.map((x) => `<li class="s-${esc(x.state)}"><span class="av-sd"></span>
    <div class="av-sb"><div class="av-tx">${esc(x.name)}</div>${x.detail ? `<div class="av-me">${esc(x.detail)}</div>` : ''}</div></li>`).join('')}</ul>`;
}

// ---------- 会话线程 ----------
function chatHtml(d, o) {
  const msgs = o.compact ? (d.messages || []).slice(0, 10) : (d.messages || []);
  return `<div class="av-ch">${msgs.map((m) => `<div class="av-cm r-${esc(m.role)}">
    <div class="av-cw">${esc(m.who)}${m.time ? ` · ${esc(m.time)}` : ''}</div><div class="av-cx">${esc(m.text)}</div></div>`).join('')}</div>`;
}

// ---------- 网页快照 ----------
function webHtml(d, o) {
  let host = ''; try { host = new URL(d.url).host; } catch { host = ''; }
  return `<div class="av-wb"><div class="av-wt">${esc(d.title || host || d.url)}</div>
    <a class="av-wu" href="${esc(d.url)}" target="_blank" rel="noopener">${esc(host || d.url)} ↗</a>
    ${d.excerpt ? `<div class="av-wx">${esc(d.excerpt)}</div>` : ''}
    ${!o.compact && d.text ? `<pre class="av-wfull">${esc(d.text)}</pre>` : ''}</div>`;
}

/** 渲染一类功能性节点的内容（o：{compact, key, checks, sorts, localSort, _sort}）。未知类型返回空串。 */
export function renderArt(kind, d, o = {}) {
  if (!d) return '';
  const fn = { table: tableHtml, metrics: metricsHtml, list: listHtml, code: codeHtml, image: imageHtml,
    timeline: timelineHtml, calendar: calendarHtml, status: statusHtml, chat: chatHtml, web: webHtml }[kind];
  if (!fn) return '';
  return `<div class="av av-${esc(kind)}${o.compact ? ' compact' : ' full'}">${fn(d, o)}</div>`;
}

// ---------- 导出（表格 → CSV；其余类别 → JSON） ----------
function download(name, text, mime) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: mime }));
  a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

/** 导出节点数据：表格按当前排序导 CSV（带 BOM，Excel 直接打开）；其余类别导 JSON。 */
export function artExport(kind, d) {
  if (!d) return;
  const base = String(d.title || t(kind === 'table' ? '表格' : '数据')).replace(/[\\/:*?"<>|]+/g, ' ').slice(0, 40);
  if (kind !== 'table') { download(`${base}.json`, JSON.stringify(d, null, 2), 'application/json'); return; }
  const cols = d.columns || [], rows = d.rows || [];
  const quote = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const text = [cols.map((c) => quote(c.label)).join(','), ...rows.map((r) => cols.map((c, i) => quote(r[i])).join(','))].join('\n');
  download(`${base}.csv`, '\ufeff' + text, 'text/csv;charset=utf-8');
}

// ---------- 本地交互（复制 / 导出 CSV / 表格搜索与本地排序） ----------
/** 给已渲染的节点 / 弹窗 / 右栏绑定本地交互。
 *  - 复制代码、导出 CSV、表格搜索：始终绑定；
 *  - 勾选与排序：传 onCheck / onSort 回调时交给调用方（画布 / 右栏共用同一份持久状态）；localSort=true 时在本组件内本地排序（弹窗）。 */
export function bindArt(root, kind, d, o = {}) {
  if (!root) return;
  const cp = root.querySelector('[data-avcopy]');
  if (cp) cp.onclick = async () => {
    const pre = root.querySelector('.av-code');
    try { await navigator.clipboard.writeText(pre ? pre.textContent : ''); cp.innerHTML = `${ic('check')}${t('已复制')}`; } catch { /* 剪贴板不可用则忽略 */ }
  };
  if (o.onCheck) root.querySelectorAll('[data-avc]').forEach((b) => { b.onclick = () => o.onCheck(+b.dataset.avc); });
  if (o.onSort) root.querySelectorAll('[data-avs]').forEach((th) => { th.onclick = () => o.onSort(+th.dataset.avs); });
  const csv = root.querySelector('[data-avcsv]');
  if (csv && kind === 'table') csv.onclick = () => artExport('table', { ...d, rows: tableRows(d, (o.localSort && o._sort) || null) });
  const q = root.querySelector('[data-avq]');
  if (q) q.oninput = () => {
    const s = q.value.trim().toLowerCase();
    root.querySelectorAll('.av-tbl tbody tr').forEach((tr) => { tr.hidden = !!s && !tr.textContent.toLowerCase().includes(s); });
  };
  if (o.localSort && !o.onSort && kind === 'table') {
    root.querySelectorAll('[data-avs]').forEach((th) => {
      th.onclick = () => {
        const i = +th.dataset.avs;
        o._sort = o._sort && o._sort[0] === i ? [i, o._sort[1] === 'asc' ? 'desc' : 'asc'] : [i, 'asc'];
        root.innerHTML = `<div class="av av-table full">${tableHtml(d, o)}</div>`;
        bindArt(root, kind, d, o);                            // 重新绑定（排序 / 搜索状态保留在闭包里）
      };
    });
  }
}
