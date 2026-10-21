// 通用：转义、图标、接口调用、提示、弹窗、Markdown、格式化
import { t, LANG } from './i18n.js';

export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const P = {
  plus: 'M12 5v14M5 12h14', home: 'M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z', book: 'M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2zM4 5v16',
  flow: 'M5 6h4v4H5zM15 14h4v4h-4zM9 8h3a3 3 0 0 1 3 3v3', layers: 'M12 3l9 5-9 5-9-5zM3 13l9 5 9-5', cpu: 'M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4',
  gear: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19 12l2-1-1-3-2 .3-1.5-1.5L17 4l-3-1-1 2h-2l-1-2-3 1 .5 2.8L6 8.3 4 8 3 11l2 1v0l-2 1 1 3 2-.3 1.5 1.5L7 20l3 1 1-2h2l1 2 3-1-.5-2.8 1.5-1.5 2 .3 1-3z',
  play: 'M7 5l12 7-12 7z', pause: 'M8 5v14M16 5v14', step: 'M6 5l9 7-9 7zM18 5v14', stop: 'M6 6h12v12H6z', x: 'M6 6l12 12M18 6L6 18',
  check: 'M5 12l5 5 9-10', alert: 'M12 8v5M12 16.5v.5M10.3 3.9L2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z',
  zin: 'M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM21 21l-4.3-4.3M8 11h6M11 8v6', zout: 'M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM21 21l-4.3-4.3M8 11h6',
  fit: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5', eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
  pen: 'M4 20h4L19 9l-4-4L4 16zM14 6l4 4', upload: 'M12 16V4M7 9l5-5 5 5M4 20h16', doc: 'M7 3h7l5 5v13H7zM14 3v5h5', bell: 'M6 9a6 6 0 1 1 12 0c0 6 3 7 3 7H3s3-1 3-7M10 20a2 2 0 0 0 4 0',
  search: 'M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14zM21 21l-4.3-4.3', sparkle: 'M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2z', menu: 'M4 6h16M4 12h16M4 18h16',
  refresh: 'M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5', clock: 'M12 7v5l3 2M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18z', trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  down: 'M6 9l6 6 6-6', save: 'M5 4h11l3 3v13H5zM8 4v5h8V4M8 20v-6h8v6', globe: 'M12 3a9 9 0 1 1 0 18 9 9 0 0 1 0-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18',
  lock: 'M6 11h12v9H6zM9 11V8a3 3 0 0 1 6 0v3', msg: 'M4 5h16v11H8l-4 4z', link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
  chart: 'M4 20h16M6.5 20v-7M12 20V5M17.5 20v-10', image: 'M4 5h16v14H4zM4 15l5-5 4 4 3-3 4 4M9 9.5a.5.5 0 1 1 0-.01', file: 'M6 3h8l4 4v14H6zM14 3v4h4',
  pin: 'M9 4h6M10 4l-.6 5L6 13.5h12L14.6 9 14 4M12 13.5V20', eyeoff: 'M4 4l16 16M10.6 10.6a3 3 0 0 0 4.2 4.2M6.7 6.7C4 8.4 2 12 2 12s4 7 10 7c2 0 3.8-.7 5.3-1.8M9.9 5.2A11 11 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.2 4',
  // v3.0 功能性节点（M3/M4）图标：表格 / 清单 / 代码 / 日历 / 指标
  table: 'M4 5h16v14H4zM4 10h16M4 14.5h16M10 5v14', list: 'M5 7h.01M5 12h.01M5 17h.01M10 7h9M10 12h9M10 17h9',
  code: 'M9 8l-4 4 4 4M15 8l4 4-4 4', cal: 'M5 5h14v15H5zM5 10h14M9 3v4M15 3v4',
  gauge: 'M12 13l4-3M4 18a8 8 0 1 1 16 0z',
};
export const ic = (n, cls = 'i') => `<svg class="${cls}" viewBox="0 0 24 24"><path d="${P[n] || ''}"/></svg>`;

