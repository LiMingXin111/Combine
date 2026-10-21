// 界面语言（F27）：以简体中文文案为键，英文词典见 en.js。切换立即生效，不打断任务。
import EN from './en.js';

const LANGUAGE_KEY = 'cb_ui_language';
let lang = localStorage.getItem(LANGUAGE_KEY) === 'zh' ? 'zh' : 'en';
document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
document.title = lang === 'en' ? 'Combine' : '康柏鹦';

export const LANG = () => lang;
export function setLang(v) {
  lang = v === 'zh-CN' || v === 'zh' ? 'zh' : v === 'system' ? ((navigator.language || 'en').toLowerCase().startsWith('zh') ? 'zh' : 'en') : 'en';
  localStorage.setItem(LANGUAGE_KEY, lang);
  document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
  document.title = lang === 'en' ? 'Combine' : '康柏鹦';
}
/** t('中文文案', {变量}) —— 英文界面下查词典，缺词条时回退中文并在控制台提示一次。 */
const missing = new Set();
const templates = Object.entries(EN).filter(([key]) => /\{\w+\}/.test(key)).map(([key, value]) => {
  const names = [];
  const expression = key.split(/(\{\w+\})/).map(part => {
    const name = part.match(/^\{(\w+)\}$/);
    if (name) { names.push(name[1]); return '(.+?)'; }
    return part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }).join('');
  return { pattern: new RegExp('^' + expression + '$', 's'), names, value };
});
export function t(zh, vars) {
  let s = zh;
  if (lang === 'en') {
    if (EN[zh] != null) s = EN[zh];
    else if (/[\u4e00-\u9fff]/.test(zh)) {
      const entry = templates.find(item => item.pattern.test(zh));
      if (entry) {
        const found = zh.match(entry.pattern), values = Object.fromEntries(entry.names.map((name, i) => [name, EN[found[i + 1]] ?? found[i + 1]]));
        s = entry.value.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? '');
      } else if (!missing.has(zh)) { missing.add(zh); console.debug('[i18n] missing:', zh); }
    }
  }
  // Normalize known legacy UI keys before inserting user values. User content
  // such as node titles must retain its original wording.
  if (lang === 'zh' && Object.hasOwn(EN, zh)) s = s.replaceAll('步骤', '节点').replaceAll('单步', '单节点').replaceAll('这一步', '这个节点').replaceAll('每一步', '每个节点').replaceAll('哪一步', '哪个节点').replaceAll('{d}/{n} 步', '{d}/{n} 个节点');
  if (vars) s = s.replace(/\{(\w+)\}/g, (_, k) => (vars[k] ?? ''));
  return s;
}
/** 数据自带中英两份时按当前语言取。 */
export const L = (zh, en) => (lang === 'en' ? en || zh : zh || en);
export const missingKeys = () => [...missing];
