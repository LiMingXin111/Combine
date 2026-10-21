"""Authoritative source propagation and a second boundary check before network I/O."""
import json
import re

PRIVATE_TYPES = {'input.text', 'input.file', 'input.image', 'input.table', 'knowledge.search'}


def private_fragments(store):
    fragments = set()
    for asset in store.list('asset'):
        name = asset.get('name', '')
        if len(name) >= 4: fragments.add(name.casefold())
    texts = [k.get('content', '') + '\n' + k.get('title', '') for k in store.list('knowledge')]
    from .capabilities import public_template_texts
    public_templates = public_template_texts()
    for cap in store.list('capability'):
        if cap.get('private', True):
            recipe = cap.get('recipe') or {}
            source = cap.get('source') or {}
            public_metadata = {source.get('source_title', ''), source.get('source_description', '')} if source.get('verified_public') else set()
            texts.extend(text for text in [cap.get('title', ''), cap.get('description', ''), recipe.get('instruction', ''), *recipe.get('stages', [])] if text.strip() not in public_templates and text not in public_metadata)
    def sources(graph):
        texts.extend(a.get('text', '') for a in graph.get('annotations', []) if a.get('private', True))
        for node in graph.get('nodes', []):
            if node['type_id'] in ['input.text', 'input.table']: texts.append(str(node.get('config', {}).get('text', node.get('config', {}).get('csv', ''))))
            if node['type_id'] == 'workflow.composite': sources(node.get('config', {}).get('definition', {}))
    for graph in store.list('workflow'): sources(graph)
    for cap in store.list('capability'):
        if cap.get('private', True): sources(cap.get('definition') or {})
    for node in store.list('node_run'):
        for item in node.get('outputs', {}).values():
            if item.get('private', True): texts.append(str(item.get('value', '')))
    for text in texts:
        text = re.sub(r'\s+', ' ', text).strip().casefold()
        if len(text) >= 24:
            for index in range(0, len(text) - 23, 24): fragments.add(text[index:index+24])
        for token in re.findall(r'[\w-]{16,}', text): fragments.add(token)
    return fragments


def assert_public_payload(store, payload):
    text = re.sub(r'\s+', ' ', json.dumps(payload, ensure_ascii=False)).casefold()
    if re.search(r'\bsk-[a-z0-9_-]{20,}', text): raise ValueError('Keep API keys in Settings → AI connections, not in a conversation or node prompt.')
    if any(fragment in text for fragment in private_fragments(store)):
        raise ValueError('This request contains protected local material or its metadata. Use Local AI for this request; private files and derived content cannot be sent to an API.')


def contains_api(graph):
    return any(n['type_id'] in ['model.api', 'network.public_search', 'network.public_fetch'] or (n['type_id'] == 'workflow.composite' and contains_api(n['config']['definition'])) for n in graph.get('nodes', []))


def safe_context(graph):
    """Cloud assistant sees graph controls, never private sources, outputs or file names."""
    nodes = []
    protected = {n['id'] for n in graph.get('nodes', []) if n['type_id'] in PRIVATE_TYPES or n['type_id'] == 'workflow.composite' or n.get('private', False)}
    changed = True
    while changed:
        before = len(protected)
        protected.update(e['target'] for e in graph.get('edges', []) if e['source'] in protected)
        changed = len(protected) != before
    for item in graph.get('nodes', []):
        private = item['id'] in protected
        config = {} if private else {k: v for k, v in item.get('config', {}).items() if k not in ['asset_id', 'image_asset_id', 'path']}
        nodes.append({'id': item['id'], 'type_id': item['type_id'], 'title': 'Private local source' if private else item.get('title', ''), 'config': config,
                      'position': item.get('position', {}), 'private_source': private})
    return {'id': graph['id'], 'version': graph['version'], 'nodes': nodes, 'edges': graph.get('edges', []), 'final_outputs': graph.get('final_outputs'),
            'annotations': [{**a, 'text': '[Private local note]'} if a.get('private', True) else a for a in graph.get('annotations', [])]}
