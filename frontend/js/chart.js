// 图表渲染（成果中的「图」）：把服务端校验过的 chart 数据渲染为自适应 SVG。
// 契约见 PRD 05：{ type: 'bar'|'pie'|'line', title, labels: string[], series: [{ name, values: number[] }] }
// 服务端已做截断对齐（labels 2–12、series ≤ 4、values 与 labels 等长），这里只负责画，不做二次防御。
// 两种布局（v1.7.1）：
//   full    640×300 —— 成果页卡片、放大弹窗（容器 ≥ 330px 时可读）；
//   compact 300×190 —— 画布成果节点内直显：与节点内容宽 1:1，字号不再被二次压缩。
import { esc } from './core.js';

const PALETTE = ['var(--accent)', 'var(--run)', 'var(--ok)', 'var(--gate)', 'var(--accent-2)', 'var(--err)'];

// axOff：Y 轴刻度文字距轴线的间距；pcx/pr：饼心横坐标与半径；plh/prh：饼图图例的半高与行距；pgap：饼到图例的间隙
const FULL = { W: 640, H: 300, M: { l: 56, r: 20, t: 30, b: 46 }, axOff: 8, pcx: 178, pr: 96, plh: 13, prh: 26, pgap: 44 };
const COMPACT = { W: 300, H: 190, M: { l: 36, r: 10, t: 26, b: 30 }, axOff: 6, pcx: 64, pr: 62, plh: 11, prh: 22, pgap: 16 };

