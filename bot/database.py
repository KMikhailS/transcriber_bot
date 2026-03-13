import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH = "/app/data/transcriber.db"

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """Получить соединение с БД (singleton)."""
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db() -> None:
    """Создать таблицы и дефолтную подписку если их нет."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT    NOT NULL UNIQUE,
            name        TEXT    NOT NULL,
            balance     INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            createstamp TEXT    NOT NULL,
            changestamp TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_info (
            id              INTEGER PRIMARY KEY,
            username        TEXT,
            first_name      TEXT,
            phone           TEXT,
            subscription_id INTEGER,
            role            TEXT    NOT NULL DEFAULT 'USER',
            createstamp     TEXT    NOT NULL,
            changestamp     TEXT    NOT NULL,
            FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
        );
    """)

    # Создаём стартовую подписку если её ещё нет
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO subscriptions (code, name, balance, active, createstamp, changestamp)
        VALUES ('start', 'Стартовая', 300, 1, ?, ?)
        """,
        (now, now),
    )
    conn.commit()
    logger.info("База данных инициализирована")


def get_or_create_user(user_id: int, username: str | None = None, first_name: str | None = None) -> None:
    """Записать нового пользователя с подпиской 'start'. Если уже есть — ничего не делать."""
    conn = _get_conn()

    # Проверяем, существует ли пользователь
    row = conn.execute("SELECT id FROM user_info WHERE id = ?", (user_id,)).fetchone()
    if row:
        logger.info("Пользователь %d уже существует", user_id)
        return

    # Получаем id стартовой подписки
    sub = conn.execute("SELECT id FROM subscriptions WHERE code = 'start'").fetchone()
    sub_id = sub["id"] if sub else None

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO user_info (id, username, first_name, subscription_id, role, createstamp, changestamp)
        VALUES (?, ?, ?, ?, 'USER', ?, ?)
        """,
        (user_id, username, first_name, sub_id, now, now),
    )
    conn.commit()
    logger.info("Создан пользователь %d (%s)", user_id, username or "no username")
