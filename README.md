# Combine

A local-first desktop application for building, editing and running visual AI workflows.

Describe a task in the right-hand conversation, let your configured model build a workflow, and refine its nodes and connections through conversation or direct editing. Inputs, processing, research, AI reasoning, visualizations and final deliverables share one editable canvas.

## Features

- Editable node graphs with typed ports, Shift selection, resizable content, reusable composites and independent workflow versions.
- Real API connections and provider model discovery; an optional downloaded local model. Supply your own model credentials.
- Local files, PDF/Office extraction, image OCR, searchable knowledge, references and local exports.
- Documents, tables, charts, images, coordinate maps, public web research and a consolidated final-answer panel.
- Skills, agents and expert nodes, including 40 pinned, licensed public method adapters. They provide instructions through installed executors; they do not execute arbitrary upstream scripts.
- Persistent execution events, pause/continue, single-node execution, cancellation, result reuse and recovery. The top button is green to start and red to stop.
- Independent local spaces and optional self-hosted teams with invitations and explicitly reviewed workflow-template sharing.

## Run the desktop app

The desktop runtime currently targets **Windows 11 x64**. Building requires Node.js 22+, Python 3.12, uv, a Rust MSVC toolchain, Microsoft C++ Build Tools and the Windows SDK. Installed application users do not need Python or Node.

```powershell
git clone https://github.com/LiMingXin111/Combine.git
cd Combine
powershell -NoProfile -ExecutionPolicy Bypass -File desktop/scripts/build.ps1 -Python312 'C:/path/to/Python312/python.exe'
```

The script installs locked dependencies, bundles Python, checks the local runtime and AppContainer boundary, then builds a Windows installer under `desktop/src-tauri/target/release/bundle/nsis/`. Use `-NoBundle` for an executable-only build. See [desktop setup](desktop/README.md).

After preparing the runtime, double-click `Start-Combine-Dev.cmd` for the development app. UI edits reload through Vite; Python changes publish immutable revisions for new requests. `Stop-Combine-Dev.cmd` stops the managed development session.

Open **Settings → AI connections** to add your own API key, select a provider/model and test the connection. No credentials are bundled. A model connection test and API workflow execution can incur charges from your provider. Local workflows without network nodes run offline after their required resources are prepared.

## Optional account and team service

The desktop app works without an account. To create or join teams on this computer:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-lock.txt
./Start-Combine-Team-Server.cmd
```

The service listens at `http://127.0.0.1:8765`. In the desktop space selector, choose **Create team** or **Join team**, use that server URL and register/sign in. For different computers, host the service behind HTTPS. See [team spaces](docs/TEAM_SPACES.md) and [self-hosting](deploy/README.md).

The repository also contains the separate browser/server compatibility application. `run.bat` or `./run.sh` starts it on loopback. Without real model settings it can use a clearly identified deterministic demo; this is not a real model answer. Its server-side data model differs from the desktop's private local workspace.

## Privacy and repository contents

Private desktop attachments, knowledge, local references and derived material stay on the device. Only public prompts you choose to send use a configured API. Team sharing uploads a reviewed, sanitized workflow definition; downloaded copies are independent and never auto-run.

This repository contains application source, dependency locks, automated synthetic contract tests, build/start scripts and required public third-party assets/licenses. It does not include API keys, user workspaces/databases, real conversations, screenshots, private documents, model weights, build tools or generated installers. Downloaded model/runtime resources have their own licenses.

## Checks

```powershell
cd desktop
npm ci
npm run build
python -m unittest discover -s tests -p 'test_*.py' -v
```

For backend regression, install the root locked requirements and run `python scripts/check.py` from the repository root. The automated tests use temporary data and intercepted model transports; they are not model-quality benchmarks. Native filesystem isolation and credential protection require Windows.

## Status and license

Combine is a development preview. Windows is the validated desktop target; macOS/Linux desktop support, arbitrary plugin execution, real-time multi-user editing, every Office variant and formal updater/rollback are not complete. Complex document layouts and CPU-model performance vary by file and device.

Combine's original code is [MIT licensed](LICENSE). Vendored libraries and public capability references retain their original terms and notices; see [third-party notices](THIRD_PARTY_NOTICES.md).
