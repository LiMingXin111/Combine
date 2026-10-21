"""Windows AppContainer launcher with no network capabilities.

Only generated runtime resources and the selected local workspace receive ACLs.
No global firewall changes, loopback exemptions, or administrator rights.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path
from ctypes import wintypes as W

PROFILE = 'Combine.Desktop.LocalRuntime.v1'


def launch(python: Path, entry: Path, root: Path, request: Path, response: Path) -> dict:
    # Profile/ACL setup and native process creation must not race across brokers.
    # Release before waiting so pause, cancellation and snapshots remain usable.
    from .leases import lease
    with lease(root / 'ipc' / 'launch.lock', wait=True):
        process, kernel = _spawn(python, entry, root, request, response)
    try:
        kernel.WaitForSingleObject(process.hProcess, 0xffffffff)
        code = W.DWORD()
        kernel.GetExitCodeProcess(process.hProcess, ctypes.byref(code))
        if code.value != 0:
            raise RuntimeError(f'Isolated worker failed (0x{code.value:08x}). No incomplete response was accepted.')
        if not response.exists():
            raise RuntimeError(f'Isolated worker exited without a response (0x{code.value:08x}).')
        return {'mode': 'appcontainer', 'network_capabilities': [], 'exit_code': code.value}
    finally:
        if process.hThread: kernel.CloseHandle(process.hThread)
        if process.hProcess: kernel.CloseHandle(process.hProcess)


def _spawn(python: Path, entry: Path, root: Path, request: Path, response: Path):
    if os.name != 'nt':
        raise RuntimeError('Windows AppContainer is required. No unrestricted fallback is permitted.')
    if python.resolve().parent.name != 'python' or python.resolve().parent.parent.name != 'resources' or entry.resolve().parent.parent != python.resolve().parent.parent:
        raise ValueError('Only the packaged desktop runtime may receive AppContainer filesystem grants.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    userenv = ctypes.WinDLL('userenv', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    sid = ctypes.c_void_p()
    userenv.CreateAppContainerProfile.argtypes = [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, ctypes.c_void_p, W.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    result = userenv.CreateAppContainerProfile(PROFILE, 'Combine local runtime', 'Local processing without network access', None, 0, ctypes.byref(sid))
    if result != 0:
        result = userenv.DeriveAppContainerSidFromAppContainerName(PROFILE, ctypes.byref(sid))
    if result != 0:
        raise OSError(f'Cannot create AppContainer: 0x{result & 0xffffffff:08x}')
    sid_text = W.LPWSTR()
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = W.BOOL
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
        raise ctypes.WinError(ctypes.get_last_error())
    profile_sid = sid_text.value
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))

    # ACL inheritance is set on narrowly scoped generated directories only.
    for directory, permission in [(python.parent, 'RX'), (entry.parent, 'RX'), (root, 'M')]:
        marker = directory / ('.appcontainer-' + permission)
        if not marker.exists() or marker.read_text(encoding='utf-8') != str(directory.resolve()):
            grant = '*' + profile_sid + ':(OI)(CI)' + permission
            # Wheels may preserve non-inheriting ACLs from pip's staging folder.
            # Apply the same narrow permission to existing bundled children too.
            p = subprocess.run(['icacls', str(directory), '/grant', grant, '/T', '/Q'], capture_output=True, creationflags=0x08000000)
            if p.returncode:
                raise RuntimeError('Cannot grant scoped AppContainer access: ' + str(directory))
            marker.write_text(str(directory.resolve()), encoding='utf-8')

    class Caps(ctypes.Structure):
        _fields_ = [('AppContainerSid', ctypes.c_void_p), ('Capabilities', ctypes.c_void_p), ('CapabilityCount', W.DWORD), ('Reserved', W.DWORD)]

    class Startup(ctypes.Structure):
        _fields_ = [('cb', W.DWORD), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR), ('lpTitle', W.LPWSTR),
                    ('dwX', W.DWORD), ('dwY', W.DWORD), ('dwXSize', W.DWORD), ('dwYSize', W.DWORD),
                    ('dwXCountChars', W.DWORD), ('dwYCountChars', W.DWORD), ('dwFillAttribute', W.DWORD),
                    ('dwFlags', W.DWORD), ('wShowWindow', W.WORD), ('cbReserved2', W.WORD),
                    ('lpReserved2', ctypes.c_void_p), ('hStdInput', W.HANDLE), ('hStdOutput', W.HANDLE), ('hStdError', W.HANDLE)]

    class StartupEx(ctypes.Structure):
        _fields_ = [('StartupInfo', Startup), ('lpAttributeList', ctypes.c_void_p)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [('hProcess', W.HANDLE), ('hThread', W.HANDLE), ('dwProcessId', W.DWORD), ('dwThreadId', W.DWORD)]

    size = ctypes.c_size_t()
    kernel.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    kernel.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, W.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel.CreateProcessW.argtypes = [W.LPCWSTR, W.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, W.BOOL, W.DWORD, ctypes.c_void_p, W.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ProcessInfo)]
    kernel.CreateProcessW.restype = W.BOOL
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    kernel.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attributes = ctypes.create_string_buffer(size.value)
    if not kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    caps = Caps(sid, None, 0, 0)
    process = ProcessInfo()
    try:
        if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x00020009, ctypes.byref(caps), ctypes.sizeof(caps), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        startup = StartupEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        temp = root / 'temp'
        temp.mkdir(exist_ok=True)
        environment = {'SYSTEMROOT': os.environ.get('SYSTEMROOT', r'C:\Windows'),
                       'WINDIR': os.environ.get('WINDIR', r'C:\Windows'), 'TEMP': str(temp), 'TMP': str(temp),
                       'PATH': str(python.parent) + ';' + str(python.parent / 'DLLs'),
                       'COMBINE_SANDBOX': 'appcontainer', 'COMBINE_SANDBOX_SID': profile_sid}
        for key in ['USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'ProgramData', 'ALLUSERSPROFILE', 'SystemDrive', 'OS', 'NUMBER_OF_PROCESSORS']:
            if key in os.environ: environment[key] = os.environ[key]
        env_buffer = ctypes.create_unicode_buffer('\0'.join(k + '=' + v for k, v in sorted(environment.items())) + '\0\0')
        args = [str(python), '-I', str(entry), '--root', str(root), '--request', str(request), '--response', str(response)]
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        flags = 0x00080000 | 0x08000000 | 0x00000400
        if not kernel.CreateProcessW(str(python), command, None, None, False, flags, env_buffer, str(root), ctypes.byref(startup), ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        return process, kernel
    finally:
        kernel.DeleteProcThreadAttributeList(attributes)
        advapi.FreeSid.argtypes = [ctypes.c_void_p]
        advapi.FreeSid(sid)
