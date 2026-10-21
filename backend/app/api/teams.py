"""Human teams and immutable, declarative workflow templates. No remote execution."""
import hashlib
import json
import math
import re
import secrets
from fastapi import APIRouter, Request
from .common import current_user
from .. import db
from ..util import ApiError, now_ms, new_id

router = APIRouter(prefix='/api/teams')
SCHEMA = 'combine.workflow-template/v1'
# Keep the upload format deliberately smaller than the local runtime format.
CONFIG = {
    'input.prompt': {'text', 'community_ref', 'community_sha'}, 'input.text': {'text'}, 'input.table': {'csv'},
    'input.public_table': {'csv'}, 'input.file': set(), 'input.image': set(),
    'transform.text': {'operation', 'template'}, 'math.calculate': {'expression'},
    'data.aggregate': {'column', 'operation'}, 'data.merge_text': {'input_count', 'labels', 'separator'},
    'model.local': {'instruction', 'max_tokens'}, 'model.api': {'instruction', 'max_tokens'},
    'knowledge.search': {'limit'}, 'human.input': {'prompt'},
    'output.document': {'heading'}, 'output.image': set(), 'output.table': set(),
    'output.chart': {'label_column', 'value_column'},
    'output.map': {'latitude', 'longitude', 'label'}, 'output.live_map': {'latitude', 'longitude', 'label'},
    'output.file': set(), 'network.public_search': {'query', 'count', 'need_content'},
    'network.public_fetch': {'url'}, 'workflow.composite': {'definition', 'capability_kind'},
}


def fail(message, status=400, code='TEAM_INVALID'):
    raise ApiError(status, code, message)


async def data(request):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 1024 * 1024: fail('The template exceeds the 1 MB limit.', 413)
        raw.extend(chunk)
    try: value = json.loads(raw)
    except (ValueError, UnicodeError): fail('Send a JSON object.')
    if not isinstance(value, dict): fail('Send a JSON object.')
    return value


def text(value, label, limit=160, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()): fail(f'Invalid {label}.')
    return value.strip()


def member(team, user, owner=False):
    row = db.q1('SELECT t.*,m.role FROM team_spaces t JOIN team_members m ON m.team_id=t.id WHERE t.id=? AND m.user_id=?', (team, user['id']))
    if not row: fail('This team is unavailable or you are no longer a member.', 404, 'TEAM_UNAVAILABLE')
    if owner and row['role'] != 'owner': fail('Only the team owner can do this.', 403, 'TEAM_PERMISSION')
    return row


def public_team(row):
    return {k: row[k] for k in ('id', 'name', 'owner_id', 'created_at', 'role')}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value): fail('Invalid template identifier.')


