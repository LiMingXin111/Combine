from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import re
from .catalog import definition
from .store import new_id, now


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def execution_node(item):
    result = {k: deepcopy(v) for k, v in item.items() if k not in ['position', 'selected', 'measured', 'dragging', 'title']}
    if item.get('type_id') == 'workflow.composite' and result.get('config', {}).get('definition'):
        sub = result['config']['definition']
        sub['nodes'] = [execution_node(n) for n in sub.get('nodes', [])]
        for key in ['title', 'position', 'updated_at']: sub.pop(key, None)
    return result


def validate(graph, depth=0, public_inputs=None):
    errors = []
    nodes, edges = graph.get('nodes', []), graph.get('edges', [])
    if depth > 12: return [{'code': 'NESTING_DEPTH', 'message': 'Composite nesting exceeds the supported depth of 12.'}]
    keys = [n.get('id') for n in nodes]
    if any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', k) for k in keys) or len(set(keys)) != len(keys):
        return [{'code': 'NODE_ID', 'message': 'Every node needs a unique identifier using letters, numbers, underscores or hyphens (maximum 128 characters).'}]
    by = {n['id']: n for n in nodes}
    adjacency = {key: [] for key in keys}
    incoming = set(public_inputs or [])
    for item in nodes:
        d = definition(item)
        if d is None:
            errors.append({'code': 'UNKNOWN_TYPE', 'node_id': item['id'], 'message': 'This node type is not installed.'})
            continue
        if item.get('type_version', '1') != d['version']:
            errors.append({'code': 'TYPE_VERSION', 'node_id': item['id'], 'message': 'This node type version is not installed.'})
        if item.get('type_id') == 'data.merge_text':
            count = item.get('config', {}).get('input_count', 2)
            if type(count) is not int or not 2 <= count <= 16: errors.append({'code': 'MERGE_INPUTS', 'node_id': item['id'], 'message': 'Combine text needs an input_count integer between 2 and 16.'})
        if item.get('type_id') == 'workflow.composite':
            sub = item.get('config', {}).get('definition')
            if not isinstance(sub, dict):
                errors.append({'code': 'COMPOSITE_MISSING', 'node_id': item['id'], 'message': 'The composite definition is missing.'})
                continue
            inner = {n['id']: n for n in sub.get('nodes', [])}
            for kind in ['inputs', 'outputs']:
                ports = sub.get(kind, [])
                if len({p['id'] for p in ports}) != len(ports): errors.append({'code': 'DUPLICATE_PORT', 'node_id': item['id'], 'message': 'Composite public ports must be unique.'})
                for port in ports:
                    target = definition(inner.get(port.get('node'), {}))
                    target_ports = target.get('inputs' if kind == 'inputs' else 'outputs', []) if target else []
                    if not any(p['id'] == port.get('port') and p['kind'] == port.get('kind') for p in target_ports):
                        errors.append({'code': 'PORT_MAPPING', 'node_id': item['id'], 'message': 'A composite public port has no compatible internal port.'})
            for issue in validate(sub, depth + 1, {(p['node'], p['port']) for p in sub.get('inputs', [])}):
                errors.append({**issue, 'node_id': item['id'] + '/' + issue.get('node_id', '')})
    for edge in edges:
        source, target = by.get(edge.get('source')), by.get(edge.get('target'))
        if not source or not target:
            errors.append({'code': 'EDGE_NODE', 'message': 'A connection references a missing node.'}); continue
        sd, td = definition(source), definition(target)
        if not sd or not td: continue
        sp = next((p for p in sd['outputs'] if p['id'] == edge.get('sourceHandle')), None)
        tp = next((p for p in td['inputs'] if p['id'] == edge.get('targetHandle')), None)
        if not sp or not tp or sp['kind'] != tp['kind']:
            errors.append({'code': 'PORT_TYPE', 'node_id': target['id'], 'message': 'Connect matching input and output types.'})
        pair = (target['id'], edge.get('targetHandle'))
        if pair in incoming: errors.append({'code': 'INPUT_CARDINALITY', 'node_id': target['id'], 'message': 'An input port accepts one connection. Use Combine text to join multiple text branches.'})
        incoming.add(pair)
        adjacency[source['id']].append(target['id'])
    visiting, visited = set(), set()
    def visit(key):
        if key in visiting: return False
        if key in visited: return True
        visiting.add(key)
        if not all(visit(child) for child in adjacency[key]): return False
        visiting.remove(key); visited.add(key)
        return True
    if not all(visit(key) for key in keys): errors.append({'code': 'CYCLE', 'message': 'The workflow contains a circular dependency.'})
    if 'final_outputs' in graph:
        selected = graph['final_outputs']
        if not isinstance(selected, list) or not selected or any(not isinstance(k, str) for k in selected) or len(selected) != len(set(selected)) or any(k not in by or not definition(by[k]) or not definition(by[k])['outputs'] or by[k].get('disabled') for k in selected):
            errors.append({'code': 'FINAL_OUTPUT', 'message': 'Choose distinct enabled result nodes that exist on this canvas.'})
    for item in nodes:
        d = definition(item)
        if not d: continue
        for port in d['inputs']:
            if port.get('required') and (item['id'], port['id']) not in incoming:
                errors.append({'code': 'INPUT_MISSING', 'node_id': item['id'], 'message': 'Connect the ' + port['id'] + ' input.'})
    return errors