// ---------- 接口 ----------
export class ApiErr extends Error { constructor(status, code, message, details) { super(message); Object.assign(this, { status, code, details }); } }

export async function api(method, path, data, opts = {}) {
  const init = { method, headers: { 'X-Combine': '1' }, credentials: 'same-origin' };
  if (data instanceof FormData) init.body = data;
  else if (data !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(data); }
  let r;
  try { r = await fetch(path, init); } catch { throw new ApiErr(0, 'NETWORK', t('网络连接失败，请检查服务是否在运行')); }
  if (opts.raw) return r;
  const txt = await r.text();
  let j = null, bad = false; try { j = txt ? JSON.parse(txt) : null; } catch { bad = true; }
  if (r.ok && bad) throw new ApiErr(r.status, 'BAD_RESPONSE', t('服务返回了无法识别的内容，请刷新页面重试'));
  if (!r.ok) {
    const e = j?.error || {};
    const err = new ApiErr(r.status, e.code || 'HTTP_' + r.status, errText(e.code, e.message), e.details);
    if (r.status === 401 && !opts.silent401) window.dispatchEvent(new CustomEvent('cb:unauth'));
    throw err;
  }
  return j;
}
export const GET = (p, o) => api('GET', p, undefined, o);
export const POST = (p, d = {}, o) => api('POST', p, d, o);
export const PATCH = (p, d = {}) => api('PATCH', p, d);
export const PUT = (p, d = {}) => api('PUT', p, d);
export const DEL = (p) => api('DELETE', p);

// 错误码 → 本地化提示（服务端 message 为中文，英文界面下按错误码翻译）
const ERR_EN = {
  UNAUTHENTICATED: 'Please sign in', LOGIN_FAILED: 'Wrong email or password', EMAIL_TAKEN: 'This email is already registered',
  PASSWORD_WEAK: 'Password must be at least 8 characters', EMAIL_INVALID: 'Invalid email', TOO_MANY_ATTEMPTS: 'Too many attempts, try again in 10 minutes',
  STATE_CONFLICT: 'The task state changed; please refresh', PLAN_CYCLE: 'This would create a dependency cycle', SETTING_LOCKED: 'This setting is protected and cannot be changed',
  BUDGET_EXCEEDED: 'Task budget exceeded', QUOTA_EXCEEDED: 'Monthly built-in quota used up', BUDGET_ACK_REQUIRED: 'Estimated cost exceeds the remaining budget',
  PRIVATE_ADDRESS_FORBIDDEN: 'The cloud edition cannot reach private or local addresses', LOCAL_DISABLED: 'Local models are only available in the self-hosted edition',
  MODEL_NOT_FOUND: 'Model not found', NOT_FOUND: 'Not found', NETWORK: 'Network error — is the server running?', UPLOAD_TYPE: 'Only text files (txt / md / csv / json / html) are supported',
  UPLOAD_TOO_LARGE: 'File is too large', ATTENTION_BLOCKED: 'Blocking items must be answered', REASON_REQUIRED: 'Please describe what to change',
  NOTHING_TO_APPLY: 'Nothing to apply', FIELD_REQUIRED: 'A required field is missing', CLIP_FAILED: 'Could not fetch the page; paste the text instead',
  RATE_LIMITED: 'Too many requests; please try again shortly', FIELD_INVALID: 'A field has an invalid value', OP_INVALID: 'This edit is not valid',
  TOO_MANY_STREAMS: 'Too many open tabs for this task; close some and retry', PROVIDER_KIND_INVALID: 'Unsupported provider type',
  REGISTRATION_CLOSED: 'Registration is currently closed', SETTING_INVALID: 'Invalid setting value', SETTING_UNKNOWN: 'Unknown setting',
  INTERNAL: 'Something went wrong. Please try again.', CSRF: 'Your request could not be verified. Refresh the page and try again.',
  VALIDATION: 'Check the form fields and try again', INVALID_OUTPUT: 'The model returned an invalid response',
  TRUNCATED_OUTPUT: 'The model response was incomplete. Please retry.', TASK_CANCELLED: 'The task was cancelled',
  MODEL_TIMEOUT: 'The model did not respond in time', LLM_TIMEOUT: 'The model did not respond in time',
  AGENT_CONNECT_FAILED: 'Could not connect to the agent', AGENT_UNAVAILABLE: 'The agent is unavailable',
  FORMAT_UNSUPPORTED: 'This export format is not supported', PATH_FORBIDDEN: 'This file path is not allowed',
};
export function errText(code, msg) {
  if (LANG() !== 'en') return msg || code;
  return ERR_EN[code] || (msg && !/[\u4e00-\u9fff]/.test(msg) ? msg : t('请求失败，请重试'));
}

