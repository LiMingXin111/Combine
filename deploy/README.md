# Self-host the account/team and compatibility web service

The Python service uses persistent SQLite and a background scheduler. Run one instance and one worker. The desktop's private runtime remains on each computer; this service does not become a cloud copy of desktop attachments or conversations.

## Local

Install Python 3.12+ and root `requirements-lock.txt` in a virtual environment. Run `scripts/start_team_server.py` or `Start-Combine-Team-Server.cmd` on Windows. The helper binds to loopback and ignores the model `.env`. Use `--port` and `--data-dir` to select an unused port and independent persistent directory.

For the separate compatibility browser application, `run.bat` / `run.sh` reads your own `.env` and opens port 8765. An empty model configuration can enable an explicitly labeled offline demonstration; provide your own configuration for real models.

## Multiple computers over HTTPS

Build the root Dockerfile and mount a durable volume at `/data`. Set `DEPLOY_MODE=cloud`, `DATA_DIR=/data`, `COOKIE_SECURE=1`, and an appropriate `REGISTRATION_MODE`. Place an HTTPS reverse proxy in front and configure a real hostname. Members enter the same origin in Team server. Do not expose the local development bridge.

The bundled Caddy deployment is available via `compose.production.yml`. Initialize a dedicated server environment:

```sh
python3 deploy/init_env.py --domain app.example.com --email admin@example.com
sh deploy/up.sh
```

This full compatibility deployment validates its model settings before launch, so configure your own real provider in the generated `.env.production`; it does not accept a demo model as production readiness. The initializer writes a new encryption master key and bootstrap credentials to ignored local files, without printing the password. Existing accounts/keys must be migrated with their original encryption key. Do not commit generated environment/bootstrap files.

For a dedicated team service without compatibility-model usage, an administrator may run `python -m uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8765 --workers 1` inside the image behind HTTPS, with explicit environment settings. Do not infer internet accessibility or secure cookies from a loopback-only test.

Check `/healthz` and `/readyz`, account creation/login, invite join, sharing and restart persistence on your own host. Back up the database and workspace using `scripts/full_backup.py`; preserve encryption keys separately. Never delete a populated volume to perform an upgrade. `deploy/build_release.py` produces a source deployment archive without user data or credentials.
