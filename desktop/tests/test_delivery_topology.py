"""Branch convergence, final-result truthfulness and generation recovery."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.catalog import BY_ID, definition
from runtime.delivery import final_ids, topology, integrate, result_summary, plan_delivery_errors
from runtime.engine import Engine, value
from runtime.graphs import validate, wrap, unwrap
from runtime.service import Service
from runtime.assistant import apply_actions
from runtime import providers


def node(key, kind, config=None):
    return {'id': key, 'type_id': kind, 'type_version': '1', 'title': key, 'position': {'x': 30, 'y': 0}, 'config': {**deepcopy(BY_ID[kind]['defaults']), **(config or {})}}


def edge(a, b, incoming='text', outgoing='text'):
    return {'id': a + '_' + b + '_' + incoming, 'source': a, 'target': b, 'sourceHandle': outgoing, 'targetHandle': incoming}


def response(text, finish='stop'):
    return {'model': 'fixture-model', 'choices': [{'finish_reason': finish, 'message': {'role': 'assistant', 'content': text, 'reasoning_content': 'PRIVATE_REASONING_FIXTURE_NEVER_PERSIST'}}], 'usage': {'total_tokens': 22, 'completion_tokens_details': {'reasoning_tokens': 12}}}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-delivery-')
        self.api = Service(Path(self.temp.name) / 'workspace')
        self.store = self.api.store

    def tearDown(self): self.temp.cleanup()

    def graph(self): return self.api.create({'template': 'blank', 'title': 'Public delivery fixture'})

    def test_three_inputs_merge_in_port_order_and_deduplicate_sources(self):
        item = node('merge', 'data.merge_text', {'input_count': 3, 'labels': 'General\nCoding\nPricing'})
        ref = {'title': 'Public source', 'url': 'https://example.com/'}
        args = {'text_3': value('text', 'Third', False, [ref]), 'text_1': value('text', 'First', False, [ref]), 'text_2': value('text', 'Second', False)}
        result = Engine(self.store).execute(item, args)['text']
        self.assertLess(result['value'].index('First'), result['value'].index('Second'))
        self.assertLess(result['value'].index('Second'), result['value'].index('Third'))
        self.assertEqual(result['sources'], [ref]); self.assertFalse(result['private'])
        self.assertEqual([s['label'] for s in result['segments']], ['General', 'Coding', 'Pricing'])

    def test_merge_preserves_private_taint_and_rejects_missing_input(self):
        item = node('merge', 'data.merge_text')
        engine = Engine(self.store)
        output = engine.execute(item, {'text_1': value('text', 'Public', False), 'text_2': value('text', 'PRIVATE_CANARY', True)})
        self.assertTrue(output['text']['private'])
        with self.assertRaises(ValueError): engine.execute(item, {'text_1': value('text', 'Incomplete')})

    def test_private_merged_branch_cannot_reach_cloud_synthesis(self):
        graph = self.graph()
        graph['nodes'] = [node('private', 'input.text', {'text': 'PRIVATE_MERGE_CANARY_123456789'}), node('shared', 'input.prompt', {'text': 'Public request'}), node('merge', 'data.merge_text'), node('writer', 'model.api'), node('report', 'output.document')]
        graph['edges'] = [edge('private', 'merge', 'text_1'), edge('shared', 'merge', 'text_2'), edge('merge', 'writer'), edge('writer', 'report')]
        self.store.put('provider', 'p', {'enabled': True}); self.store.put('setting', 'ai', {'provider_id': 'p'})
        with patch('runtime.providers.complete') as cloud:
            run = Engine(self.store, cloud_client=cloud).start(graph)
        self.assertEqual(run['state'], 'waiting_privacy')
        self.assertTrue(self.store.get('node_run', run['id'] + ':merge')['outputs']['text']['private'])
        cloud.assert_not_called()

    def test_dynamic_ports_and_invalid_merge_configuration(self):
        graph = self.graph()
        graph['nodes'] = [node('a', 'input.prompt'), node('b', 'input.prompt'), node('c', 'input.prompt'), node('merge', 'data.merge_text', {'input_count': 3})]
        graph['edges'] = [edge(key, 'merge', f'text_{index + 1}') for index, key in enumerate(['a', 'b', 'c'])]
        self.assertEqual(validate(graph), [])
        self.assertEqual([p['id'] for p in definition(graph['nodes'][-1])['inputs']], ['text_1', 'text_2', 'text_3'])
        for count in [True, 1, 17, 2.5, '3']:
            graph['nodes'][-1]['config']['input_count'] = count
            self.assertTrue(any(i['code'] == 'MERGE_INPUTS' for i in validate(graph)))

    def fixture_research(self):
        graph = self.graph()
        graph['nodes'] = [node('s1', 'network.public_search', {'query': 'public alpha'}), node('s2', 'network.public_search', {'query': 'public beta'}), node('s3', 'network.public_search', {'query': 'public gamma'}), node('writer', 'model.api'), node('report', 'output.document'), node('t2', 'output.table'), node('t3', 'output.table'), node('unrelated', 'human.input')]
        graph['edges'] = [edge('s1', 'writer'), edge('writer', 'report'), edge('s2', 't2', 'table', 'table'), edge('s3', 't3', 'table', 'table')]
        return graph

    def test_disconnected_tables_cannot_count_as_the_final_report(self):
        graph = self.fixture_research()
        self.assertEqual(final_ids(graph), ['report'])
        self.assertEqual(topology(graph)['unintegrated_research'], ['s2', 's3'])
        records = [{'node_id': 'report', 'state': 'skipped', 'message': 'Writer failed'}, {'node_id': 't2', 'state': 'succeeded', 'outputs': {'table': value('table', {})}}]
        self.assertEqual(result_summary(graph, records)['state'], 'blocked')
        self.assertEqual(result_summary(graph, records)['results'], [])

    def test_integrate_is_a_pure_draft_with_real_dependencies_and_no_lost_nodes(self):
        original = self.fixture_research(); before = deepcopy(original)
        graph = integrate(original)
        self.assertEqual(original, before)
        self.assertTrue({n['id'] for n in original['nodes']} <= {n['id'] for n in graph['nodes']})
        self.assertEqual(next(n for n in original['nodes'] if n['id'] == 'unrelated'), next(n for n in graph['nodes'] if n['id'] == 'unrelated'))
        self.assertEqual(validate(graph), []); self.assertEqual(topology(graph)['unintegrated_research'], [])
        self.assertEqual(graph['final_outputs'], ['report'])
        self.assertEqual(len([n for n in graph['nodes'] if n['type_id'] == 'data.merge_text']), 1)
        self.assertEqual(self.store.list('usage'), []); self.assertEqual(self.store.list('run'), [])
        self.assertEqual(integrate(graph), graph)

    def test_final_output_selection_is_versioned_even_without_node_changes(self):
        graph = self.api.create({'template': 'text'})
        previous = graph['version']; graph['final_outputs'] = ['report']
        saved = self.api.save({'graph': graph})['workflow']
        self.assertEqual(saved['version'], previous + 1)
        self.assertEqual(self.api.history({'id': graph['id']})[0]['final_outputs'], ['report'])

    def test_wrap_and_unwrap_remap_final_output_ids(self):
        graph = self.api.create({'template': 'text'}); graph['final_outputs'] = ['report']
        combined = wrap(graph, ['report'])
        self.assertEqual(combined['final_outputs'], [combined['nodes'][-1]['id']])
        self.assertEqual(validate(combined), [])
        expanded = unwrap(combined, combined['nodes'][-1]['id'])
        self.assertEqual(validate(expanded), [])
        self.assertTrue(all(key in {n['id'] for n in expanded['nodes']} for key in expanded['final_outputs']))

    def test_delete_final_node_does_not_leave_a_dangling_selection(self):
        graph = self.api.create({'template': 'text'}); graph['final_outputs'] = ['report']
        updated, *_ = apply_actions(graph, [{'op': 'remove_node', 'id': 'report'}], self.store)
        self.assertNotIn('final_outputs', updated)

    def test_plan_gate_requires_each_new_research_branch_to_reach_answer(self):
        graph = self.fixture_research()
        actions = [{'op': 'add_node', 'node': n} for n in graph['nodes'] if n['type_id'].startswith('network.')]
        self.assertTrue(plan_delivery_errors(graph, actions))
        self.assertEqual(plan_delivery_errors(integrate(graph), actions), [])
        self.assertEqual(plan_delivery_errors(graph, [{'op': 'update_node', 'id': 'unrelated', 'title': 'Keep me'}]), [])

    def test_separate_reports_are_not_a_shared_final_answer(self):
        graph = self.fixture_research()
        graph['nodes'] = [node('s1', 'network.public_search'), node('s2', 'network.public_search'), node('d1', 'output.document'), node('d2', 'output.document')]
        graph['edges'] = [edge('s1', 'd1'), edge('s2', 'd2')]
        actions = [{'op': 'add_node', 'node': n} for n in graph['nodes'] if n['type_id'].startswith('network.')]
        self.assertEqual(topology(graph)['unintegrated_research'], [])
        self.assertTrue(plan_delivery_errors(graph, actions))

    def test_integration_executes_all_branches_before_final_synthesis(self):
        graph = integrate(self.fixture_research())
        outputs = []
        def remote(item, args, run_id):
            if item['type_id'].startswith('network.'):
                query = item['config']['query']; ref = {'url': 'https://example.com/' + item['id'], 'title': query}
                return {'text': value('text', query, False, [ref]), 'table': value('table', {'columns': ['Title'], 'rows': [[query]]}, False, [ref])}
            return Engine(self.store).execute(item, args)
        def cloud(messages, provider_id, tokens):
            outputs.append(messages[-1]['content'])
            return {'content': 'Analysis: ' + messages[-1]['content'], 'model': 'synthetic'}
        self.store.put('provider', 'p', {'enabled': True}); self.store.put('setting', 'ai', {'provider_id': 'p'})
        # Synthetic remote executor runs public research only; all actual local
        # worker transport and real models are verified separately.
        with patch('runtime.web.status', return_value={'enabled': True, 'available': True}), patch('runtime.web.execute', side_effect=lambda store, item, arguments: remote(item, arguments, '')):
            run = Engine(self.store, remote, cloud).start(graph)
        detail = self.api.run_detail({'id': run['id']})
        self.assertEqual(run['state'], 'waiting_input')
        self.assertEqual(detail['final']['state'], 'ready')
        self.assertEqual(len(outputs), 4)
        for phrase in ['public alpha', 'public beta', 'public gamma']: self.assertIn(phrase, outputs[-1])
        self.assertEqual(len(detail['final']['results'][0]['outputs']['text']['sources']), 3)

    def setup_provider(self):
        self.store.put('provider', 'p', {'base_url': 'https://api.deepseek.com', 'enabled': True, 'model': 'deepseek-flash'})
        self.store.put('setting', 'ai', {'mode': 'api', 'provider_id': 'p'})

    def test_generation_length_recovers_with_more_output_room_and_keeps_thinking(self):
        self.setup_provider()
        with patch('runtime.vault.read', return_value='synthetic-only-key'), patch('runtime.providers.request_json', side_effect=[response('', 'length'), response('Complete final answer')]) as http:
            result = providers.complete(self.store, [{'role': 'user', 'content': 'Public report'}], purpose='workflow_node', max_tokens=4096)
        self.assertEqual(result['content'], 'Complete final answer'); self.assertEqual(result['generation_retries'], 1)
        self.assertEqual([c.args[2]['max_tokens'] for c in http.call_args_list], [4096, 16384])
        self.assertTrue(all('thinking' not in c.args[2] for c in http.call_args_list))
        self.assertEqual(len(self.store.list('usage')), 2)
        self.assertNotIn('PRIVATE_REASONING_FIXTURE', str(self.store.list('usage')))

    def test_exhausted_generation_retains_draft_and_blocks_final_export(self):
        self.setup_provider()
        graph = self.graph(); graph['nodes'] = [node('input', 'input.prompt', {'text': 'Public report'}), node('writer', 'model.api'), node('report', 'output.document')]; graph['edges'] = [edge('input', 'writer'), edge('writer', 'report')]
        def cloud(messages, provider_id, tokens): return providers.complete(self.store, messages, purpose='workflow_node', max_tokens=tokens)
        with patch('runtime.vault.read', return_value='synthetic-only-key'), patch('runtime.providers.request_json', return_value=response('Unfinished public draft', 'length')) as http:
            run = Engine(self.store, cloud_client=cloud).start(graph)
        self.assertEqual(http.call_count, 3)
        self.assertEqual(self.store.get('node_run', run['id'] + ':writer')['partial_text'], 'Unfinished public draft')
        self.assertEqual(self.api.run_detail({'id': run['id']})['final']['state'], 'blocked')
        with self.assertRaises(ValueError): self.api.export({'run_id': run['id'], 'scope': 'final'})

    def test_later_empty_generation_does_not_erase_an_earlier_partial_draft(self):
        self.setup_provider()
        with patch('runtime.vault.read', return_value='synthetic-only-key'), patch('runtime.providers.request_json', side_effect=[response('Earlier usable partial draft', 'length'), response('', 'length'), response('', 'length')]):
            with self.assertRaises(providers.GenerationLimitError) as error:
                providers.complete(self.store, [{'role': 'user', 'content': 'Public report'}], purpose='workflow_node')
        self.assertEqual(error.exception.partial_text, 'Earlier usable partial draft')

    def test_final_export_excludes_intermediate_diagnostics(self):
        graph = self.api.create({'template': 'text'})
        graph['nodes'].append(node('unrelated', 'human.input'))
        graph['final_outputs'] = ['report']; graph = self.api.save({'graph': graph})['workflow']
        run = self.api.start({'workflow_id': graph['id']})
        self.assertEqual(self.api.run_detail({'id': run['id']})['final']['state'], 'ready')
        exported = self.api.export({'run_id': run['id'], 'scope': 'final'})
        self.assertNotIn('## unrelated', exported['content'])
        self.assertNotIn('Status: waiting_input', exported['content'])
        self.assertTrue(Path(exported['local_path']).is_file())
        self.assertIn('combine-final-', exported['filename'])

    def test_capability_classification_covers_all_installed_nodes(self):
        self.assertTrue(all(d.get('capability') for d in BY_ID.values()))
        self.assertEqual(BY_ID['network.public_search']['capability'], 'research')
        self.assertEqual(BY_ID['data.merge_text']['capability'], 'integration')
        self.assertEqual(BY_ID['output.document']['capability'], 'delivery')
        self.assertEqual(BY_ID['output.map']['capability'], 'visualization')


if __name__ == '__main__': unittest.main()
