"""Provider discovery and selection contract tests; no network/model charges."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import providers, vault
from runtime.store import Store


class ProviderModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-models-')
        self.store = Store(Path(self.temp.name) / 'workspace')
        self.secret = 'synthetic-provider-key-never-sent'
        self.catalog = {'data': [{'id': 'model-flash', 'name': 'Fast model'}, {'id': 'model-pro', 'name': 'Pro model'}]}

    def tearDown(self):
        self.temp.cleanup()

    def saved(self):
        return providers.save_provider(self.store, {'name': 'Test', 'base_url': 'https://api.example.com/v1', 'model': 'model-flash', 'api_key': self.secret})

    def test_discover_before_saving_without_model_id_or_credential_persistence(self):
        with patch('runtime.providers.request_json', return_value=self.catalog) as request:
            result = providers.models(self.store, {'base_url': 'https://api.example.com/v1', 'api_key': self.secret})
        self.assertEqual(result, self.catalog['data'])
        self.assertEqual(request.call_args.args, ('https://api.example.com/v1/models', self.secret))
        self.assertEqual(request.call_args.kwargs['timeout'], 20)
        self.assertEqual(self.store.list('provider'), [])
        self.assertFalse(vault.location(self.store.root).exists())
        self.assertNotIn(self.secret.encode(), (self.store.root / 'workspace.sqlite3').read_bytes())

    def test_saved_discovery_uses_encrypted_key_and_caches_names(self):
        saved = self.saved()
        with patch('runtime.providers.request_json', return_value=self.catalog):
            result = providers.models(self.store, {'id': saved['id']})
        record = self.store.get('provider', saved['id'])
        self.assertEqual(record['model_catalog'], result)
        self.assertGreater(record['models_fetched_at'], 0)
        self.assertEqual(record['model'], 'model-flash')
        self.assertNotIn(self.secret, json.dumps(providers.list_providers(self.store)))

    def test_changed_endpoint_cannot_reuse_stored_key(self):
        saved = self.saved()
        with patch('runtime.providers.request_json') as request, patch('runtime.vault.read') as read:
            with self.assertRaisesRegex(ValueError, 'API key'):
                providers.models(self.store, {'id': saved['id'], 'base_url': 'https://other.example.com/v1'})
        request.assert_not_called()
        read.assert_not_called()

    def test_draft_key_or_endpoint_preview_does_not_replace_saved_catalog(self):
        saved = self.saved()
        self.store.update('provider', saved['id'], {'model_catalog': [{'id': 'existing'}]})
        with patch('runtime.providers.request_json', return_value=self.catalog):
            providers.models(self.store, {'id': saved['id'], 'base_url': 'https://other.example.com/v1', 'api_key': 'different-synthetic-key'})
        self.assertEqual(self.store.get('provider', saved['id'])['model_catalog'], [{'id': 'existing'}])
        self.assertEqual(vault.read(self.store.root, saved['id']), self.secret)

    def test_malformed_or_empty_list_is_explicit_and_does_not_erase_cache(self):
        saved = self.saved()
        self.store.update('provider', saved['id'], {'model_catalog': [{'id': 'existing'}]})
        for response in [{}, {'data': []}, {'data': [{'id': None}, {'id': 'bad\nmodel'}]}, []]:
            with patch('runtime.providers.request_json', return_value=response):
                with self.assertRaises(ValueError): providers.models(self.store, {'id': saved['id']})
        self.assertEqual(self.store.get('provider', saved['id'])['model_catalog'], [{'id': 'existing'}])

    def test_normalize_duplicates_and_missing_names(self):
        payload = {'data': [{'id': ' model-flash '}, {'id': 'model-flash'}, None, {'id': 'model-pro', 'display_name': 'Pro model'}]}
        with patch('runtime.providers.request_json', return_value=payload):
            result = providers.models(self.store, {'base_url': 'https://api.example.com', 'api_key': self.secret})
        self.assertEqual(result, [{'id': 'model-flash', 'name': 'model-flash'}, {'id': 'model-pro', 'name': 'Pro model'}])

    def test_switch_preserves_key_and_history_then_sends_exact_selected_model(self):
        saved = self.saved()
        self.store.put('conversation', 'history', {'messages': [{'content': 'Preserved public history'}]})
        self.store.update('provider', saved['id'], {'last_test': {'ok': True, 'model': 'model-flash'}})
        before = vault.path(self.store.root, saved['id']).read_bytes()
        providers.select_model(self.store, {'id': saved['id'], 'model': 'model-pro', 'make_default': True})
        self.assertEqual(before, vault.path(self.store.root, saved['id']).read_bytes())
        self.assertIsNone(self.store.get('provider', saved['id'])['last_test'])
        self.assertEqual(self.store.get('conversation', 'history')['messages'][0]['content'], 'Preserved public history')
        payload = {'choices': [{'message': {'content': 'Actual transport contract'}, 'finish_reason': 'stop'}], 'model': 'model-pro-build-42'}
        with patch('runtime.providers.request_json', return_value=payload) as request:
            result = providers.complete(self.store, [{'role': 'user', 'content': 'Public request'}])
        self.assertEqual(request.call_args.args[2]['model'], 'model-pro')
        self.assertEqual(result['usage']['requested_model'], 'model-pro')
        self.assertEqual(result['model'], 'model-pro-build-42')

    def test_manual_model_is_allowed_but_invalid_model_does_not_change_selection(self):
        saved = self.saved()
        providers.select_model(self.store, {'id': saved['id'], 'model': 'vendor/custom-model:latest'})
        for value in ['', '\n', 'bad\rmodel', None, 42, 'x' * 201]:
            with self.assertRaises(ValueError): providers.select_model(self.store, {'id': saved['id'], 'model': value})
        self.assertEqual(self.store.get('provider', saved['id'])['model'], 'vendor/custom-model:latest')

    def test_deepseek_short_connection_test_disables_default_thinking(self):
        saved = self.saved()
        self.store.update('provider', saved['id'], {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash'})
        payload = {'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]}
        with patch('runtime.providers.request_json', return_value=payload) as request:
            providers.test_provider(self.store, {'id': saved['id']})
        self.assertEqual(request.call_args.args[2]['thinking'], {'type': 'disabled'})
        self.assertEqual(request.call_args.args[2]['model'], 'deepseek-flash')

    def test_private_worker_cannot_discover_or_select_network_models(self):
        saved = self.saved()
        with patch.dict(os.environ, {'COMBINE_SANDBOX': 'appcontainer'}), patch('runtime.providers.request_json') as request:
            with self.assertRaises(RuntimeError): providers.models(self.store, {'id': saved['id']})
            with self.assertRaises(RuntimeError): providers.select_model(self.store, {'id': saved['id'], 'model': 'model-pro'})
        request.assert_not_called()

    def test_discovery_does_not_reexecute_cached_ai_but_model_switch_does(self):
        from runtime.service import Service
        self.saved()
        calls = []
        def cloud(*args):
            calls.append(True)
            return {'content': 'Synthetic transport contract output'}
        api = Service(self.store.root, cloud_client=cloud)
        graph = api.create({'template': 'ai', 'title': 'Cache contract'})
        graph['nodes'][0]['config']['text'] = 'A synthetic public request'
        graph = api.save({'graph': graph})['workflow']
        self.assertEqual(api.start({'workflow_id': graph['id']})['state'], 'completed')
        self.assertEqual(len(calls), 1)
        provider = providers.config(self.store)
        with patch('runtime.providers.request_json', return_value=self.catalog):
            providers.models(self.store, {'id': provider['id']})
        self.assertEqual(api.start({'workflow_id': graph['id']})['state'], 'completed')
        self.assertEqual(len(calls), 1)
        providers.select_model(self.store, {'id': provider['id'], 'model': 'model-pro'})
        self.assertEqual(api.start({'workflow_id': graph['id']})['state'], 'completed')
        self.assertEqual(len(calls), 2)


if __name__ == '__main__': unittest.main()
