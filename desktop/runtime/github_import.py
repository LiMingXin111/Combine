"""Public GitHub acquisition broker and networkless instruction installation."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import PurePosixPath
import re
import urllib.error
import urllib.parse
import urllib.request
from .community import adapt, sha, summary
from .store import new_id, now

MAX_ENTRIES = 24
MAX_BYTES = 256000
LICENSE_NAMES = {'license', 'license.md', 'license.txt', 'copying', 'copying.md', 'copying.txt'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): raise ValueError('GitHub redirected this request. Use the canonical repository URL.')


def request(url, limit=3000000):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ['api.github.com', 'raw.githubusercontent.com'] or parsed.username or parsed.password or parsed.port:
        raise ValueError('Only public GitHub API and raw source URLs are supported.')
    req = urllib.request.Request(url, headers={'User-Agent': 'Combine-GitHub-Capabilities/1', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            value = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        if error.code in [403, 429]: raise ValueError('GitHub rate limit or access restriction. Retry later or import a smaller public folder.') from None
        if error.code == 404: raise ValueError('This public repository, revision or file was not found.') from None
        raise ValueError('GitHub could not complete the download.') from None
    if len(value) > limit: raise ValueError('The GitHub resource exceeds the download limit.')
    return value


def api(repository, path): return json.loads(request('https://api.github.com/repos/' + repository + path))
def raw(repository, commit, path): return request(f'https://raw.githubusercontent.com/{repository}/{commit}/' + urllib.parse.quote(path, safe='/'), MAX_BYTES)


def parse_url(value):
    if not isinstance(value, str) or len(value) > 1600: raise ValueError('Enter a public GitHub repository or Markdown file URL.')
    url = urllib.parse.urlsplit(value.strip())
    if url.scheme != 'https' or url.hostname != 'github.com' or url.username or url.password or url.port or url.query or url.fragment:
        raise ValueError('Use an HTTPS github.com URL without credentials, query parameters or fragments.')
    parts = urllib.parse.unquote(url.path).strip('/').split('/')
    if len(parts) < 2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', p) or p in ['.', '..'] for p in parts[:2]): raise ValueError('Invalid GitHub repository URL.')
    repository = '/'.join(parts[:2]).removesuffix('.git')
    ref, path = '', ''
    if len(parts) > 2:
        if parts[2] not in ['blob', 'tree'] or len(parts) < 4: raise ValueError('Use a repository, tree or blob URL.')
        ref, path = parts[3], '/'.join(parts[4:])
    return repository, ref, clean_path(path)


def clean_path(value):
    if isinstance(value, str): value = value.rstrip('/')
    if not isinstance(value, str) or len(value) > 800 or '\\' in value or '\x00' in value or value.startswith('/') or any(p in ['.', '..', ''] for p in value.split('/') if value):
        raise ValueError('Use a relative repository path without traversal.')
    return value


def license_id(text):
    if 'Permission is hereby granted, free of charge' in text and 'THE SOFTWARE IS PROVIDED "AS IS"' in text: return 'MIT'
    if 'Apache License' in text and 'Version 2.0' in text and 'http://www.apache.org/licenses/' in text: return 'Apache-2.0'
    if 'Redistribution and use in source and binary forms' in text and 'THIS SOFTWARE IS PROVIDED' in text:
        return 'BSD-3-Clause' if 'Neither the name' in text else 'BSD-2-Clause'
    raise ValueError('This license needs review. Import supports verified MIT, Apache-2.0 and BSD license texts.')


def metadata(content, path):
    title = PurePosixPath(path).parent.name if PurePosixPath(path).name.lower() == 'skill.md' else PurePosixPath(path).stem
    description = 'Imported open-source instructions. Uses supplied context.'
    # Read simple scalar metadata as data; no YAML constructors, imports or eval.
    if content.startswith('---'):
        front = content.split('---', 2)
        if len(front) == 3:
            for line in front[1].splitlines():
                match = re.match(r'^(name|description):\s*[\'\"]?(.+?)[\'\"]?\s*$', line)
                if match:
                    if match[1] == 'name': title = match[2]
                    elif match[2] not in ['>', '|', '>-', '|-']: description = match[2]
    return title.replace('-', ' ').strip()[:150] or 'Imported capability', description[:1000]


def dependencies(content):
    result = []
    if re.search(r'(?im)^\s*(allowed-tools|tools|mcpServers|mcp-servers)\s*:', content) or re.search(r'\bMCP\b', content): result.append('Tool or MCP integration')
    if re.search(r'(?i)(?:scripts/|\b(?:python|node|npx|uvx|pip install|npm install)\s+|\.py\b|\.js\b)', content): result.append('Scripts or runtime')
    if re.search(r'(?i)(?:references/|assets/|templates/|\]\((?!https?://|#)[^)]+\))', content): result.append('Additional repository files')
    return result


def inspect(store, values):
    from .privacy import assert_public_payload
    if set(values) - {'url', 'path', 'ref'}: raise ValueError('GitHub import accepts only a public URL, path and revision.')
    repository, url_ref, url_path = parse_url(values.get('url', ''))
    path = clean_path(values.get('path') or url_path)
    ref = values.get('ref') or url_ref
    if ref and (not isinstance(ref, str) or len(ref) > 160 or not re.fullmatch(r'[A-Za-z0-9_./-]+', ref) or '..' in ref): raise ValueError('Invalid GitHub revision.')
    assert_public_payload(store, {'repository': repository, 'path': path, 'ref': ref})
    info = api(repository, '')
    if info.get('private') or info.get('visibility', 'public') != 'public': raise ValueError('Only public repositories are supported.')
    commit = api(repository, '/commits/' + urllib.parse.quote(ref or info['default_branch'], safe=''))['sha']
    if not re.fullmatch('[0-9a-f]{40}', commit): raise ValueError('GitHub returned an invalid pinned commit.')
    tree = api(repository, '/git/trees/' + commit + '?recursive=1')
    if tree.get('truncated'): raise ValueError('This repository tree is incomplete; import a smaller repository.')
    files = {f['path']: f for f in tree.get('tree', []) if f.get('type') == 'blob' and f.get('mode') != '120000'}
    if path in files: names = [path] if path.lower().endswith('.md') else []
    else:
        names = sorted((name for name in files if (not path or name.startswith(path + '/')) and name.lower().endswith('.md')
                        and PurePosixPath(name).name.lower() not in ['readme.md', 'contributing.md', 'code_of_conduct.md', 'license.md', 'changelog.md']), key=lambda name: (PurePosixPath(name).name.lower() != 'skill.md', name))
    if not names: raise ValueError('No Markdown skills or agent definitions were found at this path.')
    cache = {}
    notice_cache = {}
    def source_license(name):
        folder = PurePosixPath(name).parent
        for parent in [folder, *folder.parents]:
            prefix = '' if str(parent) == '.' else str(parent) + '/'
            matches = sorted(n for n in files if n.startswith(prefix) and '/' not in n[len(prefix):] and PurePosixPath(n).name.lower() in LICENSE_NAMES)
            if matches:
                candidate = matches[0]
                if candidate not in cache:
                    text = raw(repository, commit, candidate).decode('utf-8-sig')
                    cache[candidate] = (license_id(text), text)
                return candidate, *cache[candidate]
        raise ValueError('No applicable license file was found. This entry needs license review.')
    def source_notices(name):
        folder = PurePosixPath(name).parent
        notices = []
        for parent in [folder, *folder.parents]:
            prefix = '' if str(parent) == '.' else str(parent) + '/'
            for candidate in sorted(n for n in files if n.startswith(prefix) and '/' not in n[len(prefix):] and PurePosixPath(n).name.lower() in ['notice', 'notice.txt', 'notice.md']):
                if candidate not in notice_cache:
                    text = raw(repository, commit, candidate).decode('utf-8-sig')
                    notice_cache[candidate] = {'path': candidate, 'text': text, 'sha256': sha(text.encode('utf-8'))}
                notices.append(notice_cache[candidate])
        return notices
    entries = []
    # Fetch licenses sequentially once; document downloads are independent.
    licenses = {}
    notices = {}
    for name in names[:MAX_ENTRIES]:
        try:
            licenses[name] = source_license(name)
            notices[name] = source_notices(name)
        except ValueError as error: licenses[name] = str(error)
    def load(name):
        entry = {'id': 'source_' + sha(name.encode())[:16], 'path': name, 'repository': repository, 'commit': commit,
                 'url': f'https://github.com/{repository}/blob/{commit}/{urllib.parse.quote(name, safe="/")}', 'kind': 'skill' if name.lower().endswith('skill.md') else 'expert'}
        if isinstance(licenses[name], str): return {**entry, 'available': False, 'title': name, 'reason': licenses[name]}
        try:
            data = raw(repository, commit, name)
            content = data.decode('utf-8-sig')
            if not content.strip() or len(content) > 60000: raise ValueError('This definition is empty or exceeds 60,000 characters. It needs a dedicated adapter.')
            title, description = metadata(content, name)
            license_path, spdx, text = licenses[name]
            front = content.split('---', 2) if content.startswith('---') else []
            declaration = re.search(r'(?m)^license:\s*[\'\"]?([^\n\r]+)', front[1]) if len(front) == 3 else None
            if declaration:
                declared = declaration[1].strip().strip('\'\"')
                if declared not in [spdx, 'Complete terms in LICENSE.txt'] or (declared == 'Complete terms in LICENSE.txt' and PurePosixPath(license_path).parent != PurePosixPath(name).parent):
                    raise ValueError('The file declares different license terms. Review its applicable license before importing.')
            return {**entry, 'title': title, 'description': description, 'content': content, 'content_sha256': sha(content.encode('utf-8')), 'sha256': sha(data), 'license': spdx, 'license_path': license_path, 'license_text': text,
                    'notices': notices.get(name, []), 'dependencies': dependencies(content), 'adaptation': 'AI guidance only; scripts, tools and auxiliary files are not executed.', 'available': True}
        except (ValueError, UnicodeError) as error: return {**entry, 'available': False, 'title': name, 'reason': str(error)}
    with ThreadPoolExecutor(max_workers=4) as pool: entries = list(pool.map(load, names[:MAX_ENTRIES]))
    preview = {'id': new_id('github_preview'), 'repository': repository, 'commit': commit, 'entries': entries, 'total': len(names), 'limited': len(names) > MAX_ENTRIES, 'created_at': now()}
    store.put('github_preview', preview['id'], preview)
    return {**preview, 'entries': [summary(entry) for entry in entries]}


def install(store, values):
    from .capabilities import prepare
    preview = store.get('github_preview', values.get('preview_id', ''))
    if not preview or now() - preview['created_at'] > 24 * 60 * 60 * 1000: raise ValueError('This preview expired. Inspect the repository again.')
    ids, mode = values.get('ids'), values.get('mode', 'api')
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ENTRIES or len(set(ids)) != len(ids): raise ValueError('Choose distinct preview entries to import.')
    provider = values.get('provider_id', '')
    if not isinstance(provider, str) or len(provider) > 128: raise ValueError('Invalid AI connection.')
    kind = values.get('kind')
    if kind and kind not in ['skill', 'agent', 'expert']: raise ValueError('Choose Skill, Agent or Expert.')
    entries = {e['id']: e for e in preview['entries']}
    prepared = []
    for key in ids:
        entry = deepcopy(entries.get(key))
        if not entry or not entry.get('available'): raise ValueError('This entry cannot be imported. Resolve its dependency or license issue first.')
        if sha(entry['content'].encode('utf-8')) != entry.get('content_sha256') or license_id(entry['license_text']) != entry['license']:
            raise ValueError('The cached source failed its integrity check. Inspect it again.')
        if any(sha(n['text'].encode('utf-8')) != n['sha256'] for n in entry.get('notices', [])): raise ValueError('The cached source notice failed its integrity check.')
        if kind: entry['kind'] = kind
        adapted = adapt(entry, mode, provider)
        cap = prepare(store, {k: adapted[k] for k in ['kind', 'title', 'description', 'definition']})
        cap.update(source=adapted['source'], private=mode == 'local', created_at=now())
        prepared.append(cap)
    # Install the selected batch atomically. Repeated requests reuse an exact source+config.
    installed = []
    from .graphs import digest
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for cap in prepared:
            fingerprint = digest({'source': cap['source'], 'kind': cap['kind'], 'definition': cap['definition']})
            old = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', ('github_install', fingerprint)).fetchone()
            previous = json.loads(old['data']) if old else None
            record = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', ('capability', previous['capability_id'])).fetchone() if previous else None
            if record:
                current = json.loads(record['data'])
                if digest(current['definition']) == digest(cap['definition']) and current['kind'] == cap['kind']:
                    installed.append(current); continue
            cap['updated_at'] = now()
            for entity, key, data in [('capability', cap['id'], cap), ('capability_version', cap['id'] + ':1', cap)]:
                db.execute('INSERT INTO entities VALUES(?,?,?,?)', (entity, key, json.dumps(data, ensure_ascii=False), cap['updated_at']))
            db.execute('INSERT INTO entities VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated', ('github_install', fingerprint, json.dumps({'capability_id': cap['id']}), now()))
            installed.append(cap)
    return {'capabilities': installed, 'count': len(installed), 'execution': 'AI guidance', 'upstream_code_executed': False}
