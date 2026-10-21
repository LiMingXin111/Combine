"""Real store/graph/scheduler tests. Model transports are explicitly intercepted."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.service import Service
from runtime.capabilities import builtins, compile_recipe, save, remove, get, instance, prepare, assistant_catalog
from runtime.graphs import validate, offline_report
from runtime.privacy import contains_api, safe_context, assert_public_payload
from runtime.assistant import apply_actions, chat
from runtime.catalog import definition


class CapabilityLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-capability-')
        self.api = Service(Path(self.temp.name))
        self.store = self.api.store
        self.provider = 'provider_0123456789abcdef'
        self.store.put('provider', self.provider, {'enabled': True, 'model': 'intercepted-contract-model'})
        self.store.put('setting', 'ai', {'mode': 'api', 'provider_id': self.provider})

    def tearDown(self): self.temp.cleanup()

    def recipe(self, kind='skill', mode='api'):
        return {'kind': kind, 'title': 'Custom ' + kind, 'description': 'An explicit test capability.',
                'recipe': {'mode': mode, 'instruction': 'Preserve evidence and return the requested result.', 'stages': ['Plan', 'Produce', 'Review'] if kind == 'agent' else ['Complete the task.'], 'provider_id': self.provider, 'max_tokens': 1024}}

    def workflow(self, cap, private=False, copies=1):
        graph = self.api.create({'template': 'blank', 'title': 'Capability contract'})
        graph['nodes'] = [{'id': 'source', 'type_id': 'input.text' if private else 'input.prompt', 'config': {'text': 'ORIGINAL_TASK_12345'}, 'position': {'x': 0, 'y': 0}}]
        graph['edges'] = []
        for i in range(copies):
            key = f'cap_{i}'
            graph['nodes'] += [instance(cap, key, {'x': 400, 'y': i * 400}), {'id': f'report_{i}', 'type_id': 'output.document', 'config': {}, 'position': {'x': 800, 'y': i * 400}}]
            graph['edges'] += [{'id': f'input_{i}', 'source': 'source', 'target': key, 'sourceHandle': 'text', 'targetHandle': 'text'}, {'id': f'output_{i}', 'source': key, 'target': f'report_{i}', 'sourceHandle': 'text', 'targetHandle': 'text'}]
        return self.api.save({'graph': graph})['workflow']

    def test_six_builtin_templates_are_executable_and_do_not_create_records(self):
        caps = self.api.snapshot({})['capabilities']
        self.assertEqual(len(caps), 6)
        self.assertEqual({k: sum(c['kind'] == k for c in caps) for k in ['skill', 'agent', 'expert']}, {'skill': 2, 'agent': 2, 'expert': 2})
        for cap in caps: self.assertFalse(validate(self.workflow(cap)))
        self.assertEqual(self.store.list('capability'), [])

    def test_all_three_kinds_persist_across_service_restart(self):
        for kind in ['skill', 'agent', 'expert']:
            cap = self.api.dispatch('capability.save', self.recipe(kind))
            restored = get(Service(self.store.root).store, cap['id'])
            self.assertEqual(restored, cap)
            self.assertEqual(definition(instance(cap, 'test', {'x': 0, 'y': 0}))['capability'], kind)

    def test_agent_runs_stages_in_order_keeps_original_task_and_delivers_final_answer(self):
        cap = save(self.store, self.recipe('agent'))
        graph = self.workflow(cap)
        calls = []
        def cloud(messages, provider, tokens):
            calls.append((messages, provider, tokens))
            return {'content': f'REAL_STAGE_{len(calls)}', 'model': 'contract'}
        self.api.cloud_client = cloud
        run = self.api.start({'workflow_id': graph['id'], 'reuse': False})
        self.assertEqual(run['state'], 'completed')
        self.assertEqual(len(calls), 3)
        for i, (messages, provider, tokens) in enumerate(calls):
            self.assertIn('ORIGINAL_TASK_12345', messages[-1]['content'])
            if i: self.assertIn(f'REAL_STAGE_{i}', messages[-1]['content'])
            self.assertEqual(provider, self.provider); self.assertEqual(tokens, 1024)
        report = self.store.get('node_run', run['id'] + ':report_0')['outputs']['text']
        self.assertEqual(report['value'], 'REAL_STAGE_3')
        self.assertFalse(report['private'])
        states = [n for n in self.store.list('node_run') if n['run_id'] == run['id'] and '/stage_' in n['node_id']]
        self.assertEqual(len(states), 3)
        self.assertTrue(all(n['state'] == 'succeeded' for n in states))

    def test_skill_and_expert_execute_role_instructions(self):
        for kind in ['skill', 'expert']:
            graph = self.workflow(save(self.store, self.recipe(kind)))
            cloud = Mock(return_value={'content': 'Contract result', 'model': 'contract'})
            self.api.cloud_client = cloud
            run = self.api.start({'workflow_id': graph['id'], 'reuse': False})
            self.assertEqual(run['state'], 'completed')
            self.assertIn(kind.title() + ' instructions', cloud.call_args.args[0][0]['content'])
            self.assertEqual(cloud.call_count, 1)

    def test_private_input_never_reaches_cloud_through_an_agent(self):
        graph = self.workflow(save(self.store, self.recipe('agent')), private=True)
        cloud = Mock(); self.api.cloud_client = cloud
        run = self.api.start({'workflow_id': graph['id']})
        self.assertEqual(run['state'], 'waiting_privacy'); cloud.assert_not_called()

    def test_missing_local_model_waits_without_cloud_fallback(self):
        graph = self.workflow(save(self.store, self.recipe('expert', 'local')), private=True)
        report = offline_report(graph, self.store)
        self.assertEqual(report['status'], 'missing')
        self.assertFalse(contains_api(graph))
        cloud = Mock(); self.api.cloud_client = cloud
        run = self.api.start({'workflow_id': graph['id']})
        self.assertEqual(run['state'], 'waiting_resource'); cloud.assert_not_called()

    def test_instances_are_independent_and_library_updates_are_not_retroactive(self):
        cap = save(self.store, self.recipe())
        graph = self.workflow(cap, copies=2)
        a, b = graph['nodes'][1], graph['nodes'][3]
        a['config']['definition']['nodes'][1]['config']['instruction'] = 'Only this instance changes.'
        self.assertNotEqual(a['config'], b['config'])
        changed = self.recipe(); changed.update(id=cap['id'], version=1)
        changed['recipe']['instruction'] = 'New library instructions.'
        newer = save(self.store, changed)
        self.assertEqual(newer['version'], 2)
        self.assertEqual(b['config']['definition']['library_version'], 1)
        self.assertNotIn('New library', json.dumps(b))
        self.assertIn('New library', json.dumps(instance(newer, 'new', {'x': 0, 'y': 0})))
        self.assertEqual(self.store.get('capability_version', cap['id'] + ':1')['recipe']['instruction'], cap['recipe']['instruction'])

    def test_delete_preserves_pinned_execution_and_version_evidence(self):
        cap = save(self.store, self.recipe()); graph = self.workflow(cap)
        self.api.dispatch('capability.delete', {'id': cap['id'], 'version': 1})
        self.assertIsNone(get(self.store, cap['id']))
        self.assertIsNotNone(self.store.get('capability_version', cap['id'] + ':1'))
        self.api.cloud_client = Mock(return_value={'content': 'Still works', 'model': 'contract'})
        self.assertEqual(self.api.start({'workflow_id': graph['id']})['state'], 'completed')

    def test_stale_updates_and_deletes_cannot_overwrite_newer_version(self):
        cap = save(self.store, self.recipe())
        patch = {**self.recipe(), 'id': cap['id'], 'version': 1}
        save(self.store, patch)
        with self.assertRaises(ValueError): save(self.store, patch)
        with self.assertRaises(ValueError): remove(self.store, {'id': cap['id'], 'version': 1})
        self.assertEqual(len(self.store.list('capability_version')), 2)

    def test_invalid_recipes_and_uninstalled_tools_leave_no_records(self):
        cases = []
        for patch in [{'mode': 'shell'}, {'stages': []}, {'stages': ['x'] * 7}, {'stages': ['']}, {'instruction': ''}, {'max_tokens': True}, {'command': 'cmd.exe'}]:
            value = self.recipe(); value['recipe'].update(patch); cases.append(value)
        cases += [{'kind': 'expert', 'title': 'Invented executor', 'definition': {'nodes': [{'id': 'x', 'type_id': 'arbitrary.shell'}], 'edges': [], 'inputs': [], 'outputs': [{'id': 'text', 'node': 'x', 'port': 'text', 'kind': 'text'}]}}]
        for value in cases:
            with self.assertRaises(ValueError): save(self.store, value)
        self.assertEqual(self.store.list('capability'), [])
        self.assertEqual(self.store.list('capability_version'), [])

    def test_custom_workflow_can_include_real_web_tools_without_hiding_network_requirement(self):
        definition = {'nodes': [{'id': 'web', 'type_id': 'network.public_search', 'config': {'query': 'public query'}}], 'edges': [], 'inputs': [], 'outputs': [{'id': 'text', 'node': 'web', 'port': 'text', 'kind': 'text'}]}
        cap = save(self.store, {'kind': 'agent', 'title': 'Research tool agent', 'definition': definition})
        graph = {'nodes': [instance(cap, 'agent', {'x': 0, 'y': 0})], 'edges': []}
        self.assertTrue(contains_api(graph))
        self.assertEqual(offline_report(graph, self.store)['status'], 'online')

    def test_ai_can_add_builtin_and_register_a_recipe_as_real_nodes(self):
        graph = self.api.create({'template': 'blank', 'title': 'AI capability test'})
        source = {'id': 'source', 'type_id': 'input.prompt', 'config': {'text': 'Public task'}}
        actions = [{'op': 'add_node', 'node': source}, {'op': 'add_capability', 'capability_id': 'builtin_skill_summarize', 'id': 'saved'},
                   {'op': 'add_edge', 'source': 'source', 'target': 'saved', 'sourceHandle': 'text', 'targetHandle': 'text'}]
        updated, caps, *_ = apply_actions(graph, actions, self.store)
        self.assertFalse(validate(updated)); self.assertEqual(caps, [])
        actions[1] = {'op': 'register_capability', **self.recipe('expert'), 'node_id': 'saved'}
        updated, caps, *_ = apply_actions(graph, actions, self.store)
        self.assertEqual(len(caps), 1); self.assertEqual(caps[0]['kind'], 'expert')
        self.assertEqual(updated['nodes'][1]['config']['capability_kind'], 'expert')
        self.assertEqual(self.store.list('capability'), [])

    def test_local_ai_cannot_add_cloud_templates_or_register_cloud_recipes(self):
        graph = self.api.create({'template': 'blank', 'title': 'Local only'})
        with self.assertRaisesRegex(ValueError, 'API AI'):
            apply_actions(graph, [{'op': 'add_capability', 'capability_id': 'builtin_skill_summarize'}], self.store, 'local')
        with self.assertRaises(ValueError): prepare(self.store, self.recipe(), 'local')

    def test_cloud_catalog_hides_private_recipe_and_port_metadata(self):
        values = self.recipe('expert', 'local'); values['title'] = 'PRIVATE_RECIPE_TITLE_23988273'
        values['recipe']['instruction'] = 'PRIVATE_RECIPE_INSTRUCTION_87938992 must stay on the computer.'
        cap = save(self.store, values)
        public = assistant_catalog(self.store, 'api')
        text = json.dumps(public)
        self.assertNotIn(values['title'], text); self.assertNotIn(values['recipe']['instruction'], text)
        with self.assertRaises(ValueError): assert_public_payload(self.store, values['recipe']['instruction'])
        entry = next(c for c in public if c['id'] == cap['id'])
        self.assertEqual(entry['inputs'], [])
        context = safe_context(self.workflow(cap))
        self.assertNotIn('PRIVATE_RECIPE', json.dumps(context))

    def test_builtin_entries_are_read_only_but_copies_can_be_saved(self):
        builtin = builtins()[0]
        with self.assertRaises(ValueError): save(self.store, builtin)
        with self.assertRaises(ValueError): remove(self.store, {'id': builtin['id'], 'version': 1})
        copy = deepcopy(builtin); copy.pop('id'); copy.pop('version')
        saved = save(self.store, copy)
        self.assertTrue(saved['id'].startswith('capability_'))

    def test_conversation_discovers_registers_and_reuses_library_capabilities(self):
        public_source = {'op': 'add_node', 'node': {'id': 'source', 'type_id': 'input.prompt', 'config': {'text': 'Public task'}}}
        actions = [public_source, {'op': 'register_capability', **self.recipe('agent'), 'node_id': 'agent'},
                   {'op': 'add_node', 'node': {'id': 'report', 'type_id': 'output.document', 'config': {}}},
                   {'op': 'add_edge', 'source': 'source', 'target': 'agent', 'sourceHandle': 'text', 'targetHandle': 'text'},
                   {'op': 'add_edge', 'source': 'agent', 'target': 'report', 'sourceHandle': 'text', 'targetHandle': 'text'},
                   {'op': 'set_final_outputs', 'ids': ['report']}]
        contexts = []
        def completion(store, messages, **kwargs):
            contexts.append(messages[0]['content'])
            return {'content': json.dumps({'reply': 'Created the executable task agent.', 'actions': actions, 'run': True}), 'model': 'intercepted-test'}
        with patch('runtime.providers.config', return_value={}), patch('runtime.providers.complete', side_effect=completion):
            result = chat(self.store, {'message': 'Create a reusable task agent.', 'conversation_id': 'capability_chat'}, lambda *a: None)
        self.assertFalse(result.get('failed')); self.assertTrue(result['should_run'])
        self.assertIn('builtin_expert_research', contexts[0])
        cap = self.store.list('capability')[0]
        self.assertEqual(cap['kind'], 'agent')
        self.assertIsNotNone(self.store.get('capability_version', cap['id'] + ':1'))
        self.api.cloud_client = Mock(return_value={'content': 'Complete final result', 'model': 'intercepted-test'})
        self.assertEqual(self.api.start({'workflow_id': result['workflow']['id'], 'reuse': False})['state'], 'completed')
        reused = [{'op': 'add_node', 'node': {'id': 'second_source', 'type_id': 'input.prompt', 'config': {'text': 'Another public task'}}},
                  {'op': 'add_capability', 'capability_id': cap['id'], 'id': 'reused'},
                  {'op': 'add_edge', 'source': 'second_source', 'target': 'reused', 'sourceHandle': 'text', 'targetHandle': 'text'}]
        updated, *_ = apply_actions(result['workflow'], reused, self.store)
        self.assertEqual(updated['nodes'][-1]['config']['definition']['library_version'], 1)

    def test_nested_skill_retains_local_execution_and_source_protection(self):
        inner = {'nodes': [{'id': 'trim', 'type_id': 'transform.text', 'config': {'operation': 'trim'}}], 'edges': [],
                 'inputs': [{'id': 'text', 'kind': 'text', 'node': 'trim', 'port': 'text'}], 'outputs': [{'id': 'text', 'kind': 'text', 'node': 'trim', 'port': 'text'}]}
        skill = save(self.store, {'kind': 'skill', 'title': 'Local text procedure', 'definition': inner})
        nested = {'nodes': [instance(skill, 'nested', {'x': 0, 'y': 0})], 'edges': [],
                  'inputs': [{'id': 'text', 'kind': 'text', 'node': 'nested', 'port': 'text'}], 'outputs': [{'id': 'text', 'kind': 'text', 'node': 'nested', 'port': 'text'}]}
        agent = save(self.store, {'kind': 'agent', 'title': 'Nested procedure agent', 'definition': nested})
        graph = self.workflow(agent, private=True)
        self.assertEqual(offline_report(graph, self.store)['status'], 'ready')
        cloud = Mock(); self.api.cloud_client = cloud
        run = self.api.start({'workflow_id': graph['id']})
        self.assertEqual(run['state'], 'completed'); cloud.assert_not_called()
        self.assertTrue(self.store.get('node_run', run['id'] + ':report_0')['outputs']['text']['private'])

    def test_local_copies_of_public_templates_do_not_block_unrelated_api_capabilities(self):
        copy = deepcopy(builtins()[2]); copy.pop('id'); copy.pop('version')
        copy['title'] += ' copy'; copy['recipe']['mode'] = 'local'
        save(self.store, copy)
        api_copy = deepcopy(builtins()[2]); api_copy.pop('id'); api_copy.pop('version')
        self.assertFalse(save(self.store, api_copy)['private'])

    def test_editing_a_legacy_library_entry_retains_its_original_version(self):
        cap = prepare(self.store, self.recipe())
        self.store.put('capability', cap['id'], cap)
        self.assertIsNone(self.store.get('capability_version', cap['id'] + ':1'))
        changes = {**self.recipe(), 'id': cap['id'], 'version': 1, 'title': 'Updated legacy capability'}
        save(self.store, changes)
        self.assertEqual(self.store.get('capability_version', cap['id'] + ':1')['title'], cap['title'])
        self.assertEqual(self.store.get('capability_version', cap['id'] + ':2')['title'], changes['title'])
        other = prepare(self.store, self.recipe('expert'))
        self.store.put('capability', other['id'], other)
        remove(self.store, {'id': other['id'], 'version': 1})
        self.assertEqual(self.store.get('capability_version', other['id'] + ':1')['title'], other['title'])


if __name__ == '__main__': unittest.main()
