import sqlite3
import time
from pathlib import Path

DB_PATH = Path("data/users.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            opted_out INTEGER DEFAULT 0,
            last_sent_ts INTEGER,
            next_due_ts INTEGER,
            msg_index INTEGER DEFAULT 0,
            created_ts INTEGER DEFAULT (strftime('%s','now'))
        )
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            active INTEGER DEFAULT 1,
            last_sent_ts INTEGER,
            next_due_ts INTEGER,
            msg_index INTEGER DEFAULT 0,
            created_ts INTEGER DEFAULT (strftime('%s','now'))
        )
        """)
        conn.commit()

def upsert_user(chat_id: int, username: str, first: str, last: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (chat_id, username, first_name, last_name, opted_out)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(chat_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                opted_out=0
            """, (chat_id, username, first, last))
        conn.commit()

def set_optout(chat_id: int, value: bool = True):
    with get_conn() as conn:
        conn.execute("UPDATE users SET opted_out=? WHERE chat_id=?", 
                    (1 if value else 0, chat_id))
        conn.commit()

def get_user(chat_id: int):
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,))
        return cur.fetchone()

def due_users(now_ts: int, limit: int = 500):
    with get_conn() as conn:
        cur = conn.execute("""
            SELECT * FROM users 
            WHERE opted_out=0 AND (next_due_ts IS NULL OR next_due_ts<=?)
            ORDER BY next_due_ts IS NOT NULL, next_due_ts
            LIMIT ?
            """, (now_ts, limit))
        return cur.fetchall()

def mark_sent(chat_id: int, next_due_ts: int, new_index: int):
    with get_conn() as conn:
        conn.execute("""
            UPDATE users 
            SET last_sent_ts=?, next_due_ts=?, msg_index=? 
            WHERE chat_id=?
            """, (int(time.time()), next_due_ts, new_index, chat_id))
        conn.commit()

def upsert_group(chat_id: int, title: str):
    with get_conn() as conn:
        conn.execute("""
        INSERT INTO groups (chat_id, title, active)
        VALUES (?, ?, 1)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            active=1
        """, (chat_id, title))
        conn.commit()

def set_group_active(chat_id: int, active: bool = True):
    with get_conn() as conn:
        conn.execute("UPDATE groups SET active=? WHERE chat_id=?",
                     (1 if active else 0, chat_id))
        conn.commit()

def due_groups(now_ts: int, limit: int = 50):
    with get_conn() as conn:
        cur = conn.execute("""
        SELECT * FROM groups
        WHERE active=1 AND (next_due_ts IS NULL OR next_due_ts<=?)
        ORDER BY next_due_ts IS NOT NULL, next_due_ts
        LIMIT ?
        """, (now_ts, limit))
        return cur.fetchall()

def mark_group_sent(chat_id: int, next_due_ts: int, new_index: int):
    with get_conn() as conn:
        conn.execute("""
        UPDATE groups
        SET last_sent_ts=?, next_due_ts=?, msg_index=?
        WHERE chat_id=?
        """, (int(time.time()), next_due_ts, new_index, chat_id))
        conn.commit()