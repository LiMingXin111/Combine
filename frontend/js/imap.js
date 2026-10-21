// imap.js（v2.1.1）——画布内「可操控」攻略地图：开源 Leaflet 1.9 + 多瓦片源（主：高德公开瓦片，备：OpenStreetMap）。
// 统一挂载器：地图成果节点 / 放大弹窗 / 右栏面板 / 成果页详情 共用同一份实现。
//
// 交互约定（与画布共存，关键设计）：
// - 拖动 / 点标记 / 双击缩放：随时可用（Leaflet 原生）；滚轮缩放需先「点击地图」激活（否则滚轮继续滚动画布）
// - 事件隔离：左键 pointerdown、双击、激活后的滚轮 不冒泡到画布 —— 不会误拖节点、误触发节点放大、误缩放画布；
//   中键继续冒泡 —— 中键拖动画布（v1.7.2）在地图上照常工作
// - 视图状态（center / zoom / auto）按 key 记忆：画布 re-render 重建容器后恢复用户刚调好的视角；
//   auto=true 表示「尚未手动调整过」，容器尺寸变化（如右栏拖动调宽）时按新尺寸重新 fitBounds 保持全貌
// - 容器尺寸自适应（v2.1.1）：ResizeObserver 监听容器 —— 面板 / 弹窗尺寸变化自动 invalidateSize（100ms 节流），
//   右栏拖动调宽、面板展开收起等场景地图始终匹配容器大小；尺寸变化后视角若是自动全貌则重新取全貌
// - 瓦片源（v2.1.1）：多源可换 + tileerror 自动降级 —— OSM 对高频应用会间歇性封禁（403 Access blocked，
//   "not following the tile usage policy"）；默认改用高德公开瓦片（无 Key、GCJ-02 与内置地标库坐标一致、
//   大陆访问快、中文标注），连续失败自动切到下一源。两源坐标系不同（GCJ-02 vs WGS-84），地标库为 GCJ-02，
//   与高德完美对齐；降级到 OSM 时会有约百米级偏移，属应急瑕疵
// - 离线可用：瓦片是增强层——加载失败只丢底图，标记 / 路线 / 操控照常（容器带网格兜底底色）
// - 画布缩放（CSS transform）下的坐标正确性依赖 Leaflet 1.9 内置的 getScale 修正（勿降级到 1.7）
import { esc } from './core.js';
import { t } from './i18n.js';

// 瓦片源（主 → 备，按序尝试；一行可增删/替换）。全部无 Key。
const TILE_SOURCES = [
  { url: 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
    subdomains: '1234', attribution: '© 高德地图' },                                        // 高德（GCJ-02）
  { url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    subdomains: 'abc', attribution: '© OpenStreetMap contributors' },                       // OSM（WGS-84，降级备用）
];
const TILE_MAXZOOM = 19;
const TILE_ERROR_SWITCH = 6;      // 同一源连续失败多少个瓦片后自动降级到下一源

// 分日配色（与离线攻略图 / 成果节点一致的色系循环）
const DAY_COLORS = ['#6e5bff', '#e07b1f', '#12b76a', '#0e7490', '#db2777', '#0d9488', '#b45309'];

const VIEWS = new Map();   // key → { center:[lat,lng], zoom, auto?:boolean }：重建恢复视角用（纯内存，不落库）

const dayColor = (d) => DAY_COLORS[((+d || 1) - 1 + 700) % 7];

/** 编号针形标记（内联 SVG divIcon，不依赖任何外部图片）。 */
function pinIcon(L, n, color) {
  const svg = `<svg width="34" height="44" viewBox="0 0 34 44" xmlns="http://www.w3.org/2000/svg">`
    + `<path d="M17 43C17 43 2 26.5 2 15.5C2 7.5 8.7 1 17 1C25.3 1 32 7.5 32 15.5C32 26.5 17 43 17 43Z" fill="${color}" stroke="#fff" stroke-width="2.5"/>`
    + `<circle cx="17" cy="15.5" r="8.6" fill="#fff"/>`
    + `<text x="17" y="19.4" font-size="11.5" font-weight="700" text-anchor="middle" fill="${color}" font-family="var(--font-ui)">${n}</text></svg>`;
  return L.divIcon({ className: 'mp-pin', html: svg, iconSize: [34, 44], iconAnchor: [17, 43], popupAnchor: [0, -40] });
}

