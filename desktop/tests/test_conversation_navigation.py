"""Real local conversation/canvas persistence; model responses are intercepted."""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.service import Service
from runtime.assistant import chat


class ConversationNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-conversation-nav-')
        self.api = Service(Path(self.temp.name) / 'workspace')
        self.store = self.api.store

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_includes_older_conversations_without_messages(self):
        for i in range(36):
            self.store.put('conversation', f'chat_{i}', {'title': f'Conversation {i}', 'messages': [{'role': 'user', 'content': 'LOCAL_HISTORY_BODY_TRACE'}], 'workflow_id': None})
        result = self.api.snapshot({})
        self.assertEqual(len(result['conversations']), 36)
        self.assertNotIn('LOCAL_HISTORY_BODY_TRACE', json.dumps(result['conversations']))
        self.assertTrue(all('messages' not in item for item in result['conversations']))

    def test_manual_canvas_links_once_and_preserves_history(self):
        self.store.put('conversation', 'chat_manual', {'title': 'Original conversation', 'messages': [{'role': 'user', 'content': 'Local saved message'}], 'workflow_id': None})
        first = self.api.dispatch('workflow.create', {'conversation_id': 'chat_manual', 'title': 'Manual canvas'})
        second = self.api.dispatch('workflow.create', {'conversation_id': 'chat_manual', 'title': 'Repeated request'})
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.store.list('workflow')), 1)
        self.assertEqual(len(self.store.list('workflow_version')), 1)
        saved = self.store.get('conversation', 'chat_manual')
        self.assertEqual(saved['workflow_id'], first['id'])
        self.assertEqual(saved['title'], 'Original conversation')
        self.assertEqual(saved['messages'][0]['content'], 'Local saved message')

    def test_manual_first_conversation_is_persistent_without_model_configuration(self):
        result = self.api.create({'conversation_id': 'chat_new', 'template': 'blank'})
        conversation = self.api.dispatch('ai.conversation', {'id': 'chat_new'})
        self.assertEqual(conversation['workflow_id'], result['id'])
        self.assertEqual(conversation['messages'], [])
        self.assertEqual(result['nodes'], [])

    def test_invalid_id_is_rejected_without_creating_a_canvas(self):
        for value in ['', '../bad', 3, ['bad']]:
            with self.assertRaises(ValueError):
                self.api.create({'conversation_id': value})
        self.assertEqual(self.store.list('workflow'), [])
        self.assertEqual(self.store.list('conversation'), [])

    def test_link_failure_rolls_back_canvas_and_version(self):
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER reject_conversation BEFORE INSERT ON entities WHEN NEW.kind='conversation' BEGIN SELECT RAISE(ABORT, 'test-only failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.api.create({'conversation_id': 'chat_fail'})
        self.assertEqual(self.store.list('workflow'), [])
        self.assertEqual(self.store.list('workflow_version'), [])

    def test_first_message_titles_a_manually_created_conversation(self):
        canvas = self.api.create({'conversation_id': 'chat_title'})
        response = {'content': json.dumps({'reply': 'An explicit test reply', 'actions': [], 'run': False}), 'model': 'intercepted-test-model'}
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', return_value=response):
            result = chat(self.store, {'conversation_id': 'chat_title', 'message': 'A useful conversation title', 'workflow_id': canvas['id']}, self.api.dispatch)
        self.assertFalse(result.get('failed'))
        self.assertEqual(self.store.get('conversation', 'chat_title')['title'], 'A useful conversation title')

    def test_identifier_collision_cannot_overwrite_an_immutable_workflow(self):
        canvas = self.api.create({'conversation_id': 'original_chat', 'title': 'Original canvas'})
        prior = self.store.get('workflow_version', canvas['id'] + ':1')
        with patch('runtime.service.template', return_value={**canvas, 'title': 'Rejected replacement'}):
            with self.assertRaises(sqlite3.IntegrityError): self.api.create({'conversation_id': 'another_chat'})
        self.assertEqual(self.store.get('workflow_version', canvas['id'] + ':1'), prior)
        self.assertEqual(self.store.get('workflow', canvas['id'])['title'], 'Original canvas')
        self.assertIsNone(self.store.get('conversation', 'another_chat'))

    def test_deleted_canvas_history_can_continue_without_reopening_deleted_data(self):
        canvas = self.api.create({'conversation_id': 'chat_deleted'})
        self.api.delete_workflow({'id': canvas['id']})
        response = {'content': json.dumps({'reply': 'Continue with a new canvas when needed.', 'actions': [], 'run': False}), 'model': 'intercepted-test-model'}
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', return_value=response):
            result = chat(self.store, {'conversation_id': 'chat_deleted', 'message': 'Continue this discussion'}, self.api.dispatch)
        self.assertFalse(result.get('failed'))
        self.assertIsNone(result['workflow'])
        replacement = self.api.create({'conversation_id': 'chat_deleted'})
        self.assertNotEqual(replacement['id'], canvas['id'])
        self.assertEqual(len(self.store.get('conversation', 'chat_deleted')['messages']), 2)


if __name__ == '__main__': unittest.main()
