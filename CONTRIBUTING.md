# Contributing

Use a short branch for a focused behavior change. Include the user-visible result, relevant checks and any migration considerations in the pull request. Do not report mocked model responses as real model evaluation.

Run the locked frontend build and relevant Python contract suites. AppContainer/DPAPI checks require Windows and the prepared runtime. Keep private data, API keys, model weights, generated binaries, screenshots and real conversation logs out of commits. `python scripts/check_staged_secrets.py` checks staged paths and common credential shapes; review changes as well.

Preserve third-party source checksums, licenses and notices. Add runtime capabilities through registered node definitions and explicit permission/data-flow boundaries. Public method instructions do not grant arbitrary code execution or access to private material.