def offline_report(graph, store):
    reasons = []
    errors = validate(graph)
    resources = {r['id']: r for r in store.list('resource')}
    def walk(sub, prefix=''):
        for item in sub.get('nodes', []):
            if item.get('disabled'): continue
            path = prefix + item['id']
            d = definition(item)
            if not d:
                reasons.append({'status': 'unknown', 'node_id': path, 'message': 'The node type is unknown.'}); continue
            if d['network'] not in ['none', 'required']:
                reasons.append({'status': 'unknown', 'node_id': path, 'message': 'Network dependencies have not been verified.'})
            elif d['network'] == 'required':
                reasons.append({'status': 'online', 'node_id': path, 'message': 'This node needs an internet connection.'})
            for key in d['resources']:
                resource = resources.get(key, {})
                files = resource.get('files', [])
                if resource.get('status') != 'ready' or not files or not all(store.local_file(f['path']).is_file() and store.local_file(f['path']).stat().st_size == f['size'] and store.local_file(f['path']).stat().st_mtime_ns == f.get('mtime_ns') for f in files):
                    reasons.append({'status': 'missing', 'node_id': path, 'resource_id': key, 'message': 'Prepare ' + key + ' on this computer.'})
            asset_id = item.get('config', {}).get('asset_id') if item['type_id'] in ['input.file', 'input.image'] else item.get('config', {}).get('image_asset_id') if item['type_id'] == 'model.local' else None
            if item['type_id'] in ['input.file', 'input.image'] or asset_id:
                asset = store.get('asset', asset_id or '')
                if not asset or not store.local_file(asset['path']).is_file():
                    reasons.append({'status': 'missing', 'node_id': path, 'message': 'The required local source file is missing. Import or select a replacement.'})
            if item['type_id'] == 'model.api':
                settings = store.get('setting', 'ai', {})
                provider = store.get('provider', item.get('config', {}).get('provider_id') or settings.get('provider_id', ''))
                if not provider: reasons.append({'status': 'missing', 'node_id': path, 'message': 'Connect an API model in Settings → AI connections.'})
            if item['type_id'] in ['network.public_search', 'network.public_fetch']:
                from .web import status, official
                settings = status(store)
                selected = item.get('config', {}).get('provider_id')
                provider = store.get('provider', selected, {}) if selected else None
                if not settings['enabled'] or store.get('setting', 'ai', {}).get('mode') == 'local':
                    reasons.append({'status': 'missing', 'node_id': path, 'message': 'Web access is disabled. Select API AI and enable Web research in Settings.'})
                elif (provider is not None and (not official(provider) or not provider.get('enabled') or not provider.get('credential_saved'))) or (provider is None and not settings['available']):
                    reasons.append({'status': 'missing', 'node_id': path, 'message': 'Choose a saved official DeepSeek connection in Settings → Web research.'})
            if item['type_id'] == 'workflow.composite': walk(item.get('config', {}).get('definition') or {}, path + '/')
    walk(graph)
    status = 'unknown' if errors or any(r['status'] == 'unknown' for r in reasons) else 'online' if any(r['status'] == 'online' for r in reasons) else 'missing' if reasons else 'ready'
    from .delivery import topology
    return {'status': status, 'label': {'unknown': 'Dependency unknown', 'online': 'Internet required', 'missing': 'Preparation required', 'ready': 'Offline ready'}[status], 'delivery': topology(graph),
            'reasons': reasons, 'validation': errors, 'checked_at': now(), 'graph_hash': digest(graph), 'enforcement': 'Windows AppContainer; no internet or loopback capabilities'}


def impact(old, draft):
    def config(n): return execution_node(n)
    a, b = {n['id']: config(n) for n in old.get('nodes', [])}, {n['id']: config(n) for n in draft.get('nodes', [])}
    changed = {key for key in a.keys() | b.keys() if a.get(key) != b.get(key)}
    old_edges = {json.dumps(e, sort_keys=True) for e in old.get('edges', [])}
    new_edges = {json.dumps(e, sort_keys=True) for e in draft.get('edges', [])}
    for edge in old_edges ^ new_edges: changed.add(json.loads(edge)['target'])
    affected = set(changed)
    while True:
        after = affected | {e['target'] for e in draft.get('edges', []) if e['source'] in affected}
        if after == affected: break
        affected = after
    return {'changed': sorted(changed), 'affected': sorted(affected), 'reused': sorted(b.keys() - affected), 'added': sorted(b.keys() - a.keys()), 'removed': sorted(a.keys() - b.keys())}


