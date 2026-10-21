"""Publish complete, syntax-checked, immutable Python revisions for development.

Each broker and all of its workers use the same revision. Never change the
packaged release runtime or replace modules underneath an active request.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

BASE = Path(__file__).resolve().parents[1]


def publish(source, resources, manifest):
    files = {p.relative_to(source).as_posix(): p.read_bytes()
             for p in sorted(source.rglob('*')) if p.is_file() and '__pycache__' not in p.parts
             and (p.suffix == '.py' or (p.relative_to(source).parts[0] == 'community' and p.suffix in ['.json', '.md', '.txt']))}
    for name in ('__init__.py', 'launcher.py', 'entry.py'):
        if name not in files:
            raise ValueError('The development runtime is incomplete.')
    digest = hashlib.sha256()
    if 'community/catalog.json' in files:
        catalog = json.loads(files['community/catalog.json'])
        for entry in catalog['entries']:
            for field, checksum in [('file', 'sha256'), ('license_file', 'license_sha256')]:
                data = files.get('community/' + entry[field])
                if data is None or hashlib.sha256(data).hexdigest() != entry[checksum]:
                    raise ValueError('The community source or license snapshot is incomplete.')
    for name, content in files.items():
        # Compile in memory: a bad edit cannot replace the last usable revision.
        if name.endswith('.py'): compile(content, name, 'exec')
        elif name.endswith('.json'): json.loads(content)
        digest.update(name.encode() + b'\0' + content + b'\0')
    revision = digest.hexdigest()
    directory = resources / ('runtime-dev-' + revision)
    resources.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(manifest.read_text('utf-8')) if manifest.is_file() else {}
    if not directory.exists():
        stage = Path(tempfile.mkdtemp(prefix='runtime-dev-stage-', dir=resources))
        try:
            for name, content in files.items():
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            stage.rename(directory)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    result = {'revision': revision, 'directory': directory.name,
              'published_at': datetime.now(timezone.utc).isoformat()}
    changed = previous.get('revision') != revision
    if changed:
        temp = manifest.with_suffix('.tmp')
        temp.write_text(json.dumps(result), encoding='utf-8')
        os.replace(temp, manifest)
    return {**result, 'changed': changed}


if __name__ == '__main__':
    try:
        print(json.dumps(publish(BASE / 'runtime', BASE / 'src-tauri/resources',
                                 BASE / '.dev/runtime.json')))
    except SyntaxError as error:
        print(json.dumps({'error': 'SyntaxError', 'file': error.filename, 'line': error.lineno}))
