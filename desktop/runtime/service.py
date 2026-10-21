"""Versioned local RPC surface. No network libraries and no legacy .env access."""
from __future__ import annotations
from pathlib import Path
from copy import deepcopy
from contextlib import closing
import base64
import csv
import hashlib
import io
import json
import re
import shutil
import sqlite3
import zipfile
from .store import Store, new_id, now
from .catalog import TYPES, BY_ID
from .graphs import validate, offline_report, impact, wrap, unwrap, digest
from .engine import Engine, Waiting
from .documents import read_document


def search_knowledge(store, query, limit=5):
    terms = set(re.findall(r'\w+', query.lower()))
    if not terms: return []
    hits = []
    for item in store.list('knowledge'):
        text = (item['title'] + ' ' + item['content']).lower()
        score = sum(text.count(term) for term in terms)
        if score: hits.append({**item, 'score': score})
    return sorted(hits, key=lambda h: (-h['score'], h['id']))[:min(50, max(1, int(limit)))]


def template(kind, title):
    def node(key, type_id, x, y, config=None):
        return {'id': key, 'type_id': type_id, 'type_version': '1', 'title': BY_ID[type_id]['title'], 'position': {'x': x, 'y': y}, 'config': deepcopy(config if config is not None else BY_ID[type_id]['defaults'])}
    def edge(source, target, port='text', target_port=None):
        return {'id': new_id('edge'), 'source': source, 'target': target, 'sourceHandle': port, 'targetHandle': target_port or port}
    graph = {'id': new_id('workflow'), 'title': title or 'Untitled workflow', 'version': 1, 'nodes': [], 'edges': [], 'created_at': now()}
    if kind == 'table':
        graph['nodes'] = [node('source', 'input.table', 60, 80), node('total', 'data.aggregate', 400, 20), node('chart', 'output.chart', 400, 240), node('report', 'output.document', 740, 20)]
        graph['edges'] = [edge('source', 'total', 'table'), edge('source', 'chart', 'table'), edge('total', 'report')]
    elif kind == 'text':
        graph['nodes'] = [node('source', 'input.text', 60, 100, {'text': 'Write your own content here. This workflow runs entirely on your computer.'}), node('transform', 'transform.text', 400, 100), node('report', 'output.document', 740, 100)]
        graph['edges'] = [edge('source', 'transform'), edge('transform', 'report')]
    elif kind in ['ai', 'local_ai']:
        graph['nodes'] = [node('source', 'input.prompt' if kind == 'ai' else 'input.text', 60, 100, {'text': ''}), node('ai', 'model.api' if kind == 'ai' else 'model.local', 490, 100), node('report', 'output.document', 920, 100)]
        graph['edges'] = [edge('source', 'ai'), edge('ai', 'report')]
    elif kind != 'blank': raise ValueError('Unknown workflow template.')
    return graph


