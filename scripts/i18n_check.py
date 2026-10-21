"""检查前端英文语言包是否覆盖全部 t('中文') 文案。用法：python scripts/i18n_check.py"""
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "js"
en = (root / "en.js").read_text(encoding="utf-8")
keys = set(re.findall(r"""(?:^|[\s,{])'([^']+)'\s*:|(?:^|[\s,{])([^\s'{},:]+)\s*:""", en, re.M))
have = {a or b for a, b in keys}
missing = set()
for f in root.glob("*.js"):
    if f.name in ("en.js", "i18n.js"):
        continue
    for m in re.finditer(r"""\bt\(\s*'((?:\.|[^'])*)'""", f.read_text(encoding="utf-8")):
        if re.search(r"[\u4e00-\u9fff]", m.group(1)) and m.group(1) not in have:
            missing.add(m.group(1))
for k in sorted(missing):
    print("missing:", k)
print(f"{len(missing)} missing")
sys.exit(1 if missing else 0)
