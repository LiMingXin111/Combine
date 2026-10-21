"""Trusted HTTPS account/team broker. Session secrets never enter workspace DBs or IPC."""
from http.cookies import SimpleCookie
import ipaddress
import hashlib
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .vault import _crypt
from .store import now

DEFAULT_SERVER = 'http://127.0.0.1:8765'


class TeamError(ValueError):
    def __init__(self, message, status=0):
        super().__init__(message); self.status = status


def server_url(value):
    try: url = urllib.parse.urlsplit(str(value).strip())
    except ValueError: raise ValueError('Enter a valid team server URL.')
    if url.username or url.password or url.query or url.fragment or url.path not in ('', '/') or not url.hostname:
        raise ValueError('Use the team server origin, without credentials, paths or query strings.')
    local = url.hostname in ('127.0.0.1', 'localhost', '::1')
    if url.scheme != 'https' and not (local and url.scheme == 'http'): raise ValueError('Team servers require HTTPS. HTTP is available only on this computer for development.')
    try:
        addresses = socket.getaddrinfo(url.hostname, url.port or (443 if url.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if not local and any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses): raise ValueError('Choose a public HTTPS team server.')
    except socket.gaierror: raise TeamError('The team server cannot be reached. Check your connection and server URL.')
    return urllib.parse.urlunsplit((url.scheme, url.netloc, '', '', '')).rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def request(server, path, method='GET', payload=None, token=None):
    server = server_url(server)  # Check on every connection, including previously saved origins.
    headers = {'Content-Type': 'application/json', 'X-Combine': '1', 'Accept': 'application/json', 'User-Agent': 'Combine-Desktop/0.2'}
    if token: headers['Cookie'] = 'cb_session=' + token
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload is not None else None
    if raw and len(raw) > 1024 * 1024: raise ValueError('The template exceeds the 1 MB upload limit.')
    req = urllib.request.Request(server + path, data=raw, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=25) as reply:
            content = reply.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024: raise TeamError('The team server response is too large.')
            result = json.loads(content)
            cookie = SimpleCookie(); cookie.load(reply.headers.get('Set-Cookie', ''))
            return result, cookie['cb_session'].value if 'cb_session' in cookie else None
    except urllib.error.HTTPError as error:
        try: detail = json.loads(error.read(8192)).get('error', {})
        except (ValueError, AttributeError): detail = {}
        codes = {'LOGIN_FAILED': 'The email or password is incorrect.', 'EMAIL_TAKEN': 'This email is already registered. Sign in with your existing account.',
                 'EMAIL_INVALID': 'Enter a valid email address.', 'PASSWORD_WEAK': 'Use a password with at least eight characters.',
                 'REGISTRATION_CLOSED': 'This server is not accepting new registrations. Ask the server administrator for access.',
                 'TOO_MANY_ATTEMPTS': 'Too many sign-in attempts. Try again in ten minutes.'}
        messages = {401: 'Sign in again to access the team service. Your local work remains available.',
                    403: 'Your account does not have permission for this team action.',
                    404: 'This team resource is unavailable. The server may need the team-space update.',
                    409: 'This record changed on the server. Refresh and try again.',
                    429: 'Too many requests. Please wait and retry.'}
        message = detail.get('message', '')
        if not isinstance(message, str) or not message or re.search(r'[\u3400-\u9fff]', message): message = codes.get(detail.get('code'), messages.get(error.code, 'The team service rejected this request. Check your account and try again.'))
        raise TeamError(message, error.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        if isinstance(error, TeamError): raise
        raise TeamError('The team server is unavailable. Your workspace continues to work locally; retry when connected.')


def session(registry, required=True):
    file = registry.home / 'account.bin'
    if not file.exists():
        if required: raise ValueError('Sign in to a team account first. Local spaces do not require an account.')
        return None
    return json.loads(_crypt(file.read_bytes(), decrypt=True).decode())


def save_session(registry, value):
    file = registry.home / 'account.bin'; temp = file.with_name('account-' + uuid.uuid4().hex + '.tmp')
    try: temp.write_bytes(_crypt(json.dumps(value).encode())); temp.replace(file)
    finally: temp.unlink(missing_ok=True)


def account_info(registry):
    s = session(registry, False)
    return {'connected': bool(s), 'server': s['server'] if s else DEFAULT_SERVER, 'user': s['user'] if s else None}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value): raise ValueError('Choose a valid team resource.')
    return value


def dispatch(registry, space_id, method, p, local):
    if method == 'account.status': return account_info(registry)
    if method in ['account.login', 'account.register']:
        server = server_url(p.get('server', DEFAULT_SERVER))
        meta, _ = request(server, '/api/meta')
        if meta.get('team_spaces', {}).get('version') != 1: raise TeamError('This server does not support team spaces yet. Update the self-hosted backend or choose a compatible team server.')
        payload = {'email': str(p.get('email', '')).strip(), 'password': p.get('password', '')}
        if method == 'account.register': payload['display_name'] = str(p.get('display_name', '')).strip()
        result, cookie = request(server, '/api/auth/' + method.split('.')[1], 'POST', payload)
        if not cookie or not re.fullmatch(r'[A-Za-z0-9_-]+', cookie): raise TeamError('The server did not issue a supported login session.')
        user = result.get('user') or {}
        if not user.get('id'): raise TeamError('The server did not return an account.')
        save_session(registry, {'server': server, 'user': {k: user.get(k, '') for k in ['id', 'email', 'display_name']}, 'token': cookie})
        return account_info(registry)
    if method == 'account.logout':
        s = session(registry, False)
        if s:
            try: request(s['server'], '/api/auth/logout', 'POST', {}, s['token'])
            except TeamError: pass
        (registry.home / 'account.bin').unlink(missing_ok=True)
        with registry.db() as db: db.execute('DELETE FROM cache')
        return account_info(registry)
    s = session(registry)
    def call(path, verb='GET', body=None):
        try: return request(s['server'], '/api/teams' + path, verb, body, s['token'])[0]
        except TeamError as error:
            if error.status == 401:
                (registry.home / 'account.bin').unlink(missing_ok=True)
                with registry.db() as db: db.execute('DELETE FROM cache')
            raise
    def binding(team): return {'id': team['id'], 'server': s['server'], 'account_id': s['user']['id']}
    def attach(team, existing=None): return registry.create(team['name'], binding(team), existing)
    team_id = p.get('team_id')
    if method not in ['team.list', 'team.create', 'team.join', 'team.invite_preview', 'team.attach']:
        team_id = identifier(team_id)
    cache_prefix = s['server'] + ':' + s['user']['id'] + ':'
    if method == 'team.list':
        key = cache_prefix + 'memberships'
        try: return registry.cached(key, {**call(''), 'offline': False})
        except TeamError as error:
            if error.status: registry.cached(key, clear=True); raise
            cached = registry.cached(key)
            if cached: return {**cached, 'offline': True}
            raise
    if method == 'team.create':
        from .privacy import assert_public_payload
        from .store import Store
        name = p.get('name', '')
        assert_public_payload(Store(registry.root(space_id)), {'name': name})
        if p.get('convert_current') and registry.get(space_id)['kind'] != 'local': raise ValueError('Select a local space to convert.')
        team = call('', 'POST', {'name': name, 'request_key': identifier(p.get('request_key'))})['team']
        return {'team': team, 'space': attach(team, space_id if p.get('convert_current') else None)}
    if method == 'team.attach':
        team = call('/' + identifier(p.get('team_id')))['team']
        return {'team': team, 'space': attach(team)}
    if method == 'team.invite_preview': return call('/invitations/preview', 'POST', {'code': p.get('code')})
    if method == 'team.join':
        team = call('/join', 'POST', {'code': p.get('code')})['team']
        return {'team': team, 'space': attach(team)}
    if method == 'team.detail': return call('/' + team_id)
    if method == 'team.invite': return call('/' + team_id + '/invites', 'POST', {'max_uses': p.get('max_uses', 10)})
    if method == 'team.revoke_invite': return call('/' + team_id + '/invites/' + identifier(p.get('id')), 'DELETE')
    if method == 'team.remove_member': return call('/' + team_id + '/members/' + identifier(p.get('user_id')), 'DELETE')
    if method == 'team.library':
        key = cache_prefix + team_id
        try:
            result = call('/' + team_id + '/templates')
            return registry.cached(key, {**result, 'offline': False, 'cached_at': now()})
        except TeamError as error:
            if error.status: registry.cached(key, clear=True); raise
            cache = registry.cached(key)
            if cache: return {**cache, 'offline': True, 'message': str(error)}
            raise
    if method == 'team.publish':
        payload = local('sharing.approved', {'token': identifier(p.get('preview_token'))})
        if p.get('template_id'):
            path = '/' + team_id + '/templates/' + identifier(p['template_id']) + '/versions'
            payload['expected_version'] = p.get('expected_version')
        else: path = '/' + team_id + '/templates'
        return call(path, 'POST', payload)
    if method == 'team.import':
        template_id = identifier(p.get('template_id')); version = p.get('version')
        if type(version) is not int or version < 1: raise ValueError('Choose a valid workflow version.')
        result = call('/' + team_id + '/templates/' + template_id + '/versions/' + str(version))
        raw = json.dumps(result['definition'], ensure_ascii=False, allow_nan=False).encode()
        if hashlib.sha256(raw).hexdigest() != result.get('digest'): raise ValueError('The team workflow integrity check failed. No local copy was created.')
        return local('sharing.import', {'definition': result['definition'], 'title': result['template']['title'], 'source': {'team_id': team_id, 'template_id': template_id, 'version': version, 'server': s['server']}})
    if method == 'team.delete_template': return call('/' + team_id + '/templates/' + identifier(p.get('template_id')), 'DELETE')
    if method == 'team.delete': return call('/' + team_id, 'DELETE')
    raise ValueError('Unknown team operation.')
