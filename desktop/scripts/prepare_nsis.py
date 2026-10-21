"""Prepare Tauri's official fixed NSIS tools without a cross-volume rename.

Hashes match Tauri's upstream nsis/mod.rs. Downloads contain public tools only.
"""
from pathlib import Path
import hashlib
import io
import shutil
import urllib.request
import zipfile

BASE = Path(__file__).resolve().parents[1]
TOOLS = BASE / 'src-tauri/target/.tauri'
ZIP_URL = 'https://github.com/tauri-apps/binary-releases/releases/download/nsis-3.13/nsis-3.13.zip'
PLUGIN_URL = 'https://github.com/tauri-apps/nsis-tauri-utils/releases/download/nsis_tauri_utils-v0.5.3/nsis_tauri_utils.dll'


def download(url, sha1):
    data = urllib.request.urlopen(url, timeout=60).read()
    if hashlib.sha1(data).hexdigest() != sha1.lower(): raise ValueError('Official NSIS tool checksum mismatch.')
    return data


def prepare():
    destination = TOOLS / 'NSIS'
    exe = destination / 'NSIS.exe'
    if not exe.exists() or hashlib.sha1(exe.read_bytes()).hexdigest() != '6d45d4e8ccf18fe1fdac9164058f356102a2ecf2':
        data = download(ZIP_URL, 'DB14F8AF2EA346B786C6AE19343F61AAF5E09B8E')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                target = (TOOLS / info.filename).resolve()
                if not target.is_relative_to(TOOLS.resolve()): raise ValueError('Invalid NSIS archive path.')
            archive.extractall(TOOLS)
        shutil.copytree(TOOLS / 'nsis-3.13', destination, dirs_exist_ok=True)
    plugin = destination / 'Plugins/x86-unicode/additional/nsis_tauri_utils.dll'
    if not plugin.exists() or hashlib.sha1(plugin.read_bytes()).hexdigest() != '75197fee3c6a814fe035788d1c34ead39349b860':
        plugin.parent.mkdir(parents=True, exist_ok=True)
        plugin.write_bytes(download(PLUGIN_URL, '75197FEE3C6A814FE035788D1C34EAD39349B860'))
    print('Official NSIS toolchain verified.')


if __name__ == '__main__': prepare()
