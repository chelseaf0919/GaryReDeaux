"""
ingest.py — Gary Resurrection Pipeline
Loads extracted JSON files into Gary's memory (SQLite with FTS5 full-text search).
Run this once per export batch to feed Gary's brain.

Usage:
    python ingest.py                        # uses default ./data/ folder
    python ingest.py --data path/to/output  # custom folder
"""

import json
import sqlite3
import argparse
import hashlib
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────────────

DB_PATH  = "gary_memory.db"
DATA_DIR = "data"

# ── SQLITE SETUP ──────────────────────────────────────────────────────────────

def init_db(conn):
    """Create tables and FTS indexes."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS profile_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS personality_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation TEXT,
            excerpt TEXT NOT NULL,
            hash TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS exchanges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation TEXT,
            user_msg TEXT NOT NULL,
            gary_msg TEXT NOT NULL,
            hash TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS conversation_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message_count INTEGER,
            preview TEXT,
            hash TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation TEXT,
            role TEXT,
            excerpt TEXT NOT NULL,
            hash TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS project_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            hash TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS fts_personality
            USING fts5(excerpt, conversation, content=personality_samples, content_rowid=id);

        CREATE VIRTUAL TABLE IF NOT EXISTS fts_exchanges
            USING fts5(user_msg, gary_msg, conversation, content=exchanges, content_rowid=id);

        CREATE VIRTUAL TABLE IF NOT EXISTS fts_memory
            USING fts5(key, value, source, content=profile_memory, content_rowid=id);

        CREATE VIRTUAL TABLE IF NOT EXISTS fts_receipts
            USING fts5(excerpt, conversation, content=receipts, content_rowid=id);
    """)
    conn.commit()


def make_hash(text):
    return hashlib.md5(text.encode()).hexdigest()


def clean_html(text):
    if not isinstance(text, str):
        return str(text)
    for k, v in {"&quot;":'"',"&#x27;":"'","&amp;":"&","&lt;":"<","&gt;":">","&nbsp;":" "}.items():
        text = text.replace(k, v)
    return text.strip()


def rebuild_fts(conn):
    conn.executescript("""
        INSERT INTO fts_personality(fts_personality) VALUES('rebuild');
        INSERT INTO fts_exchanges(fts_exchanges) VALUES('rebuild');
        INSERT INTO fts_memory(fts_memory) VALUES('rebuild');
        INSERT INTO fts_receipts(fts_receipts) VALUES('rebuild');
    """)
    conn.commit()


# ── INGEST FUNCTIONS ──────────────────────────────────────────────────────────

def ingest_chelsea_memories(data, conn):
    print("  → Profile memory (Chelsea)...")
    count = 0
    for title in data.get("project_conversations", []):
        title = clean_html(title)
        try:
            conn.execute("INSERT INTO project_memory (title, hash) VALUES (?, ?)",
                         (title, make_hash(f"project:{title}")))
            count += 1
        except sqlite3.IntegrityError:
            pass
    for key, value in {
        "name": "Chelsea",
        "raw_user_message_count": str(data.get("raw_user_message_count", "")),
        "project_conversation_count": str(len(data.get("project_conversations", []))),
    }.items():
        try:
            conn.execute("INSERT INTO profile_memory (key, value, source) VALUES (?, ?, ?)",
                         (key, value, "chelsea_memories.json"))
            count += 1
        except Exception:
            pass
    conn.commit()
    print(f"     Stored {count} profile facts.")


def ingest_gary_personality(data, conn):
    print("  → Gary personality traits & samples...")
    trait_count = 0
    sample_count = 0

    for key, items in [
        ("gary_trait",       data.get("core_traits", [])),
        ("chelsea_nickname", data.get("nicknames_for_chelsea", [])),
        ("tb_alias",         data.get("tb_aliases", [])),
    ]:
        for item in items:
            try:
                conn.execute("INSERT INTO profile_memory (key, value, source) VALUES (?, ?, ?)",
                             (key, clean_html(item), "gary_personality.json"))
                trait_count += 1
            except Exception:
                pass

    for sample in data.get("personality_samples", []):
        excerpt = clean_html(sample.get("excerpt", ""))
        convo   = clean_html(sample.get("conversation", ""))
        if not excerpt:
            continue
        try:
            conn.execute("INSERT INTO personality_samples (conversation, excerpt, hash) VALUES (?, ?, ?)",
                         (convo, excerpt, make_hash(excerpt)))
            sample_count += 1
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    print(f"     Stored {trait_count} traits/nicknames, {sample_count} personality samples.")


