"""Real OS test: prove the child is AppContainer and deny external AND loopback TCP."""
from pathlib import Path
import json
import socket
import sys
import tempfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from runtime.sandbox import launch

with socket.socket() as listener, tempfile.TemporaryDirectory(prefix='combine-sandbox-') as directory:
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(1)
    # An unrestricted control connection proves the local target is reachable.
    with socket.create_connection(listener.getsockname(), timeout=1) as control:
        accepted, _ = listener.accept()
        accepted.close()
    root = Path(directory)
    request = root / 'request.json'
    response = root / 'response.json'
    request.write_text(json.dumps({'id': 'probe', 'method': 'sandbox.probe', 'params': {'loopback_port': listener.getsockname()[1]}}))
    metadata = launch(BASE/'src-tauri/resources/python/python.exe', BASE/'src-tauri/resources/runtime/entry.py', root, request, response)
    payload = json.loads(response.read_text(encoding='utf-8'))
    report = {'launcher': metadata, 'worker': payload}
    print(json.dumps(report))
    assert payload['result']['is_appcontainer']
    assert payload['result']['calculation'] == 5050
    assert all(not target['connected'] for target in payload['result']['network'])
    assert payload['result']['network'][0]['error'] == 10013
    try:
        escaped, _ = listener.accept()
        escaped.close()
        raise AssertionError('AppContainer reached the controlled loopback listener')
    except TimeoutError:
        pass
    assert payload['result']['python'].startswith('3.12.')
    assert payload['result']['child']['is_appcontainer']
    assert all(not target['connected'] for target in payload['result']['child']['network'])
    assert payload['result']['child']['network'][0]['error'] == 10013
    output = BASE / 'test-results'; output.mkdir(exist_ok=True)
    (output / 'sandbox-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
