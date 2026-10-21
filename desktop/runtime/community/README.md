# Bundled open-source capability references

This directory contains 40 commit-pinned instruction references, their complete license texts, and a checksum manifest. It is distributed with the local runtime; browsing and resolving a reference does not download anything or execute upstream code.

| Upstream repository | Pinned commit | Entries | License |
|---|---|---:|---|
| [msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents) | `4e3ac3ca6604c75113f52c695dbddcc7af928b46` | 28 | MIT |
| [github/awesome-copilot](https://github.com/github/awesome-copilot) | `613efc8c21d9b3ce642dff361d17006cd9967e28` | 12 | MIT, including the separately retained Agent Architecture license |

Original reference bytes are stored under `files/`. Copyright notices and permissions are retained under `licenses/`. `catalog.json` records the upstream repository, exact file path, commit, SHA-256, applicable license file and checksum for each entry. The source files remain unmodified.

Combine's adaptation is implemented in `../community.py`: the complete upstream document is a visible public reference input to an explicit model workflow. A short executor instruction limits the role to supplied evidence and actual installed capabilities. Tool, command, external service and file operations described by an upstream document are not automatically executed. Agents use explicit plan, production and review stages. Model selection, local privacy propagation and runtime resource checks follow the normal node engine.

Rebuild from the pinned upstream revisions with the packaged Python:

```powershell
.\src-tauri\resources\python\python.exe -I scripts/sync_community_catalog.py
```

Changing the source selection requires reviewing applicable nested and per-file license terms. The rebuild refuses unexpected license signatures and oversized references. New arbitrary repositories are previewed through the separate public GitHub import broker and installed as guidance only after a user selects entries. These are instruction adapters, not a package manager for arbitrary executable dependencies.
