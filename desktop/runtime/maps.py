"""Public map tiles through a fixed source; private coordinates never reach it."""
import base64
import urllib.request
from .leases import lease
from .store import now


def tile(store, p):
    node = store.get('node_run', str(p.get('run_id', '')) + ':' + str(p.get('node_id', '')))
    data = node.get('outputs', {}).get('map') if node else None
    if not data or data.get('private', True) or not isinstance(data.get('value'), dict) or not data['value'].get('live'):
        raise ValueError('Live map requests are blocked for this output. Private coordinates stay on this computer.')
    z, x, y = p.get('z'), p.get('x'), p.get('y')
    if any(type(v) is not int for v in [z, x, y]) or not 0 <= z <= 19 or not 0 <= x < 2**z or not 0 <= y < 2**z: raise ValueError('Invalid map tile coordinates.')
    directory = store.root / 'resources/map-tiles'; directory.mkdir(parents=True, exist_ok=True)
    target = directory / f'{z}-{x}-{y}.png'
    with lease(directory / 'fetch.lock', wait=True):
        # Keep cached tiles for at least the provider's seven-day minimum.
        if not target.is_file() or now() - int(target.stat().st_mtime * 1000) > 7 * 86400000:
            request = urllib.request.Request(f'https://tile.openstreetmap.org/{z}/{x}/{y}.png', headers={'User-Agent': 'CombineDesktop/0.2 (interactive workflow map)', 'Accept': 'image/png'})
            try:
                with urllib.request.urlopen(request, timeout=15) as response: content = response.read(1024 * 1024)
                if not content.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('The map service returned an unsupported tile.')
                target.write_bytes(content)
            except Exception:
                if not target.exists(): return {'available': False, 'message': 'Map tiles are unavailable. Your coordinates are preserved locally.'}
        return {'available': True, 'url': 'data:image/png;base64,' + base64.b64encode(target.read_bytes()).decode()}
