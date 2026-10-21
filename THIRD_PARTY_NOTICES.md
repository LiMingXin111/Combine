# Third-party notices

The root MIT license applies to Combine's original code. Other projects retain their own licenses.

- Vendored Leaflet 1.9.4: BSD-2-Clause; full license in `frontend/vendor/leaflet/LICENSE`. Existing source headers and map imagery attribution remain intact.
- Vendored QR Code Generator for JavaScript: MIT, Copyright 2009 Kazuhiko Arase; full license in `frontend/vendor/qrcode/LICENSE`.
- Bundled public method references: 28 entries from `msitarzewski/agency-agents` at `4e3ac3ca6604c75113f52c695dbddcc7af928b46`, and 12 from `github/awesome-copilot` at `613efc8c21d9b3ce642dff361d17006cd9967e28`. Each retains its source bytes, commit, checksum and applicable MIT/Agent Architecture notices in `desktop/runtime/community/`.
- npm, Python and Rust dependencies are identified by their lockfiles and retain their package licenses. Build-generated runtimes/dependencies are not vendored in this source repository.
- Optional downloaded llama.cpp and model/vision components retain their upstream licenses; installing them is explicit. Model weights and third-party font downloads are not part of this repository.
