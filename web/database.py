"""
database.py - SQLite Veritabanı Katmanı
========================================
Thread-safe SQLite işlemleri. Tüm tablo tanımları,
CRUD operasyonları ve istatistik sorguları.
"""

import sqlite3
import json
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "egg_counter.db"


class Database:
    """Thread-safe SQLite veritabanı."""

    _local = threading.local()

    def __init__(self, db_path: str = None):
        self.db_path = str(db_path or DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate_schema()
        self._init_default_settings()

    # ------------------------------------------------------------------ conn
    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(Database._local, "conn") or Database._local.conn is None:
            Database._local.conn = sqlite3.connect(self.db_path, timeout=10)
            Database._local.conn.row_factory = sqlite3.Row
            Database._local.conn.execute("PRAGMA journal_mode=WAL")
            Database._local.conn.execute("PRAGMA foreign_keys=ON")
        return Database._local.conn

    # ------------------------------------------------------------------ schema
    def _init_schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            ended_at TEXT,
            source TEXT DEFAULT '0',
            total_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running'
                CHECK(status IN ('running','paused','stopped','error')),
            config_json TEXT
        );

        CREATE TABLE IF NOT EXISTS count_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            track_id INTEGER,
            cx INTEGER DEFAULT 0,
            cy INTEGER DEFAULT 0,
            x1 INTEGER DEFAULT 0,
            y1 INTEGER DEFAULT 0,
            x2 INTEGER DEFAULT 0,
            y2 INTEGER DEFAULT 0,
            confidence REAL DEFAULT 0,
            running_total INTEGER DEFAULT 0,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS daily_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            total_count INTEGER DEFAULT 0,
            session_count INTEGER DEFAULT 0,
            first_count_at TEXT,
            last_count_at TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            description TEXT,
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            message TEXT NOT NULL,
            severity TEXT DEFAULT 'info'
                CHECK(severity IN ('info','warning','error','critical')),
            data_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            acknowledged INTEGER DEFAULT 0,
            acknowledged_at TEXT
        );

        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('daily','weekly','monthly')),
            target_count INTEGER NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS app_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL,
            changelog TEXT,
            installed_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            is_active INTEGER DEFAULT 0,
            backup_path TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_events_session
            ON count_events(session_id);
        CREATE INDEX IF NOT EXISTS idx_events_timestamp
            ON count_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_daily_date
            ON daily_summaries(date);
        CREATE INDEX IF NOT EXISTS idx_alerts_created
            ON alerts(created_at);
        CREATE INDEX IF NOT EXISTS idx_alerts_ack
            ON alerts(acknowledged);
        """)
        self.conn.commit()

    def _migrate_schema(self):
        self._ensure_column("app_versions", "package_path", "TEXT")
        self._ensure_column("app_versions", "release_url", "TEXT")
        self._ensure_column("app_versions", "release_published_at", "TEXT")
        self._ensure_column("app_versions", "installed_by", "TEXT DEFAULT 'manual'")

    def _ensure_column(self, table_name: str, column_name: str, column_def: str):
        columns = {
            row[1] for row in self.conn.execute(f"PRAGMA table_info({table_name})")
        }
        if column_name not in columns:
            self.conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}"
            )
            self.conn.commit()

    def _init_default_settings(self):
        defaults = [
            # Camera
            ("camera_source", "0", "camera", "Kamera kaynağı"),
            ("camera_width", "640", "camera", "Kamera genişliği"),
            ("camera_height", "480", "camera", "Kamera yüksekliği"),
            # Detector
            ("model_path", "models/yolo26n_mod/best_openvino_model",
             "detector", "YOLO model yolu"),
            ("conf_threshold", "0.30", "detector", "Güven eşiği"),
            ("iou_threshold", "0.45", "detector", "NMS IoU eşiği"),
            ("imgsz", "480", "detector", "Inference boyutu"),
            # Counter
            ("line_position", "0.5", "counter", "Sayım çizgisi pozisyonu"),
            ("direction", "top_to_bottom", "counter", "Sayım yönü"),
            ("roi_top", "0.25", "counter", "ROI üst sınırı"),
            ("roi_bottom", "0.75", "counter", "ROI alt sınırı"),
            ("post_cross_drop", "0", "counter", "Sayıldıktan sonra bırakma (frame)"),
            # Tracker
            ("tracker_type", "bytetrack", "tracker", "Tracker tipi"),
            ("track_buffer", "90", "tracker", "Track buffer (frame)"),
            ("match_thresh", "0.85", "tracker", "Eşleştirme eşiği"),
            # Preprocessor
            ("enable_clahe", "1", "preprocessor", "CLAHE ön işleme"),
            ("enable_stabilization", "0", "preprocessor", "Titreşim stabilizasyonu"),
            # Pipeline
            ("crop_ud", "0", "pipeline", "Üst-alt kırpma (%)"),
            ("crop_lr", "0", "pipeline", "Sol-sağ kırpma (%)"),
            # Goals
            ("daily_goal", "0", "goals", "Günlük hedef (0=kapalı)"),
            ("weekly_goal", "0", "goals", "Haftalık hedef (0=kapalı)"),
            # Display
            ("language", "tr", "display", "Arayüz dili"),
            ("theme", "light", "display", "Tema"),
            ("stream_quality", "70", "display", "Video akış kalitesi"),
            # Update
            ("update_repo_owner", "oneoblomov", "update", "GitHub sahip hesabı"),
            ("update_repo_name", "Yumurta_say-c-", "update", "GitHub depo adı"),
            ("update_channel", "stable", "update", "Güncelleme kanalı"),
            ("update_include_prerelease", "0", "update", "Ön sürümleri dahil et"),
            ("update_auto_check", "1", "update", "Otomatik güncelleme kontrolü"),
            ("update_auto_install", "0", "update", "Yeni sürümü otomatik kur"),
            ("update_restart_after_install", "1", "update", "Kurulum sonrası servisleri yeniden başlat"),
            ("update_last_check_at", "", "update", "Son güncelleme kontrol zamanı"),
            ("update_last_available_version", "", "update", "Bulunan son sürüm"),
            ("update_last_check_status", "", "update", "Son kontrol sonucu"),
            ("update_last_error", "", "update", "Son güncelleme hatası"),
            ("update_last_notified_version", "", "update", "Bildirim gönderilen son sürüm"),
            ("update_last_installed_version", "", "update", "Kurulan son sürüm"),
            # Test Mode
            ("show_test_page", "1", "test", "Test sayfasını menüde göster"),
            ("test_mode_enabled", "1", "test", "5 sn test penceresi analizi"),
            ("test_expected_batch", "30", "test", "Pencere başına beklenen yumurta"),
            ("test_window_seconds", "5", "test", "Test pencere süresi (sn)"),
        ]
        for key, value, category, desc in defaults:
            self.conn.execute(
                "INSERT OR IGNORE INTO settings (key,value,category,description) "
                "VALUES (?,?,?,?)",
                (key, value, category, desc),
            )
        self.conn.commit()

    # ============================================================ Sessions
    def create_session(self, source: str = "0",
                       config_json: str = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO sessions (source,config_json) VALUES (?,?)",
            (source, config_json),
        )
        self.conn.commit()

        # Increment daily session count
        today = date.today().isoformat()
        self.conn.execute("""
            INSERT INTO daily_summaries (date,total_count,session_count,first_count_at)
            VALUES (?,0,1,datetime('now','localtime'))
            ON CONFLICT(date) DO UPDATE SET session_count=session_count+1
        """, (today,))
        self.conn.commit()
        return cur.lastrowid

    def end_session(self, session_id: int, total_count: int):
        self.conn.execute(
            "UPDATE sessions SET ended_at=datetime('now','localtime'),"
            "total_count=?,status='stopped' WHERE id=?",
            (total_count, session_id),
        )
        self.conn.commit()

    def update_session_count(self, session_id: int, total_count: int):
        self.conn.execute(
            "UPDATE sessions SET total_count=? WHERE id=?",
            (total_count, session_id),
        )
        self.conn.commit()

    def update_session_status(self, session_id: int, status: str):
        self.conn.execute(
            "UPDATE sessions SET status=? WHERE id=?",
            (status, session_id),
        )
        self.conn.commit()

    def get_sessions(self, limit: int = 50, offset: int = 0) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: int) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_session(self, session_id: int):
        self.conn.execute(
            "DELETE FROM count_events WHERE session_id=?", (session_id,)
        )
        self.conn.execute(
            "DELETE FROM sessions WHERE id=?", (session_id,)
        )
        self.conn.commit()

    def get_sessions_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone()[0]

    # ============================================================ Count Events
    def add_count_event(self, session_id: int, event: Dict):
        cx, cy = event.get("center", (0, 0))
        bbox = event.get("bbox", (0, 0, 0, 0))
        ts = event.get("timestamp")
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        elif ts is None:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.conn.execute(
            """INSERT INTO count_events
                (session_id,timestamp,track_id,cx,cy,x1,y1,x2,y2,
                 confidence,running_total)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, ts, event.get("track_id"),
             cx, cy,
             bbox[0], bbox[1], bbox[2], bbox[3],
             event.get("confidence", 0),
             event.get("total", 0)),
        )

        # Update daily summary
        day = ts[:10]
        self.conn.execute("""
            INSERT INTO daily_summaries
                (date,total_count,session_count,first_count_at,last_count_at)
            VALUES (?,1,0,?,?)
            ON CONFLICT(date) DO UPDATE SET
                total_count=total_count+1,
                last_count_at=excluded.last_count_at
        """, (day, ts, ts))
        self.conn.commit()

    def get_events(self, session_id: int = None, date_str: str = None,
                   limit: int = 100, offset: int = 0) -> List[Dict]:
        q = "SELECT * FROM count_events WHERE 1=1"
        p: list = []
        if session_id is not None:
            q += " AND session_id=?"
            p.append(session_id)
        if date_str:
            q += " AND date(timestamp)=?"
            p.append(date_str)
        q += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        p += [limit, offset]
        return [dict(r) for r in self.conn.execute(q, p).fetchall()]

    def get_events_count(self, session_id: int = None,
                         date_str: str = None) -> int:
        q = "SELECT COUNT(*) FROM count_events WHERE 1=1"
        p: list = []
        if session_id is not None:
            q += " AND session_id=?"
            p.append(session_id)
        if date_str:
            q += " AND date(timestamp)=?"
            p.append(date_str)
        return self.conn.execute(q, p).fetchone()[0]

    # ============================================================ Daily
    def get_daily_summaries(self, start_date: str = None,
                            end_date: str = None,
                            limit: int = 365) -> List[Dict]:
        q = "SELECT * FROM daily_summaries WHERE 1=1"
        p: list = []
        if start_date:
            q += " AND date>=?"
            p.append(start_date)
        if end_date:
            q += " AND date<=?"
            p.append(end_date)
        q += " ORDER BY date DESC LIMIT ?"
        p.append(limit)
        return [dict(r) for r in self.conn.execute(q, p).fetchall()]

    def get_daily_summary(self, date_str: str) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM daily_summaries WHERE date=?", (date_str,)
        ).fetchone()
        return dict(row) if row else None

    def delete_daily(self, date_str: str):
        self.conn.execute(
            "DELETE FROM daily_summaries WHERE date=?", (date_str,)
        )
        self.conn.execute(
            "DELETE FROM count_events WHERE date(timestamp)=?", (date_str,)
        )
        self.conn.execute(
            "DELETE FROM sessions WHERE date(started_at)=?", (date_str,)
        )
        self.conn.commit()

    def reset_daily(self, date_str: str):
        """Delete count events for a day but keep the summary with 0."""
        self.conn.execute(
            "DELETE FROM count_events WHERE date(timestamp)=?", (date_str,)
        )
        self.conn.execute(
            "UPDATE daily_summaries SET total_count=0 WHERE date=?",
            (date_str,),
        )
        self.conn.commit()

    # ============================================================ Settings
    def get_setting(self, key: str, default: str = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def get_settings(self, category: str = None) -> Dict[str, str]:
        if category:
            rows = self.conn.execute(
                "SELECT key,value FROM settings WHERE category=?",
                (category,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT key,value FROM settings"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_all_settings_detailed(self) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM settings ORDER BY category,key"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_setting(self, key: str, value: str, category: str = None):
        if category:
            self.conn.execute(
                "INSERT INTO settings (key,value,category,updated_at) "
                "VALUES (?,?,?,datetime('now','localtime')) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value,category=excluded.category,"
                "updated_at=datetime('now','localtime')",
                (key, value, category),
            )
        else:
            self.conn.execute(
                "UPDATE settings SET value=?,"
                "updated_at=datetime('now','localtime') WHERE key=?",
                (value, key),
            )
        self.conn.commit()

    def set_settings_bulk(self, settings: Dict[str, str]):
        for key, value in settings.items():
            self.set_setting(key, str(value))

    # ============================================================ Alerts
    def add_alert(self, alert_type: str, message: str,
                  severity: str = "info", data: dict = None):
        self.conn.execute(
            "INSERT INTO alerts (type,message,severity,data_json) "
            "VALUES (?,?,?,?)",
            (alert_type, message, severity,
             json.dumps(data) if data else None),
        )
        self.conn.commit()

    def get_alerts(self, unack_only: bool = False,
                   limit: int = 50) -> List[Dict]:
        q = "SELECT * FROM alerts"
        if unack_only:
            q += " WHERE acknowledged=0"
        q += " ORDER BY created_at DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(q, (limit,)).fetchall()]

    def acknowledge_alert(self, alert_id: int):
        self.conn.execute(
            "UPDATE alerts SET acknowledged=1,"
            "acknowledged_at=datetime('now','localtime') WHERE id=?",
            (alert_id,),
        )
        self.conn.commit()

    def acknowledge_all_alerts(self):
        self.conn.execute(
            "UPDATE alerts SET acknowledged=1,"
            "acknowledged_at=datetime('now','localtime') "
            "WHERE acknowledged=0"
        )
        self.conn.commit()

    def get_unacknowledged_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE acknowledged=0"
        ).fetchone()[0]

    # ============================================================ Goals
    def set_goal(self, goal_type: str, target_count: int):
        self.conn.execute(
            "UPDATE goals SET active=0 WHERE type=? AND active=1",
            (goal_type,),
        )
        if target_count > 0:
            self.conn.execute(
                "INSERT INTO goals (type,target_count) VALUES (?,?)",
                (goal_type, target_count),
            )
        self.conn.commit()

    def get_active_goals(self) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM goals WHERE active=1 ORDER BY type"
        ).fetchall()
        return [dict(r) for r in rows]

    # ============================================================ Statistics
    def get_today_count(self) -> int:
        row = self.conn.execute(
            "SELECT total_count FROM daily_summaries WHERE date=?",
            (date.today().isoformat(),),
        ).fetchone()
        return row[0] if row else 0

    def get_week_count(self) -> int:
        ws = (date.today() - timedelta(
            days=date.today().weekday())).isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(total_count),0) "
            "FROM daily_summaries WHERE date>=?", (ws,)
        ).fetchone()
        return row[0]

    def get_month_count(self) -> int:
        ms = date.today().replace(day=1).isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(total_count),0) "
            "FROM daily_summaries WHERE date>=?", (ms,)
        ).fetchone()
        return row[0]

    def get_all_time_count(self) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(total_count),0) FROM daily_summaries"
        ).fetchone()
        return row[0]

    def get_daily_average(self, days: int = 30) -> float:
        start = (date.today() - timedelta(days=days)).isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(AVG(total_count),0) FROM daily_summaries "
            "WHERE date>=? AND total_count>0", (start,)
        ).fetchone()
        return round(row[0], 1)

    def get_peak_day(self) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT date,total_count FROM daily_summaries "
            "ORDER BY total_count DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_hourly_distribution(self, date_str: str = None) -> List[Dict]:
        if not date_str:
            date_str = date.today().isoformat()
        rows = self.conn.execute("""
            SELECT strftime('%H',timestamp) as hour, COUNT(*) as count
            FROM count_events WHERE date(timestamp)=?
            GROUP BY hour ORDER BY hour
        """, (date_str,)).fetchall()
        # Fill all 24 hours
        dist = {str(h).zfill(2): 0 for h in range(24)}
        for r in rows:
            dist[r["hour"]] = r["count"]
        return [{"hour": int(h), "count": c} for h, c in sorted(dist.items())]

    def get_daily_trend(self, days: int = 30) -> List[Dict]:
        start = (date.today() - timedelta(days=days)).isoformat()
        rows = self.conn.execute("""
            SELECT date,total_count FROM daily_summaries
            WHERE date>=? ORDER BY date
        """, (start,)).fetchall()
        return [{"date": r[0], "count": r[1]} for r in rows]

    def get_monthly_trend(self, months: int = 12) -> List[Dict]:
        rows = self.conn.execute("""
            SELECT strftime('%Y-%m',date) as month,
                   SUM(total_count) as count
            FROM daily_summaries
            GROUP BY month ORDER BY month DESC LIMIT ?
        """, (months,)).fetchall()
        return [{"month": r[0], "count": r[1]}
                for r in reversed(list(rows))]

    # ============================================================ Import
    def import_count_events(self, events: List[Dict]) -> int:
        """Import CSV/external events. Returns imported count."""
        imported = 0
        for e in events:
            ts = e.get("timestamp", datetime.now().isoformat())
            day = ts[:10] if isinstance(ts, str) else date.today().isoformat()

            sid = e.get("session_id")
            if not sid:
                cur = self.conn.execute(
                    "INSERT INTO sessions "
                    "(started_at,source,status) VALUES (?,'import','stopped')",
                    (ts,),
                )
                sid = cur.lastrowid

            self.conn.execute(
                """INSERT INTO count_events
                    (session_id,timestamp,track_id,cx,cy,
                     x1,y1,x2,y2,confidence,running_total)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, ts, e.get("track_id", 0),
                 e.get("cx", 0), e.get("cy", 0),
                 e.get("x1", 0), e.get("y1", 0),
                 e.get("x2", 0), e.get("y2", 0),
                 e.get("confidence", 0), e.get("running_total", 0)),
            )
            self.conn.execute("""
                INSERT INTO daily_summaries
                    (date,total_count,session_count,first_count_at,last_count_at)
                VALUES (?,1,1,?,?)
                ON CONFLICT(date) DO UPDATE SET
                    total_count=total_count+1,
                    last_count_at=excluded.last_count_at
            """, (day, ts, ts))
            imported += 1

        self.conn.commit()
        return imported

    # ============================================================ Versions
    def add_version(self, version: str, changelog: str = None,
                    backup_path: str = None,
                    package_path: str = None,
                    release_url: str = None,
                    release_published_at: str = None,
                    installed_by: str = "manual"):
        self.conn.execute("UPDATE app_versions SET is_active=0")
        self.conn.execute(
            "INSERT INTO app_versions "
            "(version,changelog,is_active,backup_path,package_path,release_url,release_published_at,installed_by) "
            "VALUES (?,?,1,?,?,?,?,?)",
            (
                version,
                changelog,
                backup_path,
                package_path,
                release_url,
                release_published_at,
                installed_by,
            ),
        )
        self.conn.commit()

    def get_versions(self) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM app_versions ORDER BY installed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_version(self) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM app_versions WHERE is_active=1"
        ).fetchone()
        return dict(row) if row else None

    def rollback_version(self, version_id: int) -> bool:
        ver = self.conn.execute(
            "SELECT * FROM app_versions WHERE id=?", (version_id,)
        ).fetchone()
        if not ver:
            return False
        self.conn.execute("UPDATE app_versions SET is_active=0")
        self.conn.execute(
            "UPDATE app_versions SET is_active=1 WHERE id=?",
            (version_id,),
        )
        self.conn.commit()
        return True

    # ============================================================ Cleanup
    def close(self):
        if hasattr(Database._local, "conn") and Database._local.conn:
            Database._local.conn.close()
            Database._local.conn = None