def validate_template(value):
    if not isinstance(value, dict) or set(value) != {'schema', 'graph'} or value['schema'] != SCHEMA: fail('Unsupported workflow template format.')
    total = [0]
    def graph(g, depth=0, inner=False):
        allowed = {'nodes', 'edges', 'final_outputs'} | ({'inputs', 'outputs', 'version', 'kind', 'title'} if inner else set())
        if depth > 5 or not isinstance(g, dict) or set(g) - allowed: fail('Invalid or overly nested workflow.')
        nodes, edges = g.get('nodes'), g.get('edges')
        if not isinstance(nodes, list) or not nodes or not isinstance(edges, list) or len(edges) > 800: fail('Invalid workflow graph.')
        total[0] += len(nodes)
        if total[0] > 200: fail('Templates support up to 200 nodes, including nested nodes.')
        ids = set()
        for n in nodes:
            if not isinstance(n, dict) or set(n) != {'id', 'type_id', 'type_version', 'title', 'position', 'config'}: fail('Only declarative node fields may be shared.')
            identifier(n['id'])
            if n['id'] in ids or not isinstance(n['type_id'], str) or n['type_id'] not in CONFIG or n['type_version'] != '1': fail('Duplicate node or unsupported node type/version.')
            ids.add(n['id']); text(n['title'], 'node title', 200)
            pos = n['position']
            if not isinstance(pos, dict) or set(pos) != {'x', 'y'} or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1000000 for v in pos.values()): fail('Invalid canvas position.')
            c = n['config']
            if not isinstance(c, dict) or set(c) - CONFIG[n['type_id']]: fail('Local references, credentials and unsupported configuration cannot be uploaded.')
            for k, v in c.items():
                if k == 'definition': graph(v, depth + 1, True)
                elif not isinstance(v, (str, int, float, bool)) or (isinstance(v, str) and len(v) > 60000) or (type(v) is float and not math.isfinite(v)): fail('Invalid node configuration.')
            if n['type_id'] in ('input.prompt', 'input.text') and c.get('text'): fail('Replace task input with an empty placeholder before sharing.')
            if n['type_id'] == 'input.prompt' and ('community_ref' in c or 'community_sha' in c):
                identifier(c.get('community_ref'))
                if not isinstance(c.get('community_sha'), str) or not re.fullmatch(r'[a-f0-9]{64}', c['community_sha']): fail('Invalid bundled method checksum.')
            if n['type_id'] in ('input.table', 'input.public_table') and c.get('csv'): fail('Table data must stay on the device.')
        for e in edges:
            if not isinstance(e, dict) or set(e) != {'id', 'source', 'target', 'sourceHandle', 'targetHandle'}: fail('Invalid edge.')
            for v in e.values(): identifier(v)
            if e['source'] not in ids or e['target'] not in ids: fail('An edge references a missing node.')
        if len({e['id'] for e in edges}) != len(edges): fail('Edge identifiers must be unique.')
        adjacency = {key: [] for key in ids}
        for e in edges: adjacency[e['source']].append(e['target'])
        visiting, visited = set(), set()
        def visit(key):
            if key in visiting: fail('Shared workflows cannot contain circular connections.')
            if key in visited: return
            visiting.add(key)
            for child in adjacency[key]: visit(child)
            visiting.remove(key); visited.add(key)
        for key in ids: visit(key)
        finals = g.get('final_outputs', [])
        if not isinstance(finals, list) or any(not isinstance(i, str) or i not in ids for i in finals) or len(set(finals)) != len(finals) or ('final_outputs' in g and not finals): fail('Invalid final output selection.')
        if inner:
            for direction in ('inputs', 'outputs'):
                ports = g.get(direction, [])
                if not isinstance(ports, list) or len(ports) > 100: fail('Invalid composite ports.')
                for p in ports:
                    if not isinstance(p, dict) or set(p) != {'id', 'kind', 'node', 'port'}: fail('Invalid composite port.')
                    for v in p.values(): identifier(v)
                    if p['node'] not in ids or p['kind'] not in ('text', 'table', 'image', 'chart', 'map'): fail('Invalid composite mapping.')
            if 'title' in g: text(g['title'], 'composite title', 200)
            if 'version' in g and (type(g['version']) is not int or g['version'] < 1): fail('Invalid composite version.')
            if 'kind' in g and g['kind'] not in ('skill', 'agent', 'expert', 'workflow'): fail('Invalid capability kind.')
    graph(value['graph'])
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if re.search(r'(?i)\b(sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9]{20,})|(?<![a-z0-9])[a-z]:[\\/]|file://', encoded): fail('Remove credentials and local paths before sharing.')
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


@router.get('')
async def listing(request: Request):
    u = current_user(request)
    return {'items': [public_team(r) for r in db.q('SELECT t.*,m.role FROM team_spaces t JOIN team_members m ON t.id=m.team_id WHERE m.user_id=? ORDER BY t.created_at', (u['id'],))]}


@router.post('')
async def create(request: Request):
    u = current_user(request); p = await data(request)
    name = text(p.get('name'), 'team name', 80); key = text(p.get('request_key'), 'request key', 100)
    with db.Tx():
        row = db.q1('SELECT * FROM team_spaces WHERE owner_id=? AND request_key=?', (u['id'], key))
        if not row:
            if db.q1('SELECT COUNT(*) AS n FROM team_spaces WHERE owner_id=?', (u['id'],))['n'] >= 20: fail('Each account can own up to 20 teams.')
            row = {'id': new_id('team'), 'name': name, 'owner_id': u['id'], 'request_key': key, 'created_at': now_ms()}
            db.insert('team_spaces', row)
            db.insert('team_members', {'team_id': row['id'], 'user_id': u['id'], 'role': 'owner', 'joined_at': now_ms()})
    return {'team': public_team({**row, 'role': 'owner'})}


