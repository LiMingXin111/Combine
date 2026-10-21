"""Boundary and transaction tests. Provider calls are intercepted, never simulated in the UI."""
from copy import deepcopy
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.service import Service, template
from runtime.assistant import apply_actions, chat, undo
from runtime.privacy import safe_context, assert_public_payload
from runtime import providers, vault
from runtime.engine import Engine, Waiting, value


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-ai-')
        self.root = Path(self.temp.name) / 'workspace'
        self.service = Service(self.root)
        self.store = self.service.store

    def tearDown(self): self.temp.cleanup()

    def test_dpapi_key_never_enters_database_or_snapshot(self):
        if os.name != 'nt': self.skipTest('Windows DPAPI')
        secret = 'synthetic-test-credential-only-123456789'
        saved = providers.save_provider(self.store, {'name': 'Test', 'base_url': 'https://api.example.com/v1', 'model': 'configured-model', 'api_key': secret})
        self.assertEqual(vault.read(self.root, saved['id']), secret)
        self.assertNotIn(secret.encode(), vault.path(self.root, saved['id']).read_bytes())
        self.assertNotIn(secret.encode(), self.root.joinpath('workspace.sqlite3').read_bytes())
        self.assertNotIn(secret, json.dumps(self.service.snapshot({})))
        self.assertFalse(vault.path(self.root, saved['id']).is_relative_to(self.root))
        with self.assertRaises(ValueError): providers.save_provider(self.store, {**saved, 'base_url': 'https://other.example.com', 'api_key': ''})
        with patch.dict(os.environ, {'COMBINE_SANDBOX': 'appcontainer'}):
            with self.assertRaises(RuntimeError): vault.read(self.root, saved['id'])

    def test_public_endpoint_and_redirect_rules(self):
        for url in ['http://api.example.com', 'https://localhost/v1', 'https://127.0.0.1/v1', 'https://10.0.0.3', 'https://user:pass@example.com', 'https://example.com?key=x']:
            with self.assertRaises(ValueError): providers.endpoint(url)
        with self.assertRaises(ValueError): providers.NoRedirect().redirect_request(None, None, 302, None, None, 'https://other.example.com')

    def test_openai_request_uses_supported_completion_limit(self):
        self.store.put('provider', 'provider_contract', {'enabled': True, 'base_url': 'https://api.openai.com/v1', 'model': 'configured-model'})
        self.store.put('setting', 'ai', {'provider_id': 'provider_contract'})
        payload = {'choices': [{'message': {'content': 'A result'}, 'finish_reason': 'stop'}], 'usage': {'total_tokens': 4}}
        with patch('runtime.vault.read', return_value='contract-key'), patch('runtime.providers.request_json', return_value=payload) as request:
            providers.complete(self.store, [{'role': 'user', 'content': 'Public request'}], max_tokens=100)
        self.assertEqual(request.call_args.args[2]['max_completion_tokens'], 100)
        self.assertNotIn('max_tokens', request.call_args.args[2])

    def test_private_content_and_filename_block_before_credentials_or_network(self):
        self.service.save_knowledge({'title': 'Confidential-note.txt', 'content': 'PRIVATE_TRACE_3297492742937583247 internal private source material'})
        for text in ['PRIVATE_TRACE_3297492742937583247', 'internal private source material', 'Confidential-note.txt']:
            with patch('runtime.providers.request_json') as request, patch('runtime.vault.read') as read:
                with self.assertRaises(ValueError): providers.complete(self.store, [{'role': 'user', 'content': text}])
                request.assert_not_called(); read.assert_not_called()

    def test_redacts_private_sources_downstream_and_notes_but_preserves_controls(self):
        graph = template('text', 'Private workflow')
        graph['nodes'][2]['title'] = 'Secret derived title'
        graph['annotations'] = [{'id': 'note1', 'text': 'Private secret note', 'position': {'x': 5, 'y': 9}, 'private': True}]
        context = safe_context(graph)
        text = json.dumps(context)
        self.assertNotIn('Secret derived title', text)
        self.assertNotIn('Private secret note', text)
        self.assertEqual(context['annotations'][0]['id'], 'note1')
        self.assertEqual(context['edges'], graph['edges'])

    def test_action_batch_is_atomic_and_validates_ports(self):
        graph = template('text', 'Test')
        prior = deepcopy(graph)
        with self.assertRaises(ValueError): apply_actions(graph, [{'op': 'set_title', 'title': 'Changed'}, {'op': 'add_edge', 'source': 'report', 'target': 'source', 'sourceHandle': 'wrong', 'targetHandle': 'wrong'}], self.store)
        self.assertEqual(graph, prior)
        self.assertEqual(self.store.list('workflow'), [])

    def test_source_protection_and_local_edit(self):
        graph = template('text', 'Test')
        action = [{'op': 'update_node', 'id': 'source', 'config': {'text': 'Revised locally'}}]
        with self.assertRaises(ValueError): apply_actions(graph, action, self.store)
        updated, *_ = apply_actions(graph, action, self.store, 'local')
        self.assertEqual(updated['nodes'][0]['config']['text'], 'Revised locally')

    def test_notes_edges_move_delete_and_undo(self):
        graph = self.service.create({'template': 'text', 'title': 'Test'})
        actions = [{'op': 'add_note', 'id': 'n1', 'text': 'A note', 'position': {'x': 10, 'y': 20}}, {'op': 'move_node', 'id': 'source', 'position': {'x': 30, 'y': 40}}, {'op': 'update_edge', 'id': graph['edges'][0]['id'], 'label': 'Source data'}]
        changed, *_ = apply_actions(graph, actions, self.store)
        self.service.save({'graph': changed})
        restored = undo(self.store, {'workflow_id': graph['id']})['workflow']
        self.assertEqual(restored['nodes'][0]['position'], graph['nodes'][0]['position'])
        self.assertFalse(restored.get('annotations'))
        self.assertEqual(restored['version'], 3)

    def test_capability_is_real_pinned_executable_graph(self):
        definition = {'nodes': [{'id': 'trim', 'title': 'Trim', 'type_id': 'transform.text', 'type_version': '1', 'position': {'x': 0, 'y': 0}, 'config': {'operation': 'trim'}}], 'edges': [], 'inputs': [{'id': 'text', 'kind': 'text', 'node': 'trim', 'port': 'text'}], 'outputs': [{'id': 'text', 'kind': 'text', 'node': 'trim', 'port': 'text'}]}
        graph = template('text', 'Test')
        actions = [{'op': 'register_capability', 'title': 'Trimmer', 'definition': definition, 'node_id': 'cap1'}, {'op': 'add_edge', 'source': 'source', 'target': 'cap1', 'sourceHandle': 'text', 'targetHandle': 'text'}]
        updated, caps, *_ = apply_actions(graph, actions, self.store)
        self.assertEqual(len(caps), 1)
        self.store.put('workflow', updated['id'], updated)
        run = self.service.start({'workflow_id': updated['id']})
        self.assertEqual(run['state'], 'completed')
        records = self.service.run_detail({'id': run['id']})['nodes']
        self.assertTrue(any(r['node_id'] == 'cap1/trim' for r in records))

    def test_private_input_never_calls_api_executor(self):
        engine = Engine(self.store, cloud_client=lambda *a: self.fail('Private data reached API'))
        with self.assertRaises(Waiting) as raised:
            engine.execute({'type_id': 'model.api', 'config': {}}, {'text': value('text', 'Protected source', True)})
        self.assertEqual(raised.exception.state, 'waiting_privacy')

    def test_missing_model_classification_is_explicit(self):
        note = self.service.save_knowledge({'title': 'Research', 'content': 'Private research note'})
        result = self.service.classify_knowledge({'id': note['id']})
        self.assertEqual(result['category_status'], 'needs_local_model')
        self.assertNotIn('category', result)

    def test_failed_model_edit_keeps_original_and_records_failure(self):
        graph = self.service.create({'template': 'text', 'title': 'Original'})
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', return_value={'content': '{broken', 'model': 'contract-test'}):
            result = chat(self.store, {'message': 'Edit this', 'workflow_id': graph['id'], 'conversation_id': 'contract-failure'}, lambda *a: None)
        self.assertTrue(result['failed'])
        self.assertEqual(self.service.workflow(graph['id']), graph)
        self.assertEqual(self.store.get('conversation', 'contract-failure')['messages'][-1]['error'], True)

    def test_idempotent_turn_does_not_repeat_provider_charge(self):
        response = {'content': json.dumps({'reply': 'Here is an answer.', 'actions': [], 'run': False}), 'model': 'contract-test'}
        params = {'message': 'A simple question', 'conversation_id': 'contract-idempotent', 'request_id': 'turn-contract'}
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', return_value=response) as completion:
            first = chat(self.store, params, lambda *a: None)
            second = chat(self.store, params, lambda *a: None)
            self.assertEqual(first, second)
            self.assertEqual(completion.call_count, 1)

    def test_note_layout_is_persisted_without_execution_version_change(self):
        graph = self.service.create({'template': 'text', 'title': 'Test'})
        graph['annotations'] = [{'id': 'note', 'text': 'A note', 'private': True, 'position': {'x': 0, 'y': 0}}]
        graph = self.service.save({'graph': graph})['workflow']
        self.service.layout({'id': graph['id'], 'positions': {'note': {'x': 100, 'y': 200}}})
        reopened = self.service.workflow(graph['id'])
        self.assertEqual(reopened['version'], graph['version'])
        self.assertEqual(reopened['annotations'][0]['position'], {'x': 100, 'y': 200})

    def test_registry_does_not_echo_content_of_local_sample_sources(self):
        self.service.create({'template': 'table', 'title': 'Local table'})
        def completion(store, messages, **kwargs):
            assert_public_payload(store, messages)
            return {'content': '{"reply":"Ready.","actions":[],"run":false}', 'model': 'contract-test'}
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', side_effect=completion):
            result = chat(self.store, {'message': 'Help me create a workflow', 'conversation_id': 'contract-registry'}, lambda *a: None)
        self.assertFalse(result.get('failed'))

    def test_private_map_request_rejected_before_http(self):
        from runtime.maps import tile
        self.store.put('node_run', 'run:map', {'outputs': {'map': value('map', {'points': [], 'live': True}, True)}})
        with patch('runtime.maps.urllib.request.urlopen') as request:
            with self.assertRaises(ValueError): tile(self.store, {'run_id': 'run', 'node_id': 'map', 'z': 1, 'x': 0, 'y': 0})
            request.assert_not_called()

    def test_invalid_action_gets_one_real_model_repair_before_commit(self):
        graph = self.service.create({'template': 'text', 'title': 'Original'})
        invalid = {'content': '{"reply":"Edit","actions":[{"op":"set_title","title":"Wrong"},{"op":"add_capability","capability_id":"invented"}],"run":false}', 'model': 'contract-test'}
        valid = {'content': '{"reply":"Corrected","actions":[{"op":"set_title","title":"Correct"}],"run":false}', 'model': 'contract-test'}
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', side_effect=[invalid, valid]) as completion:
            result = chat(self.store, {'message': 'Rename this workflow', 'workflow_id': graph['id'], 'conversation_id': 'contract-repair'}, lambda *a: None)
        self.assertEqual(completion.call_count, 2)
        self.assertEqual(result['workflow']['title'], 'Correct')
        self.assertEqual(result['workflow']['version'], 2)

    def test_private_taint_survives_complete_graph_execution(self):
        graph = self.service.create({'template': 'ai', 'title': 'Private boundary'})
        graph['nodes'][0]['type_id'] = 'input.text'
        graph['nodes'][0]['config']['text'] = 'Private imported content'
        self.store.put('provider', 'provider_contract', {'enabled': True})
        self.store.put('setting', 'ai', {'provider_id': 'provider_contract'})
        self.service.cloud_client = lambda *a: self.fail('Private graph reached cloud')
        self.service.save({'graph': graph})
        run = self.service.start({'workflow_id': graph['id']})
        self.assertEqual(run['state'], 'waiting_privacy')
        self.assertFalse(self.store.list('usage'))


if __name__ == '__main__': unittest.main()
