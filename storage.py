from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("bot.db")


def _conn() -> sqlite3.Connection:
    # timeout=30 — ждать снятия блокировки до 30 с вместо мгновенного "database is locked"
    # (актуально при живом Business-потоке + фоновых пересборках).
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL — параллельные чтения не блокируются записью; настройка персистентная на файле БД.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _create_index_if_missing(
    conn: sqlite3.Connection, name: str, table: str, columns: str, unique: bool = False
) -> None:
    """Идемпотентно создаёт индекс. CREATE INDEX IF NOT EXISTS сам по себе безопасен,
    обёртка — для единообразия с _add_column_if_missing и читаемости миграций."""
    kind = "UNIQUE INDEX" if unique else "INDEX"
    conn.execute(f"CREATE {kind} IF NOT EXISTS {name} ON {table} ({columns})")


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id TEXT PRIMARY KEY,
                my_id       TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contacts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_telegram_id TEXT NOT NULL,
                contact_alias    TEXT NOT NULL,
                original_from_id TEXT NOT NULL,
                display_name     TEXT,
                UNIQUE(user_telegram_id, original_from_id)
            );

            CREATE TABLE IF NOT EXISTS style_cards (
                user_telegram_id TEXT PRIMARY KEY,
                card_text        TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS interaction_cards (
                contact_id INTEGER PRIMARY KEY,
                card_text  TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_samples (
                contact_id           INTEGER PRIMARY KEY,
                my_sample            TEXT NOT NULL,
                contact_sample       TEXT NOT NULL,
                features_summary     TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS business_connections (
                connection_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                can_reply     INTEGER NOT NULL DEFAULT 0,
                is_enabled    INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS business_messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                chat_ref      TEXT NOT NULL,
                direction     TEXT NOT NULL,
                text          TEXT,
                date          TEXT NOT NULL,
                tg_message_id INTEGER,
                raw_meta      TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS imported_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                direction  TEXT NOT NULL,
                text       TEXT NOT NULL,
                date       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS business_chat_refs (
                owner_user_id TEXT NOT NULL,
                chat_ref      TEXT NOT NULL,
                contact_id    INTEGER NOT NULL,
                PRIMARY KEY (owner_user_id, chat_ref)
            );

            CREATE TABLE IF NOT EXISTS my_style_per_contact (
                contact_id         INTEGER PRIMARY KEY,
                card_text          TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                last_rebuild_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS deep_analysis (
                contact_id         INTEGER PRIMARY KEY,
                compatibility_text TEXT NOT NULL,
                history_text       TEXT NOT NULL,
                swot_text          TEXT NOT NULL,
                gifts_text         TEXT NOT NULL,
                updated_at         TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deep_style_analysis (
                user_telegram_id TEXT PRIMARY KEY,
                profile_text     TEXT NOT NULL,
                history_text     TEXT NOT NULL,
                swot_text        TEXT NOT NULL,
                tips_text        TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key  TEXT PRIMARY KEY,
                result     TEXT NOT NULL,   -- JSON-сериализованный результат
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT NOT NULL,        -- когда (UTC ISO)
                user_telegram_id TEXT,                -- кто (может быть NULL для системных)
                event_type       TEXT NOT NULL,       -- что: gen_reply, gen_rewrite, ...
                meta             TEXT                  -- произвольный контекст (стиль, kind)
            );

            CREATE TABLE IF NOT EXISTS running_notes (
                contact_id    INTEGER PRIMARY KEY,
                notes_text    TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                updated_at    TEXT NOT NULL
            );
        """)
        _add_column_if_missing(conn, "users", "auto_mode", "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "users", "auto_contact_id", "INTEGER")
        _add_column_if_missing(conn, "users", "trial_used", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "contacts", "username", "TEXT")
        _add_column_if_missing(conn, "message_samples", "contact_label", "TEXT")
        # user_features_summary — подмножество features_summary, убираем дубль
        ms_cols = [r[1] for r in conn.execute("PRAGMA table_info(message_samples)").fetchall()]
        if "user_features_summary" in ms_cols:
            conn.execute("ALTER TABLE message_samples DROP COLUMN user_features_summary")

        # Индексы под горячие выборки (пересборка карточек, чтение истории)
        _create_index_if_missing(
            conn, "idx_biz_msgs_lookup", "business_messages",
            "owner_user_id, chat_ref, direction, date",
        )
        _create_index_if_missing(
            conn, "idx_imported_lookup", "imported_messages",
            "contact_id, direction",
        )
        # Дедуп business-сообщений: убираем существующие дубли (по connection_id+chat_ref+
        # tg_message_id), затем ставим UNIQUE-индекс. NULL-id не трогаем (у них нет ключа).
        conn.execute(
            """
            DELETE FROM business_messages
            WHERE tg_message_id IS NOT NULL
              AND id NOT IN (
                  SELECT MIN(id) FROM business_messages
                  WHERE tg_message_id IS NOT NULL
                  GROUP BY connection_id, chat_ref, tg_message_id
              )
            """
        )
        _create_index_if_missing(
            conn, "idx_biz_msg_unique", "business_messages",
            "connection_id, chat_ref, tg_message_id", unique=True,
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── users ─────────────────────────────────────────────────────────────────────

def get_user(telegram_id: str) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def upsert_user(telegram_id: str, my_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, my_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET my_id = excluded.my_id
            """,
            (telegram_id, my_id, _now()),
        )


def get_trial_used(telegram_id: str) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT trial_used FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row["trial_used"] if row else 0


def increment_trial_used(telegram_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET trial_used = trial_used + 1 WHERE telegram_id = ?",
            (telegram_id,),
        )


# ── contacts ──────────────────────────────────────────────────────────────────

def get_or_create_contact(
    user_telegram_id: str, original_from_id: str, display_name: str
) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM contacts WHERE user_telegram_id = ? AND original_from_id = ?",
            (user_telegram_id, original_from_id),
        ).fetchone()
        if row:
            return row["id"]
        alias = str(uuid.uuid4())
        cur = conn.execute(
            """
            INSERT INTO contacts
                (user_telegram_id, contact_alias, original_from_id, display_name)
            VALUES (?, ?, ?, ?)
            """,
            (user_telegram_id, alias, original_from_id, display_name),
        )
        return cur.lastrowid


def list_contacts(user_telegram_id: str) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM contacts WHERE user_telegram_id = ?", (user_telegram_id,)
        ).fetchall()


def get_contact_by_id(contact_id: int) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()


def update_contact_username(contact_id: int, username: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE contacts SET username = ? WHERE id = ?",
            (username, contact_id),
        )


# ── style cards ───────────────────────────────────────────────────────────────

def save_style_card(user_telegram_id: str, card_text: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO style_cards (user_telegram_id, card_text, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_telegram_id) DO UPDATE SET
                card_text  = excluded.card_text,
                updated_at = excluded.updated_at
            """,
            (user_telegram_id, card_text, _now()),
        )


def get_style_card(user_telegram_id: str) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT card_text FROM style_cards WHERE user_telegram_id = ?",
            (user_telegram_id,),
        ).fetchone()
        return row["card_text"] if row else None


def delete_style_card(user_telegram_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM style_cards WHERE user_telegram_id = ?", (user_telegram_id,)
        )


# ── interaction cards ─────────────────────────────────────────────────────────

def save_interaction_card(contact_id: int, card_text: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO interaction_cards (contact_id, card_text, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(contact_id) DO UPDATE SET
                card_text  = excluded.card_text,
                updated_at = excluded.updated_at
            """,
            (contact_id, card_text, _now()),
        )


def get_interaction_card(contact_id: int) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT card_text FROM interaction_cards WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        return row["card_text"] if row else None


# ── running notes («Живой диалог» — накопительные заметки без порога) ─────────

def get_running_notes(contact_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT notes_text, message_count, updated_at FROM running_notes WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        return dict(row) if row else None


def save_running_notes(contact_id: int, notes_text: str, message_count: int) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO running_notes (contact_id, notes_text, message_count, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(contact_id) DO UPDATE SET
                notes_text    = excluded.notes_text,
                message_count = excluded.message_count,
                updated_at    = excluded.updated_at
            """,
            (contact_id, notes_text, message_count, _now()),
        )


# ── message samples (для ленивой генерации карточек) ──────────────────────────

def save_message_samples(
    contact_id: int,
    my_sample: list[str],
    contact_sample: list[str],
    features_summary: str,
    contact_label: str = "",
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO message_samples
                (contact_id, my_sample, contact_sample, features_summary, contact_label)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(contact_id) DO UPDATE SET
                my_sample        = excluded.my_sample,
                contact_sample   = excluded.contact_sample,
                features_summary = excluded.features_summary,
                contact_label    = excluded.contact_label
            """,
            (
                contact_id,
                json.dumps(my_sample, ensure_ascii=False),
                json.dumps(contact_sample, ensure_ascii=False),
                features_summary,
                contact_label,
            ),
        )


def get_message_samples(contact_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM message_samples WHERE contact_id = ?", (contact_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "my_sample":        json.loads(row["my_sample"]),
        "contact_sample":   json.loads(row["contact_sample"]),
        "features_summary": row["features_summary"],
    }


def get_any_user_samples(user_telegram_id: str) -> dict | None:
    """Первые доступные семплы для генерации style_card пользователя."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT ms.my_sample, ms.features_summary
            FROM message_samples ms
            JOIN contacts c ON ms.contact_id = c.id
            WHERE c.user_telegram_id = ?
            LIMIT 1
            """,
            (user_telegram_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "my_sample":        json.loads(row["my_sample"]),
        "features_summary": row["features_summary"],
    }


# ── imported messages (полный архив из JSON-экспорта) ─────────────────────────

def save_imported_messages(contact_id: int, messages: list[dict]) -> None:
    """messages: [{"direction": "in"/"out", "text": str, "date": str}]"""
    with _conn() as conn:
        conn.execute(
            "DELETE FROM imported_messages WHERE contact_id = ?", (contact_id,)
        )
        conn.executemany(
            "INSERT INTO imported_messages (contact_id, direction, text, date) VALUES (?, ?, ?, ?)",
            [(contact_id, m["direction"], m["text"], m["date"]) for m in messages],
        )


def get_imported_messages(contact_id: int, direction: str, limit: int = 0) -> list[str]:
    """limit=0 → все сообщения."""
    with _conn() as conn:
        if limit:
            rows = conn.execute(
                "SELECT text FROM imported_messages WHERE contact_id = ? AND direction = ? ORDER BY date DESC LIMIT ?",
                (contact_id, direction, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT text FROM imported_messages WHERE contact_id = ? AND direction = ? ORDER BY date DESC",
                (contact_id, direction),
            ).fetchall()
    return [row["text"] for row in rows]


def count_imported_messages(contact_id: int) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM imported_messages WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
    return row["cnt"] if row else 0


# ── contacts (extra lookup) ───────────────────────────────────────────────────

def find_contact_by_original_id(
    user_telegram_id: str, original_from_id: str
) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM contacts WHERE user_telegram_id = ? AND original_from_id = ?",
            (user_telegram_id, original_from_id),
        ).fetchone()


# ── business chat ref mapping ─────────────────────────────────────────────────

def upsert_chat_ref_mapping(owner_user_id: str, chat_ref: str, contact_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO business_chat_refs (owner_user_id, chat_ref, contact_id)
            VALUES (?, ?, ?)
            """,
            (owner_user_id, chat_ref, contact_id),
        )


def get_contact_id_for_chat_ref(owner_user_id: str, chat_ref: str) -> int | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT contact_id FROM business_chat_refs WHERE owner_user_id = ? AND chat_ref = ?",
            (owner_user_id, chat_ref),
        ).fetchone()
    return row["contact_id"] if row else None


# ── my style per contact ──────────────────────────────────────────────────────

def save_my_style_per_contact(
    contact_id: int, card_text: str, rebuild_count: int
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO my_style_per_contact
                (contact_id, card_text, updated_at, last_rebuild_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(contact_id) DO UPDATE SET
                card_text          = excluded.card_text,
                updated_at         = excluded.updated_at,
                last_rebuild_count = excluded.last_rebuild_count
            """,
            (contact_id, card_text, _now(), rebuild_count),
        )


def get_my_style_per_contact(contact_id: int) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT card_text FROM my_style_per_contact WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
    return row["card_text"] if row else None


def get_my_style_last_rebuild_count(contact_id: int) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_rebuild_count FROM my_style_per_contact WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
    return row["last_rebuild_count"] if row else 0


def get_all_per_contact_style_cards(owner_user_id: str) -> list[dict]:
    """Все per-contact карточки пользователя — для сборки агрегата."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT ms.card_text, c.display_name, c.contact_alias
            FROM my_style_per_contact ms
            JOIN contacts c ON ms.contact_id = c.id
            WHERE c.user_telegram_id = ?
            """,
            (owner_user_id,),
        ).fetchall()
    return [
        {
            "card_text":    row["card_text"],
            "display_name": row["display_name"] or row["contact_alias"],
        }
        for row in rows
    ]


# ── deep analysis (кэш анализа собеседника, пара) ─────────────────────────────

def save_deep_analysis(
    contact_id: int,
    compatibility_text: str,
    history_text: str,
    swot_text: str,
    gifts_text: str,
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO deep_analysis
                (contact_id, compatibility_text, history_text, swot_text, gifts_text, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(contact_id) DO UPDATE SET
                compatibility_text = excluded.compatibility_text,
                history_text       = excluded.history_text,
                swot_text          = excluded.swot_text,
                gifts_text         = excluded.gifts_text,
                updated_at         = excluded.updated_at
            """,
            (contact_id, compatibility_text, history_text, swot_text, gifts_text, _now()),
        )


def get_deep_analysis(contact_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM deep_analysis WHERE contact_id = ?", (contact_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "compatibility_text": row["compatibility_text"],
        "history_text":       row["history_text"],
        "swot_text":          row["swot_text"],
        "gifts_text":         row["gifts_text"],
    }


def delete_deep_analysis(contact_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM deep_analysis WHERE contact_id = ?", (contact_id,))


# ── deep style analysis (кэш анализа своего стиля, агрегат) ───────────────────

def save_deep_style_analysis(
    user_telegram_id: str,
    profile_text: str,
    history_text: str,
    swot_text: str,
    tips_text: str,
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO deep_style_analysis
                (user_telegram_id, profile_text, history_text, swot_text, tips_text, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_telegram_id) DO UPDATE SET
                profile_text = excluded.profile_text,
                history_text = excluded.history_text,
                swot_text    = excluded.swot_text,
                tips_text    = excluded.tips_text,
                updated_at   = excluded.updated_at
            """,
            (user_telegram_id, profile_text, history_text, swot_text, tips_text, _now()),
        )


def get_deep_style_analysis(user_telegram_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM deep_style_analysis WHERE user_telegram_id = ?",
            (user_telegram_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "profile_text": row["profile_text"],
        "history_text": row["history_text"],
        "swot_text":    row["swot_text"],
        "tips_text":    row["tips_text"],
    }


def delete_deep_style_analysis(user_telegram_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM deep_style_analysis WHERE user_telegram_id = ?", (user_telegram_id,)
        )


# ── LLM cache (контент-адресный кэш ответов генерации) ────────────────────────

def get_llm_cache(cache_key: str, max_age_sec: int) -> str | None:
    """Кэшированный результат по ключу или None (нет записи / протухла по TTL)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT result, created_at FROM llm_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
    if not row:
        return None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds()
    except (ValueError, TypeError):
        return None
    if age > max_age_sec:
        return None
    return row["result"]


def set_llm_cache(cache_key: str, result: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache (cache_key, result, created_at) VALUES (?, ?, ?)",
            (cache_key, result, _now()),
        )


# ── события / метрики исходов ─────────────────────────────────────────────────
# Фундамент продуктовой аналитики: воронка триал→оплата, что генерят, доходят ли
# до встречи. Провайдер событий провязывается в main.py по мере надобности.

def record_event(user_telegram_id: str | None, event_type: str, meta: str = "") -> None:
    """Пишет одно событие. Телеметрия best-effort — вызывать из-под try, чтобы
    сбой записи не ломал пользовательский флоу."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO events (ts, user_telegram_id, event_type, meta) VALUES (?, ?, ?, ?)",
            (_now(), user_telegram_id, event_type, meta),
        )


def count_events(event_type: str, user_telegram_id: str | None = None) -> int:
    """Сколько раз произошло событие (опционально по конкретному юзеру)."""
    with _conn() as conn:
        if user_telegram_id is None:
            row = conn.execute(
                "SELECT COUNT(*) n FROM events WHERE event_type = ?", (event_type,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) n FROM events WHERE event_type = ? AND user_telegram_id = ?",
                (event_type, user_telegram_id),
            ).fetchone()
    return row["n"]


def event_funnel() -> dict[str, int]:
    """Сводка по типам событий {event_type: count} — простой обзор воронки."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) n FROM events GROUP BY event_type"
        ).fetchall()
    return {r["event_type"]: r["n"] for r in rows}


def get_all_dated_my_messages(owner_user_id: str) -> list[dict]:
    """Все исходящие сообщения пользователя (business + imported) по ВСЕМ его
    контактам, с датами — для агрегатного анализа собственного стиля."""
    with _conn() as conn:
        biz_rows = conn.execute(
            """
            SELECT date, text, 'out' AS direction
            FROM business_messages
            WHERE owner_user_id = ? AND direction = 'out'
              AND text IS NOT NULL AND text != ''
            """,
            (owner_user_id,),
        ).fetchall()
        imp_rows = conn.execute(
            """
            SELECT im.date, im.text, 'out' AS direction
            FROM imported_messages im
            JOIN contacts c ON im.contact_id = c.id
            WHERE c.user_telegram_id = ? AND im.direction = 'out'
              AND im.text IS NOT NULL AND im.text != ''
            """,
            (owner_user_id,),
        ).fetchall()

    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for row in list(biz_rows) + list(imp_rows):
        key = (row["date"], row["text"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": row["date"], "direction": row["direction"], "text": row["text"]})
    return out


# ── business messages — аналитика ─────────────────────────────────────────────

def get_biz_messages_for_contact(
    owner_user_id: str, contact_id: int, direction: str, limit: int
) -> list[str]:
    """Тексты сообщений по контакту (date DESC), через маппинг chat_refs."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT bm.text
            FROM business_messages bm
            JOIN business_chat_refs bcr
                ON bm.chat_ref = bcr.chat_ref
               AND bm.owner_user_id = bcr.owner_user_id
            WHERE bcr.contact_id = ?
              AND bm.owner_user_id = ?
              AND bm.direction = ?
              AND bm.text IS NOT NULL
              AND bm.text != ''
            ORDER BY bm.date DESC
            LIMIT ?
            """,
            (contact_id, owner_user_id, direction, limit),
        ).fetchall()
    return [row["text"] for row in rows]


def get_all_dated_messages(owner_user_id: str, contact_id: int) -> list[dict]:
    """Все сообщения контакта (business + imported) с датами — для анализа
    собеседника (совместимость, история по периодам). Без сортировки — сортировка
    и семплирование на стороне вызывающего."""
    with _conn() as conn:
        biz_rows = conn.execute(
            """
            SELECT bm.date, bm.direction, bm.text
            FROM business_messages bm
            JOIN business_chat_refs bcr
                ON bm.chat_ref = bcr.chat_ref
               AND bm.owner_user_id = bcr.owner_user_id
            WHERE bcr.contact_id = ?
              AND bm.owner_user_id = ?
              AND bm.text IS NOT NULL
              AND bm.text != ''
            """,
            (contact_id, owner_user_id),
        ).fetchall()
        imp_rows = conn.execute(
            """
            SELECT date, direction, text
            FROM imported_messages
            WHERE contact_id = ? AND text IS NOT NULL AND text != ''
            """,
            (contact_id,),
        ).fetchall()

    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for row in list(biz_rows) + list(imp_rows):
        key = (row["date"], row["direction"], row["text"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": row["date"], "direction": row["direction"], "text": row["text"]})
    return out


def count_biz_messages_for_contact(owner_user_id: str, contact_id: int) -> int:
    """Всего сообщений (в обе стороны) для контакта через маппинг."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM business_messages bm
            JOIN business_chat_refs bcr
                ON bm.chat_ref = bcr.chat_ref
               AND bm.owner_user_id = bcr.owner_user_id
            WHERE bcr.contact_id = ?
              AND bm.owner_user_id = ?
            """,
            (contact_id, owner_user_id),
        ).fetchone()
    return row["cnt"] if row else 0


# ── business connections ──────────────────────────────────────────────────────

def upsert_business_connection(
    connection_id: str,
    owner_user_id: str,
    can_reply: bool,
    is_enabled: bool,
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO business_connections
                (connection_id, owner_user_id, can_reply, is_enabled, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                can_reply  = excluded.can_reply,
                is_enabled = excluded.is_enabled
            """,
            (
                connection_id,
                owner_user_id,
                1 if can_reply else 0,
                1 if is_enabled else 0,
                _now(),
            ),
        )


def get_business_connection(connection_id: str) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM business_connections WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()


# ── business messages ─────────────────────────────────────────────────────────

def save_business_message(
    connection_id: str,
    owner_user_id: str,
    chat_ref: str,
    direction: str,
    text: str | None,
    date: str,
    tg_message_id: int | None,
    raw_meta: dict,
) -> bool:
    """Сохраняет business-сообщение. Возвращает True, если ряд вставлен, и False,
    если это дубль (повторная доставка того же connection_id+chat_ref+tg_message_id)."""
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO business_messages
                (connection_id, owner_user_id, chat_ref, direction,
                 text, date, tg_message_id, raw_meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                connection_id,
                owner_user_id,
                chat_ref,
                direction,
                text,
                date,
                tg_message_id,
                json.dumps(raw_meta, ensure_ascii=False),
            ),
        )
        return cur.rowcount > 0


# ── auto mode ─────────────────────────────────────────────────────────────────

def get_auto_mode(telegram_id: str) -> tuple[bool, int | None]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT auto_mode, auto_contact_id FROM users WHERE telegram_id = ?",
            (telegram_id,),
        ).fetchone()
    if not row:
        return False, None
    return bool(row["auto_mode"]), row["auto_contact_id"]


def set_auto_mode(telegram_id: str, enabled: bool, contact_id: int | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET auto_mode = ?, auto_contact_id = ? WHERE telegram_id = ?",
            (1 if enabled else 0, contact_id, telegram_id),
        )


# ── удаление данных (152-ФЗ: пользователь может стереть свои данные) ───────────

def delete_contact_data(telegram_id: str, contact_id: int) -> None:
    """Полностью удаляет один контакт и все производные данные."""
    with _conn() as conn:
        # business-сообщения этого контакта — через привязанные chat_ref
        refs = [
            r["chat_ref"] for r in conn.execute(
                "SELECT chat_ref FROM business_chat_refs WHERE owner_user_id = ? AND contact_id = ?",
                (telegram_id, contact_id),
            ).fetchall()
        ]
        for ref in refs:
            conn.execute(
                "DELETE FROM business_messages WHERE owner_user_id = ? AND chat_ref = ?",
                (telegram_id, ref),
            )
        conn.execute(
            "DELETE FROM business_chat_refs WHERE owner_user_id = ? AND contact_id = ?",
            (telegram_id, contact_id),
        )
        conn.execute("DELETE FROM interaction_cards    WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM my_style_per_contact WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM deep_analysis        WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM message_samples      WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM imported_messages    WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM running_notes        WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM contacts             WHERE id = ?",         (contact_id,))
        # сбрасываем агрегатные портреты — пересоберутся без удалённого контакта
        conn.execute("DELETE FROM style_cards         WHERE user_telegram_id = ?", (telegram_id,))
        conn.execute("DELETE FROM deep_style_analysis WHERE user_telegram_id = ?", (telegram_id,))
        # если контакт был активным для авто-режима — снимаем привязку
        conn.execute(
            "UPDATE users SET auto_contact_id = NULL WHERE telegram_id = ? AND auto_contact_id = ?",
            (telegram_id, contact_id),
        )


def delete_all_user_data(telegram_id: str) -> None:
    """Полностью стирает все данные пользователя."""
    with _conn() as conn:
        cids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM contacts WHERE user_telegram_id = ?", (telegram_id,)
            ).fetchall()
        ]
        if cids:
            inlist = ",".join("?" * len(cids))
            conn.execute(f"DELETE FROM interaction_cards    WHERE contact_id IN ({inlist})", cids)
            conn.execute(f"DELETE FROM my_style_per_contact WHERE contact_id IN ({inlist})", cids)
            conn.execute(f"DELETE FROM deep_analysis        WHERE contact_id IN ({inlist})", cids)
            conn.execute(f"DELETE FROM message_samples      WHERE contact_id IN ({inlist})", cids)
            conn.execute(f"DELETE FROM imported_messages    WHERE contact_id IN ({inlist})", cids)
            conn.execute(f"DELETE FROM running_notes        WHERE contact_id IN ({inlist})", cids)
        conn.execute("DELETE FROM contacts             WHERE user_telegram_id = ?", (telegram_id,))
        conn.execute("DELETE FROM style_cards          WHERE user_telegram_id = ?", (telegram_id,))
        conn.execute("DELETE FROM deep_style_analysis  WHERE user_telegram_id = ?", (telegram_id,))
        conn.execute("DELETE FROM business_messages    WHERE owner_user_id = ?",    (telegram_id,))
        conn.execute("DELETE FROM business_chat_refs WHERE owner_user_id = ?",    (telegram_id,))
        conn.execute("DELETE FROM users              WHERE telegram_id = ?",      (telegram_id,))
