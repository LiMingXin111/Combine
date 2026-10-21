"""Single-request private worker. Request/response spool stays in the local ACL scope."""
from pathlib import Path
import argparse
import json
import os
import sys
import traceback
import importlib.util

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# AppContainer can read this package but not enumerate its parent directory.
# Load the fixed package path directly instead of widening filesystem access.
package_spec = importlib.util.spec_from_file_location('runtime', Path(__file__).with_name('__init__.py'), submodule_search_locations=[str(Path(__file__).resolve().parent)])
package = importlib.util.module_from_spec(package_spec)
sys.modules['runtime'] = package
package_spec.loader.exec_module(package)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', required=True)
    p.add_argument('--request', required=True)
    p.add_argument('--response', required=True)
    args = p.parse_args()
    response = Path(args.response)
    try:
        request = json.loads(Path(args.request).read_text(encoding='utf-8'))
        if request['method'] in ['sandbox.probe', 'sandbox.probe_child']:
            import socket
            import ctypes
            from ctypes import wintypes as W
            token = W.HANDLE()
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            advapi = ctypes.WinDLL('advapi32', use_last_error=True)
            kernel.GetCurrentProcess.restype = W.HANDLE
            advapi.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, ctypes.POINTER(W.HANDLE)]
            advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token))
            is_container = W.DWORD()
            size = W.DWORD()
            advapi.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
            advapi.GetTokenInformation(token, 29, ctypes.byref(is_container), ctypes.sizeof(is_container), ctypes.byref(size))
            kernel.CloseHandle.argtypes = [W.HANDLE]
            kernel.CloseHandle(token)
            network = []
            for host, port in [('1.1.1.1', 443), ('127.0.0.1', request.get('params', {}).get('loopback_port', 9))]:
                try:
                    with socket.create_connection((host, port), timeout=2):
                        network.append({'target': host, 'connected': True})
                except OSError as error:
                    network.append({'target': host, 'connected': False, 'error': getattr(error, 'winerror', None), 'failure': type(error).__name__})
            result = {'is_appcontainer': bool(is_container.value), 'network': network, 'calculation': sum(range(101)), 'python': sys.version.split()[0]}
            if request['method'] == 'sandbox.probe':
                import subprocess
                import uuid
                token = uuid.uuid4().hex
                child_request = Path(args.root) / 'temp' / (token + '.request.json')
                child_response = Path(args.root) / 'temp' / (token + '.response.json')
                try:
                    child_request.write_text(json.dumps({'id': 'child-probe', 'method': 'sandbox.probe_child', 'params': request.get('params', {})}), encoding='utf-8')
                    child = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--root', args.root, '--request', str(child_request), '--response', str(child_response)], input=b'', capture_output=True, creationflags=0x08000000)
                    if child.returncode or not child_response.exists(): raise RuntimeError('The isolated child probe failed to start.')
                    result['child'] = json.loads(child_response.read_text(encoding='utf-8'))['result']
                finally:
                    child_request.unlink(missing_ok=True); child_response.unlink(missing_ok=True)
        else:
            from runtime.service import Service
            result = Service(Path(args.root)).dispatch(request['method'], request.get('params', {}))
        payload = {'id': request.get('id'), 'result': result}
    except Exception as error:
        payload = {'id': None, 'error': {'code': getattr(error, 'code', 'RUNTIME_ERROR'), 'message': str(error)}}
    # The broker reads only after this process exits. A unique response file
    # therefore needs no rename, which AppContainer can reject in LocalAppData.
    with response.open('x', encoding='utf-8') as output:
        output.write(json.dumps(payload, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
