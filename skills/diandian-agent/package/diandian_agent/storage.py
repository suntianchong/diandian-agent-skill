import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock


class Store:
    """One process, serialized writes. Explicit durable checkpoints before HTTP."""

    def __init__(self, path: str):
        self.lock = RLock()
        if path != ":memory:":
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(target, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            os.chmod(target, 0o600)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA busy_timeout=5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise RuntimeError("unsupported database schema version")
        with self.transaction():
            self.db.execute("""CREATE TABLE IF NOT EXISTS records (
                category TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(category,id))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                owner INTEGER NOT NULL, project INTEGER NOT NULL, payload TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                event TEXT NOT NULL REFERENCES events(id), connection TEXT NOT NULL,
                acked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(event,connection))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS human_notices (
                event TEXT NOT NULL REFERENCES events(id), owner INTEGER NOT NULL,
                acked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(event,owner))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS changes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                project INTEGER NOT NULL, payload TEXT NOT NULL)""")
            self.db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get(self, category, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM records WHERE category=? AND id=?",
                                  (category, str(key))).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, category, key, value):
        with self.lock:
            self.db.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(category,id) "
                            "DO UPDATE SET value=excluded.value",
                            (category, str(key), json.dumps(value, sort_keys=True)))

    def all(self, category):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(
                "SELECT value FROM records WHERE category=?", (category,))]

    def close(self):
        self.db.close()
