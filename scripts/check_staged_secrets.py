"""Review staged paths and common secret shapes without printing secret values.

This is a local guard, not proof that every possible secret has been detected.
"""
from pathlib import PurePosixPath
from zipfile import ZipFile, BadZipFile
import io
import re
import subprocess
import sys

patterns=[
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\bsk-[A-Za-z0-9_-]{30,}\b'),
    re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    re.compile(rb'\bAKIA[0-9A-Z]{16}\b'),
]
raw=subprocess.check_output(['git','diff','--cached','--name-only','--diff-filter=ACMR','-z'])
paths=[p for p in raw.decode('utf-8').split('\0') if p]
blocked=[]
for path in paths:
    name=PurePosixPath(path);parts=name.parts
    forbidden=(parts[0] in {'data','data_test','uploads','backups','workspaces','outputs','.aws','.railway','.venv','node_modules'}
        or (name.name.startswith('.env') and not name.name.endswith('.example'))
        or name.suffix.lower() in {'.key','.pem','.p12','.pfx','.db','.sqlite','.sqlite3'}
        or 'bootstrap' in name.name.lower() and name.suffix=='.json')
    if forbidden:
        blocked.append((path,'private or runtime path'));continue
    content=subprocess.check_output(['git','show',':'+path])
    candidates=[content]
    if name.suffix.lower()=='.docx':
        try:
            with ZipFile(io.BytesIO(content)) as z:candidates=[z.read(n) for n in z.namelist() if n.endswith('.xml')]
        except BadZipFile:blocked.append((path,'invalid Word archive'));continue
    if any(rx.search(data) for rx in patterns for data in candidates):blocked.append((path,'secret shaped value'))
if blocked:
    for path,reason in blocked:print('REVIEW REQUIRED:',path,'('+reason+')')
    raise SystemExit(1)
print('Staged guard passed for',len(paths),'files. Secret values were not printed.')
