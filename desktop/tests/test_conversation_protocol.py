"""Multi-turn protocol, bounded correction and no-partial-edit regressions."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.assistant import CanvasFormatError, canvas_command, chat, model_history, parse_json
from runtime.service import Service
from runtime import providers


def response(reply='Ready.', actions=None, run=False):
    return {'content': json.dumps({'reply': reply, 'actions': actions or [], 'run': run}), 'model': 'contract-model'}


class ConversationProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-protocol-')
        self.api = Service(Path(self.temp.name) / 'workspace')
        self.store = self.api.store

    def tearDown(self):
        self.temp.cleanup()

    def test_fences_bom_and_single_fenced_object_with_prose(self):
        payload = response()['content']
        for text in [payload, '\ufeff' + payload, '```JSON\n' + payload + '\n```', 'Here is the response:\n```json\n' + payload + '\n```\nReady.']:
            self.assertEqual(canvas_command(text)['reply'], 'Ready.')

    def test_embedded_markdown_and_json_strings_do_not_get_extracted_as_commands(self):
        reply = 'Example code: ```json {"run":true} ``` and braces { like these }.'
        self.assertEqual(canvas_command(response(reply)['content'])['reply'], reply)

    def test_ambiguous_multiple_objects_and_blocks_are_rejected(self):
        payload = response()['content']
        for text in [payload + '\n' + payload, '```json\n' + payload + '\n```\n```json\n' + payload + '\n```', '[ ' + payload + ' ]', json.dumps(payload)]:
            with self.assertRaises(CanvasFormatError): canvas_command(text)

    def test_duplicate_fields_and_nonfinite_numbers_are_rejected(self):
        for text in ['{"reply":"Ready","actions":[],"run":false,"run":true}', '{"number":NaN}', '{"number":Infinity}', '{"number":1e999}']:
            with self.assertRaises(CanvasFormatError): parse_json(text)

    def test_wrong_envelopes_cannot_become_fake_success_or_truthy_auto_run(self):
        for value in [{}, {'message': 'Done'}, {'reply': 'Done'}, {'reply': '', 'actions': [], 'run': False}, {'reply': 'Done', 'actions': {}, 'run': False}, {'reply': 'Done', 'actions': [], 'run': 'false'}, {'reply': 'Done', 'actions': [], 'run': 1}]:
            with self.assertRaises(CanvasFormatError): canvas_command(json.dumps(value))

    def test_generic_local_classification_json_remains_supported(self):
        self.assertEqual(parse_json('{"category":"Research","tags":["local"]}')['category'], 'Research')

    def test_history_uses_json_assistant_envelopes_without_replaying_actions(self):
        history = model_history([
            {'role': 'user', 'content': 'First request', 'scope': 'api'},
            {'role': 'assistant', 'content': 'I added a node.', 'scope': 'api', 'changes': ['add_node']},
            {'role': 'assistant', 'content': 'OLD APPLICATION ERROR', 'scope': 'api', 'error': True},
            {'role': 'user', 'content': 'LOCAL PRIVATE HISTORY', 'scope': 'local'},
            {'role': 'user', 'content': 'Continue editing', 'scope': 'api'},
        ], 'api')
        self.assertEqual(len(history), 3)
        self.assertEqual(json.loads(history[1]['content']), {'reply': 'I added a node.', 'actions': [], 'run': False})
        self.assertNotIn('OLD APPLICATION ERROR', str(history))
        self.assertNotIn('LOCAL PRIVATE HISTORY', str(history))

    def test_two_turn_plain_question_never_creates_an_empty_workflow(self):
        captured = []
        def complete(store, messages, **kwargs):
            captured.append(deepcopy(messages))
            return response('An actual contract answer.')
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', side_effect=complete):
            first = chat(self.store, {'message': 'What does a workflow do?', 'conversation_id': 'questions'}, lambda *a: None)
            second = chat(self.store, {'message': 'Explain that a little more.', 'conversation_id': 'questions'}, lambda *a: None)
        self.assertFalse(first.get('failed'))
        self.assertFalse(second.get('failed'))
        self.assertIsNone(first['workflow'])
        self.assertIsNone(second['workflow'])
        self.assertEqual(self.store.list('workflow'), [])
        self.assertEqual(json.loads(captured[1][2]['content'])['actions'], [])

    def test_syntax_repair_gets_precise_error_and_invalid_candidate_as_data(self):
        invalid = {'content': '{"reply":"Hello", "actions": [], "run": false,}', 'model': 'contract-model'}
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', side_effect=[invalid, response('Hello.')]) as complete:
            result = chat(self.store, {'message': 'Hello', 'conversation_id': 'syntax'}, lambda *a: None)
        self.assertFalse(result.get('failed'))
        repair = complete.call_args_list[1].args[1]
        self.assertTrue(all(m['role'] != 'assistant' for m in repair))
        self.assertIn('column', repair[-1]['content'])
        self.assertIn('rejected_candidate', repair[-1]['content'])
        self.assertEqual(complete.call_args_list[1].kwargs['purpose'], 'canvas_repair')

    def test_two_corrections_commit_only_one_valid_batch(self):
        graph = self.api.create({'template': 'text', 'title': 'Original'})
        invalid = {'content': '{broken', 'model': 'contract-model'}
        wrong = response('Rename.', [{'op': 'set_title', 'title': 'Must never be saved'}, {'op': 'remove_node', 'id': 'missing'}])
        final = response('Renamed.', [{'op': 'set_title', 'title': 'Correct title'}])
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', side_effect=[invalid, wrong, final]) as complete:
            result = chat(self.store, {'message': 'Rename this workflow', 'workflow_id': graph['id'], 'conversation_id': 'bounded-success'}, lambda *a: None)
        self.assertEqual(complete.call_count, 3)
        self.assertFalse(result.get('failed'))
        self.assertEqual(result['workflow']['title'], 'Correct title')
        self.assertEqual(result['workflow']['version'], 2)

    def test_permanent_invalid_response_keeps_graph_and_allows_fresh_retry(self):
        graph = self.api.create({'template': 'text', 'title': 'Original'})
        params = {'message': 'Rename this workflow', 'workflow_id': graph['id'], 'conversation_id': 'retry', 'request_id': 'failed-turn'}
        invalid = {'content': 'Not a JSON command.', 'model': 'contract-model'}
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', return_value=invalid) as complete:
            failed = chat(self.store, params, lambda *a: None)
        self.assertEqual(complete.call_count, 3)
        self.assertTrue(failed['failed'])
        self.assertTrue(failed['retryable'])
        self.assertEqual(self.api.workflow(graph['id']), graph)
        good = response('Renamed.', [{'op': 'set_title', 'title': 'Retried title'}])
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', return_value=good) as complete:
            retried = chat(self.store, {**params, 'request_id': 'fresh-turn'}, lambda *a: None)
        self.assertFalse(retried.get('failed'))
        self.assertEqual(retried['workflow']['version'], 2)
        self.assertNotIn(failed['message']['content'], str(complete.call_args.args[1]))

    def test_invalid_local_response_never_falls_back_to_cloud(self):
        self.store.put('setting', 'ai', {'mode': 'local'})
        with patch('runtime.providers.complete') as complete:
            failed = chat(self.store, {'message': 'Local request', 'conversation_id': 'local-invalid'}, lambda *a: {'content': 'Invalid local JSON'})
        self.assertTrue(failed['failed'])
        complete.assert_not_called()
        self.assertEqual(self.store.list('workflow'), [])

    def test_deepseek_only_format_repair_disables_thinking(self):
        self.store.put('provider', 'provider_contract', {'enabled': True, 'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com'})
        self.store.put('setting', 'ai', {'mode': 'api', 'provider_id': 'provider_contract'})
        payload = {'choices': [{'message': {'content': response()['content']}, 'finish_reason': 'stop'}], 'model': 'deepseek-flash'}
        with patch('runtime.vault.read', return_value='synthetic-key'), patch('runtime.providers.request_json', return_value=payload) as request:
            providers.complete(self.store, [{'role': 'user', 'content': 'Public JSON request'}], json_mode=True, purpose='canvas_assistant')
            self.assertNotIn('thinking', request.call_args.args[2])
            providers.complete(self.store, [{'role': 'user', 'content': 'Public JSON correction'}], json_mode=True, purpose='canvas_repair')
            self.assertEqual(request.call_args.args[2]['thinking'], {'type': 'disabled'})
            self.assertEqual(request.call_args.args[2]['response_format'], {'type': 'json_object'})


if __name__ == '__main__': unittest.main()
