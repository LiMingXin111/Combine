"""Trusted broker: launches all private operations inside the networkless worker."""
from pathlib import Path
import argparse
import json
import os
import sys
import uuid
import importlib.util

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Development brokers load their immutable revision instead of the release copy.
if __package__ in (None, ''):
    package_path = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('runtime', package_path / '__init__.py', submodule_search_locations=[str(package_path)])
    package = importlib.util.module_from_spec(spec)
    sys.modules['runtime'] = package
    spec.loader.exec_module(package)
from runtime.sandbox import launch


def dispatch(root, request):
    from runtime.devtrace import span
    with span(request.get('method'), request_id=request.get('id')) as outcome:
        result = _dispatch(root, request)
        if outcome is not None:
            outcome['rejected'] = 'error' in result
        return result


def _dispatch(root, request):
    from runtime.spaces import Spaces
    registry = Spaces(root)
    method = request.get('method')
    p = dict(request.get('params', {}))
    space_id = p.pop('_space_id', None) or registry.active()
    space = registry.get(space_id)
    root = registry.root(space_id)
    (root / 'ipc').mkdir(parents=True, exist_ok=True)
    request = {**request, 'params': p}
    if method == 'spaces.list': return {'id': request.get('id'), 'result': registry.list()}
    if method == 'spaces.create': return {'id': request.get('id'), 'result': registry.create(p.get('name'))}
    if method == 'spaces.activate': return {'id': request.get('id'), 'result': registry.activate(p.get('id'))}
    if method == 'spaces.rename': return {'id': request.get('id'), 'result': registry.rename(space_id, p.get('name'))}
    if method == 'workspace.checkpoint':
        results = []
        for item in registry.list()['items']:
            target = registry.root(item['id'])
            if (target / 'workspace.sqlite3').is_file():
                results.append(local_call(target, 'workspace.checkpoint', {}))
        return {'id': request.get('id'), 'result': {'saved': True, 'spaces': len(results)}}
    if method and method.startswith(('account.', 'team.')):
        from runtime.teams import dispatch as team_dispatch
        result = team_dispatch(registry, space_id, method, p, lambda name, params: local_call(root, name, params))
        return {'id': request.get('id'), 'result': result}
    if method == 'github.inspect':
        from runtime.github_import import inspect
        from runtime.store import Store
        return {'id': request.get('id'), 'result': inspect(Store(root), p)}
    if method == 'map.tile':
        from runtime.maps import tile
        from runtime.store import Store
        return {'id': request.get('id'), 'result': tile(Store(root), p)}
    if method in ['provider.list', 'provider.save', 'provider.delete', 'provider.test', 'provider.models', 'provider.select_model', 'ai.settings', 'ai.chat']:
        from runtime.store import Store
        from runtime import providers
        store = Store(root)
        if method == 'provider.list': result = providers.list_providers(store)
        elif method == 'provider.save': result = providers.save_provider(store, p)
        elif method == 'provider.delete': result = providers.delete_provider(store, p)
        elif method == 'provider.test': result = providers.test_provider(store, p)
        elif method == 'provider.models': result = providers.models(store, p)
        elif method == 'provider.select_model': result = providers.select_model(store, p)
        elif method == 'ai.settings':
            values = p.get('values', {})
            if set(values) - {'mode', 'provider_id', 'auto_classify', 'web_enabled', 'web_provider_id'} or values.get('mode', 'api') not in ['api', 'local']: raise ValueError('Invalid AI preferences.')
            if 'web_enabled' in values and type(values['web_enabled']) is not bool: raise ValueError('Web access must be enabled or disabled.')
            if values.get('web_provider_id'):
                from runtime.web import official
                if not official(store.get('provider', values['web_provider_id'], {})): raise ValueError('Choose an official DeepSeek connection for web research.')
            result = store.put('setting', 'ai', {**store.get('setting', 'ai', {}), **values})
        else:
            from runtime.assistant import chat
            result = chat(store, p, lambda method, params: local_call(root, method, params))
        return {'id': request.get('id'), 'result': result}
    if method == 'run.start':
        from runtime.store import Store
        from runtime.privacy import contains_api
        store = Store(root)
        existing = store.get('run', p.get('run_id', ''), {})
        graph = existing.get('graph') or store.get('workflow', p.get('workflow_id', ''), {})
        if contains_api(graph):
            from runtime.service import Service
            from runtime.engine import Waiting
            from runtime.providers import complete
            def execute(node, inputs, run_id):
                reply = local_call(root, 'node.execute', {'node': node, 'inputs': inputs, 'run_id': run_id})
                if 'waiting' in reply: raise Waiting(reply['waiting'], reply['message'])
                return reply['outputs']
            result = Service(root, execute, lambda messages, provider, tokens: complete(store, messages, provider, purpose='workflow_node', max_tokens=tokens)).start(p)
            return {'id': request.get('id'), 'result': result}
    if method in ['resource.install', 'resource.verify', 'resource.cancel']:
        from runtime.resources import install, verify
        if set(p) != {'id'}: raise ValueError('Resource operations accept only an approved resource identifier.')
        if method == 'resource.cancel':
            from runtime.store import Store
            return {'id': request.get('id'), 'result': Store(root).update('resource', p['id'], {'control': 'cancel'})}
        return {'id': request.get('id'), 'result': (install if method == 'resource.install' else verify)(root, p['id'])}
    result = isolated(root, request)
    if method == 'workspace.snapshot' and 'result' in result:
        from runtime.providers import list_providers
        from runtime.store import Store
        result['result']['ai']['providers'] = list_providers(Store(root))
        result['result']['workspace'].update(space_id=space_id, kind=space['kind'], team=space['team'])
    if method == 'workspace.settings' and 'result' in result and result['result'].get('workspace_name'):
        registry.rename(space_id, result['result']['workspace_name'])
    if method == 'workspace.diagnostics' and 'result' in result:
        from runtime.teams import account_info
        account = account_info(registry)
        result['result']['online_accounts'] = 'Team account connected: ' + account['server'] if account['connected'] else 'Optional team account not connected. Local work is independent.'
        result['result']['legacy_service'] = 'Optional account/team service. Only explicitly reviewed workflow templates are uploaded; no private workspace synchronization.'
        result['result']['space'] = {'id': space_id, 'kind': space['kind'], 'team': space['team']}
    return result


