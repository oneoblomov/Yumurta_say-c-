from .database import Database


def test_obsolete_test_settings_are_removed(tmp_path):
    db_path = tmp_path / "legacy.db"
    db = Database(str(db_path))

    db.conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, category, description) VALUES (?, ?, ?, ?)",
        ("test_mode_enabled", "1", "test", "legacy"),
    )
    db.conn.commit()

    db._migrate_schema()

    cur = db.conn.execute("SELECT value FROM settings WHERE key = ?", ("test_mode_enabled",))
    assert cur.fetchone() is None
