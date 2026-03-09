import sqlite3
import unittest
from pathlib import Path

from .database import Database


def test_migration_updates_old_test_expected(tmp_path):
    # create a fresh database and then simulate an old installation
    db_path = tmp_path / "legacy.db"
    # instantiate to create schema and defaults
    db = Database(str(db_path))

    # force legacy value
    db.conn.execute("UPDATE settings SET value = ? WHERE key = ?", ("30", "test_expected_batch"))
    db.conn.commit()

    # run default initialization again which includes migration logic
    db._init_default_settings()

    cur = db.conn.execute("SELECT value FROM settings WHERE key = ?", ("test_expected_batch",))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "55", "migration should bump old 30 to 55"
