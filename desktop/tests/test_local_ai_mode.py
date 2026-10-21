"""Local mode, resource readiness and provider maintenance; no real model/network calls."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import providers, vault
from runtime.assistant import chat
from runtime.resources import catalog, local_ai_status
from runtime.service import Service


class LocalModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-local-mode-')
        self.service = Service(Path(self.temp.name) / 'workspace')
        self.store = self.service.store

    def tearDown(self): self.temp.cleanup()

    def provider(self, **extra):
        return providers.save_provider(self.store, {'name': 'Synthetic API', 'base_url': 'https://api.example.com/v1', 'model': 'synthetic-model', 'api_key': 'synthetic-private-test-key', **extra})

    def prepared(self):
        for key in ['llama-cpu', 'qwen3vl-4b']:
            file = self.store.root / 'resources' / (key + '.fixture')
            file.write_text('Explicit metadata fixture, not a model binary', encoding='utf-8')
            stat = file.stat()
            self.store.put('resource', key, {'status': 'ready', 'files': [{'path': file.relative_to(self.store.root).as_posix(), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}]})

    def test_snapshot_identifies_local_model_and_missing_runtime_without_network(self):
        self.store.put('setting', 'ai', {'mode': 'local'})
        with patch('runtime.providers.request_json') as request:
            local = self.service.snapshot({})['ai']['local']
        self.assertEqual(local['model'], 'Qwen3-VL 4B')
        self.assertFalse(local['ready'])
        self.assertEqual([r['status'] for r in local['requirements']], ['missing', 'missing'])
        request.assert_not_called()

    def test_readiness_requires_both_runtime_and_model_and_current_files(self):
        self.prepared()
        self.assertTrue(local_ai_status(self.store, catalog(self.store))['ready'])
        file = self.store.root / 'resources/qwen3vl-4b.fixture'
        file.write_text('Changed fixture', encoding='utf-8')
        local = local_ai_status(self.store, catalog(self.store))
        self.assertFalse(local['ready'])
        self.assertEqual(local['requirements'][1]['status'], 'needs_verification')
        file.unlink()
        self.assertFalse(local_ai_status(self.store, catalog(self.store))['ready'])

    def test_downloading_is_not_ready(self):
        self.prepared()
        resources = catalog(self.store)
        resources[0]['status'] = 'downloading'
        local = local_ai_status(self.store, resources)
        self.assertFalse(local['ready'])
        self.assertEqual(local['requirements'][0]['status'], 'downloading')

    def test_saving_first_inactive_api_connection_preserves_local_mode(self):
        self.store.put('setting', 'ai', {'mode': 'local', 'web_enabled': False})
        saved = self.provider(make_default=False)
        settings = self.store.get('setting', 'ai')
        self.assertEqual(settings['mode'], 'local')
        self.assertEqual(settings['provider_id'], saved['id'])
        self.assertFalse(settings['web_enabled'])

    def test_editing_saved_default_does_not_activate_api_without_explicit_choice(self):
        saved = self.provider()
        self.store.update('setting', 'ai', {'mode': 'local'})
        before = vault.path(self.store.root, saved['id']).read_bytes()
        providers.save_provider(self.store, {**saved, 'api_key': '', 'model': 'updated-fixture-model', 'make_default': False})
        self.assertEqual(self.store.get('setting', 'ai')['mode'], 'local')
        self.assertEqual(before, vault.path(self.store.root, saved['id']).read_bytes())

    def test_deleting_default_preserves_local_mode_and_other_preferences(self):
        saved = self.provider()
        self.store.update('setting', 'ai', {'mode': 'local', 'web_enabled': False, 'auto_classify': False})
        providers.delete_provider(self.store, {'id': saved['id']})
        settings = self.store.get('setting', 'ai')
        self.assertEqual({key: settings[key] for key in ['mode', 'provider_id', 'web_enabled', 'auto_classify']},
                         {'mode': 'local', 'provider_id': None, 'web_enabled': False, 'auto_classify': False})

    def test_explicit_api_activation_still_works(self):
        self.store.put('setting', 'ai', {'mode': 'local'})
        self.provider(make_default=True)
        self.assertEqual(self.store.get('setting', 'ai')['mode'], 'api')

    def test_local_mode_rejects_cloud_completion_before_http(self):
        self.provider()
        self.store.update('setting', 'ai', {'mode': 'local'})
        with patch('runtime.providers.request_json') as request:
            with self.assertRaisesRegex(ValueError, 'Local-only AI'):
                providers.complete(self.store, [{'role': 'user', 'content': 'Private fixture'}])
        request.assert_not_called()
        self.assertEqual(self.store.list('usage'), [])

    def test_local_conversation_routes_only_to_local_executor(self):
        self.provider()
        self.store.update('setting', 'ai', {'mode': 'local'})
        calls = []
        def local(method, params):
            calls.append(method)
            return {'content': json.dumps({'reply': 'Synthetic routing receipt', 'actions': [], 'run': False})}
        with patch('runtime.providers.complete') as cloud, patch('runtime.providers.request_json') as request:
            result = chat(self.store, {'message': 'Local routing fixture', 'conversation_id': 'local_fixture'}, local)
        self.assertFalse(result.get('failed', False))
        self.assertEqual(calls, ['ai.local_completion'])
        cloud.assert_not_called()
        request.assert_not_called()
        reply = self.store.get('conversation', 'local_fixture')['messages'][-1]
        self.assertEqual(reply['model'], 'Qwen3-VL 4B (local)')
        self.assertEqual(reply['scope'], 'local')


if __name__ == '__main__': unittest.main()
