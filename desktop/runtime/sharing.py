"""Networkless template preparation and import. Sources/results never enter uploads."""
from copy import deepcopy
import json
import re
from .catalog import BY_ID
from .graphs import digest, validate
from .privacy import PRIVATE_TYPES, assert_public_payload
from .store import new_id, now

SCHEMA = 'combine.workflow-template/v1'
# Exact portable schema is also validated at the server boundary.
CONFIG = {
    'input.prompt': {'text', 'community_ref', 'community_sha'}, 'input.text': {'text'}, 'input.table': {'csv'}, 'input.public_table': {'csv'}, 'input.file': set(), 'input.image': set(),
    'transform.text': {'operation', 'template'}, 'math.calculate': {'expression'}, 'data.aggregate': {'column', 'operation'},
    'data.merge_text': {'input_count', 'labels', 'separator'}, 'model.local': {'instruction', 'max_tokens'}, 'model.api': {'instruction', 'max_tokens'},
    'knowledge.search': {'limit'}, 'human.input': {'prompt'}, 'output.document': {'heading'}, 'output.image': set(), 'output.table': set(),
    'output.chart': {'label_column', 'value_column'}, 'output.map': {'latitude', 'longitude', 'label'}, 'output.live_map': {'latitude', 'longitude', 'label'},
    'output.file': set(), 'network.public_search': {'query', 'count', 'need_content'}, 'network.public_fetch': {'url'},
    'workflow.composite': {'definition', 'capability_kind'},
}


def prepare(store, p):
    graph = store.get('workflow', p.get('workflow_id', ''))
    if not graph or graph.get('deleted'): raise ValueError('Save a workflow before sharing it.')
    if graph['version'] != p.get('version'): raise ValueError('The workflow changed. Reopen the share preview.')
    title = str(p.get('title', 'Shared workflow')).strip()
    description = str(p.get('description', '')).strip()
    if not 1 <= len(title) <= 160 or len(description) > 2000: raise ValueError('Enter a title (up to 160 characters) and description (up to 2,000 characters).')
    redactions = []
    from .community import catalog, reference_text
    references = {reference_text(e): e for e in catalog()}
    references_used = set()
    def sanitize(g, inherited=False, depth=0):
        if depth > 5: raise ValueError('Shared composites support at most five nested levels.')
        protected = {n['id'] for n in g.get('nodes', []) if inherited or n['type_id'] in PRIVATE_TYPES or n.get('private')}
        # Conservatively protect composites containing any local source.
        def has_private(sub):
            return any(n['type_id'] in PRIVATE_TYPES or n.get('private') or (n['type_id'] == 'workflow.composite' and has_private(n.get('config', {}).get('definition') or {})) for n in sub.get('nodes', []))
        protected.update(n['id'] for n in g.get('nodes', []) if n['type_id'] == 'workflow.composite' and has_private(n.get('config', {}).get('definition') or {}))
        while True:
            before = len(protected)
            protected.update(e['target'] for e in g.get('edges', []) if e['source'] in protected)
            if len(protected) == before: break
        result = {'nodes': [], 'edges': [], 'final_outputs': deepcopy(g.get('final_outputs', []))}
        mapping = {n['id']: f'n{i + 1}' for i, n in enumerate(g.get('nodes', []))}
        result['final_outputs'] = [mapping[i] for i in result['final_outputs'] if i in mapping]
        if not result['final_outputs']: result.pop('final_outputs')
        for n in g.get('nodes', []):
            kind = n['type_id']
            if kind not in CONFIG: raise ValueError('This workflow contains an unsupported shared node type.')
            private = n['id'] in protected
            defaults = deepcopy(BY_ID[kind]['defaults'])
            c = defaults if private or not p.get('include_configuration', True) else deepcopy(n.get('config', {}))
            c = {k: v for k, v in c.items() if k in CONFIG[kind]}
            if kind.startswith('input.'): c = {'text': ''} if kind in ['input.prompt', 'input.text'] else {'csv': ''} if kind in ['input.table', 'input.public_table'] else {}
            if kind == 'input.prompt' and not private and p.get('include_configuration', True):
                entry = references.get(n.get('config', {}).get('text', ''))
                if entry:
                    c.update(community_ref=entry['id'], community_sha=entry['sha256'])
                    references_used.add(entry['id'])
            if kind == 'knowledge.search': c = {'limit': 5}
            if kind == 'human.input': c = {'prompt': 'Add your input'}
            if kind == 'workflow.composite':
                sub = n.get('config', {}).get('definition')
                if not isinstance(sub, dict): raise ValueError('A composite is missing its internal definition.')
                clean, submap = sanitize(sub, private, depth + 1)
                for direction in ['inputs', 'outputs']:
                    clean[direction] = [{k: (submap[v] if k == 'node' else v) for k, v in port.items() if k in ['id', 'kind', 'node', 'port']} for port in sub.get(direction, [])]
                clean['version'] = 1
                ck = n.get('config', {}).get('capability_kind') or sub.get('kind')
                if ck in ['skill', 'agent', 'expert']: clean['kind'] = ck
                c = {'definition': clean, **({'capability_kind': ck} if ck in ['skill', 'agent', 'expert'] else {})}
            if private: redactions.append(f"Node {mapping[n['id']]}: local source and derived configuration replaced with defaults.")
            result['nodes'].append({'id': mapping[n['id']], 'type_id': kind, 'type_version': '1', 'title': BY_ID[kind]['title'] if private else n.get('title', BY_ID[kind]['title']), 'position': deepcopy(n.get('position', {'x': 0, 'y': 0})), 'config': c})
        for i, e in enumerate(g.get('edges', [])):
            result['edges'].append({'id': f'e{i + 1}', 'source': mapping[e['source']], 'target': mapping[e['target']], 'sourceHandle': e['sourceHandle'], 'targetHandle': e['targetHandle']})
        return result, mapping
    clean, _ = sanitize(graph)
    if not clean['nodes']: raise ValueError('Add at least one node before sharing.')
    definition = {'schema': SCHEMA, 'graph': clean}
    payload = {'title': title, 'description': description, 'definition': definition}
    assert_public_payload(store, payload)
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode()) > 950000: raise ValueError('The workflow template is too large to share (1 MB limit).')
    if re.search(r'(?i)\b(sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{20,})|(?<![a-z0-9])[a-z]:[\\/]|file://', encoded): raise ValueError('Remove credentials and local paths from reusable configuration.')
    for old in store.list('share_preview'):
        if now() - old.get('created_at', 0) > 1800000: store.delete('share_preview', old['id'])
    token = new_id('preview')
    item = {'workflow_id': graph['id'], 'version': graph['version'], 'graph_digest': digest(graph), 'payload': payload, 'created_at': now(), 'redactions': redactions}
    store.put('share_preview', token, item)
    return {'token': token, 'payload': payload, 'redactions': redactions, 'node_count': len(clean['nodes']), 'expires_at': item['created_at'] + 1800000,
            'references': sorted(references_used),
            'excluded': ['Attachments and file names', 'Input text and table data', 'Knowledge and local references', 'Run results and history', 'Conversations and annotations', 'API keys and provider connections', 'Local output paths', 'Capability source documents (verified bundled methods use ID and checksum references)']}


