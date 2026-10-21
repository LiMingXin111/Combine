"""在线备份 SQLite 数据库（服务运行中也可执行），并只保留最近 N 份。

用法：
    python scripts/backup.py                 # 备份到 data/backups/
    python scripts/backup.py --keep 14       # 保留 14 份
    python scripts/backup.py --restore data/backups/combine-20261006-013000.db   # 恢复（需先停止服务）

说明：
- 使用 sqlite3 的 backup API，得到一致性快照，不会读到写了一半的数据；
- 备份文件包含加密后的 API Key 密文，恢复时必须使用同一个 SECRETS_MASTER_KEY（或同一个 data/master.key），
  否则已保存的 Key 无法解密，需要用户重新填写；
- 备份文件与主密钥不要放在同一个地方。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import config  # noqa: E402


def backup(keep: int) -> Path:
    out_dir = config.DATA_DIR / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"combine-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(str(config.DB_PATH))
    try:
        with sqlite3.connect(str(dst)) as out:
            src.backup(out)
    finally:
        src.close()
    olds = sorted(out_dir.glob("combine-*.db"))
    for p in olds[:-keep] if keep > 0 else []:
        p.unlink(missing_ok=True)
    return dst


def restore(path: str) -> None:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"备份文件不存在：{p}")
    with sqlite3.connect(str(p)) as c:                       # 先确认是完整可读的数据库
        if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("备份文件校验失败，已放弃恢复")
    if config.DB_PATH.exists():
        shutil.copy2(config.DB_PATH, config.DB_PATH.with_suffix(".before-restore.db"))
    for suffix in ("-wal", "-shm"):                         # 旧的 WAL 文件会把恢复后的数据覆盖回去
        Path(str(config.DB_PATH) + suffix).unlink(missing_ok=True)
    shutil.copy2(p, config.DB_PATH)
    print(f"已恢复到 {config.DB_PATH}（原库另存为 .before-restore.db）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=int(config.env("BACKUP_KEEP", "7") or 7))
    ap.add_argument("--restore")
    a = ap.parse_args()
    if a.restore:
        restore(a.restore)
    else:
        print(backup(a.keep))
