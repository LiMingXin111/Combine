"""Back up the live SQLite snapshot and workspace files; keep deployment secrets separate."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import config


def backup(keep=7):
    if keep < 1:
        raise ValueError("keep must be positive")
    if not config.DB_PATH.is_file():
        raise ValueError("database does not exist")
    target_dir = config.DATA_DIR / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = "combine-full-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f") + ".tar.gz"
    output = target_dir / filename
    output.touch(mode=0o600, exist_ok=False)
    count = 0
    with tempfile.TemporaryDirectory(prefix="combine-backup-") as temporary:
        database = Path(temporary) / "combine.db"
        source = sqlite3.connect(config.DB_PATH.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(database)
        try:
            source.backup(destination)
        finally:
            source.close()
            destination.close()
        with tarfile.open(output, "w:gz") as archive:
            archive.add(database, arcname="combine.db")
            workspace = config.DATA_DIR / "workspace"
            if workspace.exists():
                base = workspace.resolve()
                for path in sorted(workspace.rglob("*")):
                    if path.is_file() and not path.is_symlink() and base in path.resolve().parents:
                        archive.add(path, arcname="workspace/" + path.relative_to(workspace).as_posix(), recursive=False)
                        count += 1
            metadata = Path(temporary) / "metadata.json"
            metadata.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "workspace_files": count,
                                             "master_key_included": False, "version": config.VERSION}), encoding="utf-8")
            archive.add(metadata, arcname="metadata.json")
    for previous in sorted(target_dir.glob("combine-full-*.tar.gz"))[:-keep]:
        previous.unlink()
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", type=int, default=7)
    args = parser.parse_args()
    print(backup(args.keep))