def approved(store, token):
    item = store.get('share_preview', token)
    if not item or now() - item['created_at'] > 1800000: raise ValueError('The share preview expired. Review the template again.')
    graph = store.get('workflow', item['workflow_id'])
    if not graph or graph.get('deleted') or digest(graph) != item['graph_digest']: raise ValueError('The workflow changed after preview. Review the template again.')
    assert_public_payload(store, item['payload'])
    return {**item['payload'], 'request_key': token}


def import_template(store, p):
    value = p.get('definition')
    if not isinstance(value, dict) or value.get('schema') != SCHEMA: raise ValueError('Unsupported team workflow format.')
    graph = deepcopy(value.get('graph'))
    count = [0]
    def hydrate(g, depth=0):
        if not isinstance(g, dict) or depth > 5: raise ValueError('Invalid team composite.')
        count[0] += len(g.get('nodes', []))
        if count[0] > 200: raise ValueError('The team workflow exceeds 200 nodes.')
        # Never retain unrecognized fields supplied by a server.
        allowed = {'nodes', 'edges', 'inputs', 'outputs', 'version', 'kind', 'final_outputs'}
        if set(g) - allowed: raise ValueError('Unsupported team workflow fields.')
        for n in g.get('nodes', []):
            kind = n.get('type_id')
            if kind not in CONFIG or set(n) != {'id', 'type_id', 'type_version', 'title', 'position', 'config'}: raise ValueError('Unsupported team node.')
            c = n.get('config')
            if not isinstance(c, dict) or set(c) - CONFIG[kind]: raise ValueError('A shared template contains local references or unsupported configuration.')
            if kind == 'workflow.composite': hydrate(c.get('definition'), depth + 1)
            ref, sha = c.get('community_ref'), c.get('community_sha')
            n['config'] = {**deepcopy(BY_ID[kind]['defaults']), **{k: v for k, v in c.items() if k not in ['community_ref', 'community_sha']}}
            if kind in ['input.prompt', 'input.text']: n['config']['text'] = ''
            if kind == 'input.prompt' and (ref or sha):
                from .community import catalog, reference_text
                entry = next((e for e in catalog() if e['id'] == ref and e['sha256'] == sha), None)
                if not entry: raise ValueError('A bundled open-source method version is unavailable. Install the matching Combine version before downloading this workflow.')
                n['config']['text'] = reference_text(entry)
            if kind in ['input.table', 'input.public_table']: n['config']['csv'] = ''
            for key in ['asset_id', 'image_asset_id', 'provider_id']:
                if key in n['config']: n['config'][key] = ''
        issues = validate(g, public_inputs={(p['node'], p['port']) for p in g.get('inputs', [])})
        if issues: raise ValueError('Invalid shared graph: ' + issues[0]['message'])
    hydrate(graph)
    graph.update(id=new_id('workflow'), title=str(p.get('title', 'Team workflow'))[:160], version=1, created_at=now(), team_source=p.get('source', {}))
    store.put('workflow_version', graph['id'] + ':1', graph)
    return store.put('workflow', graph['id'], graph)
