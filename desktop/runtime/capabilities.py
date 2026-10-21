"""Versioned, local capability recipes compiled only to installed node executors."""
from copy import deepcopy
from functools import lru_cache
import json
from .store import new_id, now

KINDS = ('skill', 'agent', 'expert', 'workflow')


def compile_recipe(kind, recipe):
    if kind not in KINDS[:3] or not isinstance(recipe, dict):
        raise ValueError('Choose a Skill, Agent or Expert recipe.')
    if set(recipe) - {'mode', 'instruction', 'stages', 'provider_id', 'max_tokens'}:
        raise ValueError('Recipes accept instructions and installed AI models only.')
    mode = recipe.get('mode', 'api')
    instruction = recipe.get('instruction', '')
    stages = recipe.get('stages', [])
    tokens = recipe.get('max_tokens', 8192 if mode == 'api' else 768)
    provider = recipe.get('provider_id', '')
    if mode not in ('api', 'local'): raise ValueError('Choose API AI or Local AI.')
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 16000:
        raise ValueError('Add instructions of 1–16,000 characters.')
    if not isinstance(stages, list) or not 1 <= len(stages) <= 6 or any(not isinstance(s, str) or not s.strip() or len(s) > 4000 for s in stages):
        raise ValueError('Add 1–6 nonempty stages, each up to 4,000 characters.')
    if type(tokens) is not int or not 128 <= tokens <= 32768: raise ValueError('Output tokens must be between 128 and 32,768.')
    if not isinstance(provider, str) or len(provider) > 128: raise ValueError('Invalid AI connection identifier.')
    recipe = dict(mode=mode, instruction=instruction.strip(), stages=[s.strip() for s in stages], provider_id=provider, max_tokens=tokens)
    nodes = [{'id': 'context', 'type_id': 'transform.text', 'type_version': '1', 'title': 'Original task', 'position': {'x': 0, 'y': 120}, 'config': {'operation': 'trim', 'template': '{text}'}}]
    edges = []
    def connect(source, target, port='text'):
        edges.append({'id': f'edge_{len(edges)}', 'source': source, 'target': target, 'sourceHandle': 'text', 'targetHandle': port})
    for i, stage in enumerate(recipe['stages']):
        key = f'stage_{i + 1}'
        if i:
            merge = f'context_{i + 1}'
            nodes.append({'id': merge, 'type_id': 'data.merge_text', 'type_version': '1', 'title': 'Keep task and previous result', 'position': {'x': i * 800, 'y': 120}, 'config': {'input_count': 2, 'labels': 'Original task\nPrevious stage', 'separator': '\n\n'}})
            connect('context', merge, 'text_1'); connect(f'stage_{i}', merge, 'text_2'); connect(merge, key)
        else: connect('context', key)
        prompt = (f'{kind.title()} instructions:\n{recipe["instruction"]}\n\nStage {i + 1} of {len(stages)}:\n{stage}\n\n'
                  'Complete this stage using the supplied task and evidence. Preserve sources and constraints. '
                  'Return the actual useful result. Do not claim tools, browsing, delegation or professional certification you have not used.')
        nodes.append({'id': key, 'type_id': 'model.' + mode, 'type_version': '1', 'title': stage.splitlines()[0][:90], 'position': {'x': i * 800 + 400, 'y': 120},
                      'config': {'instruction': prompt, 'max_tokens': tokens, **({'provider_id': provider} if mode == 'api' else {'image_asset_id': ''})}})
    return recipe, {'nodes': nodes, 'edges': edges, 'inputs': [{'id': 'text', 'kind': 'text', 'node': 'context', 'port': 'text'}],
                    'outputs': [{'id': 'text', 'kind': 'text', 'node': f'stage_{len(stages)}', 'port': 'text'}]}