def wrap(graph, selected):
    selected = set(selected)
    if not selected or not selected <= {n['id'] for n in graph['nodes']}: raise ValueError('Select existing nodes to create a composite.')
    result = deepcopy(graph)
    inside = [n for n in result['nodes'] if n['id'] in selected]
    inner_edges, outer_edges, inputs, outputs = [], [], [], []
    ports = {}
    composite_id = new_id('composite')
    for edge in result['edges']:
        s, t = edge['source'] in selected, edge['target'] in selected
        if s and t: inner_edges.append(edge)
        elif t:
            target = definition(next(n for n in inside if n['id'] == edge['target']))
            kind = next(p['kind'] for p in target['inputs'] if p['id'] == edge['targetHandle'])
            pid = 'in' + str(len(inputs) + 1)
            inputs.append({'id': pid, 'kind': kind, 'node': edge['target'], 'port': edge['targetHandle']})
            outer_edges.append({**edge, 'target': composite_id, 'targetHandle': pid})
        elif s:
            pair = (edge['source'], edge['sourceHandle'])
            if pair not in ports:
                source = definition(next(n for n in inside if n['id'] == edge['source']))
                kind = next(p['kind'] for p in source['outputs'] if p['id'] == edge['sourceHandle'])
                pid = 'out' + str(len(outputs) + 1); ports[pair] = pid
                outputs.append({'id': pid, 'kind': kind, 'node': pair[0], 'port': pair[1]})
            outer_edges.append({**edge, 'source': composite_id, 'sourceHandle': ports[pair]})
        else: outer_edges.append(edge)
    # Terminal ports remain publicly readable even without external consumers.
    for item in inside:
        for port in definition(item)['outputs']:
            pair = (item['id'], port['id'])
            if pair not in ports and not any(e['source'] == item['id'] and e['sourceHandle'] == port['id'] for e in inner_edges):
                pid = 'out' + str(len(outputs) + 1); ports[pair] = pid
                outputs.append({'id': pid, 'kind': port['kind'], 'node': pair[0], 'port': pair[1]})
    position = {'x': min(n.get('position', {}).get('x', 0) for n in inside), 'y': min(n.get('position', {}).get('y', 0) for n in inside)}
    for item in inside: item['position'] = {axis: item.get('position', {}).get(axis, 0) - position[axis] for axis in ['x', 'y']}
    sub = {'id': new_id('definition'), 'version': 1, 'nodes': inside, 'edges': inner_edges, 'inputs': inputs, 'outputs': outputs}
    result['nodes'] = [n for n in result['nodes'] if n['id'] not in selected] + [{'id': composite_id, 'type_id': 'workflow.composite', 'type_version': '1', 'title': 'Composite node', 'position': position, 'config': {'definition': sub}}]
    result['edges'] = outer_edges
    if any(key in selected for key in result.get('final_outputs', [])):
        result['final_outputs'] = list(dict.fromkeys(composite_id if key in selected else key for key in result['final_outputs']))
    return result


def unwrap(graph, node_id):
    result = deepcopy(graph)
    item = next(n for n in result['nodes'] if n['id'] == node_id)
    if item['type_id'] != 'workflow.composite': raise ValueError('Choose a composite node.')
    sub = deepcopy(item['config']['definition'])
    mapping = {n['id']: new_id('node') for n in sub['nodes']}
    for n in sub['nodes']:
        n['id'] = mapping[n['id']]
        n['position'] = {axis: n.get('position', {}).get(axis, 0) + item.get('position', {}).get(axis, 0) for axis in ['x', 'y']}
    edges = []
    for edge in result['edges']:
        edge = deepcopy(edge)
        if edge['source'] == node_id:
            port = next(p for p in sub['outputs'] if p['id'] == edge['sourceHandle'])
            edge.update(source=mapping[port['node']], sourceHandle=port['port'])
        if edge['target'] == node_id:
            port = next(p for p in sub['inputs'] if p['id'] == edge['targetHandle'])
            edge.update(target=mapping[port['node']], targetHandle=port['port'])
        edges.append(edge)
    edges += [{**e, 'id': new_id('edge'), 'source': mapping[e['source']], 'target': mapping[e['target']]} for e in sub['edges']]
    result['nodes'] = [n for n in result['nodes'] if n['id'] != node_id] + sub['nodes']
    result['edges'] = edges
    if node_id in result.get('final_outputs', []):
        result['final_outputs'] = list(dict.fromkeys(key for old in result['final_outputs'] for key in ([mapping[p['node']] for p in sub['outputs']] if old == node_id else [old])))
    return result
