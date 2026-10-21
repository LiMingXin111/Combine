# Combine desktop

React + TypeScript + Vite + React Flow, hosted in Tauri 2 with a bundled Python 3.12 runtime. Local workers use Windows AppContainer without internet/loopback capabilities; a trusted broker handles explicitly allowed public API and resource requests. Keys use Windows DPAPI outside workspace databases.

## Build prerequisites

- Windows 11 x64, Node.js 22+ and npm.
- Python 3.12 and uv (`uv --version`). Pass the absolute Python executable to the build script.
- Rust MSVC, Visual Studio C++ Build Tools and Windows SDK. `cargo --version` must work.
- WebView2. The installer is configured to include its offline installer.

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File desktop/scripts/build.ps1 -Python312 'C:/path/to/Python312/python.exe'
```

The build prepares Python and locked OCR/PDF dependencies, runs contract tests and the AppContainer probe, obtains fixed-checksum NSIS tools, and creates an unsigned installer. Compilation/downloads require internet. Dependencies are pinned in the npm, Python and Cargo locks. Do not commit generated resources, weights or workspaces.

## Development

After the runtime has been built, run `Start-Combine-Dev.cmd` at the repository root. Use `Start-Combine-Dev.cmd -Browser` for the optional loopback browser preview. `-Check` verifies prerequisites; `Stop-Combine-Dev.cmd` stops only the managed session.

Frontend updates use Vite. Python edits are syntax-checked and published as immutable revisions; active requests keep their revision, and invalid edits preserve the last usable version. Rust/configuration changes require recompilation. Dependencies are prepared again when their locks change. Press Ctrl+Shift+I for WebView2 DevTools.

Development workspaces are under `.dev/native-workspace` or `.dev/browser-workspace`, with device-local encrypted credential storage. None is part of the source repository. Production workspaces use the application data directory.

## Use

- Add a real provider connection in Settings, discover its models and select one. The app never supplies someone else's key or a pretend API response.
- Use the permanent right conversation to create/edit workflows. The left side selects spaces and conversations; Add node opens the capability library.
- Apply manual changes before executing. The top green Run workflow becomes red Stop workflow while the run is active. Stopping retains completed outputs and can wait for a current external call to return.
- Local files/knowledge and their derived content cannot be sent through API nodes. Use a prepared Local AI node for private analysis.
- Prepare local runtime/model resources explicitly before offline use. Missing resources wait or fail; there is no silent cloud fallback.
- Team accounts are optional. Share only a reviewed template through the space selector/Share to team; downloaders add their own inputs and AI connection. See [team usage](../docs/TEAM_SPACES.md).

## File support and limits

Local parsers support text/Markdown/JSON/CSV/TSV/static HTML, DOCX/DOCM, XLSX/XLSM, PPTX/PPTM, text/scanned PDF and PNG/JPEG/WebP/BMP/TIFF/GIF. Macros/scripts are never executed. Spreadsheet formulas use saved values and disclose missing caches. Embedded Office images may need separate import. Encrypted PDFs require a locally unlocked copy. Legacy Office and ODF are not silently converted.

Inputs are limited to 64 MB; decompressed Office XML to 256 MB. Workflow composites support 12 nested levels. The portable team template format has tighter limits: 1 MB, 200 total nodes and five nested levels. Do not treat these limits as a guarantee of visual-layout fidelity or model memory/performance.

## Tests

```powershell
cd desktop
npm ci
npm run build
python -m unittest discover -s tests -p 'test_*.py' -v
./src-tauri/resources/python/python.exe -I tests/sandbox_probe.py
```

Tests construct synthetic data; no saved conversation or personal model key is needed. The sandbox probe must use the prepared runtime on Windows. Local-model inference requires separately downloaded resources and is not replaced with a fake success.
