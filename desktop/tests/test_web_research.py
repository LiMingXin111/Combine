"""Explicit synthetic HTTP contracts; live public API checks are recorded separately."""
from copy import deepcopy
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import web, providers
from runtime.engine import Engine, Waiting, value
from runtime.graphs import wrap, offline_report
from runtime.privacy import contains_api
from runtime.service import Service
from runtime.assistant import chat


def page(content='Public fixture page content'):
    return {'title': 'Public fixture', 'url': 'https://example.com/article', 'content': content, 'summary': 'Public summary', 'snippet': 'Public snippet', 'date': '2026-10-19T12:00:00Z'}


def completion(content=None, calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls: message.update(tool_calls=calls, reasoning_content='Fixture reasoning must be returned to DeepSeek')
    return {'choices': [{'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}], 'model': 'synthetic-model', 'usage': {'total_tokens': 11}}


def call(name='web_search', arguments=None):
    return {'id': 'call_public', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments or {'query': 'public documentation', 'count': 2})}}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-web-')
        self.api = Service(Path(self.temp.name) / 'workspace')
        self.store = self.api.store
        self.provider = self.store.put('provider', 'deepseek_fixture', {'name': 'Synthetic DeepSeek', 'base_url': web.ORIGIN, 'model': 'synthetic-model', 'enabled': True, 'credential_saved': True})
        self.store.put('setting', 'ai', {'mode': 'api', 'provider_id': self.provider['id']})
        self.secret = patch('runtime.vault.read', return_value='synthetic-secret-never-real')
        self.secret.start()
        self.dns = patch('runtime.web.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.215.14', 443))])
        self.dns.start()

    def tearDown(self):
        self.secret.stop(); self.dns.stop(); self.temp.cleanup()

    def test_search_actual_contract_origin_key_and_sources(self):
        with patch('runtime.providers.request_json', return_value={'results': [page()]}) as http:
            result = web.request(self.store, 'search', {'query': 'Public query', 'count': 2, 'need_content': True})
        self.assertEqual(http.call_args.args[:2], (web.ORIGIN + '/v1/tools/search', 'synthetic-secret-never-real'))
        self.assertEqual(http.call_args.args[2], {'query': 'Public query', 'count': 2, 'need_content': True})
        self.assertEqual(result['sources'][0]['published_at'], page()['date'])
        self.assertTrue(result['sources'][0]['retrieved_at'])
        self.assertEqual(result['sources'][0]['engine'], 'DeepSeek Search')
        usage = self.store.list('usage')[0]
        self.assertEqual(usage['estimated_cost'], 0.00075)
        self.assertEqual(usage['currency'], 'USD')

    def test_fetch_does_not_send_key_to_target_and_marks_truncation(self):
        with patch('runtime.providers.request_json', return_value={'title': 'Actual title', 'content': 'X' * 120001}) as http:
            result = web.request(self.store, 'fetch', {'url': 'https://example.com/page#fragment'})
        self.assertEqual(http.call_args.args[0], web.ORIGIN + '/v1/tools/fetch')
        self.assertEqual(http.call_args.args[2], {'url': 'https://example.com/page'})
        self.assertTrue(result['pages'][0]['content_truncated'])
        self.assertEqual(len(result['pages'][0]['content']), 120000)

    def test_public_url_rejects_private_files_credentials_and_ports(self):
        for url in ['file:///c:/private.docx', 'http://localhost/x', 'https://x.local/path', 'http://127.0.0.1/x', 'http://10.0.0.1', 'https://169.254.169.254/x', 'http://[::1]/', 'https://name:secret@example.com/', 'https://example.com:9443/', 'https://example.com/\nsecret', 'https://127.1/', 'http://2130706433/']:
            with self.subTest(url=url), self.assertRaises(ValueError): web.public_url(url)
        self.assertEqual(web.public_url('https://example.com'), 'https://example.com/')

    def test_invalid_queries_never_call_network(self):
        for params in [{'query': ''}, {'query': 'x' * 129}, {'query': 'ok', 'count': True}, {'query': 'ok', 'count': 51}, {'query': 'ok', 'need_content': 'false'}, {'query': 'ok', 'api_key': 'secret'}]:
            with patch('runtime.providers.request_json') as http, self.assertRaises(ValueError): web.request(self.store, 'search', params)
            http.assert_not_called()

    def test_private_dns_target_never_reaches_deepseek(self):
        with patch('runtime.web.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.5', 443))]), patch('runtime.providers.request_json') as http, self.assertRaises(ValueError):
            web.request(self.store, 'fetch', {'url': 'https://private.example.com/file'})
        http.assert_not_called()

    def test_private_filename_and_derived_output_never_leave(self):
        self.store.put('asset', 'asset_fixture', {'name': 'PRIVATE_CANARY_document.pdf'})
        self.store.put('node_run', 'private_run', {'outputs': {'text': value('text', 'PRIVATE_DERIVED_CANARY_123456789')}})
        for operation, params in [('search', {'query': 'PRIVATE_CANARY_document.pdf'}), ('search', {'query': 'PRIVATE_DERIVED_CANARY_123456789'}), ('fetch', {'url': 'https://example.com/PRIVATE_CANARY_document.pdf'})]:
            with patch('runtime.providers.request_json') as http, self.assertRaises(ValueError): web.request(self.store, operation, params)
            http.assert_not_called()

    def test_private_input_cannot_be_overridden_by_public_config(self):
        item = {'type_id': 'network.public_fetch', 'config': {'url': 'https://example.com'}}
        with patch('runtime.providers.request_json') as http, self.assertRaises(Waiting) as waiting:
            web.execute(self.store, item, {'text': value('text', 'Private material', True)})
        self.assertEqual(waiting.exception.state, 'waiting_privacy'); http.assert_not_called()

    def test_disabled_web_and_local_mode_reject_before_http(self):
        for values in [{'mode': 'api', 'web_enabled': False}, {'mode': 'local', 'web_enabled': True}]:
            self.store.put('setting', 'ai', {**values, 'provider_id': self.provider['id']})
            self.assertFalse(web.status(self.store)['available'])
            with patch('runtime.providers.request_json') as http, self.assertRaises(ValueError): web.request(self.store, 'search', {'query': 'public query'})
            http.assert_not_called()

    def test_proxy_or_ambiguous_keys_never_used_as_deepseek_credentials(self):
        self.store.put('provider', self.provider['id'], {**self.provider, 'base_url': 'https://api.example.com/v1'})
        with patch('runtime.providers.request_json') as http, self.assertRaises(ValueError): web.request(self.store, 'fetch', {'url': 'https://example.com'})
        http.assert_not_called()
        self.store.put('provider', 'second', {**self.provider, 'id': 'second'})
        self.store.put('provider', 'third', {**self.provider, 'id': 'third'})
        self.assertFalse(web.status(self.store)['available'])
        with self.assertRaises(ValueError): web.connection(self.store)
        self.assertEqual(web.connection(self.store, 'second')['id'], 'second')

    def test_empty_search_is_not_a_fabricated_answer_or_billable_result(self):
        with patch('runtime.providers.request_json', return_value={'results': []}):
            output = web.execute(self.store, {'type_id': 'network.public_search', 'config': {'query': 'public query'}}, {})
        self.assertEqual(output['text']['sources'], [])
        self.assertIn('No matching', output['text']['value'])
        self.assertEqual(self.store.list('usage')[0]['estimated_cost'], 0)

    def test_malformed_and_empty_fetch_fail_explicitly(self):
        for response in [{}, {'content': ''}, {'content': 123}, []]:
            with patch('runtime.providers.request_json', return_value=response), self.assertRaises(ValueError): web.request(self.store, 'fetch', {'url': 'https://example.com'})

    def test_two_new_runs_refetch_and_nested_web_uses_broker(self):
        graph = self.api.create({'template': 'blank'})
        graph['nodes'] = [{'id': 'web', 'type_id': 'network.public_fetch', 'config': {'url': 'https://example.com'}, 'position': {'x': 0, 'y': 0}}]
        graph = wrap(graph, ['web'])
        graph = wrap(graph, [graph['nodes'][0]['id']])
        self.assertTrue(contains_api(graph)); self.assertEqual(offline_report(graph, self.store)['status'], 'online')
        with patch('runtime.providers.request_json', return_value={'title': 'Public page', 'content': 'Public content'}) as http:
            for _ in range(2):
                run = Engine(self.store, remote_executor=lambda *a: self.fail('Web node must run on the broker')).start(graph)
                self.assertEqual(run['state'], 'completed')
        self.assertEqual(http.call_count, 2)

    def test_worker_cannot_invoke_web_transport(self):
        with patch.dict(os.environ, {'COMBINE_SANDBOX': 'appcontainer'}), patch('runtime.providers.request_json') as http, self.assertRaises(RuntimeError):
            web.request(self.store, 'search', {'query': 'public query'})
        http.assert_not_called()

    def test_web_outage_preserves_independent_local_branch(self):
        graph = self.api.create({'template': 'blank'})
        graph['nodes'] = [{'id': 'web', 'type_id': 'network.public_search', 'config': {'query': 'public query'}}, {'id': 'math', 'type_id': 'math.calculate', 'config': {'expression': '10 + 20'}}]
        with patch('runtime.providers.request_json', side_effect=ValueError('Synthetic web outage')):
            run = Engine(self.store).start(graph)
        self.assertEqual(run['state'], 'partial')
        self.assertEqual(self.store.get('node_run', run['id'] + ':math')['outputs']['text']['value'], '30')
        self.assertEqual(self.store.get('node_run', run['id'] + ':web')['state'], 'failed')

    def test_tool_loop_uses_actual_search_results_and_preserves_reasoning(self):
        captured = []
        def transport(url, key, payload, **kwargs):
            captured.append(deepcopy(payload))
            if url.endswith('/tools/search'): return {'results': [page()]}
            if any(m['role'] == 'tool' for m in payload['messages']):
                return completion(json.dumps({'reply': 'Found the public fixture.', 'actions': [], 'run': False}))
            return completion(calls=[call()])
        with patch('runtime.providers.request_json', side_effect=transport):
            result = providers.complete(self.store, [{'role': 'user', 'content': 'Find current public documentation.'}], json_mode=True, purpose='canvas_assistant')
        self.assertEqual(len(result['sources']), 1)
        self.assertEqual(result['web_activity'][0]['state'], 'succeeded')
        self.assertEqual(captured[-1]['messages'][-2]['reasoning_content'], 'Fixture reasoning must be returned to DeepSeek')
        self.assertEqual(len(self.store.list('usage')), 3)
        self.assertNotIn('reasoning_content', str(self.store.list('usage')))

    def test_failed_tool_is_reported_to_model_without_fabricated_evidence(self):
        replies = [completion(calls=[call('web_fetch', {'url': 'https://localhost/private'})]), completion('{"reply":"That URL is unavailable.","actions":[],"run":false}')]
        with patch('runtime.providers.request_json', side_effect=replies) as http:
            result = providers.complete(self.store, [{'role': 'user', 'content': 'Read a public page.'}], purpose='canvas_assistant')
        self.assertEqual(http.call_count, 2)
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['web_activity'][0]['state'], 'failed')
        self.assertIn('error', json.loads(http.call_args.args[2]['messages'][-1]['content']))

    def test_chat_keeps_evidence_through_json_repair(self):
        first = {'content': 'Invalid fixture JSON', 'model': 'synthetic', 'sources': [{'title': 'Public source', 'url': 'https://example.com', 'retrieved_at': '2026-10-20T00:00:00Z'}], 'web_activity': [{'tool': 'web_search', 'state': 'succeeded', 'source_count': 1}], 'web_evidence': [{'pages': [page()]}]}
        second = {'content': '{"reply":"Read the public source.","actions":[],"run":false}', 'model': 'synthetic'}
        with patch('runtime.providers.complete', side_effect=[first, second]) as complete:
            result = chat(self.store, {'message': 'Find public information.'}, lambda *a: None)
        self.assertFalse(result.get('failed'))
        self.assertEqual(len(result['message']['sources']), 1)
        self.assertIn('Public fixture page content', str(complete.call_args.args[1]))
        self.assertIsNone(result['workflow'])


if __name__ == '__main__': unittest.main()
