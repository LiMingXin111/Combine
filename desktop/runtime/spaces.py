"""Device space registry. Only the trusted broker may resolve workspace paths."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import sqlite3
import uuid


class Spaces:
    def __init__(self, base):
        self.base = Path(base).resolve()
        self.home = self.base.parent / ('.combine-spaces-' + hashlib.sha256(str(self.base).encode()).hexdigest()[:16])
        self.home.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS spaces(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,team TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO spaces(id,name,kind) VALUES('local','My workspace','local');
                INSERT OR IGNORE INTO meta VALUES('active','local');''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.home / 'spaces.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        try: yield db; db.commit()
        except Exception: db.rollback(); raise
        finally: db.close()

    def active(self):
        with self.db() as db: return db.execute("SELECT value FROM meta WHERE key='active'").fetchone()[0]

    def get(self, key=None):
        key = key or self.active()
        if not isinstance(key, str): raise ValueError('Invalid space identifier.')
        with self.db() as db: row = db.execute('SELECT * FROM spaces WHERE id=?', (key,)).fetchone()
        if not row: raise ValueError('This space is unavailable. Reopen the space selector.')
        return {**dict(row), 'team': json.loads(row['team'])}

    def root(self, key=None):
        row = self.get(key)
        # Never accept paths from IPC, metadata or a server.
        return self.base if row['id'] == 'local' else self.home / 'workspaces' / row['id']

    def list(self):
        with self.db() as db: rows = db.execute('SELECT * FROM spaces ORDER BY rowid').fetchall()
        return {'active_id': self.active(), 'items': [{**dict(r), 'team': json.loads(r['team'])} for r in rows]}

    def create(self, name, team=None, existing=None):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80: raise ValueError('Give this space a name (1–80 characters).')
        name = name.strip()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM spaces').fetchall()
            if team:
                match = next((r for r in rows if json.loads(r['team']) == team), None)
                if match: return self.get(match['id'])
            if len(rows) >= 100: raise ValueError('This device supports up to 100 spaces.')
            if existing:
                row = self.get(existing)
                if row['kind'] != 'local': raise ValueError('This space is already connected to a team.')
                key = row['id']
                db.execute('UPDATE spaces SET kind=?,team=? WHERE id=?', ('team', json.dumps(team), key))
            else:
                key = 'space_' + uuid.uuid4().hex
                db.execute('INSERT INTO spaces VALUES(?,?,?,?)', (key, name, 'team' if team else 'local', json.dumps(team or {})))
        from .store import Store
        store = Store(self.root(key))
        if not existing: store.put('setting', 'general', {'theme': 'light', 'language': 'en', 'output_language': 'en', 'workspace_name': name})
        return self.get(key)

    def activate(self, key):
        self.get(key)
        with self.db() as db: db.execute("UPDATE meta SET value=? WHERE key='active'", (key,))
        return {'active_id': key}

    def rename(self, key, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80: raise ValueError('Give this space a name (1–80 characters).')
        row = self.get(key)
        with self.db() as db: db.execute('UPDATE spaces SET name=? WHERE id=?', (name.strip(), row['id']))
        from .store import Store
        store = Store(self.root(key)); old = store.get('setting', 'general', {})
        store.put('setting', 'general', {**old, 'workspace_name': name.strip()})
        return self.get(key)

    def cached(self, key, value=None, clear=False):
        with self.db() as db:
            if clear: db.execute('DELETE FROM cache WHERE key=?', (key,)); return None
            if value is not None:
                db.execute('INSERT INTO cache VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value)))
                return value
            row = db.execute('SELECT value FROM cache WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None
