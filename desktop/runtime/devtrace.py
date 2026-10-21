"""Opt-in trusted development tracing; never store request/response contents.

Only the development host sets COMBINE_DEV_TRACE. The AppContainer environment
does not inherit it. Logs and credentials stay outside worker filesystem grants.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
import uuid

_request = ContextVar('development_request', default=None)
_methods = frozenset('''
workspace.snapshot workspace.settings workspace.backup workspace.restore
workspace.diagnostics workspace.checkpoint workflow.create workflow.save
workflow.preview workflow.layout workflow.integrate workflow.delete workflow.duplicate workflow.wrap
workflow.unwrap workflow.check workflow.history workflow.publish
workflow.from_library workflow.ai_plan workflow.undo run.start run.control
run.detail run.events run.edit run.export asset.import asset.delete asset.preview
knowledge.save knowledge.search knowledge.delete knowledge.get knowledge.archive
knowledge.classify notification.read node.execute ai.local_completion
ai.conversation ai.settings ai.chat provider.list provider.save provider.delete
provider.test provider.models provider.select_model map.tile resource.install resource.verify
resource.cancel sandbox.probe sandbox.probe_child
'''.split())


@contextmanager
def span(method, phase='broker', request_id=None):
    filename = os.environ.get('COMBINE_DEV_TRACE')
    if not filename or os.environ.get('COMBINE_SANDBOX') == 'appcontainer':
        yield
        return
    token = None
    outcome = {}
    if _request.get() is None:
        # Ignore arbitrary caller-provided identifiers that could contain data.
        identifier = request_id if isinstance(request_id, str) and re.fullmatch(r'[a-f0-9-]{36}', request_id) else str(uuid.uuid4())
        token = _request.set(identifier)
    method = method if isinstance(method, str) and method in _methods else 'unknown'
    base = {'request_id': _request.get(), 'method': method, 'phase': phase,
            'revision': Path(__file__).parent.name.removeprefix('runtime-dev-')}
    started = time.monotonic()
    _write(filename, {**base, 'event': 'started'})
    try:
        yield outcome
    except Exception as error:
        _write(filename, {**base, 'event': 'failed', 'error_type': type(error).__name__,
                          'elapsed_ms': round((time.monotonic() - started) * 1000, 1)})
        raise
    else:
        _write(filename, {**base, 'event': 'rejected' if outcome.get('rejected') else 'finished',
                          'elapsed_ms': round((time.monotonic() - started) * 1000, 1)})
    finally:
        if token is not None:
            _request.reset(token)


def _write(filename, item):
    try:
        from .leases import lease
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        with lease(path.with_suffix('.lock'), wait=True):
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                path.replace(path.with_suffix('.previous.jsonl'))
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'at': datetime.now(timezone.utc).isoformat(), **item}) + '\n')
    except OSError:
        # Debug logging must never interrupt the user's task.
        pass
