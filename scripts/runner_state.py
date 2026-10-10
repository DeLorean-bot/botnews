"""Authenticated encryption of runner state in a dedicated Git branch.

Uses no artifacts/caches as a durable database, and fails closed if state disappears.
The monitor-state branch contains only encrypted state, never bot credentials.
"""
import argparse
import base64
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from cryptography.fernet import Fernet

BRANCH = 'monitor-state'

def git(*args, input=None):
    result = subprocess.run(['git', *args], input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('Git state operation failed: ' + args[0])
    return result.stdout

def cipher():
    password = os.environ.get('STATE_ENCRYPTION_KEY', '')
    if len(password) < 32:
        raise ValueError('STATE_ENCRYPTION_KEY must be a random secret of at least 32 characters')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(password.encode()).digest()))

def restore(initialize=False):
    crypt = cipher()
    refs = git('ls-remote', '--heads', 'origin', BRANCH).strip()
    target = Path(os.getenv('DATABASE_PATH','data/news.sqlite3'))
    target.parent.mkdir(parents=True, exist_ok=True)
    if not refs:
        if not initialize:
            raise RuntimeError('No saved state. Run manually with initialize=true for the first run only.')
        print('Initializing new state; old publications will be baselined.')
        return
    git('fetch', 'origin', BRANCH)
    encoded = git('show', 'FETCH_HEAD:state.enc')
    raw = crypt.decrypt(encoded)
    target.write_bytes(raw)
    with closing(sqlite3.connect(target)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Restored database is corrupt')
    print('Encrypted state restored.')

def save():
    target = Path(os.getenv('DATABASE_PATH','data/news.sqlite3'))
    if not target.exists():
        raise RuntimeError('Database missing; refusing to overwrite saved state')
    crypt = cipher()
    with tempfile.TemporaryDirectory() as directory:
        backup = Path(directory) / 'backup.sqlite3'
        with closing(sqlite3.connect(target)) as source, closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
        encrypted = Path(directory) / 'state.enc'
        encrypted.write_bytes(crypt.encrypt(backup.read_bytes()))
        blob = git('hash-object','-w',str(encrypted)).decode().strip()
    tree = git('mktree', input=f'100644 blob {blob}\tstate.enc\n'.encode()).decode().strip()
    # Root snapshot replaces the previous encrypted snapshot; no unbounded DB history.
    git('config','user.name','News monitor state')
    git('config','user.email','monitor@users.noreply.github.com')
    commit = git('commit-tree',tree,'-m','Save encrypted monitor state').decode().strip()
    git('push','--force','origin',f'{commit}:refs/heads/{BRANCH}')
    print('Encrypted state saved.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['restore','save'])
    parser.add_argument('--initialize', action='store_true')
    args = parser.parse_args()
    if args.action == 'restore':
        restore(args.initialize)
    else:
        save()