// ---------- 提示 / 弹窗 ----------
let toastTimer;
export function toast(msg, err = false) {
  const el = $('#toast'); el.textContent = msg; el.className = 'toast on' + (err ? ' err' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('on'), 3200);
}
export const fail = (e) => toast(e?.message || String(e), true);

let closeCurrent = null;
/** 关闭当前弹窗（等价于点「关闭」）。 */
export const closeModal = (v = null) => closeCurrent?.(v);

export function modal(title, bodyHtml, buttons = [], width = 560) {
  closeCurrent?.(null);                                  // 同一时间只保留一个弹窗，先结束上一个的 Promise
  return new Promise((resolve) => {
    const ov = $('#overlay');
    ov.innerHTML = `<div class="modal" style="width:min(${width}px,100%)" role="dialog" aria-modal="true">
      <div class="modal-head"><h3>${esc(title)}</h3><button class="icon-btn" data-m="__close" aria-label="${esc(t('关闭'))}">${ic('x')}</button></div>
      <div class="modal-body">${bodyHtml}</div>
      ${buttons.length ? `<div class="modal-foot">${buttons.map((b, i) => `<button class="btn ${b.cls || ''}" data-m="${i}">${b.label}</button>`).join('')}</div>` : ''}</div>`;
    ov.classList.add('on');
    let done = false;
    const close = (v) => {
      if (done) return; done = true; closeCurrent = null;
      ov.classList.remove('on'); ov.innerHTML = ''; ov.onclick = null; document.removeEventListener('keydown', onKey); resolve(v);
    };
    closeCurrent = close;
    const onKey = (e) => { if (e.key === 'Escape') close(null); };
    document.addEventListener('keydown', onKey);
    ov.onclick = async (e) => {
      if (e.target === ov) return close(null);
      const go = e.target.closest('[data-go]');           // 弹窗不在 #app 内，需要自己处理站内链接
      if (go) { close(null); location.hash = '#' + go.dataset.go; return; }
      const b = e.target.closest('[data-m]'); if (!b) return;
      if (b.dataset.m === '__close') return close(null);
      const def = buttons[+b.dataset.m];
      if (def.onClick) {
        if (b.disabled) return;                              // 防止连点重复提交
        b.disabled = true;
        try { const v = await def.onClick(ov); if (v !== false) close(v ?? def.value ?? true); else b.disabled = false; } catch (err) { fail(err); b.disabled = false; }
      } else close(def.value ?? true);
    };
    setTimeout(() => $('input,textarea', ov)?.focus(), 30);
  });
}
export const confirmBox = (title, text, okLabel, danger) =>
  modal(title, `<p>${esc(text)}</p>`, [{ label: t('取消'), value: false }, { label: okLabel || t('确认'), cls: danger ? 'danger' : 'primary', value: true }], 440);