def prepare(store, values, mode=None):
    from .graphs import validate
    from .privacy import assert_public_payload, PRIVATE_TYPES, contains_api
    kind = values.get('kind', 'workflow')
    title, description = values.get('title', ''), values.get('description', '')
    if kind not in KINDS: raise ValueError('Unknown capability kind.')
    if not isinstance(title, str) or not title.strip() or len(title) > 150: raise ValueError('Add a name of 1–150 characters.')
    if not isinstance(description, str) or len(description) > 1000: raise ValueError('Descriptions can contain up to 1,000 characters.')
    recipe = None
    if values.get('recipe') is not None:
        recipe, definition = compile_recipe(kind, values['recipe'])
        if mode == 'local' and recipe['mode'] != 'local': raise ValueError('Local AI cannot create an API capability. Choose Local AI in its recipe.')
    else: definition = deepcopy(values.get('definition'))
    if not isinstance(definition, dict) or not definition.get('nodes') or not definition.get('outputs'): raise ValueError('A capability needs an executable internal workflow and at least one public output.')
    if len(json.dumps(definition, ensure_ascii=False, allow_nan=False)) > 250000: raise ValueError('This capability definition is too large.')
    def inspect(graph, depth=0):
        if depth > 12 or len(graph.get('nodes', [])) > 100 or len(graph.get('edges', [])) > 200: raise ValueError('This capability exceeds supported workflow limits.')
        private = False
        for node in graph.get('nodes', []):
            private |= node.get('type_id') in PRIVATE_TYPES or node.get('type_id') == 'model.local'
            if node.get('type_id') == 'workflow.composite': private |= inspect(node.get('config', {}).get('definition') or {}, depth + 1)
        return private
    private = inspect(definition) or mode == 'local'
    check = {'nodes': [{'id': 'capcheck', 'type_id': 'workflow.composite', 'config': {'definition': definition}}], 'edges': []}
    errors = [e for e in validate(check) if not (e['code'] == 'INPUT_MISSING' and e.get('node_id') == 'capcheck')]
    if errors: raise ValueError('Invalid capability workflow: ' + '; '.join(e['message'] for e in errors[:5]))
    if mode == 'local' and contains_api(definition): raise ValueError('Local AI cannot create a network-dependent capability.')
    if not private: assert_public_payload(store, {'title': title, 'description': description, 'definition': definition})
    if mode == 'api' and private: raise ValueError('API AI cannot register private local capability contents. Create this capability locally.')
    result = {'id': new_id('capability'), 'kind': kind, 'title': title.strip(), 'description': description.strip(), 'version': 1,
            'definition': definition, 'recipe': recipe, 'private': private, 'created_at': now()}
    origin = get(store, values.get('origin_id') or values.get('id')) if values.get('origin_id') or values.get('id') else None
    if origin and origin.get('source'): result['source'] = deepcopy(origin['source'])
    return result


def save(store, values):
    cap = prepare(store, values)
    key = values.get('id') or cap['id']
    if not isinstance(key, str) or not key.startswith('capability_'): raise ValueError('Built-in capabilities are read-only. Save a copy instead.')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', ('capability', key)).fetchone()
        old = json.loads(row['data']) if row else None
        if old and values.get('version') != old['version']: raise ValueError('This capability changed. Reopen its latest version before saving.')
        if values.get('id') and not old: raise ValueError('This capability was deleted. Save a new copy instead.')
        if old:
            # Earlier desktop versions stored only the current library pointer.
            db.execute('INSERT OR IGNORE INTO entities VALUES(?,?,?,?)', ('capability_version', key + ':' + str(old['version']), json.dumps(old, ensure_ascii=False), old.get('updated_at', now())))
        cap.update(id=key, version=old['version'] + 1 if old else 1, created_at=old['created_at'] if old else cap['created_at'], updated_at=now())
        db.execute('INSERT INTO entities VALUES(?,?,?,?)', ('capability_version', key + ':' + str(cap['version']), json.dumps(cap, ensure_ascii=False), cap['updated_at']))
        db.execute('INSERT INTO entities VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated', ('capability', key, json.dumps(cap, ensure_ascii=False), cap['updated_at']))
    return cap


def remove(store, values):
    key = values['id']
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', ('capability', key)).fetchone()
        if not row: raise ValueError('Built-in or missing capabilities cannot be deleted.')
        old = json.loads(row['data'])
        if old['version'] != values.get('version'): raise ValueError('This capability changed. Reopen it before deleting.')
        db.execute('INSERT OR IGNORE INTO entities VALUES(?,?,?,?)', ('capability_version', key + ':' + str(old['version']), row['data'], old.get('updated_at', now())))
        db.execute('DELETE FROM entities WHERE kind=? AND id=?', ('capability', key))
    return {'deleted': True, 'existing_instances_preserved': True}


