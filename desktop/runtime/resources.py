"""Network broker: fixed public downloads only; never accepts workspace content or URLs."""
from pathlib import Path
import hashlib
import json
import os
import urllib.request
import zipfile
from .store import Store, now
from .leases import lease, Busy

REVISION = '1cd86afb9a95c410a6038ab3b40d8b578c892266'
CATALOG = [
    {'id': 'llama-cpu', 'title': 'llama.cpp CPU runtime', 'description': 'On-device execution without a GPU or a separate model service.', 'version': 'b11739',
     'downloads': [{'name': 'llama-cpu.zip', 'url': 'https://github.com/ggml-org/llama.cpp/releases/download/b11739/llama-b11739-bin-win-cpu-x64.zip', 'sha256': 'b1e0a14abdb4665c1b821b377ba063b221cec62882c373065cf8fb672988d3ff', 'size': 19708508}]},
    {'id': 'qwen3vl-4b', 'title': 'Qwen3-VL 4B', 'description': 'A quantized local text and vision model. Prepared once, available offline.', 'version': REVISION, 'license': 'Apache-2.0',
     'downloads': [
         {'name': 'Qwen3VL-4B-Instruct-Q4_K_M.gguf', 'url': 'https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/' + REVISION + '/Qwen3VL-4B-Instruct-Q4_K_M.gguf', 'sha256': '66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a', 'size': 2497281664},
         {'name': 'mmproj-Qwen3VL-4B-Instruct-F16.gguf', 'url': 'https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/' + REVISION + '/mmproj-Qwen3VL-4B-Instruct-F16.gguf', 'sha256': '256f3a43bd4205ffef48d6b92715e1e70b5b0e9aef06522584967513a9985331', 'size': 836180256}
     ]}
]


def catalog(store):
    for resource in store.list('resource'):
        if resource.get('status') != 'downloading': continue
        try:
            with lease(store.root / 'resources' / resource['id'] / 'download.lock'):
                current = store.get('resource', resource['id'])
                if current.get('status') == 'downloading':
                    store.update('resource', resource['id'], {'status': 'missing', 'message': 'Download interrupted. Retry to resume the saved data.'})
        except Busy: pass
    return [{**{k: v for k, v in d.items() if k != 'downloads'}, 'download_size': sum(f['size'] for f in d['downloads']), 'status': 'missing', **(store.get('resource', d['id']) or {})} for d in CATALOG]


def local_ai_status(store, resources):
    """Report the installed local assistant, using the executor's file metadata checks."""
    requirements = []
    for key in ['llama-cpu', 'qwen3vl-4b']:
        resource = next(r for r in resources if r['id'] == key)
        state = resource.get('status', 'missing')
        if state == 'ready':
            try:
                files = resource.get('files', [])
                valid = bool(files) and all(store.local_file(f['path']).is_file() and
                    store.local_file(f['path']).stat().st_size == f['size'] and
                    store.local_file(f['path']).stat().st_mtime_ns == f.get('mtime_ns') for f in files)
            except (OSError, ValueError, KeyError):
                valid = False
            if not valid: state = 'needs_verification'
        requirements.append({'id': key, 'title': resource['title'], 'status': state})
    return {'model_id': 'qwen3vl-4b', 'model': next(r['title'] for r in resources if r['id'] == 'qwen3vl-4b'),
            'ready': all(r['status'] == 'ready' for r in requirements), 'requirements': requirements}


def checksum(path):
    value = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''): value.update(block)
    return value.hexdigest()


def install(root, key):
    if key not in {r['id'] for r in CATALOG}: raise ValueError('This resource is not in the approved download catalog.')
    with lease(Path(root).resolve() / 'resources' / key / 'download.lock'):
        return _install(root, key)


