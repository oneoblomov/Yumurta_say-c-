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


def test_set_daily_count_overwrites_summary(tmp_path):
    db_path = tmp_path / "manual_import.db"
    db = Database(str(db_path))

    db.set_daily_count("2026-04-02", 123)
    db.set_daily_count("2026-04-02", 321)

    row = db.get_daily_summary("2026-04-02")
    assert row is not None
    assert row["total_count"] == 321
