// 地图攻略渲染（成果中的「地图」，v1.9）：把服务端校验过的地图数据渲染为自适应 SVG。
// 契约见 PRD 05：{ title, city, markers: [{ name, lat, lng, day 1–7, note }] } —— 坐标 GCJ-02。
// 服务端已做校验（2–24 个点、坐标范围、天编号），这里只负责画，不做二次防御。
//
// 说明：这是**离线可用的攻略示意图**（无需任何地图 Key，供应链与演示环境完全可控）；
// 真实在线底图（腾讯地图等白名单合规服务商）由用户在设置中自配 Key 后切换，见 PRD 06 §地图能力。
// 投影：等距圆柱 + 中心纬度经度压缩（小区度范围内与真实长宽比一致，不把地图拉变形）。
// 两种布局（同 renderChart 模式）：
//   full    640×420 —— 成果页卡片、放大弹窗；
//   compact 300×240 —— 画布成果节点内直显。
import { esc } from './core.js';

const DAY_COLORS = ['var(--accent)', 'var(--run)', 'var(--ok)', 'var(--accent-2)'];

const FULL = { W: 640, H: 420, M: { l: 34, r: 24, t: 40, b: 46 }, R: 9, fs: 12, nfs: 10 };
const COMPACT = { W: 300, H: 240, M: { l: 18, r: 14, t: 30, b: 34 }, R: 7, fs: 10, nfs: 8.5 };

/** 把「非均匀刻度」收成 1/2/5×10ⁿ。 */
function nice(v) {
  if (!(v > 0)) return 1;
  const e = Math.pow(10, Math.floor(Math.log10(v)));
  const m = v / e;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * e;
}

function fmtKm(km) {
  if (km >= 10) return String(Math.round(km));
  if (km >= 1) return String(Math.round(km * 10) / 10);
  return String(Math.round(km * 100) / 100);
}

