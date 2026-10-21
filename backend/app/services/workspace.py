"""任务工作区（F32）：Agent 可以真正读写文件的沙箱目录。

- 目录：DATA_DIR/workspace/<user_id>/（按用户隔离，INV-08）；
- 路径安全：只接受相对路径，解析后必须仍在该用户工作区内（防 `..` 与符号链接逃逸）；
- 只开放「文件」级操作：不创建/删除目录树，删除只针对单个文件；
- 命令入口仅解释 echo/pwd/ls/dir/cat/type/head，不启动 shell 或任意子进程；
- 云端版一律不可用（core/policy.py 在能力层已拦截，这里再兜一层）。
"""
from __future__ import annotations

import shlex
from pathlib import Path

from .. import config

ROOT = config.DATA_DIR / "workspace"
MAX_WRITE_BYTES = 256 * 1024
MAX_READ_CHARS = 4000


def root_for(user_id: str) -> Path:
    d = ROOT / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "files").mkdir(exist_ok=True)
    return d


def resolve(user_id: str, rel: str, *, must_exist: bool = False) -> Path | None:
    """把模型给的文件名解析为工作区内的绝对路径；越界或非法返回 None。"""
    base = root_for(user_id) / "files"
    name = str(rel or "").strip().replace("\\", "/")
    if not name or name.startswith("/") or ":" in name or "\x00" in name:
        return None                                   # 拒绝绝对路径 / 盘符 / 空名
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or len(parts) > 8:
        return None
    target = base.joinpath(*parts)
    try:
        real_base = base.resolve()
        real = target.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if real != real_base and real_base not in real.parents:
        return None                                   # 软链接 / 大小写等造成的越界
    if must_exist and not target.is_file():
        return None
    return target


def _rel(user_id: str, p: Path) -> str:
    return p.relative_to(root_for(user_id) / "files").as_posix()


def task_path(task_id: str, version: int, path: str) -> str:
    name = str(path or "").strip().replace("\\", "/")
    prefix = f"任务/{task_id}/v{version}/"
    return name if name.startswith(prefix) else prefix + name


def snapshot(user_id: str, task_id: str, version: int, path: str) -> str | None:
    """Immutable copy for historical artifact cards, including legacy workspace paths."""
    from ..util import new_id
    import shutil
    source = resolve(user_id, path, must_exist=True)
    if source is None:
        return None
    rel = f"交付/{task_id}/v{version}/{new_id('file')}/{source.name}"
    target = resolve(user_id, rel)
    if target is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return rel


def write(user_id: str, path: str, content: str) -> dict:
    p = resolve(user_id, path)
    if p is None:
        return {"error": "PATH_DENIED", "message": "只允许写入任务工作区内的相对路径"}
    data = str(content or "")
    if len(data.encode("utf-8")) > MAX_WRITE_BYTES:
        return {"error": "TOO_LARGE", "message": f"单个文件最大 {MAX_WRITE_BYTES // 1024} KB"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8")
    except OSError as e:
        return {"error": "WRITE_FAILED", "message": str(e)[:160]}
    return {"written": _rel(user_id, p), "bytes": len(data.encode("utf-8"))}


def delete(user_id: str, path: str) -> dict:
    p = resolve(user_id, path, must_exist=True)
    if p is None:
        return {"error": "NOT_FOUND", "message": "文件不存在或路径不合法（只允许删除工作区内的单个文件）"}
    try:
        p.unlink()
    except OSError as e:
        return {"error": "DELETE_FAILED", "message": str(e)[:160]}
    return {"deleted": _rel(user_id, p)}


def list_files(user_id: str) -> dict:
    base = root_for(user_id) / "files"
    items = []
    for f in sorted(base.rglob("*")):
        if f.is_file():
            st = f.stat()
            items.append({"path": f.relative_to(base).as_posix(), "bytes": st.st_size, "mtime": int(st.st_mtime * 1000)})
        if len(items) >= 100:
            break
    return {"items": items, "count": len(items)}


def read(user_id: str, path: str) -> dict:
    p = resolve(user_id, path, must_exist=True)
    if p is None:
        return {"error": "NOT_FOUND", "message": "文件不存在或路径不合法"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"error": "READ_FAILED", "message": str(e)[:160]}
    clipped = text[:MAX_READ_CHARS]
    return {"path": _rel(user_id, p), "chars": len(text), "content": clipped,
            "truncated": len(text) > len(clipped)}


def run_command(user_id: str, command: str) -> dict:
    cmd = str(command or "").strip()
    if not cmd:
        return {"error": "EMPTY_COMMAND"}
    if len(cmd) > 500:
        return {"error": "TOO_LONG", "message": "命令过长"}
    # This interface is deliberately interpreted in-process. A shell's cwd is not a sandbox.
    if any(c in cmd for c in "\r\n;&|<>`$"):
        return {"error": "COMMAND_DENIED", "message": "仅支持工作区命令 echo / pwd / ls / dir / cat / type / head，不支持 shell 语法"}
    try:
        argv = shlex.split(cmd.replace("\\", "/"))
    except ValueError:
        return {"error": "COMMAND_DENIED", "message": "命令引号不完整"}
    name, args = argv[0].lower(), argv[1:]
    if name == "echo":
        output = " ".join(args) + "\n"
    elif name == "pwd" and not args:
        output = ".\n"
    elif name in ("ls", "dir") and not args:
        output = "\n".join(f["path"] for f in list_files(user_id)["items"])
    elif name in ("cat", "type", "head") and len(args) == 1:
        target = resolve(user_id, args[0], must_exist=True)
        if target is None:
            return {"error": "PATH_DENIED", "message": "只允许读取工作区内的相对文件路径"}
        try:
            with target.open(encoding="utf-8", errors="replace") as source:
                output = source.read(MAX_READ_CHARS + 1)
        except OSError:
            return {"error": "READ_FAILED"}
    else:
        return {"error": "COMMAND_DENIED", "message": "仅支持 echo / pwd / ls / dir / cat / type / head；本接口不启动任意程序"}
    return {"command": cmd, "exit_code": 0, "stdout": output[:MAX_READ_CHARS], "stderr": "",
            "truncated": len(output) > MAX_READ_CHARS}


def unavailable_reason() -> str:
    if config.DEPLOY_MODE != "self_hosted":
        return "云端版不开放本机文件与命令"
    return "该能力未开启"


def delete_user_files(user_id: str) -> None:
    import shutil
    base = ROOT.resolve()
    target = (base / user_id).resolve()
    if target == base or target.parent != base:
        raise ValueError("invalid workspace cleanup target")
    if target.exists():
        shutil.rmtree(target)