def local_call(root, method, params):
    result = isolated(root, {'id': 'local', 'method': method, 'params': params})
    if 'error' in result: raise ValueError(result['error']['message'])
    return result['result']


def isolated(root, request):
    from runtime.devtrace import span
    with span(request.get('method'), phase='worker') as outcome:
        result = _isolated(root, request)
        if outcome is not None:
            outcome['rejected'] = 'error' in result
        return result


def _isolated(root, request):
    token = uuid.uuid4().hex
    source = root / 'ipc' / (token + '.request.json')
    destination = root / 'ipc' / (token + '.response.json')
    source.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    try:
        directory = Path(__file__).resolve().parent.parent
        resources = directory if (directory / 'python/python.exe').is_file() else directory / 'src-tauri/resources'
        python = resources / 'python/python.exe'
        entry = Path(__file__).with_name('entry.py') if directory == resources else resources / 'runtime/entry.py'
        if not python.is_file() or not entry.is_file(): raise RuntimeError('The packaged local runtime is missing. Prepare the desktop runtime first.')
        launch(python, entry, root, source, destination)
        return json.loads(destination.read_text(encoding='utf-8'))
    finally:
        source.unlink(missing_ok=True); destination.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.buffer.read().decode('utf-8'))
        result = dispatch(args.root, request)
    except Exception as error:
        result = {'error': {'code': 'DESKTOP_RUNTIME', 'message': str(error)}}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__': main()
