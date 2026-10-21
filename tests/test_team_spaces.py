"""Real HTTP, distinct accounts and persistent SQLite. No AI calls or live user data."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from tests.test_e2e import Server, Client


def definition():
    return {'schema': 'combine.workflow-template/v1', 'graph': {
        'nodes': [
            {'id': 'source', 'type_id': 'input.prompt', 'type_version': '1', 'title': 'Task', 'position': {'x': 0, 'y': 0}, 'config': {'text': ''}},
            {'id': 'report', 'type_id': 'output.document', 'type_version': '1', 'title': 'Report', 'position': {'x': 400, 'y': 0}, 'config': {'heading': 'Report'}}],
        'edges': [{'id': 'edge', 'source': 'source', 'target': 'report', 'sourceHandle': 'text', 'targetHandle': 'text'}], 'final_outputs': ['report']}}


class TeamSpaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(RATE_LIMIT='0', WECHAT_MODE='off')
        cls.owner, cls.member, cls.other = [Client(cls.srv) for _ in range(3)]
        cls.users = []
        for i, client in enumerate([cls.owner, cls.member, cls.other]):
            cls.users.append(client.post('/api/auth/register', {'email': f'team{i}@example.test', 'password': 'team-test-password', 'display_name': f'Tester {i}'}, expect=200)['user'])

    @classmethod
    def tearDownClass(cls): cls.srv.stop()

    def setUp(self):
        self.key = uuid.uuid4().hex
        self.team = self.owner.post('/api/teams', {'name': 'Research team', 'request_key': self.key}, expect=200)['team']
        self.path = '/api/teams/' + self.team['id']

    def invite(self, uses=10): return self.owner.post(self.path + '/invites', {'max_uses': uses}, expect=200)

    def join(self, client=None, code=None): return (client or self.member).post('/api/teams/join', {'code': code or self.invite()['code']}, expect=200)

    def publish(self, client=None, value=None, key=None):
        return (client or self.owner).post(self.path + '/templates', {'title': 'Report workflow', 'description': 'A reusable method', 'request_key': key or uuid.uuid4().hex, 'definition': value or definition()}, expect=200)['template']

    def test_auth_and_membership_boundaries(self):
        Client(self.srv).get('/api/teams', expect=401)
        self.assertEqual(self.owner.get('/api/teams')['items'][-1]['id'], self.team['id'])
        self.other.get(self.path, expect=404)
        self.other.get(self.path + '/templates', expect=404)
        self.other.post(self.path + '/invites', {}, expect=404)
        self.join()
        detail = self.member.get(self.path, expect=200)
        self.assertEqual(detail['team']['role'], 'member')
        self.assertEqual(detail['invites'], [])
        self.assertNotIn('email', detail['members'][0])
        self.member.post(self.path + '/invites', {}, expect=403)

    def test_creation_and_join_are_idempotent(self):
        again = self.owner.post('/api/teams', {'name': 'Research team', 'request_key': self.key}, expect=200)
        self.assertEqual(again['team']['id'], self.team['id'])
        invite = self.invite(1)
        preview = self.member.post('/api/teams/invitations/preview', {'code': invite['code']}, expect=200)
        self.assertFalse(preview['already_joined'])
        self.join(code=invite['code']); self.join(code=invite['code'])
        self.other.post('/api/teams/join', {'code': invite['code']}, expect=410)
        current = self.owner.get(self.path)
        self.assertEqual(next(i for i in current['invites'] if i['id'] == invite['id'])['uses'], 1)
        conn = self.srv.db()
        stored = conn.execute('SELECT token_hash FROM team_invites WHERE id=?', (invite['id'],)).fetchone()[0]
        self.assertEqual(stored, hashlib.sha256(invite['code'].encode()).hexdigest())
        self.assertNotEqual(stored, invite['code'])

    def test_invite_revoke_and_expiry(self):
        invite = self.invite()
        self.owner.call('DELETE', self.path + '/invites/' + invite['id'], expect=200)
        self.member.post('/api/teams/invitations/preview', {'code': invite['code']}, expect=410)
        invite = self.invite()
        conn = self.srv.db(); conn.execute('UPDATE team_invites SET expires_at=0 WHERE id=?', (invite['id'],)); conn.commit()
        self.member.post('/api/teams/join', {'code': invite['code']}, expect=410)

    def test_members_can_publish_and_download_without_changing_others(self):
        self.join(); shared = self.publish(self.member)
        other = self.publish()
        items = self.member.get(self.path + '/templates')['items']
        self.assertEqual(len(items), 2)
        v = self.owner.get(f"{self.path}/templates/{shared['id']}/versions/1", expect=200)
        self.assertEqual(v['definition'], definition())
        self.other.get(f"{self.path}/templates/{shared['id']}/versions/1", expect=404)
        self.member.call('DELETE', f"{self.path}/templates/{other['id']}", expect=403)
        self.member.post(f"{self.path}/templates/{other['id']}/versions", {'expected_version': 1, 'definition': definition()}, expect=403)
        self.owner.call('DELETE', f"{self.path}/templates/{shared['id']}", expect=200)

    def test_immutable_versions_and_cas(self):
        shared = self.publish(key='same-request')
        self.assertEqual(shared['id'], self.publish(key='same-request')['id'])
        v2 = definition(); v2['graph']['nodes'][1]['config']['heading'] = 'Revised'
        path = f"{self.path}/templates/{shared['id']}"
        self.owner.post(path + '/versions', {'expected_version': 1, 'definition': v2}, expect=200)
        self.owner.post(path + '/versions', {'expected_version': 1, 'definition': definition()}, expect=409)
        self.assertEqual(self.owner.get(path + '/versions/1')['definition'], definition())
        self.assertEqual(self.owner.get(path + '/versions/2')['definition'], v2)

    def test_member_removal_and_leave_revoke_access(self):
        self.join(); shared = self.publish(self.member)
        member_id = self.users[1]['id']; owner_id = self.users[0]['id']
        self.member.call('DELETE', self.path + '/members/' + owner_id, expect=400)
        self.owner.call('DELETE', self.path + '/members/' + member_id, expect=200)
        self.member.get(self.path + '/templates', expect=404)
        self.member.get(f"{self.path}/templates/{shared['id']}/versions/1", expect=404)
        self.join(); self.member.call('DELETE', self.path + '/members/' + member_id, expect=200)
        self.member.get(self.path, expect=404)

    def test_upload_rejects_local_data_and_credentials(self):
        variants = []
        for key, value in [('asset_id', 'local-file'), ('provider_id', 'provider_secret'), ('output', 'run output'), ('path', 'C:/private/file.txt')]:
            d = definition(); d['graph']['nodes'][0]['config'][key] = value; variants.append(d)
        d = definition(); d['graph']['nodes'][0]['config']['text'] = 'PRIVATE-CANARY'; variants.append(d)
        d = definition(); d['graph']['nodes'][1]['config']['heading'] = 'sk-' + 'secret' * 8; variants.append(d)
        d = definition(); d['graph']['annotations'] = [{'text': 'private note'}]; variants.append(d)
        d = definition(); d['graph']['nodes'][0]['type_id'] = 'exec.shell'; variants.append(d)
        for d in variants:
            with self.subTest(d=d): self.owner.post(self.path + '/templates', {'title': 'Test', 'request_key': uuid.uuid4().hex, 'definition': d}, expect=400)
        self.assertEqual(self.owner.get(self.path + '/templates')['items'], [])

    def test_delete_cascades_team_records(self):
        self.join(); self.publish(); self.invite()
        self.member.call('DELETE', self.path, expect=403)
        self.owner.call('DELETE', self.path, expect=200)
        self.owner.get(self.path, expect=404)
        conn = self.srv.db()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM team_members WHERE team_id=?', (self.team['id'],)).fetchone()[0], 0)
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_public_urls_and_reference_checksums_are_portable(self):
        d = definition()
        d['graph']['nodes'][1]['config']['heading'] = 'See https://example.org and references:\nReview the source.'
        d['graph']['nodes'][0]['config'].update(community_ref='community_method', community_sha='a' * 64)
        self.publish(value=d)

    def test_malformed_graphs_are_rejected_without_server_errors(self):
        values = []
        d = definition(); d['graph']['nodes'][0]['type_id'] = []; values.append(d)
        d = definition(); d['graph']['final_outputs'] = []; values.append(d)
        d = definition(); d['graph']['edges'].append({'id': 'cycle', 'source': 'report', 'target': 'source', 'sourceHandle': 'text', 'targetHandle': 'text'}); values.append(d)
        d = definition(); d['graph']['nodes'][0]['config'].update(community_ref='method', community_sha='bad'); values.append(d)
        d = definition(); d['graph']['nodes'][1]['config']['heading'] = 'C:/private/data'; values.append(d)
        for d in values:
            self.owner.post(self.path + '/templates', {'title': 'Invalid', 'request_key': uuid.uuid4().hex, 'definition': d}, expect=400)
        self.assertEqual(self.owner.get(self.path + '/templates')['items'], [])

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI credential protection')
    def test_desktop_session_protection_expiry_and_english_errors(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'desktop'))
        from runtime.spaces import Spaces
        from runtime.teams import dispatch, TeamError, account_info, session, server_url
        from runtime.service import Service
        with tempfile.TemporaryDirectory() as directory:
            registry = Spaces(Path(directory) / 'workspace')
            local = Service(registry.root()).dispatch
            result = dispatch(registry, 'local', 'account.login', {'server': self.owner.base, 'email': 'team0@example.test', 'password': 'team-test-password'}, local)
            self.assertTrue(result['connected']); self.assertNotIn('token', result)
            encrypted = (registry.home / 'account.bin').read_bytes()
            self.assertNotIn(b'team0@example.test', encrypted)
            self.assertNotIn(session(registry)['token'].encode(), encrypted)
            dispatch(registry, 'local', 'team.list', {}, local)
            # Expire only this broker's session, leaving other clients independent.
            digest = hashlib.sha256(session(registry)['token'].encode()).hexdigest()
            conn = self.srv.db(); conn.execute('UPDATE sessions SET expires_at=0 WHERE token_hash=?', (digest,)); conn.commit()
            with self.assertRaisesRegex(TeamError, 'Sign in again'): dispatch(registry, 'local', 'team.list', {}, local)
            self.assertFalse(account_info(registry)['connected'])
            self.assertEqual(local('workspace.snapshot', {})['workflows'], [])
            with self.assertRaisesRegex(TeamError, 'email or password'): dispatch(registry, 'local', 'account.login', {'server': self.owner.base, 'email': 'team0@example.test', 'password': 'wrong-password'}, local)
            for origin in ['http://example.com', 'https://user:pass@example.com', 'https://127.0.0.2', 'https://example.com/path']:
                with self.assertRaises(ValueError): server_url(origin)


if __name__ == '__main__': unittest.main()
