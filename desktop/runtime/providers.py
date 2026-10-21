"""Real OpenAI-compatible HTTP transport. Keys never enter workspace records."""
from pathlib import Path
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from . import vault
from .privacy import assert_public_payload
from .store import new_id, now


def trusted():
    if os.environ.get('COMBINE_SANDBOX') == 'appcontainer': raise RuntimeError('Network model access is disabled in the private worker.')


def endpoint(value):
    parsed = urllib.parse.urlsplit(str(value).strip().rstrip('/'))
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use an HTTPS API base URL without credentials or query parameters.')
    if parsed.hostname.lower() in ['localhost', 'localhost.localdomain'] or parsed.hostname.lower().endswith(('.local', '.internal')):
        raise ValueError('Use a public HTTPS provider endpoint. Local models are configured separately.')
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global: raise ValueError('Private network API endpoints are not supported.')
    except ValueError as error:
        if 'not supported' in str(error): raise
    return urllib.parse.urlunsplit(parsed).rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('The provider redirected an authenticated request. Update its base URL explicitly.')


def request_json(url, key, payload=None, timeout=180):
    trusted()
    parsed = urllib.parse.urlsplit(url)
    for _, _, _, _, address in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM):
        if not ipaddress.ip_address(address[0]).is_global: raise ValueError('The provider resolved to a private network address.')
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'User-Agent': 'Combine-Desktop/0.2'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024: raise ValueError('The model response exceeded the supported response size.')
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        hints = {401: 'Check your API key.', 403: 'Check your account permissions.', 402: 'Check your provider balance.', 404: 'Check the base URL and model name.', 429: 'The provider is rate limiting requests. Retry when available.'}
        if url.startswith('https://api.deepseek.com/v1/tools/'):
            hints.update({400: 'Review the public query or URL.', 403: 'DeepSeek or the target page denied access.', 404: 'The page or web resource could not be read. Try another public source.', 429: 'DeepSeek web access is rate limited. Retry later.'})
            raise ValueError(f'DeepSeek web request failed (HTTP {error.code}). ' + hints.get(error.code, 'Retry later or use another public source.')) from None
        raise ValueError(f'Provider request failed (HTTP {error.code}). ' + hints.get(error.code, 'Review the provider status and model configuration.')) from None
    except (urllib.error.URLError, TimeoutError):
        if url.startswith('https://api.deepseek.com/v1/tools/'):
            raise ValueError('DeepSeek web tools could not be reached. Check the internet connection and retry; no webpage result was fabricated.') from None
        raise ValueError('The model service could not be reached. Your conversation and workflow are preserved; retry when connected.') from None


def list_providers(store):
    return [{**p, 'has_key': bool(p.get('credential_saved')) if os.environ.get('COMBINE_SANDBOX') == 'appcontainer' else vault.path(store.root, p['id']).is_file()} for p in store.list('provider')]