def _install(root, key):
    store = Store(Path(root))
    resource = next((r for r in CATALOG if r['id'] == key), None)
    if not resource: raise ValueError('This resource is not in the approved download catalog.')
    directory = store.root / 'resources' / key
    directory.mkdir(parents=True, exist_ok=True)
    total = sum(f['size'] for f in resource['downloads'])
    record = {'title': resource['title'], 'version': resource['version'], 'status': 'downloading', 'downloaded_bytes': 0, 'download_size': total, 'control': 'continue', 'files': []}
    store.put('resource', key, record)
    done = 0
    try:
        for item in resource['downloads']:
            target = directory / item['name']; partial = target.with_suffix(target.suffix + '.partial')
            if not target.exists() or checksum(target) != item['sha256']:
                offset = partial.stat().st_size if partial.exists() else 0
                headers = {'User-Agent': 'Combine-Desktop/0.1', 'Accept-Encoding': 'identity'}
                if offset: headers['Range'] = 'bytes=' + str(offset) + '-'
                request = urllib.request.Request(item['url'], headers=headers)
                with urllib.request.urlopen(request, timeout=60) as response:
                    if offset and response.status != 206: offset = 0
                    count = offset
                    with partial.open('ab' if offset else 'wb') as output:
                        while True:
                            if store.get('resource', key).get('control') == 'cancel': raise InterruptedError('Download cancelled. Downloaded data is kept for retry.')
                            block = response.read(4 * 1024 * 1024)
                            if not block: break
                            output.write(block); count += len(block)
                            if count > item['size']: raise ValueError('The download exceeded its declared size.')
                            store.update('resource', key, {'downloaded_bytes': done + count})
                if count != item['size'] or checksum(partial) != item['sha256']: raise ValueError('SHA-256 verification failed. The resource was not activated.')
                partial.replace(target)
            done += item['size']
            if target.suffix == '.zip':
                with zipfile.ZipFile(target) as archive:
                    for info in archive.infolist():
                        dest = (directory / info.filename).resolve()
                        if not dest.is_relative_to(directory.resolve()) or (info.external_attr >> 16) & 0o170000 == 0o120000: raise ValueError('The runtime archive contains an unsafe path.')
                    archive.extractall(directory)
                executables = list(directory.rglob('llama-cli.exe'))
                if not executables: raise ValueError('The verified archive does not contain llama-cli.exe.')
                record['executable'] = executables[0].relative_to(store.root).as_posix()
                vision = list(directory.rglob('llama-mtmd-cli.exe'))
                if vision: record['vision_executable'] = vision[0].relative_to(store.root).as_posix()
            elif item['name'].startswith('mmproj'): record['projector_path'] = target.relative_to(store.root).as_posix()
            else: record['model_path'] = target.relative_to(store.root).as_posix()
        files = []
        for path in directory.rglob('*'):
            if path.is_file() and path.name != 'download.lock' and not path.name.startswith('.appcontainer'):
                files.append({'path': path.relative_to(store.root).as_posix(), 'size': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns, 'sha256': checksum(path)})
        record.update(status='ready', files=files, downloaded_bytes=total, verified_at=now())
        return store.put('resource', key, record)
    except Exception as error:
        store.update('resource', key, {'status': 'missing' if isinstance(error, InterruptedError) else 'error', 'message': str(error)})
        raise


def verify(root, key):
    if key not in {r['id'] for r in CATALOG}: raise ValueError('This resource is not in the approved download catalog.')
    with lease(Path(root).resolve() / 'resources' / key / 'download.lock'):
        return _verify(root, key)


def _verify(root, key):
    store = Store(Path(root)); resource = store.get('resource', key)
    if not resource or not resource.get('files'): raise ValueError('Prepare this resource first.')
    for item in resource['files']:
        path = store.local_file(item['path'])
        if not path.exists() or path.stat().st_size != item['size'] or checksum(path) != item['sha256']:
            store.update('resource', key, {'status': 'error', 'message': 'A resource file is missing or changed.'})
            raise ValueError('A resource file is missing or changed. Reinstall it before use.')
        item['mtime_ns'] = path.stat().st_mtime_ns
    return store.update('resource', key, {'status': 'ready', 'files': resource['files'], 'verified_at': now()})
