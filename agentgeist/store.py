import json
import sqlite3
from pathlib import Path


class Store:
    """Durable tick inbox and submission outbox. Never advances the feed on retry."""

    def __init__(self, path="state/agentgeist.sqlite3"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS incidents (
            team TEXT, tick_id INTEGER, tick TEXT NOT NULL, decision TEXT,
            judgment TEXT, escalation TEXT, spent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(team,tick_id))""")
        self.db.commit()

    def save_tick(self, team, tick):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO incidents(team,tick_id,tick) VALUES(?,?,?)",
                            (team, tick.tick_id, tick.model_dump_json()))

    def update(self, team, tick_id, column, value):
        if column not in {"decision", "judgment", "escalation"}:
            raise ValueError("Invalid outbox column")
        with self.db:
            self.db.execute(f"UPDATE incidents SET {column}=? WHERE team=? AND tick_id=?",
                            (json.dumps(value), team, tick_id))

    def pending(self, team):
        self.db.row_factory = sqlite3.Row
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM incidents WHERE team=? AND (judgment IS NULL OR escalation IS NULL "
            "OR json_extract(escalation,'$.status') IN ('pending','unknown')) ORDER BY tick_id", (team,))]

    def spent(self, team, tick_id):
        return self.db.execute("SELECT spent FROM incidents WHERE team=? AND tick_id=?", (team, tick_id)).fetchone()[0]

    def reserve(self, team, tick_id, units):
        with self.db:
            self.db.execute("UPDATE incidents SET spent=spent+? WHERE team=? AND tick_id=?", (units, team, tick_id))