def model_id(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Choose a model or enter the exact model ID from your provider.')
    return value.strip()


def save_provider(store, p):
    trusted()
    old = store.get('provider', p.get('id', ''), {})
    key = old.get('id') or new_id('provider')
    base = endpoint(p.get('base_url', ''))
    secret = str(p.get('api_key', '')).strip()
    if secret and (len(secret) < 8 or '\n' in secret or '\r' in secret): raise ValueError('Enter a valid API key.')
    if old and old['base_url'] != base and not secret: raise ValueError('Enter the API key again when changing the provider endpoint.')
    if not secret and not vault.path(store.root, key).is_file(): raise ValueError('Enter an API key. It is encrypted on this computer.')
    model = model_id(p.get('model', ''))
    if secret: vault.save(store.root, key, secret)
    result = store.put('provider', key, {'name': str(p.get('name', '')).strip() or 'API connection', 'base_url': base, 'model': model,
                                      'protocol': 'openai-compatible', 'enabled': True, 'last_test': None, 'credential_saved': True,
                                      'credential_revision': new_id('credential') if secret else old.get('credential_revision')})
    if old.get('base_url') == base and not secret and old.get('model_catalog'):
        result = store.update('provider', key, {'model_catalog': old['model_catalog'], 'models_fetched_at': old.get('models_fetched_at')})
    settings = store.get('setting', 'ai', {'mode': 'api'})
    if not settings.get('provider_id') or p.get('make_default', True):
        store.put('setting', 'ai', {**settings, 'mode': 'api' if p.get('make_default', True) else settings.get('mode', 'api'), 'provider_id': key})
    return {**result, 'has_key': True}


def select_model(store, p):
    provider = config(store, p.get('id'))
    model = model_id(p.get('model'))
    if not vault.path(store.root, provider['id']).is_file():
        raise ValueError('Add the connection API key on this computer first.')
    changes = {'model': model}
    if model != provider['model']:
        changes['last_test'] = None
    saved = store.update('provider', provider['id'], changes)
    if p.get('make_default', False):
        settings = store.get('setting', 'ai', {})
        store.put('setting', 'ai', {**settings, 'mode': 'api', 'provider_id': provider['id']})
    return {**saved, 'has_key': True}


def delete_provider(store, p):
    trusted(); key = p['id']; vault.delete(store.root, key); store.delete('provider', key)
    settings = store.get('setting', 'ai', {})
    if settings.get('provider_id') == key: store.put('setting', 'ai', {**settings, 'provider_id': None})
    return {'deleted': True}


def config(store, provider_id=None):
    trusted(); settings = store.get('setting', 'ai', {})
    provider = store.get('provider', provider_id or settings.get('provider_id', ''))
    if not provider or not provider.get('enabled'): raise ValueError('Connect an AI model in Settings → AI connections. No test answer will be substituted.')
    return provider


class GenerationLimitError(ValueError):
    def __init__(self, content, purpose):
        self.partial_text = content if isinstance(content, str) else ''
        message = 'The model reached its generation limit before completing the node. Any partial draft is preserved; it is not passed downstream as a final answer.' if purpose == 'workflow_node' else 'The provider truncated its response. No partial canvas edit was applied. Retry with a smaller change.'
        super().__init__(message)


def complete(store, messages, provider_id=None, json_mode=False, purpose='assistant', max_tokens=4096):
    from .web import status, TOOLS, tool_call
    from copy import deepcopy
    # Only the canvas assistant gets automatic tools. Workflow web access is
    # explicit through executable nodes; private/local completions never enter.
    enabled = purpose == 'canvas_assistant' and status(store)['available']
    messages = deepcopy(messages)
    sources, activity, evidence = [], [], []
    length_retries = 0
    best_partial = ''
    for round_index in range(7):
        tools = TOOLS if enabled and round_index < 4 else None
        try:
            result = _complete(store, messages, provider_id, json_mode, purpose, max_tokens, tools)
        except GenerationLimitError as error:
            if len(error.partial_text) > len(best_partial): best_partial = error.partial_text
            if purpose != 'workflow_node' or length_retries >= 2 or max_tokens >= 65536:
                error.partial_text = best_partial
                raise
            # Preserve reasoning quality. A provider token limit is not a
            # financial budget: retry the node with room for its actual answer.
            length_retries += 1
            max_tokens = min(65536, max(16384, max_tokens * 2))
            continue
        message = result.pop('_message')
        calls = message.get('tool_calls', [])
        if not calls:
            return {**result, 'sources': sources, 'web_activity': activity, 'web_evidence': evidence, 'generation_retries': length_retries}
        if not tools or not isinstance(calls, list) or len(calls) > 8:
            raise ValueError('The model returned unsupported web tool calls. Retry with a smaller research request.')
        # DeepSeek thinking-mode calls require the original reasoning_content
        # alongside the tool calls; retain it only in memory for this request.
        messages.append(message)
        for call in calls:
            function = call.get('function', {})
            try:
                output = tool_call(store, function.get('name'), function.get('arguments'))
                for source in output['sources']:
                    if not any(s['url'] == source['url'] for s in sources): sources.append(source)
                activity.append({'tool': function['name'], 'state': 'succeeded', 'source_count': len(output['sources']), 'usage_id': output['usage_id']})
                # Bound model context, retaining original full captures in nodes
                # when a reusable workflow is requested.
                for page in output['pages']:
                    if len(page.get('content', '')) > 16000: page['content_truncated'] = True
                    page['content'] = page.get('content', '')[:16000]
                evidence.append(output)
            except ValueError as error:
                output = {'error': str(error), 'notice': 'This tool failed. Do not claim successful retrieval or invent data. Explain the limitation or try a different public source.'}
                activity.append({'tool': function.get('name'), 'state': 'failed', 'message': str(error)})
                evidence.append(output)
            messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(output, ensure_ascii=False)})
        if round_index == 3:
            messages.append({'role': 'system', 'content': 'Finish with the evidence already retrieved and the required JSON envelope. Disclose missing evidence. For more research, create web nodes as a reusable workflow; do not invent results.'})
    raise ValueError('The web research response could not be completed. Saved canvas changes are preserved.')


