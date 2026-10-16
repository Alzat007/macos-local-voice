#!/usr/bin/env python3
"""Read-only check of Git-tracked release files. Never prints matched secrets."""
import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DIRS = {'.venv', '.local-voice-runtime', 'models', 'outputs', 'private', 'data', 'logs', '__pycache__'}
PRIVATE_FILES = {'voice_profile.json', 'voice-ready.json', 'model-download.json', 'sample-check.json', '.env', '.DS_Store'}
PRIVATE_SUFFIXES = {'.wav','.mp3','.m4a','.flac','.aiff','.mp4','.mov','.safetensors','.bin','.log','.zip','.skill','.pyc'}
PATTERNS = [
    ('personal home path', re.compile(r'/' + r'Users/[A-Za-z0-9_.-]+(?:/|\\)')),
    ('Windows personal home path', re.compile(r'[A-Za-z]:\\' + r'Users\\[A-Za-z0-9_.-]+')),
    ('GitHub credential', re.compile(r'(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}')),
    ('private key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
]

def release_files(root):
    result = subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,text=True)
    if result.returncode == 0 and Path(result.stdout.strip()).resolve() == root.resolve():
        tracked = subprocess.check_output(['git','-C',str(root),'ls-files','-z'])
        return [Path(item.decode('utf-8')) for item in tracked.split(b'\0') if item]
    return [path.relative_to(root) for path in root.rglob('*')
            if path.is_file() and '.git' not in path.relative_to(root).parts
            and '__pycache__' not in path.relative_to(root).parts]

def inspect_file(root, relative):
    problems = []
    target = root / relative
    if relative.is_absolute() or '..' in relative.parts:
        return ['invalid release path']
    if any(part in PRIVATE_DIRS for part in relative.parts) or relative.name in PRIVATE_FILES or relative.name.startswith('.env.'):
        problems.append('private runtime/data file')
    if relative.suffix.lower() in PRIVATE_SUFFIXES or relative.name.startswith('ref_voice.'):
        problems.append('media/model/runtime artifact')
    if target.is_symlink():
        return problems + ['symbolic link is not a portable source file']
    if not target.is_file():
        return problems + ['tracked file is missing']
    if target.stat().st_size > 1_000_000:
        return problems + ['unexpected large release file']
    raw = target.read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return problems + ['non-text file requires explicit review']
    if b'\0' in raw:
        problems.append('binary content')
    for label, expression in PATTERNS:
        if expression.search(text):
            problems.append(label)
    return problems

def audit(root):
    files = release_files(root)
    issues = [{'file':path.as_posix(),'issues':problems} for path in files
              if (problems := inspect_file(root,path))]
    top = subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,text=True)
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
        # Working-tree content must match the selected index, otherwise a cleaned
        # working file could conceal sensitive older content still staged for commit.
        changed = subprocess.run(['git','-C',str(root),'diff','--quiet'],capture_output=True)
        if changed.returncode:
            issues.append({'file':'.','issues':['tracked working files differ from the index; review and restage before release audit']})
    if not files:
        issues.append({'file':'.','issues':['no release files selected']})
    return {'status':'pass' if not issues else 'fail','file_count':len(files),'issues':issues}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    args = parser.parse_args()
    report = audit(args.root.resolve())
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='pass' else 1

if __name__=='__main__':
    raise SystemExit(main())
