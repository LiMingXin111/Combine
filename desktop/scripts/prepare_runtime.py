"""Bundle Python locally; end users never need a separate Python installation."""
from pathlib import Path
import shutil
import sys
import json
import filecmp

BASE = Path(__file__).resolve().parents[1]
TARGET = BASE / 'src-tauri/resources'


def copy_changed(source, destination):
    if not Path(destination).is_file() or not filecmp.cmp(source, destination, shallow=False):
        shutil.copy2(source, destination)
    return destination


def prepare():
    source = Path(sys.base_prefix)
    assert sys.version_info[:2] == (3, 12), 'Build the bundled runtime with Python 3.12.'
    python = TARGET / 'python'
    python.mkdir(parents=True, exist_ok=True)
    for name in ['python.exe', 'python3.dll', 'python312.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt']:
        if (source / name).exists(): copy_changed(source / name, python / name)
    for name in ['DLLs', 'Lib']:
        shutil.copytree(source / name, python / name, dirs_exist_ok=True, copy_function=copy_changed,
                        ignore=shutil.ignore_patterns('__pycache__', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip', 'site-packages'))
    shutil.copytree(BASE / 'runtime', TARGET / 'runtime', dirs_exist_ok=True, copy_function=copy_changed,
                    ignore=shutil.ignore_patterns('__pycache__', '.appcontainer-*'))
    for folder in [python, TARGET / 'runtime']:
        (folder / '.appcontainer-RX').unlink(missing_ok=True)
    print(json.dumps({'python': str(python / 'python.exe'), 'version': sys.version.split()[0]}))


if __name__ == '__main__': prepare()
