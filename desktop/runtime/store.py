from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time
import uuid


def new_id(prefix='id'): return prefix + '_' + uuid.uuid4().hex[:16]
def now(): return int(time.time() * 1000)


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ['assets', 'exports', 'backups', 'resources', 'ipc', 'temp']:
            (self.root / name).mkdir(exist_ok=True)
        with self.connection() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS entities (kind TEXT NOT NULL,id TEXT NOT NULL,data TEXT NOT NULL,updated INTEGER NOT NULL,PRIMARY KEY(kind,id));
              CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,created INTEGER NOT NULL,data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY,data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
              INSERT OR IGNORE INTO meta VALUES('schema_version','1');
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.root / 'workspace.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally: db.close()

    def list(self, kind):
        with self.connection() as db:
            return [json.loads(r['data']) for r in db.execute('SELECT data FROM entities WHERE kind=? ORDER BY updated DESC,id', (kind,))]

    def get(self, kind, key, default=None):
        with self.connection() as db:
            row = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', (kind, key)).fetchone()
            return json.loads(row['data']) if row else default

    def put(self, kind, key, data):
        data = {**data, 'id': key, 'updated_at': now()}
        with self.connection() as db:
            db.execute('INSERT INTO entities VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated', (kind, key, json.dumps(data, ensure_ascii=False, allow_nan=False), data['updated_at']))
        return data

    def update(self, kind, key, patch):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', (kind, key)).fetchone()
            if not row: raise ValueError('Record was not found.')
            data = {**json.loads(row['data']), **patch, 'id': key, 'updated_at': now()}
            db.execute('UPDATE entities SET data=?,updated=? WHERE kind=? AND id=?', (json.dumps(data, ensure_ascii=False, allow_nan=False), data['updated_at'], kind, key))
        return data

    def create_conversation_workflow(self, conversation_id, graph):
        """Idempotently create a manual canvas and link its local conversation."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', ('conversation', conversation_id)).fetchone()
            conversation = json.loads(row['data']) if row else {'id': conversation_id, 'messages': [], 'title': graph['title'], 'created_at': now()}
            if conversation.get('workflow_id'):
                linked = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', ('workflow', conversation['workflow_id'])).fetchone()
                workflow = json.loads(linked['data']) if linked else None
                if workflow and not workflow.get('deleted'): return workflow
            timestamp = now()
            graph = {**graph, 'updated_at': timestamp}
            conversation = {**conversation, 'workflow_id': graph['id'], 'updated_at': timestamp}
            for kind, key, data in [('workflow', graph['id'], graph), ('workflow_version', graph['id'] + ':1', {**graph, 'id': graph['id'] + ':1'}), ('conversation', conversation_id, conversation)]:
                statement = 'INSERT INTO entities VALUES(?,?,?,?)'
                if kind == 'conversation': statement += ' ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated'
                db.execute(statement, (kind, key, json.dumps(data, ensure_ascii=False, allow_nan=False), timestamp))
        return graph

    def delete(self, kind, key):
        with self.connection() as db: db.execute('DELETE FROM entities WHERE kind=? AND id=?', (kind, key))

    def apply_workflow(self, graph, expected_version, changed):
        """Commit the immutable definition and current pointer in one CAS transaction."""
        data = {**graph, 'updated_at': now()}
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM entities WHERE kind=? AND id=?', ('workflow', data['id'])).fetchone()
            if not row or json.loads(row['data']).get('deleted') or json.loads(row['data']).get('version') != expected_version:
                raise ValueError('This draft is out of date. Reopen the latest version before applying changes.')
            if changed:
                key = data['id'] + ':' + str(data['version'])
                immutable = {**data, 'id': key}
                db.execute('INSERT INTO entities VALUES(?,?,?,?)', ('workflow_version', key, json.dumps(immutable, ensure_ascii=False, allow_nan=False), data['updated_at']))
            db.execute('UPDATE entities SET data=?,updated=? WHERE kind=? AND id=?', (json.dumps(data, ensure_ascii=False, allow_nan=False), data['updated_at'], 'workflow', data['id']))
        return data

    def event(self, run_id, event, **data):
        payload = {'type': event, **data}
        with self.connection() as db:
            cursor = db.execute('INSERT INTO events(run_id,created,data) VALUES(?,?,?)', (run_id, now(), json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            return cursor.lastrowid

    def events(self, run_id, after=0):
        with self.connection() as db:
            return [{'seq': row['seq'], 'created_at': row['created'], **json.loads(row['data'])} for row in db.execute('SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 1000', (run_id, int(after)))]

    def cached(self, key):
        with self.connection() as db:
            row = db.execute('SELECT data FROM cache WHERE key=?', (key,)).fetchone()
            return json.loads(row['data']) if row else None

    def cache(self, key, data):
        with self.connection() as db: db.execute('INSERT OR REPLACE INTO cache VALUES(?,?)', (key, json.dumps(data, ensure_ascii=False, allow_nan=False)))

    def local_file(self, path):
        result = (self.root / path).resolve()
        if not result.is_relative_to(self.root): raise ValueError('The path leaves this workspace.')
        return result