class Service:
    def __init__(self, root, remote_executor=None, cloud_client=None):
        self.store = Store(root); self.remote_executor = remote_executor; self.cloud_client = cloud_client

    def dispatch(self, method, p):
        if not isinstance(p, dict): raise ValueError('Parameters must be an object.')
        allowed = {
            'workspace.snapshot': self.snapshot, 'workspace.settings': self.settings,
            'workflow.create': self.create, 'workflow.save': self.save, 'workflow.preview': self.preview, 'workflow.layout': self.layout, 'workflow.integrate': self.integrate,
            'workflow.delete': self.delete_workflow, 'workflow.duplicate': self.duplicate,
            'workflow.wrap': self.wrap, 'workflow.unwrap': self.unwrap, 'workflow.check': self.check,
            'workflow.history': self.history, 'workflow.publish': self.publish, 'workflow.from_library': self.from_library,
            'workflow.ai_plan': self.ai_plan,
            'run.start': self.start, 'run.control': self.control, 'run.detail': self.run_detail,
            'run.events': self.run_events, 'run.edit': self.edit_output, 'run.export': self.export,
            'asset.import': self.import_asset, 'asset.delete': self.delete_asset,
            'knowledge.save': self.save_knowledge, 'knowledge.search': self.search, 'knowledge.delete': self.delete_knowledge, 'knowledge.get': self.get_knowledge,
            'knowledge.archive': self.archive,
            'notification.read': self.read_notification, 'workspace.backup': self.backup,
            'workspace.restore': self.restore, 'workspace.diagnostics': self.diagnostics,
            'workspace.checkpoint': self.checkpoint,
            'node.execute': self.execute_node, 'asset.preview': self.asset_preview,
            'knowledge.classify': self.classify_knowledge, 'ai.local_completion': self.local_completion,
            'ai.conversation': self.get_conversation, 'workflow.undo': self.undo,
            'capability.save': self.save_capability, 'capability.delete': self.delete_capability,
            'community.resolve': self.resolve_community, 'github.install': self.install_github,
            'sharing.preview': self.share_preview, 'sharing.approved': self.share_approved, 'sharing.import': self.import_shared,
        }
        if method not in allowed: raise ValueError('Unknown or unavailable desktop operation: ' + str(method))
        return allowed[method](p)

    def share_preview(self, p):
        from .sharing import prepare
        return prepare(self.store, p)

    def share_approved(self, p):
        from .sharing import approved
        return approved(self.store, p.get('token', ''))

    def import_shared(self, p):
        from .sharing import import_template
        return import_template(self.store, p)

    def snapshot(self, p):
        from .community import summaries
        from .capabilities import list_capabilities
        from .leases import recover_runs
        recover_runs(self.store)
        workflows = [w for w in self.store.list('workflow') if not w.get('deleted')]
        from .resources import catalog, local_ai_status
        resources = catalog(self.store)
        assets = [{**a, 'parsed': {**a['parsed'], 'text': '', 'tables': [], 'parts': [{'location': part['location']} for part in a['parsed']['parts']]}} for a in self.store.list('asset')]
        knowledge = [{**k, 'content': k['content'][:200], 'preview': True} for k in self.store.list('knowledge')]
        runs = [{k: v for k, v in r.items() if k != 'graph'} for r in self.store.list('run')[:100]]
        from .providers import list_providers
        ai_settings = self.store.get('setting', 'ai', {'mode': 'api', 'auto_classify': True})
        from .web import status as web_status
        return {'protocol': 2, 'product': 'Combine', 'version': '0.2.0',
                'workspace': {'id': digest(str(self.store.root))[:12], 'name': self.store.get('setting', 'general', {}).get('workspace_name', 'My workspace'), 'location': str(self.store.root), 'execution': 'local', 'sandbox': 'appcontainer'},
                'types': TYPES, 'workflows': workflows, 'runs': runs,
                'assets': assets, 'knowledge': knowledge, 'library': self.store.list('library'),
                'resources': resources, 'settings': self.store.get('setting', 'general', {'theme': 'light', 'language': 'en', 'output_language': 'en', 'workspace_name': 'My workspace'}),
                'ai': {**ai_settings, 'providers': list_providers(self.store), 'local': local_ai_status(self.store, resources), 'web': web_status(self.store)}, 'capabilities': list_capabilities(self.store),
                'community': summaries(),
                'conversations': [{k: v for k, v in c.items() if k != 'messages'} for c in self.store.list('conversation')],
                'usage': self.store.list('usage')[:100],
                'notifications': self.store.list('notification')[:100]}

    def save_capability(self, p):
        from .capabilities import save
        return save(self.store, p)

    def resolve_community(self, p):
        from .community import resolve
        return resolve(self.store, p)

    def install_github(self, p):
        from .github_import import install
        return install(self.store, p)

    def delete_capability(self, p):
        from .capabilities import remove
        return remove(self.store, p)

    def settings(self, p):
        current = self.store.get('setting', 'general', {})
        if p.get('reset'): return self.store.put('setting', 'general', {'theme': 'light', 'language': 'en', 'output_language': 'en', 'workspace_name': 'My workspace'})
        values = p.get('values', {})
        allowed = {'theme', 'output_language', 'workspace_name', 'notifications'}
        if set(values) - allowed: raise ValueError('Unknown or retired setting. Task budgets and private data sharing are unavailable.')
        if values.get('theme', 'dark') not in ['dark', 'light']: raise ValueError('Choose a valid theme.')
        if values.get('output_language', 'en') not in ['en', 'zh', 'follow_input']: raise ValueError('Choose a valid output language.')
        return self.store.put('setting', 'general', {**current, **values, 'language': 'en'})

    def workflow(self, key):
        item = self.store.get('workflow', key)
        if not item or item.get('deleted'): raise ValueError('The workflow was not found.')
        return item

    def create(self, p):
        graph = template(p.get('template', 'blank'), str(p.get('title', '')).strip())
        if p.get('conversation_id') is not None:
            cid = p['conversation_id']
            if not isinstance(cid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', cid): raise ValueError('Invalid conversation identifier.')
            return self.store.create_conversation_workflow(cid, graph)
        self.store.put('workflow_version', graph['id'] + ':1', graph)
        return self.store.put('workflow', graph['id'], graph)

    def save(self, p):
        graph = deepcopy(p['graph']); old = self.workflow(graph['id'])
        if graph.get('version') != old['version']: raise ValueError('This draft is out of date. Reopen the latest version before applying changes.')
        old_nodes = {n['id']: n for n in old['nodes']}
        for item in graph['nodes']:
            if item['type_id'] == 'workflow.composite' and item['id'] in old_nodes and old_nodes[item['id']]['type_id'] == 'workflow.composite':
                from .graphs import execution_node
                a, b = execution_node(old_nodes[item['id']]), execution_node(item)
                previous = a['config']['definition'].pop('version', 1)
                b['config']['definition'].pop('version', None)
                item['config']['definition']['version'] = previous + int(a != b)
        report = impact(old, graph)
        # Incomplete drafts are savable; execution still requires validation.
        changed = bool(report['changed'] or report['added'] or report['removed'] or graph.get('annotations', []) != old.get('annotations', []) or graph['title'] != old['title'] or graph.get('final_outputs') != old.get('final_outputs'))
        graph['version'] = old['version'] + int(changed)
        graph['created_at'] = old['created_at']
        result = self.store.apply_workflow(graph, old['version'], changed)
        return {'workflow': result, 'impact': report, 'check': offline_report(graph, self.store)}

    def preview(self, p):
        graph = p['graph']
        return {'impact': impact(self.workflow(graph['id']), graph), 'check': offline_report(graph, self.store), 'read_only': True}

    def integrate(self, p):
        from .delivery import integrate
        graph = integrate(p['graph'], p.get('source_ids'), p.get('output_id'))
        return {'graph': graph, 'check': offline_report(graph, self.store), 'read_only': True}

    def layout(self, p):
        graph = self.workflow(p['id'])
        positions = p.get('positions', {})
        for item in graph['nodes'] + graph.get('annotations', []):
            if item['id'] in positions:
                position = positions[item['id']]
                if set(position) != {'x', 'y'} or any(type(position[a]) not in [int, float] or abs(position[a]) > 1000000 for a in ['x', 'y']): raise ValueError('Invalid node position.')
                item['position'] = position
        self.store.update('workflow', graph['id'], {'nodes': graph['nodes'], 'annotations': graph.get('annotations', [])})
        return {'saved': True, 'version': graph['version'], 'execution_unchanged': True}

    def delete_workflow(self, p):
        item = self.workflow(p['id'])
        active = [r for r in self.store.list('run') if r['workflow_id'] == item['id'] and r['state'] in ['running', 'pausing', 'cancelling']]
        if active: raise ValueError('Cancel active runs before deleting this workflow.')
        self.store.update('workflow', item['id'], {'deleted': True})
        return {'deleted': True, 'original_files_preserved': True}

    def duplicate(self, p):
        graph = deepcopy(self.workflow(p['id']))
        graph.update(id=new_id('workflow'), title=graph['title'] + ' copy', version=1, created_at=now())
        self.store.put('workflow_version', graph['id'] + ':1', graph)
        return self.store.put('workflow', graph['id'], graph)

    def wrap(self, p): return {'graph': wrap(p['graph'], p['node_ids'])}
    def unwrap(self, p): return {'graph': unwrap(p['graph'], p['node_id'])}
    def check(self, p): return offline_report(p.get('graph') or self.workflow(p['id']), self.store)
    def history(self, p): return [v for v in self.store.list('workflow_version') if v['id'].rsplit(':', 1)[0] == p['id']]

    def publish(self, p):
        graph = deepcopy(self.workflow(p['id']))
        # Portable definitions contain configuration but never private input values or old outputs.
        def clean(sub):
            for n in sub.get('nodes', []):
                if n['type_id'] in ['input.text', 'input.table', 'input.file', 'human.input', 'knowledge.search']:
                    n['config'] = deepcopy(BY_ID[n['type_id']]['defaults'])
                    if n['type_id'] == 'input.table': n['config']['csv'] = ''
                if n['type_id'] == 'workflow.composite': clean(n['config']['definition'])
                for key in ['api_key', 'password', 'token', 'path', 'image_asset_id']: n.get('config', {}).pop(key, None)
        clean(graph)
        key = p.get('library_id') or new_id('template')
        previous = self.store.get('library', key, {})
        graph['id'] = key; graph['version'] = previous.get('version', 0) + 1
        return self.store.put('library', key, {'title': p.get('title') or graph['title'], 'version': graph['version'], 'definition': graph, 'created_at': now()})

    def from_library(self, p):
        source = self.store.get('library', p['id'])
        if not source: raise ValueError('This template is unavailable.')
        graph = deepcopy(source['definition'])
        graph.update(id=new_id('workflow'), title=source['title'], version=1, created_at=now(), template_version=source['version'])
        self.store.put('workflow_version', graph['id'] + ':1', graph)
        return self.store.put('workflow', graph['id'], graph)

    def start(self, p):
        if p.get('run_id'):
            existing = self.store.get('run', p['run_id'])
            graph = existing['graph'] if existing else self.workflow(p['workflow_id'])
        else: graph = self.workflow(p['workflow_id'])
        return Engine(self.store, self.remote_executor, self.cloud_client).start(graph, p.get('run_id'), p.get('single', False), p.get('reuse', True))

    def control(self, p):
        if p['action'] not in ['pause', 'cancel']: raise ValueError('Choose pause or cancel.')
        run = self.store.get('run', p['run_id'])
        if not run: raise ValueError('This run was not found.')
        if run['state'] in ['completed', 'partial', 'failed', 'cancelled']: return run
        state = ('pausing' if p['action'] == 'pause' else 'cancelling') if run['state'] == 'running' else ('paused' if p['action'] == 'pause' else 'cancelled')
        return self.store.update('run', p['run_id'], {'control': p['action'], 'state': state})

    def run_detail(self, p):
        run = self.store.get('run', p['id'])
        if not run: raise ValueError('The run was not found.')
        nodes = [n for n in self.store.list('node_run') if n['run_id'] == p['id']]
        for item in nodes:
            edit = self.store.get('edit', run['workflow_id'] + ':' + item['node_id'])
            if edit and edit.get('source_run') == run['id'] and edit.get('fingerprint') == item.get('fingerprint') and 'text' in item.get('outputs', {}):
                item['outputs']['text']['value'] = edit['text']; item['outputs']['text']['private'] = True; item['edited'] = True
        from .delivery import result_summary, topology
        return {'run': run, 'nodes': nodes, 'events': self.store.events(p['id'], p.get('after', 0)), 'final': result_summary(run['graph'], nodes), 'delivery': topology(run['graph'])}

    def run_events(self, p): return self.store.events(p['id'], p.get('after', 0))

    def edit_output(self, p):
        run = self.store.get('run', p['run_id'])
        node = self.store.get('node_run', p['run_id'] + ':' + p['node_id'])
        if not run or not node or 'text' not in node.get('outputs', {}): raise ValueError('Choose a completed text output.')
        if run['state'] in ['running', 'pausing', 'cancelling']: raise ValueError('Pause the run before editing its output.')
        key = run['workflow_id'] + ':' + p['node_id']
        return self.store.put('edit', key, {'text': str(p['text']), 'fingerprint': node['fingerprint'], 'source_run': p['run_id'], 'created_at': now()})

    def export(self, p):
        detail = self.run_detail({'id': p['run_id']})
        if p.get('scope') == 'final':
            if detail['final']['state'] != 'ready': raise ValueError('The final answer is not ready. Complete or retry the blocked nodes first.')
            detail['nodes'] = detail['final']['results']
        final_only = p.get('scope') == 'final'
        sections = ['# ' + (detail['nodes'][0]['title'] if len(detail['nodes']) == 1 else 'Final answers'), ''] if final_only else ['# ' + detail['run']['title'], 'Run: ' + detail['run']['id'], 'Status: ' + detail['run']['state'], '']
        for n in sorted(detail['nodes'], key=lambda n: n['node_id']):
            if n['state'] != 'succeeded':
                sections += ['## ' + n['title'], n.get('message', n['state']), '']; continue
            if not final_only or len(detail['nodes']) > 1: sections.append('## ' + n['title'])
            for port, data in n.get('outputs', {}).items():
                if data['kind'] == 'text': sections.append(data['value'])
                else: sections.append('```json\n' + json.dumps(data['value'], ensure_ascii=False, indent=2) + '\n```')
                for source in data.get('sources', []):
                    if final_only and source.get('url'):
                        sections.append('Source: [' + source.get('title', source['url']) + '](' + source['url'] + ')' + (' · Retrieved ' + source['retrieved_at'] if source.get('retrieved_at') else ''))
                    else: sections.append('Source: ' + json.dumps(source, ensure_ascii=False))
            sections.append('')
        body = '\n\n'.join(sections)
        filename = ('combine-final-' if p.get('scope') == 'final' else 'combine-') + p['run_id'] + '.md'
        (self.store.root / 'exports' / filename).write_text(body, encoding='utf-8')
        return {'filename': filename, 'content': body, 'local_path': str(self.store.root / 'exports' / filename)}

    def import_asset(self, p):
        name = Path(str(p['name'])).name
        if not name or name in ['.', '..']: raise ValueError('Choose a file.')
        data = base64.b64decode(p['content_base64'], validate=True)
        if len(data) > 64 * 1024 * 1024: raise ValueError('This adapter currently accepts files up to 64 MB.')
        key = new_id('asset')
        relative = 'assets/' + key + Path(name).suffix.lower()
        path = self.store.local_file(relative); path.write_bytes(data)
        try: parsed = read_document(path)
        except Exception as error: parsed = {'status': 'failed', 'text': '', 'parts': [], 'tables': [], 'issues': [str(error)]}
        result = self.store.put('asset', key, {'name': name, 'path': relative, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'parsed': parsed, 'created_at': now(), 'private': True})
        if parsed.get('text'):
            self.store.put('knowledge', key, {'title': name, 'content': parsed['text'], 'asset_id': key, 'sources': [{'asset_id': key, 'title': name, 'location': part['location']} for part in parsed['parts']], 'created_at': now(), 'private': True})
        return result

    def delete_asset(self, p):
        asset = self.store.get('asset', p['id'])
        if not asset: raise ValueError('The file was not found.')
        used = []
        def references(graph, workflow):
            for item in graph.get('nodes', []):
                if item.get('config', {}).get('asset_id') == p['id']: used.append(workflow)
                if item['type_id'] == 'workflow.composite': references(item['config']['definition'], workflow)
        for graph in self.store.list('workflow'): references(graph, graph['title'])
        if used and not p.get('confirm'): return {'requires_confirmation': True, 'affected_workflows': sorted(set(used))}
        self.store.delete('asset', p['id']); self.store.delete('knowledge', p['id'])
        self.store.local_file(asset['path']).unlink(missing_ok=True)
        # Existing run snapshots retain source ids; a missing source is disclosed.
        return {'deleted': True, 'original_files_preserved': True, 'affected_workflows': used}

    def save_knowledge(self, p):
        if not str(p.get('content', '')).strip(): raise ValueError('Enter some note content.')
        key = p.get('id') or new_id('note')
        old = self.store.get('knowledge', key, {})
        return self.store.put('knowledge', key, {**old, 'title': str(p.get('title', '')).strip() or 'Untitled note', 'content': p['content'], 'private': True, 'category_status': 'pending', 'version': old.get('version', 0) + 1, 'created_at': old.get('created_at', now())})

    def execute_node(self, p):
        item, args = p['node'], p.get('inputs', {})
        if item.get('type_id') not in BY_ID or item['type_id'] in ['model.api', 'workflow.composite']: raise ValueError('This executor is unavailable in the private worker.')
        engine = Engine(self.store); engine.run_id = p.get('run_id', '')
        if engine.run_id:
            run = self.store.get('run', engine.run_id)
            if not run: raise ValueError('The execution record is unavailable.')
            engine.run_settings = run.get('execution_settings', {})
        try: return {'outputs': engine.execute(item, args)}
        except Waiting as error: return {'waiting': error.state, 'message': str(error)}

    def asset_preview(self, p):
        asset = self.store.get('asset', p['id'])
        if not asset: raise ValueError('The image is unavailable.')
        from PIL import Image
        with Image.open(self.store.local_file(asset['path'])) as image:
            image = image.convert('RGB'); image.thumbnail((1000, 700))
            buffer = io.BytesIO(); image.save(buffer, format='JPEG', quality=85)
        return {'url': 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode(), 'width': image.width, 'height': image.height, 'private': True}

    def local_completion(self, p):
        return {'content': Engine(self.store).local_model(str(p['instruction']), str(p['text']), p.get('max_tokens', 2048))}

    def classify_knowledge(self, p):
        from .assistant import parse_json
        item = self.store.get('knowledge', p['id'])
        if not item: raise ValueError('This local knowledge record is unavailable.')
        if any(self.store.get('resource', key, {}).get('status') != 'ready' for key in ['llama-cpu', 'qwen3vl-4b']):
            return self.store.update('knowledge', item['id'], {'category_status': 'needs_local_model', 'classification_message': 'Optional Local AI is required to classify private material. No content was sent to an API.'})
        self.store.update('knowledge', item['id'], {'category_status': 'classifying'})
        try:
            engine = Engine(self.store); engine.run_settings = {'output_language': 'en'}
            raw = engine.local_model('Classify this private document. Return JSON only: {"category":"short English category", "tags":["tag"], "summary":"one factual sentence"}. Base the result only on the given document. Do not invent content.', item['title'] + '\n' + item['content'][:1800], 256)
            result = parse_json(raw)
            if not isinstance(result.get('category'), str) or not isinstance(result.get('tags'), list) or not isinstance(result.get('summary'), str): raise ValueError('The local model returned an invalid classification.')
            return self.store.update('knowledge', item['id'], {'category': result['category'][:100], 'tags': [str(t)[:60] for t in result['tags'][:8]], 'summary': result['summary'][:1500], 'category_status': 'classified', 'category_source': 'local_model', 'classified_at': now()})
        except Exception as error:
            return self.store.update('knowledge', item['id'], {'category_status': 'failed', 'classification_message': str(error)})

    def get_conversation(self, p):
        from .assistant import conversation
        return conversation(self.store, p)

    def undo(self, p):
        from .assistant import undo
        return undo(self.store, p)

    def search(self, p): return search_knowledge(self.store, p.get('query', ''), p.get('limit', 5))
    def get_knowledge(self, p):
        item = self.store.get('knowledge', p['id'])
        if not item: raise ValueError('This source was removed or is unavailable.')
        return item
    def delete_knowledge(self, p): self.store.delete('knowledge', p['id']); return {'deleted': True, 'original_files_preserved': True}

    def archive(self, p):
        node = self.store.get('node_run', p['run_id'] + ':' + p['node_id'])
        if not node or node['state'] != 'succeeded' or not node.get('outputs', {}).get('text'): raise ValueError('Choose a successful text output to archive.')
        return self.save_knowledge({'title': node['title'], 'content': node['outputs']['text']['value']})

    def read_notification(self, p):
        if p.get('id'): self.store.update('notification', p['id'], {'read': True})
        else:
            for notice in self.store.list('notification'): self.store.update('notification', notice['id'], {'read': True})
        return {'read': True}

    def backup(self, p):
        key = new_id('backup')
        directory = self.store.root / 'backups'
        db_copy = directory / (key + '.sqlite3')
        with self.store.connection() as db, closing(sqlite3.connect(db_copy)) as target: db.backup(target)
        target_path = directory / (key + '.zip')
        checks = {}
        with zipfile.ZipFile(target_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            files = [(db_copy, 'workspace.sqlite3')]
            for folder in ['assets', 'exports']:
                files += [(f, f.relative_to(self.store.root).as_posix()) for f in (self.store.root / folder).rglob('*') if f.is_file() and not f.is_symlink()]
            for file, name in files:
                archive.write(file, name); checks[name] = hashlib.sha256(file.read_bytes()).hexdigest()
            archive.writestr('manifest.json', json.dumps({'schema': 1, 'created_at': now(), 'files': checks, 'models_included': False, 'credentials_included': False}))
        db_copy.unlink()
        return self.store.put('backup', key, {'name': target_path.name, 'path': str(target_path), 'size': target_path.stat().st_size, 'created_at': now(), 'models_included': False})

    def restore(self, p):
        if not p.get('confirm'): raise ValueError('Confirm before restoring a backup.')
        if any(r['state'] in ['running', 'pausing', 'cancelling'] for r in self.store.list('run')): raise ValueError('Stop active runs before restoring a backup.')
        backup = self.store.get('backup', p['id'])
        if not backup: raise ValueError('Select a backup from this workspace.')
        with zipfile.ZipFile(backup['path']) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            if manifest.get('schema') != 1: raise ValueError('This backup schema is unsupported.')
            for name, checksum in manifest['files'].items():
                if name != 'workspace.sqlite3' and not name.startswith(('assets/', 'exports/')): raise ValueError('Invalid backup path.')
                self.store.local_file(name)
                if hashlib.sha256(archive.read(name)).hexdigest() != checksum: raise ValueError('The backup failed its integrity check.')
            temp = self.store.root / 'temp' / (new_id('restore') + '.sqlite3')
            temp.write_bytes(archive.read('workspace.sqlite3'))
            with closing(sqlite3.connect(temp)) as source:
                if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('The backup database is corrupt.')
                safety = self.backup({})
                with self.store.connection() as target: source.backup(target)
            temp.unlink()
            for name in manifest['files']:
                if name == 'workspace.sqlite3': continue
                dest = self.store.local_file(name); dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(archive.read(name))
        self.store.put('backup', backup['id'], backup)
        self.store.put('backup', safety['id'], {**safety, 'reason': 'Automatic checkpoint before restore'})
        for resource in self.store.list('resource'):
            self.store.update('resource', resource['id'], {'status': 'needs_verification'})
        from .leases import recover_runs
        recover_runs(self.store)
        return {'restored': True, 'resources_require_recheck': True, 'safety_backup_id': safety['id']}

    def diagnostics(self, p):
        return {'version': '0.2.0', 'schema_version': 1, 'database': 'ok', 'sandbox': 'Windows AppContainer', 'private_data_location': 'This computer',
                'models': self.store.list('resource'), 'backups': self.store.list('backup'), 'offline_acceptance': 'In progress; see the implementation evidence',
                'online_accounts': 'Not connected. Local workspace does not require an account.',
                'legacy_service': 'Separate compatibility service; never receives workspace content.'}

    def checkpoint(self, p):
        for run in self.store.list('run'):
            if run['state'] not in ['running', 'pausing', 'cancelling']: continue
            control = 'cancel' if run.get('control') == 'cancel' else 'pause'
            self.store.update('run', run['id'], {'control': control})
            self.store.event(run['id'], 'run.exit_checkpoint', control=control)
        return {'saved': True, 'message': 'Completed outputs and execution events are saved locally.'}

    def ai_plan(self, p):
        goal = str(p.get('goal', '')).strip()
        if not goal: raise ValueError('Enter a goal.')
        schema = {'nodes': [{'id': 'unique', 'type_id': 'input.text', 'type_version': '1', 'title': 'Title', 'position': {'x': 0, 'y': 0}, 'config': {'text': 'input'}}],
                  'edges': [{'id': 'unique', 'source': 'node-id', 'target': 'node-id', 'sourceHandle': 'text', 'targetHandle': 'text'}]}
        prompt = 'Create a runnable workflow as JSON only. Use only the supplied node types and connect every required input. Never invent a tool or URL. All private data remains local. Shape: ' + json.dumps(schema) + '\nAvailable types: ' + json.dumps([t for t in TYPES if t['network'] == 'none' and t['id'] != 'workflow.composite'])
        raw = Engine(self.store).local_model(prompt, goal, 2048)
        match = re.search(r'\{.*\}', raw, re.S)
        if not match: raise ValueError('The local planner did not return a valid graph. You can still build it manually.')
        graph = json.loads(match.group())
        graph.update(id=new_id('workflow'), title=goal[:120], version=1, created_at=now())
        errors = validate(graph)
        if errors: raise ValueError('The proposed graph needs correction: ' + '; '.join(e['message'] for e in errors))
        self.store.put('workflow_version', graph['id'] + ':1', graph); self.store.put('workflow', graph['id'], graph)
        return {'workflow': graph, 'run': Engine(self.store).start(graph)}
