"""Interactive local team-service entry point; private model .env is not loaded."""
import argparse
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description='Start the local Combine account and team service.')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', default=str(ROOT / 'data' / 'team-service'))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535: parser.error('Choose a port from 1 to 65535.')
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', args.port)) == 0:
            raise SystemExit(f'Port {args.port} is already in use. No existing process was stopped. Use --port to choose another port.')
    directory = Path(args.data_dir).resolve(); directory.mkdir(parents=True, exist_ok=True)
    os.environ.update(COMBINE_IGNORE_DOTENV='1', DATA_DIR=str(directory), DB_PATH=str(directory / 'combine.db'),
                      DEPLOY_MODE='self_hosted', REGISTRATION_MODE='open', COOKIE_SECURE='0', WECHAT_MODE='off')
    sys.path.insert(0, str(ROOT / 'backend'))
    import uvicorn
    print(f'Combine team service: http://127.0.0.1:{args.port}', flush=True)
    print('Desktop: space selector > Create team / Join team. Register an account on this server.', flush=True)
    print('Only this computer can connect. For other computers, use a self-hosted HTTPS server; see docs/TEAM_SPACES.md.', flush=True)
    uvicorn.run('app.main:app', host='127.0.0.1', port=args.port, workers=1, log_level='warning', access_log=False)


if __name__ == '__main__': main()
