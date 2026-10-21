"""Deployment regressions with temporary data and no network model calls."""
import base64
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.mkdtemp(prefix="combine-deployment-")
os.environ.update(DATA_DIR=TEMP, DB_PATH=str(Path(TEMP) / "combine.db"), COMBINE_IGNORE_DOTENV="1",
                  FAKE_LLM="1", WEB_SEARCH_MODE="fake", WECHAT_MODE="off")
sys.path.insert(0, str(ROOT / "backend"))
from app import config, db
from app.security import verify_password
from scripts import bootstrap_admin, full_backup, production_check, start_hosted
from tests.test_e2e import Server, Client


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init_env, release = load("init_env"), load("build_release")


def tearDownModule():
    db.reset_connection()
    shutil.rmtree(TEMP, ignore_errors=True)


class Deployment(unittest.TestCase):
    def test_railway_rejects_missing_or_wrong_persistent_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            for mount in ("", str(Path(directory) / "other")):
                with patch.dict(os.environ, DATA_DIR=directory, RAILWAY_ENVIRONMENT_ID="test", RAILWAY_VOLUME_MOUNT_PATH=mount):
                    with self.assertRaisesRegex(ValueError, "persistent volume"):
                        start_hosted.prepare_storage()

    def test_hosted_bootstrap_happens_before_server_exec(self):
        with tempfile.TemporaryDirectory() as directory:
            script = (
                "import os,sys;sys.path[:0]=['backend','scripts'];import start_hosted;"
                "start_hosted.prepare_storage=lambda:None;"
                "def_unused=None;"
                "\ndef launch(executable,args):\n"
                " from app import db\n"
                " assert db.q1('SELECT email,is_admin FROM users')=={'email':'admin@example.com','is_admin':1}\n"
                " assert 'BOOTSTRAP_ADMIN_PASSWORD' not in os.environ\n"
                " assert 'BOOTSTRAP_ADMIN_EMAIL' not in os.environ\n"
                " assert args[args.index('--port')+1]=='9876'\n"
                " assert args[args.index('--workers')+1]=='1'\n"
                " db.reset_connection()\n"
                " print('Bootstrap precedes server startup')\n"
                "\nos.execv=launch\nstart_hosted.main()\n"
            )
            env = dict(os.environ, DATA_DIR=directory, DB_PATH=str(Path(directory) / "combine.db"),
                       DEPLOY_MODE="cloud", COOKIE_SECURE="1", TRUST_PROXY="1", RATE_LIMIT="1", FAKE_LLM="0",
                       PLATFORM_BASE_URL="https://example.com/v1", PLATFORM_API_KEY="unused-test-key",
                       PLATFORM_MODEL_FAST="m", PLATFORM_MODEL_STANDARD="m", PLATFORM_MODEL_STRONG="m",
                       SECRETS_MASTER_KEY=base64.b64encode(b"t" * 32).decode(), PORT="9876",
                       BOOTSTRAP_ADMIN_EMAIL="admin@example.com", BOOTSTRAP_ADMIN_PASSWORD="test-password-never-logged")
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Bootstrap precedes server startup", result.stdout)
            self.assertNotIn(env["BOOTSTRAP_ADMIN_PASSWORD"], result.stdout + result.stderr)

    def test_generated_secrets_and_closed_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            output, credentials = init_env.initialize("APP.example.com", "admin@example.com", Path(directory) / ".env.production")
            env = init_env.read_env(output)
            admin = json.loads(credentials.read_text())
            self.assertEqual(len(base64.b64decode(env["SECRETS_MASTER_KEY"])), 32)
            self.assertGreaterEqual(len(admin["password"]), 24)
            self.assertEqual(env["COMBINE_DOMAIN"], "app.example.com")
            self.assertEqual(env["REGISTRATION_MODE"], "closed")
            self.assertEqual(env["PLATFORM_API_KEY"], "")
            before = output.read_bytes(), credentials.read_bytes()
            with self.assertRaises(ValueError):
                init_env.initialize("other.example.com", "other@example.com", output)
            self.assertEqual(before, (output.read_bytes(), credentials.read_bytes()))

    def test_invalid_host_never_writes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            for domain in ("https://example.com", "example.com/path", "example.com:443", "localhost", "x\nY=z"):
                with self.assertRaises(ValueError):
                    init_env.initialize(domain, "admin@example.com", Path(directory) / ".env.production")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_bootstrap_preserves_existing_password(self):
        db.ex("DELETE FROM users")
        try:
            self.assertTrue(bootstrap_admin.bootstrap({"email": "Admin@example.com", "password": "first-strong-password"}))
            original = db.q1("SELECT * FROM users")
            self.assertEqual(original["is_admin"], 1)
            self.assertTrue(verify_password("first-strong-password", original["password_hash"]))
            self.assertFalse(bootstrap_admin.bootstrap({"email": "other@example.com", "password": "replacement-password"}))
            self.assertEqual(db.q1("SELECT * FROM users")["password_hash"], original["password_hash"])
        finally:
            db.ex("DELETE FROM users")

    def test_invalid_bootstrap_does_not_create_account(self):
        db.ex("DELETE FROM users")
        with self.assertRaises(ValueError):
            bootstrap_admin.bootstrap({"email": "admin@example.com", "password": "short"})
        self.assertIsNone(db.q1("SELECT id FROM users"))

    def test_production_rejects_fake_model_and_missing_key(self):
        with patch.object(config, "FAKE_LLM", True), patch.object(config, "SECRETS_MASTER_KEY", ""):
            with self.assertRaises(ValueError) as error:
                production_check.check()
            self.assertIn("FAKE_LLM", str(error.exception))
            self.assertIn("SECRETS_MASTER_KEY", str(error.exception))

    def test_production_accepts_complete_configuration(self):
        with patch.multiple(config, DEPLOY_MODE="cloud", COOKIE_SECURE=True, TRUST_PROXY=True, RATE_LIMIT=True,
                            FAKE_LLM=False, PLATFORM_MODELS=dict(fast="m", standard="m", strong="m"),
                            SECRETS_MASTER_KEY=base64.b64encode(b"x" * 32).decode()):
            production_check.check()

    def test_backup_has_restorable_database_and_files_without_master_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "combine.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TABLE marker (value TEXT)")
                connection.execute("INSERT INTO marker VALUES ('snapshot')")
            (root / "workspace").mkdir()
            (root / "workspace" / "result.md").write_text("delivery", encoding="utf-8")
            (root / "master.key").write_text("must-not-enter-backup", encoding="utf-8")
            with patch.multiple(config, DATA_DIR=root, DB_PATH=database):
                full_backup.backup(keep=1)
                output = full_backup.backup(keep=1)
            self.assertEqual(len(list((root / "backups").glob("*.tar.gz"))), 1)
            with tarfile.open(output) as archive:
                self.assertEqual(set(archive.getnames()), {"combine.db", "workspace/result.md", "metadata.json"})
                self.assertEqual(archive.extractfile("workspace/result.md").read(), b"delivery")
                restored = root / "restored.db"
                restored.write_bytes(archive.extractfile("combine.db").read())
            with closing(sqlite3.connect(restored)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "snapshot")

    def test_release_excludes_credentials_userdata_and_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            for name in release.FILES:
                (root / name).write_text("source", encoding="utf-8")
            for name in release.DIRECTORIES:
                (root / name).mkdir()
                (root / name / "source.py").write_text("pass", encoding="utf-8")
            for name in (".env", ".admin-bootstrap.json", "deploy/.env.production", "backend/secret.pyc"):
                (root / name).write_text("secret", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "combine.db").write_text("private", encoding="utf-8")
            output = Path(directory) / "release.tar.gz"
            members, checksum = release.build_release(output, root)
            self.assertEqual(checksum, hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(len(members), len(release.FILES) + len(release.DIRECTORIES))
            with tarfile.open(output) as archive:
                self.assertTrue(all(m.isfile() and not Path(m.name).is_absolute() and ".." not in Path(m.name).parts for m in archive))

    def test_cloud_startup_secure_login_and_registration_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, DATA_DIR=directory, DB_PATH=str(Path(directory) / "combine.db"),
                       DEPLOY_MODE="cloud", COOKIE_SECURE="1", TRUST_PROXY="1", REGISTRATION_MODE="closed")
            credential = dict(email="admin@example.com", password="production-test-password")
            result = subprocess.run([sys.executable, "scripts/bootstrap_admin.py"], cwd=ROOT, env=env,
                                    input=json.dumps(credential), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(credential["password"], result.stdout + result.stderr)
            server = Server(data_dir=directory, DEPLOY_MODE="cloud", COOKIE_SECURE="1", TRUST_PROXY="1", REGISTRATION_MODE="closed",
                            SECRETS_MASTER_KEY=base64.b64encode(b"t" * 32).decode())
            try:
                client = Client(server)
                client.get("/healthz", expect=200)
                client.get("/readyz", expect=200)
                request = urllib.request.Request(client.base + "/api/auth/login", data=json.dumps(credential).encode(),
                                                 headers={"Content-Type": "application/json", "X-Combine": "1"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    cookie = response.headers["Set-Cookie"]
                    self.assertIn("Secure", cookie)
                    self.assertIn("HttpOnly", cookie)
                    self.assertIn("SameSite=lax", cookie)
                client.post("/api/auth/register", dict(email="other@example.com", password="other-password"), expect=403)
            finally:
                server.stop(keep=True)


if __name__ == "__main__":
    unittest.main()
