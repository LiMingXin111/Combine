"""Stable executable node definitions; visual grouping is never execution."""
from copy import deepcopy


def node(key, title, category, description, inputs, outputs, config, network='none', resources=None):
    return {'id': key, 'version': '1', 'title': title, 'category': category, 'description': description,
            'inputs': [{'id': k, 'kind': v, 'required': True} for k, v in inputs],
            'outputs': [{'id': k, 'kind': v} for k, v in outputs], 'defaults': config,
            'network': network, 'resources': resources or [], 'executor_version': '1'}


TYPES = [
    node('input.prompt', 'Shared prompt', 'Input', 'Information you choose to share with your configured API model.', [], [('text', 'text')], {'text': ''}),
    node('input.public_table', 'Shared table', 'Input', 'CSV explicitly shared in an API conversation. Local file tables remain private.', [], [('table', 'table')], {'csv': 'Category,Amount\nResearch,120\nDesign,80\nDevelopment,240'}),
    node('input.text', 'Text input', 'Input', 'Write or paste information into your local workflow.', [], [('text', 'text')], {'text': ''}),
    node('input.image', 'Image', 'Input', 'Display an imported image directly on your canvas. Its content stays local.', [], [('image', 'image')], {'asset_id': ''}),
    node('input.file', 'Local file', 'Input', 'Read an imported document with its source locations.', [], [('text', 'text'), ('table', 'table')], {'asset_id': ''}),
    node('input.table', 'Table input', 'Input', 'Read CSV data without executing formulas.', [], [('table', 'table')], {'csv': 'Category,Amount\nResearch,120\nDesign,80\nDevelopment,240'}),
    node('transform.text', 'Transform text', 'Processing', 'Trim, change case or apply a local text template.', [('text', 'text')], [('text', 'text')], {'operation': 'trim', 'template': '{text}'}),
    node('math.calculate', 'Calculate', 'Processing', 'Evaluate a restricted arithmetic expression locally.', [], [('text', 'text')], {'expression': '120 + 80 + 240'}),
    node('data.aggregate', 'Aggregate table', 'Processing', 'Calculate count, sum, average, minimum or maximum.', [('table', 'table')], [('table', 'table'), ('text', 'text')], {'column': 'Amount', 'operation': 'sum'}),
    node('data.merge_text', 'Combine text', 'Processing', 'Join 2–16 text branches in order before AI synthesis. Preserves sources and privacy. Ports are text_1 through text_N, controlled by input_count.', [('text_1', 'text'), ('text_2', 'text')], [('text', 'text')], {'input_count': 2, 'labels': 'Input 1\nInput 2', 'separator': '\n\n'}),
    node('model.local', 'Local AI', 'AI', 'Generate or rewrite with a downloaded on-device model. No cloud fallback.', [('text', 'text')], [('text', 'text')], {'instruction': 'Write a clear, concise summary. Preserve source references.', 'max_tokens': 768, 'image_asset_id': ''}, resources=['qwen3vl-4b', 'llama-cpu']),
    node('model.api', 'AI model', 'AI', 'Use your API model for reasoning, writing or a custom skill. Private local sources are blocked.', [('text', 'text')], [('text', 'text')], {'instruction': 'Complete the requested task with clear, useful output.', 'provider_id': '', 'max_tokens': 8192}, network='required'),
    node('knowledge.search', 'Search knowledge', 'Knowledge', 'Search this workspace and preserve source references.', [], [('text', 'text')], {'query': '', 'limit': 5}),
    node('human.input', 'Human input', 'Interaction', 'Wait for an explicit response before continuing.', [], [('text', 'text')], {'prompt': 'Add your input', 'value': ''}),
    node('output.document', 'Document', 'Output', 'Keep a complete local document, including sources.', [('text', 'text')], [('text', 'text')], {'heading': 'Untitled document'}),
    node('output.image', 'Image output', 'Output', 'Show an actual image on the canvas without uploading it.', [('image', 'image')], [('image', 'image')], {}),
    node('output.table', 'Table', 'Output', 'Display a complete data table and export it as CSV.', [('table', 'table')], [('table', 'table')], {}),
    node('output.chart', 'Chart', 'Output', 'Visualize numeric columns using local rendering.', [('table', 'table')], [('chart', 'chart')], {'label_column': 'Category', 'value_column': 'Amount'}),
    node('output.map', 'Offline map', 'Output', 'Show known coordinates without remote map tiles or geocoding.', [('table', 'table')], [('map', 'map')], {'latitude': 'lat', 'longitude': 'lng', 'label': 'name'}),
    node('output.live_map', 'Interactive map', 'Output', 'Render public coordinates on an interactive OpenStreetMap layer. Private coordinates remain local.', [('table', 'table')], [('map', 'map')], {'latitude': 'lat', 'longitude': 'lng', 'label': 'name'}, network='required'),
    node('output.file', 'Export file', 'Output', 'Write a Markdown file inside this workspace.', [('text', 'text')], [('text', 'text')], {'filename': 'report.md'}),
    node('network.public_search', 'Web search', 'Online', 'Search public pages with DeepSeek. Returns real sources, timestamps, text and a results table. Private inputs are blocked.', [], [('text', 'text'), ('table', 'table')], {'query': '', 'count': 5, 'need_content': True, 'provider_id': ''}, network='required'),
    node('network.public_fetch', 'Public web page', 'Online', 'Read a public URL as Markdown with DeepSeek Fetch. Saves its source and retrieval time. Private inputs are blocked.', [], [('text', 'text')], {'url': '', 'provider_id': ''}, network='required'),
    node('workflow.composite', 'Composite node', 'Workflow', 'Run a versioned internal workflow through public ports.', [], [], {'definition': None}),
]
for item in TYPES:
    if item['id'] in ['network.public_search', 'network.public_fetch']:
        item['inputs'] = [{'id': 'text', 'kind': 'text', 'required': False}]
        item['executor_version'] = '2'
    # Capability groups remain independent of internet use and run status.
    key = item['id']
    item['capability'] = ('research' if key.startswith('network.') else 'knowledge' if key.startswith('knowledge.') else
                          'reasoning' if key.startswith('model.') else 'integration' if key == 'data.merge_text' else
                          'processing' if key.startswith(('data.', 'math.', 'transform.')) else 'interaction' if key.startswith('human.') else
                          'visualization' if key in ['output.chart', 'output.map', 'output.live_map', 'output.image'] else
                          'delivery' if key.startswith('output.') else 'workflow' if key.startswith('workflow.') else 'input')
BY_ID = {definition['id']: definition for definition in TYPES}


def definition(item):
    key = item.get('type_id') or item.get('data', {}).get('type_id')
    if key not in BY_ID: return None
    result = deepcopy(BY_ID[key])
    if key == 'data.merge_text':
        count = item.get('config', {}).get('input_count', 2)
        if type(count) is int and 2 <= count <= 16:
            labels = str(item.get('config', {}).get('labels', '')).splitlines()
            result['inputs'] = [{'id': f'text_{i + 1}', 'kind': 'text', 'required': True,
                                 'label': labels[i].strip()[:100] if i < len(labels) and labels[i].strip() else f'Input {i + 1}'} for i in range(count)]
    if key == 'workflow.composite':
        sub = item.get('config', {}).get('definition') or {}
        kind = item.get('config', {}).get('capability_kind') or sub.get('kind')
        if kind in ['skill', 'agent', 'expert']:
            result.update(capability=kind, category=kind.title(), title=kind.title() + ' node')
        result['inputs'] = [{'id': p['id'], 'kind': p['kind'], 'required': True} for p in sub.get('inputs', [])]
        result['outputs'] = [{'id': p['id'], 'kind': p['kind']} for p in sub.get('outputs', [])]
    return result
