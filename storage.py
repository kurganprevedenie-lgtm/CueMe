from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("bot.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
        """)


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