def _complete(store, messages, provider_id=None, json_mode=False, purpose='assistant', max_tokens=4096, tools=None):
    trusted()
    if store.get('setting', 'ai', {}).get('mode') == 'local': raise ValueError('Local-only AI is selected. Switch to API AI in Settings to make a network model call.')
    assert_public_payload(store, messages)
    provider = config(store, provider_id)
    payload = {'model': provider['model'], 'messages': messages, 'stream': False, 'max_tokens': max_tokens}
    if urllib.parse.urlsplit(provider['base_url']).hostname == 'api.deepseek.com' and purpose in ['connection_test', 'canvas_repair']:
        payload['thinking'] = {'type': 'disabled'}
    if urllib.parse.urlsplit(provider['base_url']).hostname == 'api.openai.com':
        payload['max_completion_tokens'] = payload.pop('max_tokens')
    if json_mode: payload['response_format'] = {'type': 'json_object'}
    if tools: payload['tools'] = tools
    start = now()
    result = request_json(provider['base_url'] + '/chat/completions', vault.read(store.root, provider['id']), payload)
    choice = next(iter(result.get('choices', [])), {})
    message = choice.get('message', {})
    content = message.get('content')
    usage = result.get('usage', {})
    ledger = store.put('usage', new_id('usage'), {'provider_id': provider['id'], 'model': result.get('model', provider['model']), 'requested_model': provider['model'], 'purpose': purpose,
                      'prompt_tokens': usage.get('prompt_tokens'), 'completion_tokens': usage.get('completion_tokens'), 'total_tokens': usage.get('total_tokens'),
                      'provider_request_id': result.get('id'), 'created_at': start, 'duration_ms': now() - start,
                      'response_format': 'json_object' if json_mode else 'text',
                      'response_characters': len(content) if isinstance(content, str) else 0,
                      'finish_reason': choice.get('finish_reason') if choice.get('finish_reason') in ['stop', 'length', 'tool_calls', 'content_filter', 'insufficient_system_resource'] else 'unknown',
                      'requested_max_tokens': max_tokens, 'reasoning_tokens': usage.get('completion_tokens_details', {}).get('reasoning_tokens'),
                      'estimated_cost': None, 'pricing_status': 'unavailable'})
    if choice.get('finish_reason') == 'length': raise GenerationLimitError(content, purpose)
    if not message.get('tool_calls') and (not isinstance(content, str) or not content.strip()): raise ValueError('The model returned no usable answer. No simulated answer was substituted.')
    return {'content': content.strip() if isinstance(content, str) else '', 'usage': ledger, 'provider_id': provider['id'], 'model': ledger['model'], '_message': message}


def test_provider(store, p):
    result = complete(store, [{'role': 'user', 'content': 'Reply with exactly OK.'}], p.get('id'), purpose='connection_test', max_tokens=32)
    store.update('provider', result['provider_id'], {'last_test': {'ok': True, 'at': now(), 'model': result['model']}})
    return {'ok': True, 'reply': result['content'], 'model': result['model'], 'usage': result['usage']}


def models(store, p):
    trusted()
    old = store.get('provider', p.get('id', ''), {})
    base = endpoint(p.get('base_url', old.get('base_url', '')))
    secret = str(p.get('api_key', '')).strip()
    if secret:
        if len(secret) < 8 or '\n' in secret or '\r' in secret:
            raise ValueError('Enter a valid API key to fetch available models.')
    elif old and base == old['base_url']:
        secret = vault.read(store.root, old['id'])
    else:
        raise ValueError('Enter the API key for this endpoint before fetching models.')
    result = request_json(base + '/models', secret, timeout=20)
    if not isinstance(result, dict) or not isinstance(result.get('data'), list):
        raise ValueError('This endpoint did not return a model list. Enter a model ID manually or check the API base URL.')
    catalog, seen = [], set()
    for item in result['data']:
        if not isinstance(item, dict): continue
        try: key = model_id(item.get('id'))
        except ValueError: continue
        if key in seen: continue
        name = item.get('name') or item.get('display_name') or key
        name = name.strip()[:200] if isinstance(name, str) else key
        catalog.append({'id': key, 'name': name or key})
        seen.add(key)
        if len(catalog) == 2000: break
    if not catalog:
        raise ValueError('No models were returned for this key. Check account access or enter a model ID manually.')
    if old and 'base_url' not in p and 'api_key' not in p:
        store.update('provider', old['id'], {'model_catalog': catalog, 'models_fetched_at': now()})
    return catalog
