"""Create a source-only deployment archive. Credentials and user data are never included."""
import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ("Dockerfile", ".dockerignore", "requirements.txt", "requirements-lock.txt", "compose.production.yml", "railway.json")
DIRECTORIES = ("backend", "frontend", "scripts", "deploy")


def build_release(output, root=ROOT):
    output, root = Path(output).resolve(), Path(root).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    candidates = [root / name for name in FILES]
    for name in DIRECTORIES:
        candidates.extend(p for p in (root / name).rglob("*") if p.is_file())
    included = []
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(candidates):
            relative = path.relative_to(root)
            if path.is_symlink() or root not in path.resolve().parents or "__pycache__" in relative.parts or path.suffix in (".pyc", ".tar", ".gz", ".zip"):
                continue
            if any(part.startswith(".env") or part == ".admin-bootstrap.json" for part in relative.parts):
                continue
            if not path.is_file():
                raise ValueError(f"missing deployment source: {relative}")
            member = archive.gettarinfo(str(path), arcname=relative.as_posix())
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.mode = 0o755 if path.suffix == ".sh" else 0o644
            with path.open("rb") as source:
                archive.addfile(member, source)
            included.append(relative.as_posix())
    with output.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{checksum}  {output.name}\n", encoding="utf-8")
    return included, checksum


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT.parent / "outputs" / ("combine-production-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".tar.gz")))
    args = parser.parse_args()
    members, checksum = build_release(args.output)
    print(f"Archive: {Path(args.output).resolve()}\nFiles: {len(members)}\nSHA256: {checksum}")
