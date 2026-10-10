import importlib.util
import os
from pathlib import Path
import sqlite3
import subprocess
import pytest
from contextlib import closing

@pytest.fixture
def state(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'runner_state.py'
    spec = importlib.util.spec_from_file_location('runner_state', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    remote, repo = tmp_path/'remote.git', tmp_path/'repo'
    subprocess.run(['git','init','--bare',str(remote)], check=True,capture_output=True)
    repo.mkdir()
    subprocess.run(['git','init',str(repo)], check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'remote','add','origin',str(remote)],check=True,capture_output=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv('STATE_ENCRYPTION_KEY', 'a-random-test-key-at-least-32-characters-long')
    monkeypatch.setenv('DATABASE_PATH', 'data/test.sqlite3')
    return module

def test_encrypted_roundtrip(state):
    state.restore(initialize=True)
    path = Path(os.environ['DATABASE_PATH'])
    with closing(sqlite3.connect(path)) as db:
        with db:
            db.execute('CREATE TABLE test(value TEXT)')
            db.execute("INSERT INTO test VALUES('private-state-value')")
    state.save()
    encrypted = state.git('ls-remote','--heads','origin','monitor-state')
    assert encrypted
    path.unlink()
    state.restore()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT value FROM test').fetchone()[0] == 'private-state-value'
    assert b'private-state-value' not in state.git('show','FETCH_HEAD:state.enc')

def test_missing_state_stops(state):
    with pytest.raises(RuntimeError, match='No saved state'):
        state.restore()
