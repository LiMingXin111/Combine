import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.spaces import Spaces
from runtime.service import Service, template
from runtime.sharing import prepare, approved, import_template


class SpacesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = (Path(self.temp.name) / 'original').resolve()
        self.registry = Spaces(self.base)
        self.service = Service(self.base)

    def tearDown(self): self.temp.cleanup()

    def test_default_preserves_original_and_new_spaces_are_isolated(self):
        graph = self.service.create({'template': 'text', 'title': 'Original'})
        self.assertEqual(self.registry.root('local'), self.base)
        other = self.registry.create('Second space'); target = self.registry.root(other['id'])
        self.assertNotEqual(target, self.base)
        self.registry.activate(other['id'])
        self.assertEqual(Spaces(self.base).active(), other['id'])
        self.assertEqual(Service(target).snapshot({})['workflows'], [])
        self.assertEqual(self.service.workflow(graph['id'])['title'], 'Original')
        self.registry.rename(other['id'], 'Renamed')
        self.assertEqual(Service(target).snapshot({})['workspace']['name'], 'Renamed')
        for key in ['../original', str(self.base), 'missing', '../../']:
            with self.assertRaises(ValueError): self.registry.root(key)

    def test_convert_preserves_data_and_never_uploads(self):
        graph = self.service.create({'template': 'text', 'title': 'Keep me'})
        team = {'id': 'team_123', 'server': 'https://example.test', 'account_id': 'account_123'}
        converted = self.registry.create('Team', team, 'local')
        self.assertEqual(converted['id'], 'local'); self.assertEqual(converted['kind'], 'team')
        self.assertEqual(self.registry.root('local'), self.base)
        self.assertEqual(self.service.workflow(graph['id'])['title'], 'Keep me')
        self.assertEqual(self.registry.create('Team', team)['id'], 'local')
        with self.assertRaises(ValueError): self.registry.create('Other', {**team, 'id': 'team_other'}, 'local')
        other_account = self.registry.create('Team', {**team, 'account_id': 'another'})
        self.assertNotEqual(other_account['id'], 'local')

    def test_preview_strips_private_and_downstream_values(self):
        graph = self.service.create({'template': 'text', 'title': 'Private task'})
        graph['nodes'][0]['config']['text'] = 'CONFIDENTIAL_CANARY_1234567890 private text'
        graph['nodes'][0]['title'] = 'private-document.pdf'
        graph['nodes'][1]['config']['template'] = 'derived secret CONFIDENTIAL_CANARY_1234567890'
        graph['annotations'] = [{'text': 'SECRET_NOTE_1234567890123456789', 'private': True}]
        self.service.save({'graph': graph}); saved = self.service.workflow(graph['id'])
        preview = prepare(self.service.store, {'workflow_id': graph['id'], 'version': saved['version'], 'title': 'Reusable method'})
        payload = json.dumps(preview['payload'])
        for fragment in ['CONFIDENTIAL', 'private-document.pdf', 'SECRET_NOTE', 'annotations', 'asset_id', 'provider_id']: self.assertNotIn(fragment, payload)
        self.assertEqual(len(preview['redactions']), 3)
        self.assertEqual(approved(self.service.store, preview['token'])['definition'], preview['payload']['definition'])
        self.assertEqual(self.service.snapshot({})['runs'], [])

    def test_local_inputs_and_keys_reset_but_public_instructions_remain(self):
        graph = self.service.create({'template': 'ai', 'title': 'Reusable public AI'})
        graph['nodes'][0]['config']['text'] = 'one-time public task'
        graph['nodes'][1]['config'].update(provider_id='provider_secret_id', instruction='Compare sources and provide citations.')
        self.service.save({'graph': graph}); g = self.service.workflow(graph['id'])
        p = prepare(self.service.store, {'workflow_id': g['id'], 'version': g['version']})
        nodes = p['payload']['definition']['graph']['nodes']
        self.assertEqual(nodes[0]['config']['text'], '')
        self.assertNotIn('provider_id', nodes[1]['config'])
        self.assertEqual(nodes[1]['config']['instruction'], 'Compare sources and provide citations.')

    def test_private_fragments_in_title_or_instruction_block_upload(self):
        self.service.save_knowledge({'title': 'Private knowledge', 'content': 'SPECIAL_PRIVATE_CANARY_0123456789 source material'})
        g = self.service.create({'template': 'ai'})
        for p in [{'title': 'SPECIAL_PRIVATE_CANARY_0123456789'}, {}]:
            if not p:
                g['nodes'][1]['config']['instruction'] = 'Use SPECIAL_PRIVATE_CANARY_0123456789'
                self.service.save({'graph': g}); g = self.service.workflow(g['id'])
            with self.assertRaises(ValueError): prepare(self.service.store, {'workflow_id': g['id'], 'version': g['version'], **p})

    def test_preview_is_bound_to_saved_graph_and_expires(self):
        g = self.service.create({'template': 'ai'})
        p = prepare(self.service.store, {'workflow_id': g['id'], 'version': g['version']})
        g['title'] = 'Changed'; self.service.save({'graph': g})
        with self.assertRaises(ValueError): approved(self.service.store, p['token'])
        g = self.service.workflow(g['id']); p = prepare(self.service.store, {'workflow_id': g['id'], 'version': g['version']})
        self.service.store.update('share_preview', p['token'], {'created_at': 0})
        with self.assertRaises(ValueError): approved(self.service.store, p['token'])

    def test_downloads_are_independent_without_runs_or_connections(self):
        g = self.service.create({'template': 'ai'})
        p = prepare(self.service.store, {'workflow_id': g['id'], 'version': 1})
        a = import_template(self.service.store, {'definition': p['payload']['definition'], 'title': 'Team copy A'})
        b = import_template(self.service.store, {'definition': p['payload']['definition'], 'title': 'Team copy B'})
        self.assertNotEqual(a['id'], b['id']); self.assertEqual(a['version'], 1)
        a['nodes'][1]['config']['instruction'] = 'Edited'; self.service.save({'graph': a})
        self.assertNotEqual(self.service.workflow(a['id'])['nodes'][1]['config'], self.service.workflow(b['id'])['nodes'][1]['config'])
        self.assertEqual(b['nodes'][1]['config']['provider_id'], '')
        self.assertEqual(self.service.snapshot({})['runs'], [])
        bad = copy.deepcopy(p['payload']['definition']); bad['graph']['nodes'][0]['config']['path'] = 'C:/secret'
        with self.assertRaises(ValueError): import_template(self.service.store, {'definition': bad})

    def test_composites_keep_executable_ports_and_isolate_inputs(self):
        g = self.service.create({'template': 'ai'})
        wrapped = self.service.wrap({'graph': g, 'node_ids': [n['id'] for n in g['nodes']]})
        # wrap returns an editable graph; save before preparing a public template.
        graph = wrapped.get('graph', wrapped)
        self.service.save({'graph': graph}); graph = self.service.workflow(g['id'])
        p = prepare(self.service.store, {'workflow_id': g['id'], 'version': graph['version']})
        result = import_template(self.service.store, {'definition': p['payload']['definition']})
        self.assertEqual(result['nodes'][0]['type_id'], 'workflow.composite')
        self.assertTrue(result['nodes'][0]['config']['definition']['nodes'])

    def test_bundled_methods_share_verified_references_without_source_upload(self):
        from runtime.community import catalog, adapt, reference_text
        entry = catalog()[0]; cap = adapt(entry)
        g = self.service.create({'template': 'blank'})
        g['nodes'] = [{'id': 'task', 'type_id': 'input.prompt', 'type_version': '1', 'title': 'Task', 'position': {'x': 0, 'y': 0}, 'config': {'text': 'one-off task'}},
                      {'id': 'cap', 'type_id': 'workflow.composite', 'type_version': '1', 'title': cap['title'], 'position': {'x': 400, 'y': 0}, 'config': {'definition': cap['definition'], 'capability_kind': cap['kind']}}]
        g['edges'] = [{'id': 'e', 'source': 'task', 'target': 'cap', 'sourceHandle': 'text', 'targetHandle': 'text'}]
        self.service.save({'graph': g}); g = self.service.workflow(g['id'])
        p = prepare(self.service.store, {'workflow_id': g['id'], 'version': g['version']})
        self.assertEqual(p['references'], [entry['id']])
        self.assertNotIn(entry['content'][:100], json.dumps(p['payload']))
        result = import_template(self.service.store, {'definition': p['payload']['definition']})
        nodes = result['nodes'][1]['config']['definition']['nodes']
        self.assertTrue(any(n.get('config', {}).get('text') == reference_text(entry) for n in nodes))


if __name__ == '__main__': unittest.main()