/** Leaflet 是否已就绪（vendor 脚本加载成功）。 */
export function leafletAvailable() { return !!window.L; }

/** 挂载瓦片层：多源顺序尝试，同一源连续失败自动降级（v2.1.1）。 */
function attachTiles(L, map) {
  let idx = 0;
  let errs = 0;
  let layer = null;
  const attach = () => {
    const src = TILE_SOURCES[idx];
    if (layer) map.removeLayer(layer);
    layer = L.tileLayer(src.url, {
      maxZoom: TILE_MAXZOOM, minZoom: 2, subdomains: src.subdomains, attribution: src.attribution,
    }).addTo(map);
    layer.on('tileerror', () => {
      if (++errs >= TILE_ERROR_SWITCH && idx < TILE_SOURCES.length - 1) { idx += 1; errs = 0; attach(); }
    });
    layer.on('tileload', () => { errs = 0; });   // 有成功即视为「该源可用」，计数清零
  };
  attach();
}

/**
 * 把一张 map 规格（{title, city, markers:[{name, lat, lng, day, note}]}）挂载为可操控地图。
 * @param {HTMLElement} box   容器（会变成 .leaflet-container，需有固定高度）
 * @param {object} spec       地图数据（与节点输出的 map 字段一致）
 * @param {object} opts       { key, onActiveChange } —— key 用于视角记忆
 * @returns {{map, isActive, setActive, destroy}|null}
 */
