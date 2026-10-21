"""Development loopback transport. Each request pins its own Python revision.

Production uses Tauri IPC. This optional browser helper never logs RPC bodies.
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import argparse
import json
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/rpc' or self.headers.get('Origin') not in ['http://127.0.0.1:1420', 'http://localhost:1420'] or self.headers.get('X-Combine-Desktop') != '1' or self.headers.get('Host') not in ['127.0.0.1:1421', 'localhost:1421']:
            self.send_error(403); return
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.send_error(400); return
        if not 0 < size <= 90 * 1024 * 1024:
            self.send_error(413); return
        try:
            manifest = BASE / '.dev/runtime.json'
            directory = 'runtime'
            if manifest.exists():
                revision = json.loads(manifest.read_text('utf-8'))['revision']
                if len(revision) != 64 or any(c not in '0123456789abcdef' for c in revision):
                    raise ValueError('Invalid development revision.')
                directory = 'runtime-dev-' + revision
            reply = subprocess.run([sys.executable, '-I', str(BASE / 'src-tauri/resources' / directory / 'launcher.py'), '--root', self.server.workspace],
                                   input=self.rfile.read(size), capture_output=True, creationflags=0x08000000)
            if reply.returncode:
                raise RuntimeError('Development broker failed.')
            result = json.loads(reply.stdout)
        except Exception:
            result = {'error': {'code': 'DEV_BRIDGE', 'message': 'The development broker failed. Check the local development setup.'}}
        payload = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    if Path(sys.executable).resolve() != (BASE / 'src-tauri/resources/python/python.exe').resolve():
        raise SystemExit('Start this bridge with src-tauri/resources/python/python.exe.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=str(BASE / '.dev/browser-workspace'))
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', 1421), Handler)
    server.workspace = str(Path(args.root).resolve())
    print('Desktop development bridge: 127.0.0.1:1421', flush=True)
    server.serve_forever()