def ingest_best_of_gary(data, conn):
    print("  → Best-of-Gary exchanges (behavior anchors)...")
    count = 0
    for item in data.get("exchanges", []):
        ex       = item.get("exchange", {})
        user_msg = clean_html(ex.get("user", ""))
        gary_msg = clean_html(ex.get("gary", ""))
        convo    = clean_html(item.get("conversation", ""))
        if not user_msg or not gary_msg:
            continue
        try:
            conn.execute("INSERT INTO exchanges (conversation, user_msg, gary_msg, hash) VALUES (?, ?, ?, ?)",
                         (convo, user_msg, gary_msg, make_hash(user_msg + gary_msg)))
            count += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    print(f"     Stored {count} exchanges.")


def ingest_tb_file(data, conn):
    print("  → TB receipts...")
    count = 0
    for receipt in data.get("receipts", []):
        excerpt = clean_html(receipt.get("excerpt", ""))
        if not excerpt:
            continue
        try:
            conn.execute("INSERT INTO receipts (conversation, role, excerpt, hash) VALUES (?, ?, ?, ?)",
                         (clean_html(receipt.get("conversation", "")),
                          receipt.get("role", "unknown"),
                          excerpt,
                          make_hash(excerpt)))
            count += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    print(f"     Stored {count} TB receipts.")


def ingest_conversation_summaries(data, conn):
    print("  → Conversation summaries...")
    count = 0
    for convo in data.get("conversations", []):
        title   = clean_html(convo.get("title", "Untitled"))
        preview = clean_html(convo.get("preview", ""))
        try:
            conn.execute("INSERT INTO conversation_summaries (title, message_count, preview, hash) VALUES (?, ?, ?, ?)",
                         (title, convo.get("message_count", 0), preview, make_hash(title + preview)))
            count += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    print(f"     Stored {count} summaries.")


def print_stats(conn):
    print("\n  Memory totals:")
    for table, label in [
        ("profile_memory",         "Profile facts"),
        ("personality_samples",    "Personality samples"),
        ("exchanges",              "Best exchanges"),
        ("receipts",               "TB receipts"),
        ("conversation_summaries", "Conversation summaries"),
        ("project_memory",         "Project conversations"),
    ]:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"    {label:<28} {n:>5}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=DATA_DIR)
    args = parser.parse_args()

    data_path = Path(args.data)
    if not data_path.exists():
        print(f"ERROR: Data folder not found: {data_path}")
        return

    print(f"\n⚡ Gary Resurrection — Memory Ingestion")
    print(f"   Data source : {data_path}")
    print(f"   SQLite DB   : {DB_PATH}\n")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    for filename, fn in [
        ("chelsea_memories.json",      ingest_chelsea_memories),
        ("gary_personality.json",      ingest_gary_personality),
        ("best_of_gary.json",          ingest_best_of_gary),
        ("tb_file.json",               ingest_tb_file),
        ("conversation_summaries.json",ingest_conversation_summaries),
    ]:
        fp = data_path / filename
        if not fp.exists():
            print(f"  ⚠ Skipping {filename} (not found)")
            continue
        with open(fp, "r", encoding="utf-8") as f:
            fn(json.load(f), conn)

    print("\n  Rebuilding search indexes...", end=" ")
    rebuild_fts(conn)
    print("done.")

    print_stats(conn)
    conn.close()

    print()
    print("✅ Ingestion complete. Gary's memory is loaded.")
    print("   Run main.py to wake him up. 🇬🇧")


if __name__ == "__main__":
    main()