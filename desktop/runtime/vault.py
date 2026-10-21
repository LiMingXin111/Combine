"""Per-Windows-user DPAPI secrets, outside the AppContainer workspace ACL."""
from pathlib import Path
import ctypes
from ctypes import wintypes as W
import hashlib
import os
import re


def location(root):
    root = Path(root).resolve()
    return root.parent / ('.combine-vault-' + hashlib.sha256(str(root).encode()).hexdigest()[:16])


def _crypt(data, decrypt=False):
    if os.name != 'nt' or os.environ.get('COMBINE_SANDBOX') == 'appcontainer':
        raise RuntimeError('API credentials are available only to the trusted Windows broker.')
    class Blob(ctypes.Structure):
        _fields_ = [('size', W.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, W.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), W.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, W.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), 'Combine API credential', None, None, None, 1, ctypes.byref(output))
    if not ok: raise RuntimeError('Windows credential protection failed. The key was not saved or exposed.')
    try: return ctypes.string_at(output.data, output.size)
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(output.data)


def path(root, key):
    if not re.fullmatch(r'provider_[a-f0-9]{16}', key): raise ValueError('Invalid provider identifier.')
    return location(root) / (key + '.bin')


def save(root, key, secret):
    target = path(root, key); target.parent.mkdir(parents=True, exist_ok=True)
    data = _crypt(secret.encode('utf-8'))
    temp = target.with_suffix('.tmp'); temp.write_bytes(data); temp.replace(target)


def read(root, key):
    target = path(root, key)
    if not target.is_file(): raise ValueError('Add an API key in Settings → AI connections.')
    return _crypt(target.read_bytes(), decrypt=True).decode('utf-8')


def delete(root, key): path(root, key).unlink(missing_ok=True)