def invite_row(code):
    code = text(code, 'invite code', 100)
    row = db.q1('SELECT i.*,t.name FROM team_invites i JOIN team_spaces t ON t.id=i.team_id WHERE token_hash=?', (hashlib.sha256(code.encode()).hexdigest(),))
    if not row or row['revoked'] or row['expires_at'] <= now_ms(): fail('This invite is invalid, expired or revoked.', 410, 'INVITE_UNAVAILABLE')
    return row


@router.post('/invitations/preview')
async def preview_invite(request: Request):
    u = current_user(request); p = await data(request); row = invite_row(p.get('code'))
    joined = bool(db.q1('SELECT 1 FROM team_members WHERE team_id=? AND user_id=?', (row['team_id'], u['id'])))
    if row['uses'] >= row['max_uses'] and not joined: fail('This invite has reached its use limit.', 410)
    return {'team': {'id': row['team_id'], 'name': row['name']}, 'already_joined': joined, 'expires_at': row['expires_at']}


@router.post('/join')
async def join(request: Request):
    u = current_user(request); p = await data(request)
    with db.Tx():
        row = invite_row(p.get('code'))
        existing = db.q1('SELECT role FROM team_members WHERE team_id=? AND user_id=?', (row['team_id'], u['id']))
        if not existing:
            if row['uses'] >= row['max_uses']: fail('This invite has reached its use limit.', 410)
            db.insert('team_members', {'team_id': row['team_id'], 'user_id': u['id'], 'role': 'member', 'joined_at': now_ms()})
            db.ex('UPDATE team_invites SET uses=uses+1 WHERE id=?', (row['id'],))
    return {'team': public_team(member(row['team_id'], u))}


@router.get('/{team_id}')
async def detail(team_id: str, request: Request):
    u = current_user(request); t = member(team_id, u)
    members = db.q('SELECT m.user_id,m.role,m.joined_at,u.display_name FROM team_members m JOIN users u ON u.id=m.user_id WHERE team_id=? ORDER BY joined_at', (team_id,))
    invites = db.q('SELECT id,expires_at,max_uses,uses,revoked,created_at FROM team_invites WHERE team_id=? ORDER BY created_at DESC LIMIT 100', (team_id,)) if t['role'] == 'owner' else []
    return {'team': public_team(t), 'members': members, 'invites': invites, 'user_id': u['id']}


@router.post('/{team_id}/invites')
async def invite(team_id: str, request: Request):
    u = current_user(request); member(team_id, u, True); p = await data(request)
    uses = p.get('max_uses', 10)
    if type(uses) is not int or not 1 <= uses <= 100: fail('Choose an invite use limit between 1 and 100.')
    code = 'cmb_' + secrets.token_urlsafe(24)
    row = {'id': new_id('invite'), 'team_id': team_id, 'token_hash': hashlib.sha256(code.encode()).hexdigest(), 'created_by': u['id'], 'expires_at': now_ms() + 7 * 86400000, 'max_uses': uses, 'created_at': now_ms()}
    with db.Tx():
        member(team_id, u, True)
        db.insert('team_invites', row)
    return {'code': code, 'id': row['id'], 'expires_at': row['expires_at'], 'max_uses': uses}


@router.delete('/{team_id}/invites/{invite_id}')
async def revoke(team_id: str, invite_id: str, request: Request):
    member(team_id, current_user(request), True)
    db.ex('UPDATE team_invites SET revoked=1 WHERE id=? AND team_id=?', (invite_id, team_id))
    return {'revoked': True}