export function mountLeafletMap(box, spec, opts = {}) {
  const L = window.L;
  if (!L || !box || !spec) return null;
  if (box._imap) return box._imap;                        // 同一 DOM 上不重复挂载
  const markers = (Array.isArray(spec.markers) ? spec.markers : [])
    .filter((m) => Number.isFinite(+m.lat) && Number.isFinite(+m.lng));
  if (markers.length < 2) return null;
  const key = String(opts.key || '');

  const map = L.map(box, {
    zoomControl: true,          // 左上角 +/− 按钮（地图节点小，按钮比滚轮更易用）
    attributionControl: true,   // 右下角署名（数据来源要求）
    trackResize: false,         // 不挂 window resize（容器尺寸变化由下方 ResizeObserver 精确处理）
    keyboard: false,            // 不抢画布键盘快捷键
    scrollWheelZoom: false,     // 点击激活后才开启滚轮缩放
    zoomSnap: 0.5,
    minZoom: 2,
  });
  attachTiles(L, map);

  // 分日路线 + 编号标记（markers 数组顺序即游览顺序；同一天内按顺序连线）
  const byDay = new Map();
  for (const m of markers) { const d = +m.day || 1; if (!byDay.has(d)) byDay.set(d, []); byDay.get(d).push(m); }
  for (const [d, list] of byDay) {
    if (list.length < 2) continue;
    const pts = list.map((m) => [+m.lat, +m.lng]);
    L.polyline(pts, { color: '#ffffff', weight: 7, opacity: 0.9 }).addTo(map);   // 白描边：路线在底图上更清晰
    L.polyline(pts, { color: dayColor(d), weight: 4, opacity: 1 }).addTo(map);
  }
  for (const [d, list] of byDay) list.forEach((m, i) => {
    // bubblingMouseEvents：标记点击也要冒泡到地图 —— 否则点编号针无法激活（Leaflet 标记默认不冒泡）
    const mk = L.marker([+m.lat, +m.lng], { icon: pinIcon(L, i + 1, dayColor(d)), title: String(m.name || ''), bubblingMouseEvents: true });
    mk.addTo(map);
    const line = `D${d}${m.note ? ' · ' + esc(m.note) : ''}`;
    mk.bindPopup(`<div class="im-pop"><b>${esc(m.name || '')}</b><span>${line}</span></div>`,
      { closeButton: false, offset: [0, -34] });
  });

  // 视角：优先恢复「用户调过的」记忆（重建不丢）；否则 / 仅自动全貌时 → fitBounds 到全部点位（取全貌）
  const bounds = L.latLngBounds(markers.map((m) => [+m.lat, +m.lng]));
  let fitting = false;
  const doFit = () => {
    if (!box.clientWidth || !box.clientHeight) return;   // 容器尚未就绪（隐藏 / 塌陷）
    fitting = true;
    try { map.fitBounds(bounds, { padding: [28, 28], maxZoom: 16, animate: false }); } catch { /* 尺寸尚未定 */ }
    fitting = false;
  };
  const saved = key ? VIEWS.get(key) : null;
  if (saved && !saved.auto) map.setView(saved.center, saved.zoom);
  else doFit();
  // auto=true ⇔ 本次视角变化来自自动 fitBounds（用户尚未手动调整）——容器尺寸变化时据此决定是否重取全貌
  if (key) map.on('moveend zoomend', () => VIEWS.set(key, { center: [map.getCenter().lat, map.getCenter().lng], zoom: map.getZoom(), auto: fitting }));

  // 容器尺寸自适应（v2.1.1）：右栏拖动调宽 / 弹窗尺寸变化时地图始终匹配容器大小。
  // 100ms 节流（leading + trailing）——拖动过程中也持续跟随，停止后补一次收尾。
  const applySize = () => {
    if (!box.clientWidth || !box.clientHeight) return;
    try { map.invalidateSize({ animate: false, pan: false }); } catch { return; }
    const v = key ? VIEWS.get(key) : null;
    if (!v || v.auto) doFit();                            // 仍是自动全貌 → 按新尺寸重新取全貌（用户调过则保持）
  };
  let roLast = 0;
  let roT = 0;
  const ro = new ResizeObserver(() => {
    const now = Date.now();
    if (now - roLast >= 100) { roLast = now; applySize(); }
    else { clearTimeout(roT); roT = setTimeout(() => { roLast = Date.now(); applySize(); }, 100); }
  });
  ro.observe(box);

  // 激活态：点击地图 → 开启滚轮缩放（否则滚轮留给画布平移）；Esc / 点画布空白由画布侧调用 setActive(false)
  const host = box.parentElement;
  const hint = document.createElement('div');
  hint.className = 'im-hint';
  hint.textContent = t('点击地图启用滚轮缩放');
  if (host) host.appendChild(hint);
  let active = false;
  const setActive = (on) => {
    const v = !!on;
    if (v === active) return;
    active = v;
    box.classList.toggle('im-on', active);
    hint.textContent = active ? t('滚轮缩放已开启 · 点击空白处或按 Esc 退出') : t('点击地图启用滚轮缩放');
    hint.classList.toggle('on', active);
    if (active) map.scrollWheelZoom.enable(); else map.scrollWheelZoom.disable();
    opts.onActiveChange?.(active);
  };
  map.on('click', () => setActive(true));
  map.on('popupopen', () => setActive(true));   // 点开标记气泡（标记点击不冒泡）同样视为「正在操作地图」

  // 事件隔离：不让地图手势冒泡到画布（左键 / 双击 / 激活后的滚轮；中键放行 = 画布平移）
  const stopL = (e) => { if (e.button === 0) e.stopPropagation(); };
  const stopDbl = (e) => e.stopPropagation();
  const stopWh = (e) => { if (active) e.stopPropagation(); };
  box.addEventListener('pointerdown', stopL);
  box.addEventListener('dblclick', stopDbl);
  box.addEventListener('wheel', stopWh, { passive: true });

  const handle = {
    map,
    isActive: () => active,
    setActive,
    fitAll: doFit,             // 手动取全貌（供外部调用；v2.1.1）
    destroy() {
      ro.disconnect();
      clearTimeout(roT);
      box.removeEventListener('pointerdown', stopL);
      box.removeEventListener('dblclick', stopDbl);
      box.removeEventListener('wheel', stopWh);
      hint.remove();
      try { map.remove(); } catch { /* 已销毁 */ }
      if (box._imap === handle) box._imap = null;
    },
  };
  box._imap = handle;
  return handle;
}