def builtins():
    specs = [
        ('skill', 'summarize', 'Evidence summary', 'Summarize supplied material with traceable sources.', 'Summarize faithfully. Separate findings from assumptions and preserve source references.', ['Write a concise, structured summary of the supplied task and evidence.']),
        ('skill', 'structure', 'Structured writing', 'Turn a brief into a clear document.', 'Produce clear, complete writing. Preserve constraints and never invent citations.', ['Write the requested document with useful headings and actionable detail.']),
        ('agent', 'delivery', 'Task delivery agent', 'Plan, produce and review a result in three visible stages.', 'Work towards the user goal using only supplied context. State missing information and tool limitations.', ['Plan the approach, constraints and acceptance criteria.', 'Produce the complete deliverable according to the original task and plan.', 'Review the draft against the original task. Return the corrected, complete final deliverable, not only review notes.']),
        ('agent', 'analysis', 'Analysis agent', 'Extract evidence, analyze it and deliver recommendations.', 'Analyze the supplied evidence carefully. Preserve URLs and distinguish known facts from uncertainty.', ['Identify the question and extract the relevant evidence.', 'Compare alternatives and reason through the evidence.', 'Deliver a complete answer with conclusions, limitations and supporting sources.']),
        ('expert', 'research', 'Research methods expert', 'Review research design and evidence quality.', 'Act as a research methods reviewer. Evaluate research questions, method, sampling, bias, validity and evidence. This is an AI role, not a verified human credential.', ['Provide a concrete research review and prioritized revisions grounded in the supplied material.']),
        ('expert', 'software', 'Software review expert', 'Review an architecture or implementation brief.', 'Review software correctness, interfaces, maintainability, failure recovery and privacy. Do not claim to run code or inspect files that were not supplied.', ['Return evidence-based findings and practical fixes, ordered by impact.']),
    ]
    result = []
    for kind, slug, title, description, instruction, stages in specs:
        recipe, definition = compile_recipe(kind, {'mode': 'api', 'instruction': instruction, 'stages': stages})
        result.append({'id': f'builtin_{kind}_{slug}', 'kind': kind, 'title': title, 'description': description, 'recipe': recipe, 'definition': definition, 'version': 1, 'builtin': True, 'private': False})
    return result


def list_capabilities(store):
    return [{**c, 'kind': c.get('kind', 'workflow')} for c in store.list('capability')] + builtins()


@lru_cache(maxsize=1)
def public_template_texts():
    """Product-supplied text is public even when used in a local-only copy."""
    texts = {'Plan the approach and acceptance criteria.', 'Produce the requested deliverable.',
             'Review and return the complete corrected final answer.',
             'Complete the requested task using the instructions and supplied evidence.'}
    for cap in builtins():
        texts.update([cap['title'], cap['title'] + ' copy', cap['description'], cap['recipe']['instruction'], *cap['recipe']['stages']])
    return frozenset(texts)


def get(store, key):
    if isinstance(key, str) and key.startswith('community_'):
        from .community import get as get_community
        return get_community(key)
    return next((c for c in list_capabilities(store) if c['id'] == key), None)


def instance(cap, key, position):
    return {'id': key, 'type_id': 'workflow.composite', 'type_version': '1', 'title': cap['title'], 'position': position,
            'config': {'capability_kind': cap.get('kind', 'workflow'), **({'capability_source': deepcopy(cap['source'])} if cap.get('source') else {}), 'definition': {**deepcopy(cap['definition']), 'id': cap['id'], 'version': cap['version'], 'library_version': cap['version'], 'kind': cap.get('kind', 'workflow')}}}


def assistant_catalog(store, mode):
    from .privacy import contains_api
    from .community import assistant_entries
    return [{'id': c['id'], 'kind': c.get('kind', 'workflow'), 'version': c['version'],
             'execution': 'api' if contains_api(c['definition']) else 'local',
             'title': 'Private local capability' if mode == 'api' and c.get('private', True) else c['title'],
             'description': '' if mode == 'api' and c.get('private', True) else c['description'],
             'inputs': [] if mode == 'api' and c.get('private', True) else [{'id': p['id'], 'kind': p['kind']} for p in c['definition'].get('inputs', [])],
             'outputs': [] if mode == 'api' and c.get('private', True) else [{'id': p['id'], 'kind': p['kind']} for p in c['definition'].get('outputs', [])]} for c in list_capabilities(store)] + assistant_entries()