/** spec → SVG 字符串；数据不足（< 2 个点）时返回空串。opts.compact=true 用于画布成果节点内直显。 */
export function renderMap(spec, opts = {}) {
  const ms = spec && Array.isArray(spec.markers) ? spec.markers.filter((m) => m && typeof m.lat === 'number' && typeof m.lng === 'number') : [];
  if (ms.length < 2) return '';
  const g = opts.compact ? COMPACT : FULL;

  // —— 投影与自适应 ——（等距圆柱 + 中心纬度压缩：1° 经度 ≈ cos(lat) × 1° 纬度，比例真实不变形）
  const lat0 = ms.reduce((a, m) => a + m.lat, 0) / ms.length;
  const kx = Math.max(Math.cos((lat0 * Math.PI) / 180), 0.2);
  const P = ms.map((m) => ({ ...m, px: m.lng * kx, py: -m.lat }));
  const minX = Math.min(...P.map((p) => p.px)), maxX = Math.max(...P.map((p) => p.px));
  const minY = Math.min(...P.map((p) => p.py)), maxY = Math.max(...P.map((p) => p.py));
  const spanX = Math.max(maxX - minX, 2e-3), spanY = Math.max(maxY - minY, 2e-3);
  const iw = g.W - g.M.l - g.M.r, ih = g.H - g.M.t - g.M.b;
  const s = Math.min(iw / spanX, ih / spanY);                       // px / 度
  const ox = g.M.l + (iw - spanX * s) / 2, oy = g.M.t + (ih - spanY * s) / 2;
  const X = (p) => ox + (p.px - minX) * s, Y = (p) => oy + (p.py - minY) * s;

  // —— 经纬网格 ——
  const step = nice(spanX / 3.4);
  let grid = '';
  for (let v = Math.ceil(minX / step) * step; v <= maxX + 1e-9; v += step) {
    const x = ox + (v - minX) * s;
    grid += `<line x1="${x.toFixed(1)}" y1="${g.M.t}" x2="${x.toFixed(1)}" y2="${g.H - g.M.b}" class="mp-grid"/>`;
  }
  const vstep = nice(spanY / 3.4) / kx;                             // 纬度方向的步长（py 单位）
  for (let v = Math.ceil(minY / vstep) * vstep; v <= maxY + 1e-9; v += vstep) {
    const y = oy + (v - minY) * s;
    grid += `<line x1="${g.M.l}" y1="${y.toFixed(1)}" x2="${g.W - g.M.r}" y2="${y.toFixed(1)}" class="mp-grid"/>`;
  }

  // —— 路线：同一天内按数组顺序连线 ——
  const byDay = {};
  ms.forEach((m, i) => (byDay[m.day || 1] ||= []).push(i));
  let routes = '';
  for (const [day, idxs] of Object.entries(byDay)) {
    if (idxs.length < 2) continue;
    const col = DAY_COLORS[(+day - 1 + DAY_COLORS.length) % DAY_COLORS.length];
    const pts = idxs.map((i) => `${X(P[i]).toFixed(1)},${Y(P[i]).toFixed(1)}`).join(' ');
    routes += `<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" opacity=".9"/>`;
  }

  // —— 标记：编号圆点 + 名称标签（贪心避让：上 → 下 → 斜四向，密集处也不互相压字）——
  const placed = [];
  const box = (cx, cy, w, h) => ({ x1: cx - w / 2, x2: cx + w / 2, y1: cy - h / 2, y2: cy + h / 2 });
  const hit = (b) => placed.some((p) => !(b.x2 < p.x1 || b.x1 > p.x2 || b.y2 < p.y1 || b.y1 > p.y2));
  let mk = '';
  ms.forEach((m, i) => {
    const col = DAY_COLORS[((m.day || 1) - 1 + DAY_COLORS.length) % DAY_COLORS.length];
    const x = X(P[i]), y = Y(P[i]);
    const w = Math.min(String(m.name).length * g.fs, g.W * 0.6), h = g.fs + 4;
    const up = g.R + 8 + h / 2, side = g.R + 5 + h / 2;
    const cands = [[0, -up], [0, up], [w / 2 + 12, -side], [-w / 2 - 12, -side], [w / 2 + 12, side], [-w / 2 - 12, side]];
    let lx = x, ly = y - up;
    for (const [dx, dy] of cands) {
      const cx = x + dx, cy = y + dy;
      if (cx - w / 2 < 3 || cx + w / 2 > g.W - 3 || cy - h / 2 < 2 || cy + h / 2 > g.H - 3) continue;
      if (!hit(box(cx, cy, w, h))) { lx = cx; ly = cy; break; }
    }
    placed.push(box(lx, ly, w, h));
    mk += `<g><title>${esc(`${i + 1}. ${m.name}${m.note ? ' — ' + m.note : ''}`)}</title>` +
      `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${g.R + 1.5}" fill="${col}" opacity=".2"/>` +
      `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${g.R}" fill="${col}" class="mp-dot"/>` +
      `<text x="${x.toFixed(1)}" y="${(y + (g.nfs > 9 ? 4.2 : 3.6)).toFixed(1)}" text-anchor="middle" class="mp-num">${i + 1}</text>` +
      `<text x="${lx.toFixed(1)}" y="${(ly + g.fs * 0.35).toFixed(1)}" text-anchor="middle" class="mp-lb">${esc(m.name)}</text></g>`;
  });

  // —— 比例尺（s px ≈ 1° 纬度 ≈ 111 km）——
  const kmPerPx = 111 / s;
  const targets = [0.2, 0.5, 1, 2, 5, 10, 20, 50, 100];
  const km = targets.find((k) => k >= kmPerPx * (g.W * 0.16)) || 100;
  const barPx = km / kmPerPx;
  const bx = g.W - g.M.r - barPx, by = g.H - 12;
  const scale = `<g><line x1="${bx}" y1="${by}" x2="${bx + barPx}" y2="${by}" class="mp-scale"/>` +
    `<line x1="${bx}" y1="${by - 3}" x2="${bx}" y2="${by + 3}" class="mp-scale"/>` +
    `<line x1="${bx + barPx}" y1="${by - 3}" x2="${bx + barPx}" y2="${by + 3}" class="mp-scale"/>` +
    `<text x="${bx + barPx / 2}" y="${by - 5}" text-anchor="middle" class="mp-ax">≈ ${fmtKm(km)} km</text></g>`;

  // —— 指北针 ——
  const cx0 = g.W - g.M.r - 9, cy0 = g.M.t - 6;
  const compass = `<g class="mp-compass"><path d="M ${cx0} ${cy0 - 9} L ${cx0 - 5} ${cy0 + 2} L ${cx0} ${cy0 - 1.5} L ${cx0 + 5} ${cy0 + 2} Z" fill="var(--accent)"/>` +
    `<text x="${cx0}" y="${cy0 + 12}" text-anchor="middle" class="mp-ax">N</text></g>`;

  // —— 左上信息 & 天数图例（图例放左下角，与右下的比例尺分开）——
  const days = [...new Set(ms.map((m) => m.day || 1))].sort((a, b) => a - b);
  const head = `${esc(spec.city || '')}${spec.city ? ' · ' : ''}${ms.length} 个地点`;
  let lg = '';
  if (days.length > 1) {
    lg = days.map((d, j) => {
      const col = DAY_COLORS[(d - 1 + DAY_COLORS.length) % DAY_COLORS.length];
      return `<g transform="translate(${g.M.l + j * 46}, ${g.H - 13})">
        <circle cx="5" cy="-3.5" r="4.5" fill="${col}"/><text x="14" y="0" class="mp-ax">D${d}</text></g>`;
    }).join('');
  }

  const aria = esc(spec.title || 'map');
  return `<svg class="mapview" viewBox="0 0 ${g.W} ${g.H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${aria}">` +
    `<rect x="0.5" y="0.5" width="${g.W - 1}" height="${g.H - 1}" rx="10" class="mp-bg"/>` +
    grid + routes + mk + scale + compass +
    `<text x="${g.M.l}" y="${g.M.t - 20}" class="mp-head">${head}</text>` + lg +
    `</svg>`;
}