// ---------- Markdown（转义后再做有限的格式化；引用 [[c:id]] → 角标） ----------
export function md(src, cites = []) {
  const idx = {}; cites.forEach((c, i) => { idx[c.chunk_id] = { n: i + 1, c }; });
  let n = Object.keys(idx).length;
  const inline = (raw) => {
    const tokens = [], token = (html) => `\u0000${tokens.push(html) - 1}\u0000`;
    let line = String(raw).replace(/\u0000/g, '').replace(/`([^`]+)`/g, (_, code) => token(`<code>${esc(code)}</code>`));
    line = line.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => token(`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`));
    line = esc(line).replace(/\[\[c:([\w-]+)\]\]/g, (_, id) => {
      if (!idx[id]) idx[id] = { n: ++n, c: { chunk_id: id } };
      return `<span class="cite" data-cite="${id}" title="${esc(idx[id].c.doc_title || '')}">${idx[id].n}</span>`;
    }).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    return line.replace(/\u0000(\d+)\u0000/g, (_, i) => tokens[+i] || '');
  };
  const lines = String(src || '').replace(/\r\n/g, '\n').split('\n');
  const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((s) => s.trim());
  let html = '', list = '', fence = false, code = [];
  const closeList = () => { if (list) { html += `</${list}>`; list = ''; } };
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    if (/^\s*```/.test(raw)) {
      closeList();
      if (fence) { html += `<pre><code>${esc(code.join('\n'))}</code></pre>`; code = []; }
      fence = !fence; continue;
    }
    if (fence) { code.push(raw); continue; }
    if (raw.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1]) && cells(lines[i + 1]).every((v) => /^:?-{3,}:?$/.test(v))) {
      closeList(); const head = cells(raw); i++;
      html += `<div class="md-table-wrap"><table><thead><tr>${head.map((v) => `<th>${inline(v)}</th>`).join('')}</tr></thead><tbody>`;
      while (i + 1 < lines.length && lines[i + 1].includes('|') && lines[i + 1].trim()) {
        const row = cells(lines[++i]); html += `<tr>${head.map((_, c) => `<td>${inline(row[c] || '')}</td>`).join('')}</tr>`;
      }
      html += '</tbody></table></div>'; continue;
    }
    const h = raw.match(/^(#{1,6})\s+(.*)/), li = raw.match(/^\s*([-*]|\d+\.)\s+(.*)/), quote = raw.match(/^>\s?(.*)/);
    if (li) {
      const tag = /^\d/.test(li[1]) ? 'ol' : 'ul';
      if (list !== tag) { closeList(); html += `<${tag}>`; list = tag; }
      html += `<li>${inline(li[2])}</li>`; continue;
    }
    closeList();
    if (h) html += `<h3>${inline(h[2])}</h3>`;
    else if (quote) html += `<blockquote>${inline(quote[1])}</blockquote>`;
    else if (raw.trim()) html += `<p>${inline(raw)}</p>`;
  }
  closeList();
  if (fence) html += `<pre><code>${esc(code.join('\n'))}</code></pre>`;
  return `<div class="md">${html}</div>`;
}

// ---------- 格式化 ----------
export const money = (v) => '¥' + (Number(v) || 0).toFixed((Number(v) || 0) < 0.1 ? 4 : 2);
export function dur(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  if (s < 60) return LANG() === 'en' ? `${s}s` : `${s}秒`;
  const m = Math.floor(s / 60), r = s % 60;
  return LANG() === 'en' ? `${m}m ${r}s` : `${m}分${r}秒`;
}
export const hhmmss = (ms) => new Date(ms).toTimeString().slice(0, 8);
export function ago(ms) {
  const d = (Date.now() - ms) / 1000;
  if (d < 60) return t('刚刚');
  if (d < 3600) return t('{n} 分钟前', { n: Math.floor(d / 60) });
  if (d < 86400) return t('{n} 小时前', { n: Math.floor(d / 3600) });
  return new Date(ms).toLocaleDateString();
}
export const debounce = (fn, ms) => { let h; const d = (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; d.cancel = () => clearTimeout(h); return d; };
/** 执行期间禁用按钮并忽略重复触发（按钮连点、Ctrl+Enter 连按）。 */
export async function once(btn, fn) {
  if (btn?.dataset.busy) return;
  if (btn) { btn.dataset.busy = '1'; btn.disabled = true; }
  try { return await fn(); } finally { if (btn) { delete btn.dataset.busy; btn.disabled = false; } }
}
