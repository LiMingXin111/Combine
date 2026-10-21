"""Real revision publication and metadata-only trace regression checks."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location('development_publisher', BASE / 'scripts/dev_runtime.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
from runtime.devtrace import span


class DevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='combine-development-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.resources = self.root / 'resources'
        self.manifest = self.root / 'dev/runtime.json'
        self.source.mkdir()
        for name in ('__init__.py', 'launcher.py', 'entry.py'):
            (self.source / name).write_text('# synthetic revision\n', 'utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def publish(self):
        return publisher.publish(self.source, self.resources, self.manifest)

    def test_running_revision_unchanged_after_edit_or_deleted_module(self):
        (self.source / 'executor.py').write_text('answer = 41\n', 'utf-8')
        first = self.publish()
        (self.source / 'executor.py').write_text('answer = 42\n', 'utf-8')
        second = self.publish()
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual((self.resources / first['directory'] / 'executor.py').read_text(), 'answer = 41\n')
        self.assertEqual((self.resources / second['directory'] / 'executor.py').read_text(), 'answer = 42\n')
        (self.source / 'executor.py').unlink()
        third = self.publish()
        self.assertFalse((self.resources / third['directory'] / 'executor.py').exists())
        self.assertTrue((self.resources / first['directory'] / 'executor.py').exists())

    def test_bad_edit_retains_last_usable_manifest_and_runtime(self):
        first = self.publish()
        manifest = self.manifest.read_bytes()
        (self.source / 'entry.py').write_text('def invalid(:\n', 'utf-8')
        with self.assertRaises(SyntaxError):
            self.publish()
        self.assertEqual(self.manifest.read_bytes(), manifest)
        self.assertTrue((self.resources / first['directory'] / 'entry.py').is_file())

    def test_unchanged_content_does_not_publish_or_touch_release_runtime(self):
        release = self.resources / 'runtime'
        release.mkdir(parents=True)
        (release / 'sentinel.py').write_text('release = True\n')
        first = self.publish()
        second = self.publish()
        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'])
        self.assertEqual(first['revision'], second['revision'])
        self.assertEqual((release / 'sentinel.py').read_text(), 'release = True\n')

    def test_community_sources_and_licenses_are_immutable_revision_resources(self):
        import hashlib
        community = self.source / 'community'; community.mkdir()
        source = community / 'method.md'; source.write_text('Public method one', 'utf-8')
        license = community / 'license.txt'; license.write_text('Public license', 'utf-8')
        manifest = {'entries': [{'file': 'method.md', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'license_file': 'license.txt', 'license_sha256': hashlib.sha256(license.read_bytes()).hexdigest()}]}
        (community / 'catalog.json').write_text(json.dumps(manifest), 'utf-8')
        first = self.publish()
        self.assertEqual((self.resources / first['directory'] / 'community/method.md').read_text(), 'Public method one')
        source.write_text('Public method two', 'utf-8')
        with self.assertRaises(ValueError): self.publish()
        self.assertEqual(json.loads(self.manifest.read_text())['revision'], first['revision'])
        manifest['entries'][0]['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        (community / 'catalog.json').write_text(json.dumps(manifest), 'utf-8')
        second = self.publish()
        self.assertNotEqual(first['revision'], second['revision'])
        self.assertEqual((self.resources / first['directory'] / 'community/method.md').read_text(), 'Public method one')

    def test_broker_imports_own_snapshot_despite_release_on_sys_path(self):
        shutil.copytree(BASE / 'runtime', self.source, dirs_exist_ok=True)
        revision = self.publish()
        code = ('import runpy, sys; sys.path.insert(0, sys.argv[1]); '
                'runpy.run_path(sys.argv[2], run_name="probe"); '
                'import runtime.engine; print(runtime.engine.__file__); print(runtime.engine.EXECUTION_REVISION)')
        reply = subprocess.run([sys.executable, '-I', '-c', code, str(BASE),
                                str(self.resources / revision['directory'] / 'launcher.py')],
                               capture_output=True, text=True, check=True)
        self.assertIn(revision['directory'], reply.stdout)
        self.assertIn('desktop-runtime-3:' + revision['directory'], reply.stdout)

    def test_trace_correlates_worker_without_payloads_or_error_messages(self):
        trace = self.root / 'logs/rpc.jsonl'
        identifier = '01234567-89ab-cdef-0123-456789abcdef'
        secret = 'synthetic-key-and-private-document-text'
        with patch.dict(os.environ, {'COMBINE_DEV_TRACE': str(trace)}):
            with self.assertRaisesRegex(ValueError, secret):
                with span('ai.chat', request_id=identifier):
                    with span('node.execute', phase='worker'):
                        raise ValueError(secret)
        items = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertEqual(len(items), 4)
        self.assertTrue(all(item['request_id'] == identifier for item in items))
        self.assertEqual(items[-1]['event'], 'failed')
        self.assertNotIn(secret, trace.read_text())
        self.assertEqual(items[-1]['error_type'], 'ValueError')

    def test_trace_rejects_data_in_identifiers_and_marks_runtime_rejection(self):
        trace = self.root / 'logs/rpc.jsonl'
        with patch.dict(os.environ, {'COMBINE_DEV_TRACE': str(trace)}):
            with span('private.document', request_id='private-document-title') as outcome:
                outcome['rejected'] = True
        text = trace.read_text()
        self.assertNotIn('private.document', text)
        self.assertNotIn('private-document', text)
        self.assertEqual(json.loads(text.splitlines()[-1])['event'], 'rejected')

    def test_worker_cannot_enable_trace_or_gain_host_log_write_access(self):
        trace = self.root / 'logs/rpc.jsonl'
        with patch.dict(os.environ, {'COMBINE_DEV_TRACE': str(trace), 'COMBINE_SANDBOX': 'appcontainer'}):
            with span('node.execute'):
                pass
        self.assertFalse(trace.exists())


if __name__ == '__main__':
    unittest.main()
