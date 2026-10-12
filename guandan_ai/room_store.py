"""SQLite checkpoint store; participant credentials are stored only as hashes."""
import json
from pathlib import Path
import sqlite3
import threading


class RoomStore:
    def __init__(self, path=None):
        self.lock = threading.RLock()
        self.db = None
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS checkpoints (kind TEXT, id TEXT, payload TEXT, PRIMARY KEY(kind,id))")
            self.db.commit()

    def put(self, kind, key, payload):
        if self.db:
            with self.lock, self.db:
                self.db.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)", (kind, key, json.dumps(payload, ensure_ascii=False)))

    def all(self, kind):
        if not self.db:
            return []
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM checkpoints WHERE kind=?", (kind,))]

    def close(self):
        if self.db:
            with self.lock:
                self.db.close()
                self.db = None
