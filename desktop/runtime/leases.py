"""OS-owned leases: released on process exit, including crashes."""
from contextlib import contextmanager
import os
import time


class Busy(Exception):
    pass


@contextmanager
def lease(path, wait=False, check=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0: handle.write(b'0'); handle.flush()
        acquired = False
        try:
            while not acquired:
                if check: check()
                try:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if not wait: raise Busy('This operation is already running.')
                    time.sleep(.1)
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)


def run_lease(store, run_id):
    from .graphs import digest
    return store.root / 'temp' / ('run-' + digest(run_id) + '.lock')


def recover_runs(store):
    for run in store.list('run'):
        if run['state'] not in ['running', 'pausing', 'cancelling']: continue
        try:
            with lease(run_lease(store, run['id'])):
                # Reload inside the lease so a concurrent completed run stays completed.
                current = store.get('run', run['id'])
                if current['state'] not in ['running', 'pausing', 'cancelling']: continue
                state = 'cancelled' if current.get('control') == 'cancel' else 'paused'
                message = 'Recovered after interruption. Completed local outputs are preserved.'
                store.update('run', run['id'], {'state': state, 'message': message, 'recovered': True})
                for node in store.list('node_run'):
                    if node['run_id'] == run['id'] and node['state'] == 'running':
                        store.update('node_run', node['id'], {'state': 'interrupted', 'message': 'Interrupted before completion; resume to retry local computation.'})
                store.event(run['id'], 'run.recovered', state=state)
        except Busy:
            continue
