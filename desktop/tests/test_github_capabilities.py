"""Open-source acquisition, adaptation and isolation contracts; model calls are intercepted."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import community, github_import as gh
from runtime.capabilities import instance, save, assistant_catalog
from runtime.service import Service
from runtime.graphs import validate, offline_report
from runtime.privacy import contains_api
from runtime.assistant import apply_actions
from runtime.store import now

MIT = 'MIT License\nCopyright (c) Test authors\nPermission is hereby granted, free of charge, to any person obtaining a copy.\nTHE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.'
BODY = '---\nname: Evidence review\ndescription: Review supplied evidence.\n---\n# Evidence review\nCompare the supplied evidence and state limitations.\n'
COMMIT = 'a' * 40


class GitHubCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-github-')
        self.api = Service(Path(self.temp.name)); self.store = self.api.store
        self.provider = 'provider_0123456789abcdef'
        self.store.put('provider', self.provider, {'enabled': True, 'model': 'intercepted-test'})
        self.store.put('setting', 'ai', {'mode': 'api', 'provider_id': self.provider})

    def tearDown(self): self.temp.cleanup()

    def graph(self, cap, private=False):
        graph = self.api.create({'template': 'blank', 'title': 'Open-source capability contract'})
        graph['nodes'] = [{'id': 'source', 'type_id': 'input.text' if private else 'input.prompt', 'config': {'text': 'PUBLIC_ORIGINAL_TASK'}, 'position': {'x': 0, 'y': 0}}, instance(cap, 'cap', {'x': 400, 'y': 0}), {'id': 'report', 'type_id': 'output.document', 'config': {}, 'position': {'x': 800, 'y': 0}}]
        graph['edges'] = [{'id': 'in', 'source': 'source', 'target': 'cap', 'sourceHandle': 'text', 'targetHandle': 'text'}, {'id': 'out', 'source': 'cap', 'target': 'report', 'sourceHandle': 'text', 'targetHandle': 'text'}]
        return self.api.save({'graph': graph})['workflow']

    def preview(self, names=None, extra=None, content=BODY, license_map=None):
        names = names or ['skills/example/SKILL.md']
        tree = [{'path': name, 'type': 'blob', 'mode': '100644'} for name in ['LICENSE', *names]] + (extra or [])
        def api(repo, path):
            if path == '': return {'default_branch': 'main', 'visibility': 'public', 'private': False}
            if path.startswith('/commits/'): return {'sha': COMMIT}
            if path.startswith('/git/trees/'): return {'tree': tree, 'truncated': False}
            raise AssertionError(path)
        def raw(repo, commit, name):
            self.assertEqual(commit, COMMIT)
            if license_map and name in license_map: return license_map[name].encode('utf-8')
            return (MIT if name.endswith('LICENSE') else content).encode('utf-8')
        with patch('runtime.github_import.api', side_effect=api), patch('runtime.github_import.raw', side_effect=raw):
            return gh.inspect(self.store, {'url': 'https://github.com/example/project'})

    def test_all_40_bundled_methods_have_pinned_sources_full_text_and_licenses(self):
        entries = community.catalog()
        self.assertEqual(len(entries), 40)
        self.assertEqual({kind: sum(e['kind'] == kind for e in entries) for kind in ['skill', 'agent', 'expert']}, {'skill': 12, 'agent': 8, 'expert': 20})
        for entry in entries:
            self.assertRegex(entry['commit'], r'^[a-f0-9]{40}$')
            self.assertEqual(gh.license_id(entry['license_text']), 'MIT')
            self.assertGreater(len(entry['content']), 100)
            self.assertEqual(community.sha(community.local(entry['file']).read_bytes()), entry['sha256'])
            cap = community.get(entry['id'])
            self.assertEqual(validate(self.graph(cap)), [])

    def test_api_guidance_nodes_actually_receive_complete_upstream_reference_and_task(self):
        for kind in ['skill', 'agent', 'expert']:
            entry = next(e for e in community.catalog() if e['kind'] == kind)
            graph = self.graph(community.get(entry['id'], 'api', self.provider))
            calls = []
            def cloud(messages, provider, tokens):
                calls.append(messages)
                self.assertEqual(provider, self.provider)
                self.assertIn(entry['content'], messages[-1]['content'])
                self.assertIn('PUBLIC_ORIGINAL_TASK', messages[-1]['content'])
                self.assertIn(entry['url'], messages[-1]['content'])
                return {'content': 'Delivered result ' + str(len(calls)), 'model': 'intercepted-test'}
            self.api.cloud_client = cloud
            run = self.api.start({'workflow_id': graph['id'], 'reuse': False})
            self.assertEqual(run['state'], 'completed')
            self.assertEqual(len(calls), 3 if kind == 'agent' else 1)
            self.assertEqual(self.store.get('node_run', run['id'] + ':report')['outputs']['text']['value'], 'Delivered result ' + str(len(calls)))

    def test_long_sources_are_not_truncated_into_a_short_prompt(self):
        entry = max(community.catalog(), key=lambda e: len(e['content']))
        self.assertGreater(len(entry['content']), 16000)
        cap = community.get(entry['id'])
        self.assertIn(entry['content'], next(n for n in cap['definition']['nodes'] if n['id'] == 'method_reference')['config']['text'])

    def test_local_community_variant_has_no_hidden_cloud_dependency_or_fallback(self):
        cap = self.api.dispatch('community.resolve', {'id': community.catalog()[0]['id'], 'mode': 'local'})
        graph = self.graph(cap, private=True)
        self.assertFalse(contains_api(graph))
        self.assertEqual(offline_report(graph, self.store)['status'], 'missing')
        self.api.cloud_client = Mock()
        self.assertEqual(self.api.start({'workflow_id': graph['id']})['state'], 'waiting_resource')
        self.api.cloud_client.assert_not_called()

    def test_private_material_cannot_escape_through_open_source_guidance(self):
        graph = self.graph(community.get(community.catalog()[0]['id']), private=True)
        self.api.cloud_client = Mock()
        self.assertEqual(self.api.start({'workflow_id': graph['id']})['state'], 'waiting_privacy')
        self.api.cloud_client.assert_not_called()

    def test_ai_discovers_and_adds_real_community_capabilities_in_both_modes(self):
        entry = community.catalog()[0]
        self.assertIn(entry['id'], json.dumps(assistant_catalog(self.store, 'api')))
        for mode in ['api', 'local']:
            graph = self.api.create({'template': 'blank', 'title': 'AI use'})
            graph['nodes'] = [{'id': 'source', 'type_id': 'input.prompt', 'config': {'text': 'User task'}}]
            actions = [{'op': 'add_capability', 'capability_id': entry['id'], 'id': 'cap'}, {'op': 'add_edge', 'source': 'source', 'sourceHandle': 'text', 'target': 'cap', 'targetHandle': 'text'}]
            updated, *_ = apply_actions(graph, actions, self.store, mode)
            self.assertEqual(contains_api(updated), mode == 'api')

    def test_configured_copies_retain_provenance_across_versions(self):
        entry = community.catalog()[0]; cap = community.get(entry['id'])
        copy = save(self.store, {'origin_id': entry['id'], 'kind': cap['kind'], 'title': 'Configured public copy', 'definition': cap['definition']})
        updated = save(self.store, {**copy, 'description': 'Edited metadata'})
        self.assertEqual(updated['source']['sha256'], entry['sha256'])
        self.assertEqual(updated['source']['license_text'], entry['license_text'])
        self.assertEqual(updated['version'], 2)

    def test_url_validation_rejects_credentials_private_hosts_queries_and_traversal(self):
        invalid = ['http://github.com/a/b', 'https://github.com.evil.test/a/b', 'https://localhost/a/b', 'https://user:pass@github.com/a/b', 'https://github.com/a/b?token=secret', 'https://github.com/a/b#fragment', 'https://github.com/a/b/blob/main/../secret.md', 'https://github.com/a/b/blob/main/%2e%2e/secret.md', 'https://github.com:443/a/b']
        with patch('runtime.github_import.request') as request:
            for url in invalid:
                with self.assertRaises(ValueError): gh.inspect(self.store, {'url': url})
            request.assert_not_called()

    def test_private_metadata_is_rejected_before_github_requests(self):
        self.store.put('asset', 'asset_private', {'name': 'ConfidentialNotes.md'})
        with patch('runtime.github_import.api') as request:
            with self.assertRaises(ValueError): gh.inspect(self.store, {'url': 'https://github.com/example/project', 'path': 'ConfidentialNotes.md'})
            request.assert_not_called()

    def test_preview_pins_commit_and_retains_hash_and_full_license_without_installing(self):
        preview = self.preview()
        self.assertEqual(preview['commit'], COMMIT)
        self.assertEqual(len(preview['entries']), 1)
        self.assertEqual(preview['entries'][0]['license'], 'MIT')
        self.assertNotIn('content', preview['entries'][0])
        self.assertEqual(self.store.get('github_preview', preview['id'])['entries'][0]['content'], BODY)
        self.assertEqual(self.store.list('capability'), [])

    def test_tool_and_script_requirements_are_disclosed_as_guidance_not_automatically_run(self):
        preview = self.preview(content=BODY + '\nUse MCP, python scripts/analyze.py and references/method.md.\n')
        entry = preview['entries'][0]
        self.assertEqual(len(entry['dependencies']), 3)
        installed = gh.install(self.store, {'preview_id': preview['id'], 'ids': [entry['id']], 'mode': 'api'})
        self.assertFalse(installed['upstream_code_executed'])
        self.assertTrue(all(n['type_id'] in ['input.prompt', 'data.merge_text', 'model.api'] for n in installed['capabilities'][0]['definition']['nodes']))

    def test_install_is_networkless_idempotent_and_keeps_complete_license(self):
        preview = self.preview(); values = {'preview_id': preview['id'], 'ids': [preview['entries'][0]['id']]}
        with patch('runtime.github_import.request') as request:
            first, second = gh.install(self.store, values), gh.install(self.store, values)
            request.assert_not_called()
        self.assertEqual(first['capabilities'][0]['id'], second['capabilities'][0]['id'])
        self.assertEqual(len(self.store.list('capability')), 1)
        self.assertEqual(first['capabilities'][0]['source']['license_text'], MIT)

    def test_failed_batch_installs_nothing_and_corrupted_preview_is_rejected(self):
        preview = self.preview(); key = preview['entries'][0]['id']
        with self.assertRaises(ValueError): gh.install(self.store, {'preview_id': preview['id'], 'ids': [key, 'missing']})
        self.assertEqual(self.store.list('capability'), [])
        cached = self.store.get('github_preview', preview['id']); cached['entries'][0]['content'] += 'tampered'
        self.store.put('github_preview', preview['id'], cached)
        with self.assertRaisesRegex(ValueError, 'integrity'): gh.install(self.store, {'preview_id': preview['id'], 'ids': [key]})

    def test_expired_preview_and_unknown_mode_are_rejected(self):
        preview = self.preview(); values = {'preview_id': preview['id'], 'ids': [preview['entries'][0]['id']]}
        with self.assertRaises(ValueError): gh.install(self.store, {**values, 'mode': 'shell'})
        self.store.update('github_preview', preview['id'], {'created_at': now() - 25 * 60 * 60 * 1000})
        with self.assertRaises(ValueError): gh.install(self.store, values)

    def test_unknown_or_overriding_license_is_not_enabled_for_import(self):
        names = ['skills/example/SKILL.md']
        preview = self.preview(names=names, extra=[{'path': 'skills/example/LICENSE', 'type': 'blob', 'mode': '100644'}], license_map={'skills/example/LICENSE': 'All rights reserved.'})
        self.assertFalse(preview['entries'][0]['available'])
        with self.assertRaises(ValueError): gh.install(self.store, {'preview_id': preview['id'], 'ids': [preview['entries'][0]['id']]})

    def test_per_file_license_declarations_and_notice_texts_are_preserved(self):
        restricted = self.preview(content=BODY.replace('name:', 'license: Proprietary\nname:'))
        self.assertFalse(restricted['entries'][0]['available'])
        preview = self.preview(extra=[{'path': 'NOTICE', 'type': 'blob', 'mode': '100644'}], license_map={'NOTICE': 'Attribution to upstream authors.'})
        cap = gh.install(self.store, {'preview_id': preview['id'], 'ids': [preview['entries'][0]['id']]})['capabilities'][0]
        self.assertEqual(cap['source']['notices'][0]['text'], 'Attribution to upstream authors.')

    def test_large_repositories_report_the_24_entry_scan_limit(self):
        preview = self.preview(names=[f'skills/skill{i:02}/SKILL.md' for i in range(26)])
        self.assertEqual(preview['total'], 26); self.assertTrue(preview['limited'])
        self.assertEqual(len(preview['entries']), 24)

    def test_symlinks_are_not_imported_as_capability_documents(self):
        with self.assertRaises(ValueError): self.preview(names=['README.md'], extra=[{'path': 'skills/link/SKILL.md', 'type': 'blob', 'mode': '120000'}])

    def test_deleted_or_modified_install_does_not_corrupt_the_source_snapshot(self):
        preview = self.preview(); values = {'preview_id': preview['id'], 'ids': [preview['entries'][0]['id']]}
        first = gh.install(self.store, values)['capabilities'][0]
        edited = deepcopy(first); edited['definition']['nodes'][1]['config']['instruction'] = 'User modification.'
        save(self.store, edited)
        second = gh.install(self.store, values)['capabilities'][0]
        self.assertNotEqual(second['id'], first['id'])
        self.assertNotIn('User modification', json.dumps(second))

    def test_redirects_and_unapproved_download_hosts_are_rejected(self):
        with self.assertRaises(ValueError): gh.NoRedirect().redirect_request(None, None, 302, None, None, 'https://evil.test')
        for url in ['https://evil.test/source', 'http://raw.githubusercontent.com/a/b/c', 'https://token@api.github.com/a', 'https://api.github.com:443/a']:
            with self.assertRaises(ValueError): gh.request(url)

    def test_single_file_blob_url_and_directory_paths_are_parsed_without_execution(self):
        self.assertEqual(gh.parse_url('https://github.com/example/project/blob/main/skills/test/SKILL.md'), ('example/project', 'main', 'skills/test/SKILL.md'))
        self.assertEqual(gh.parse_url('https://github.com/example/project.git'), ('example/project', '', ''))
        self.assertEqual(gh.clean_path('skills/test/'), 'skills/test')


if __name__ == '__main__': unittest.main()
