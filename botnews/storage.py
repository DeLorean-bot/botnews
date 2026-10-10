import json
import sqlite3
from contextlib import closing
from pathlib import Path
from .models import canonical, now

class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, url TEXT UNIQUE NOT NULL,
          reliability REAL NOT NULL, enabled INTEGER DEFAULT 1, initialized INTEGER DEFAULT 0,
          last_ok TEXT, error TEXT, failures INTEGER DEFAULT 0, next_poll REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS posts(
          id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES sources(id), url TEXT UNIQUE,
          title TEXT, body TEXT, published TEXT, discovered TEXT, fingerprint TEXT, links TEXT,
          status TEXT DEFAULT 'pending', analysis TEXT, score INTEGER DEFAULT 0,
          duplicate_of INTEGER, attempts INTEGER DEFAULT 0, next_retry REAL DEFAULT 0, error TEXT);
        CREATE INDEX IF NOT EXISTS post_hash ON posts(fingerprint);
        CREATE INDEX IF NOT EXISTS post_status ON posts(status,next_retry);
        CREATE TABLE IF NOT EXISTS deliveries(
          post_id INTEGER, channel_id INTEGER, message_id INTEGER, state TEXT, created TEXT,
          PRIMARY KEY(post_id,channel_id));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS feedback(
          post_id INTEGER, admin_id INTEGER, verdict TEXT, reason TEXT, category TEXT, created TEXT,
          PRIMARY KEY(post_id,admin_id));
        CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS suggestions(url TEXT PRIMARY KEY,source_id INTEGER,created TEXT);
        ''')

    def execute(self, sql, args=()):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def seed(self, path):
        for s in json.loads(Path(path).read_text(encoding="utf-8")):
            self.add_source(**s)

    def add_source(self, name, kind, url, reliability=0.5):
        from .sources import validate_source
        validate_source(kind, url)
        self.execute("INSERT OR IGNORE INTO sources(name,kind,url,reliability) VALUES(?,?,?,?)",
                     (name, kind, canonical(url), max(0, min(1, reliability))))

    def ingest(self, post, baseline=False):
        # Keep cross-source copies as evidence; suppress them later during analysis.
        cur = self.execute('''INSERT OR IGNORE INTO posts(source_id,url,title,body,published,discovered,fingerprint,links,status)
                           VALUES(?,?,?,?,?,?,?,?,?)''',
                           (post.source_id, canonical(post.url), post.title, post.text, post.published,
                            now(), post.fingerprint, json.dumps(post.links), "baseline" if baseline else "pending"))
        return cur.lastrowid if cur.rowcount else None

    def setting(self, key, default=None):
        rows = self.rows("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    def backup(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as target:
            self.db.backup(target)