@router.delete('/{team_id}/members/{user_id}')
async def remove_member(team_id: str, user_id: str, request: Request):
    u = current_user(request); t = member(team_id, u)
    if user_id == t['owner_id']: fail('The owner must keep membership. Delete the team instead.')
    if u['id'] != user_id and t['role'] != 'owner': fail('Only the owner can remove other members.', 403)
    db.ex('DELETE FROM team_members WHERE team_id=? AND user_id=?', (team_id, user_id))
    return {'removed': True}


@router.delete('/{team_id}')
async def delete_team(team_id: str, request: Request):
    member(team_id, current_user(request), True)
    db.ex('DELETE FROM team_spaces WHERE id=?', (team_id,))
    return {'deleted': True}


def get_template(team_id, template_id, user, write=False):
    team = member(team_id, user)
    row = db.q1('SELECT * FROM team_templates WHERE team_id=? AND id=?', (team_id, template_id))
    if not row: fail('This shared workflow is unavailable.', 404)
    if write and row['author_id'] != user['id'] and team['role'] != 'owner': fail('Only its author or the team owner can change this workflow.', 403)
    return row


@router.get('/{team_id}/templates')
async def templates(team_id: str, request: Request):
    member(team_id, current_user(request))
    return {'items': db.q('SELECT t.*,COALESCE(u.display_name,\'Former member\') AS author_name FROM team_templates t LEFT JOIN users u ON u.id=t.author_id WHERE team_id=? ORDER BY t.updated_at DESC LIMIT 500', (team_id,))}


@router.get('/{team_id}/templates/{template_id}/versions/{version}')
async def template_version(team_id: str, template_id: str, version: int, request: Request):
    row = get_template(team_id, template_id, current_user(request))
    v = db.q1('SELECT * FROM team_template_versions WHERE template_id=? AND version=?', (template_id, version))
    if not v: fail('This workflow version is unavailable.', 404)
    return {'template': row, 'version': v['version'], 'definition': db.j(v['definition']), 'digest': v['digest']}


@router.post('/{team_id}/templates')
async def publish(team_id: str, request: Request):
    u = current_user(request); member(team_id, u); p = await data(request)
    title = text(p.get('title'), 'workflow title', 160); description = text(p.get('description', ''), 'description', 2000, True)
    key = text(p.get('request_key'), 'request key', 100)
    encoded, sha = validate_template(p.get('definition'))
    with db.Tx():
        member(team_id, u)
        old = db.q1('SELECT * FROM team_templates WHERE team_id=? AND request_key=?', (team_id, key))
        if old:
            if old['author_id'] != u['id']: fail('This request key is already in use.', 409)
            return {'template': old}
        row = {'id': new_id('shared'), 'team_id': team_id, 'author_id': u['id'], 'title': title, 'description': description, 'version': 1, 'request_key': key, 'created_at': now_ms(), 'updated_at': now_ms()}
        db.insert('team_templates', row)
        db.insert('team_template_versions', {'template_id': row['id'], 'version': 1, 'definition': encoded, 'digest': sha, 'created_at': now_ms()})
    return {'template': row}


@router.post('/{team_id}/templates/{template_id}/versions')
async def update_template(team_id: str, template_id: str, request: Request):
    u = current_user(request); p = await data(request)
    encoded, sha = validate_template(p.get('definition'))
    with db.Tx():
        row = get_template(team_id, template_id, u, True)
        if type(p.get('expected_version')) is not int or row['version'] != p['expected_version']: fail('A newer team version exists. Refresh before publishing.', 409, 'TEAM_VERSION_CONFLICT')
        version = row['version'] + 1
        db.insert('team_template_versions', {'template_id': template_id, 'version': version, 'definition': encoded, 'digest': sha, 'created_at': now_ms()})
        db.update('team_templates', {'id': template_id}, {'version': version, 'updated_at': now_ms()})
    return {'template': {**row, 'version': version}}


@router.delete('/{team_id}/templates/{template_id}')
async def delete_template(team_id: str, template_id: str, request: Request):
    get_template(team_id, template_id, current_user(request), True)
    db.ex('DELETE FROM team_templates WHERE id=? AND team_id=?', (template_id, team_id))
    return {'deleted': True}