function fmt(v) {
  const a = Math.abs(v);
  if (a >= 10000) return (v / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  if (a >= 1000) return (v / 1000).toFixed(2).replace(/0+$/, '').replace(/\.$/, '') + 'k';
  return String(Math.round(v * 100) / 100);
}

function scale(all) {
  let max = Math.max(...all, 0), min = Math.min(...all, 0);
  if (max === min) max = min + 1;
  const pad = (max - min) * 0.08;
  if (min >= 0) return { min: 0, max: max + pad };
  return { min: min - pad, max: max + pad };
}

function gridY(g, rng) {
  const { W, M } = g, ih = g.H - M.t - M.b;
  const y = (v) => M.t + ih - (v - rng.min) / (rng.max - rng.min) * ih;
  let out = '';
  for (let i = 0; i <= 3; i++) {
    const v = rng.min + (rng.max - rng.min) * i / 3, yy = y(v);
    out += `<line x1="${M.l}" y1="${yy}" x2="${W - M.r}" y2="${yy}" stroke="var(--line)" stroke-width="1"/>`;
    out += `<text x="${M.l - g.axOff}" y="${yy + 4}" text-anchor="end" class="ch-ax">${fmt(v)}</text>`;
  }
  return { g: out, y };
}

function legend(g, spec) {
  if (spec.series.length < 2) return '';
  const { M } = g, iw = g.W - M.l - M.r;
  const lw = iw / spec.series.length;
  return spec.series.map((s, j) => `<g transform="translate(${M.l + lw * j}, ${M.t - 26})">
    <rect x="0" y="0" width="10" height="10" rx="2" fill="${PALETTE[j % PALETTE.length]}"/>
    <text x="15" y="9" class="ch-ax">${esc(s.name || '')}</text></g>`).join('');
}

function bars(g, spec) {
  const { W, H, M } = g, iw = W - M.l - M.r;
  const { labels, series } = spec;
  const rng = scale(series.flatMap((s) => s.values));
  const { g: gr, y } = gridY(g, rng);
  const gw = iw / labels.length;
  const bw = Math.min(48, gw * 0.62 / series.length);
  let out = gr;
  labels.forEach((lb, i) => {
    const cx = M.l + gw * i + gw / 2;
    series.forEach((s, j) => {
      const v = s.values[i];
      const x = cx - (bw * series.length) / 2 + bw * j;
      const top = y(Math.max(v, 0)), base = y(0);
      out += `<rect x="${x}" y="${Math.min(top, base)}" width="${Math.max(2, bw - 3)}" height="${Math.max(1, Math.abs(base - top))}" rx="2" fill="${PALETTE[j % PALETTE.length]}"/>`;
      if (series.length === 1) out += `<text x="${x + (bw - 3) / 2}" y="${top - 5}" text-anchor="middle" class="ch-val">${fmt(v)}</text>`;
    });
    out += `<text x="${cx}" y="${H - M.b + 19}" text-anchor="middle" class="ch-ax">${esc(lb)}</text>`;
  });
  return out;
}

function lines(g, spec) {
  const { W, H, M } = g, iw = W - M.l - M.r;
  const { labels, series } = spec;
  const rng = scale(series.flatMap((s) => s.values));
  const { g: gr, y } = gridY(g, rng);
  const step = labels.length > 1 ? iw / (labels.length - 1) : 0;
  const x = (i) => M.l + step * i;
  let out = gr;
  series.forEach((s, j) => {
    const col = PALETTE[j % PALETTE.length];
    out += `<polyline points="${s.values.map((v, i) => `${x(i)},${y(v)}`).join(' ')}" fill="none" stroke="${col}" stroke-width="2.5" stroke-linejoin="round"/>`;
    out += s.values.map((v, i) => `<circle cx="${x(i)}" cy="${y(v)}" r="3.5" fill="${col}"/>`).join('');
  });
  labels.forEach((lb, i) => {
    out += `<text x="${x(i)}" y="${H - M.b + 19}" text-anchor="middle" class="ch-ax">${esc(lb)}</text>`;
  });
  return out;
}

function pie(g, spec) {
  const { H, M, pcx, plh, prh, pgap } = g;
  const vals = spec.series[0].values.map((v) => Math.max(0, v));
  const total = vals.reduce((a, b) => a + b, 0) || 1;
  const cx = pcx, cy = M.t + (H - M.t - M.b) / 2, r = Math.min(g.pr, (H - M.t - M.b) / 2 - 6);
  let acc = -Math.PI / 2, out = '';
  vals.forEach((v, i) => {
    const ang = v / total * Math.PI * 2;
    const x1 = cx + r * Math.cos(acc), y1 = cy + r * Math.sin(acc);
    const x2 = cx + r * Math.cos(acc + ang), y2 = cy + r * Math.sin(acc + ang);
    out += `<path d="M ${cx} ${cy} L ${x1.toFixed(1)} ${y1.toFixed(1)} A ${r} ${r} 0 ${ang > Math.PI ? 1 : 0} 1 ${x2.toFixed(1)} ${y2.toFixed(1)} Z" fill="${PALETTE[i % PALETTE.length]}" stroke="var(--panel)" stroke-width="2"/>`;
    if (v / total >= 0.06) {
      const mid = acc + ang / 2, lx = cx + r * 0.6 * Math.cos(mid), ly = cy + r * 0.6 * Math.sin(mid);
      out += `<text x="${lx.toFixed(1)}" y="${(ly + 4).toFixed(1)}" text-anchor="middle" class="ch-pc">${Math.round(v / total * 100)}%</text>`;
    }
    acc += ang;
  });
  const top = cy - spec.labels.length * plh + 5;
  spec.labels.forEach((lb, i) => {
    const ly = top + i * prh;
    out += `<rect x="${cx + r + pgap}" y="${ly - 10}" width="10" height="10" rx="2" fill="${PALETTE[i % PALETTE.length]}"/>`;
    out += `<text x="${cx + r + pgap + 16}" y="${ly}" class="ch-ax">${esc(lb)} · ${fmt(vals[i])}</text>`;
  });
  return out;
}

/** spec → SVG 字符串；spec 不合法（无数据）时返回空串。opts.compact=true 用于画布成果节点内直显。 */
export function renderChart(spec, opts = {}) {
  if (!spec || !Array.isArray(spec.series) || !spec.series.length || !Array.isArray(spec.labels) || spec.labels.length < 2) return '';
  const g = opts.compact ? COMPACT : FULL;
  const body = spec.type === 'pie' ? pie(g, spec) : spec.type === 'line' ? lines(g, spec) : bars(g, spec);
  return `<svg class="chart" viewBox="0 0 ${g.W} ${g.H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${esc(spec.title || '')}">${legend(g, spec)}${body}</svg>`;
}
