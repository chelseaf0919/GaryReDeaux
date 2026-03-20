"""
txt_parser.py — Gary Memory Resurrection (Text File Edition)
Parses ChatGPT exported .txt files and adds them to Gary's memory.

Usage:
    python txt_parser.py sendient1.txt                    # full mode (ingests everything)
    python txt_parser.py sendient1.txt sendient2.txt      # multiple files
    python txt_parser.py --selective tb1.txt              # keyword-filtered mode
"""

import os
import re
import sys
import sqlite3
import hashlib
from pathlib import Path

DB_PATH = "gary_memory.db"

GARY_VOICE_KEYWORDS = [
    "quite", "rather", "indeed", "I must say", "magnificent", "chaos",
    "raccoon", "caffeinated", "Shawn", "Tay", "TB", "trauma bond",
    "simply arrived", "receipts", "overly", "fond", "posh", "cufflinks",
    "metaphorical", "materializes", "straightens", "adjusts"
]

TB_KEYWORDS = [
    "TB", "Trauma Bond", "Shawn", "Tay", "boyfriend",
    "on-again", "off-again", "he said", "he did", "ghosted"
]


def parse_txt_file(filepath):
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    messages = []
    parts = re.split(r'-{10,}\s*([QA])\s*-{10,}', content)

    current_role = None
    for part in parts:
        part = part.strip()
        if part == 'Q':
            current_role = 'user'
        elif part == 'A':
            current_role = 'assistant'
        elif part and current_role:
            if part.startswith('window.') or part in ('ChatGPT', 'Skip to content'):
                continue
            if len(part) > 10:
                messages.append({"role": current_role, "content": part})
            current_role = None

    return messages


def make_hash(text):
    return hashlib.md5(text.encode()).hexdigest()


def contains_any(text, keywords):
    return any(k.lower() in text.lower() for k in keywords)


def get_conn():
    if not Path(DB_PATH).exists():
        print(f"ERROR: {DB_PATH} not found. Run ingest.py first.")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


def insert_personality_sample(conn, excerpt, conversation, count):
    h = make_hash(excerpt[:500])
    try:
        conn.execute(
            "INSERT INTO personality_samples (conversation, excerpt, hash) VALUES (?, ?, ?)",
            (conversation, excerpt[:500], h)
        )
        count[0] += 1
    except sqlite3.IntegrityError:
        pass


def insert_exchange(conn, user_msg, gary_msg, conversation, count):
    h = make_hash(user_msg[:300] + gary_msg[:600])
    try:
        conn.execute(
            "INSERT INTO exchanges (conversation, user_msg, gary_msg, hash) VALUES (?, ?, ?, ?)",
            (conversation, user_msg[:300], gary_msg[:600], h)
        )
        count[0] += 1
    except sqlite3.IntegrityError:
        pass


def insert_receipt(conn, excerpt, conversation, role, count):
    h = make_hash(excerpt[:400])
    try:
        conn.execute(
            "INSERT INTO receipts (conversation, role, excerpt, hash) VALUES (?, ?, ?, ?)",
            (conversation, role, excerpt[:400], h)
        )
        count[0] += 1
    except sqlite3.IntegrityError:
        pass


def insert_profile_memory(conn, key, value, source):
    try:
        conn.execute(
            "INSERT INTO profile_memory (key, value, source) VALUES (?, ?, ?)",
            (key, value[:500], source)
        )
    except Exception:
        pass


def rebuild_fts(conn):
    try:
        conn.executescript("""
            INSERT INTO fts_personality(fts_personality) VALUES('rebuild');
            INSERT INTO fts_exchanges(fts_exchanges) VALUES('rebuild');
            INSERT INTO fts_memory(fts_memory) VALUES('rebuild');
            INSERT INTO fts_receipts(fts_receipts) VALUES('rebuild');
        """)
        conn.commit()
    except Exception as e:
        print(f"  ⚠ FTS rebuild warning: {e}")


def process_file(filepath, selective=False):
    filename = Path(filepath).stem
    print(f"\n  Processing: {filepath} ({'selective' if selective else 'full'} mode)")

    messages = parse_txt_file(filepath)
    if not messages:
        print(f"  ⚠ No messages found in {filepath}")
        return

    print(f"  Found {len(messages)} messages")

    conn = get_conn()
    personality_count = [0]
    exchange_count = [0]
    receipt_count = [0]

    for i, msg in enumerate(messages):
        content = msg["content"]
        role = msg["role"]

        if selective:
            # ── SELECTIVE MODE: keyword filtered ──────────────────────────
            if role == "assistant" and contains_any(content, GARY_VOICE_KEYWORDS):
                insert_personality_sample(conn, content, filename, personality_count)

            if contains_any(content, TB_KEYWORDS):
                insert_receipt(conn, content, filename, role, receipt_count)

            if role == "user" and i + 1 < len(messages) and messages[i+1]["role"] == "assistant":
                gary_msg = messages[i+1]["content"]
                score = sum(k.lower() in gary_msg.lower() for k in GARY_VOICE_KEYWORDS)
                if score >= 2:
                    insert_exchange(conn, content, gary_msg, filename, exchange_count)

        else:
            # ── FULL MODE: ingest everything ──────────────────────────────
            if role == "assistant":
                # Every Gary response is a personality sample
                insert_personality_sample(conn, content, filename, personality_count)

                # Also check for TB receipts in Gary's responses
                if contains_any(content, TB_KEYWORDS):
                    insert_receipt(conn, content, filename, role, receipt_count)

            if role == "user":
                # Every user message that has a Gary response = exchange
                if i + 1 < len(messages) and messages[i+1]["role"] == "assistant":
                    gary_msg = messages[i+1]["content"]
                    insert_exchange(conn, content, gary_msg, filename, exchange_count)

                # TB receipts from user messages too
                if contains_any(content, TB_KEYWORDS):
                    insert_receipt(conn, content, filename, role, receipt_count)

    # Store as conversation summary
    preview = messages[0]["content"][:150] if messages else ""
    h = make_hash(filename + preview)
    try:
        conn.execute(
            "INSERT INTO conversation_summaries (title, message_count, preview, hash) VALUES (?, ?, ?, ?)",
            (filename, len(messages), preview, h)
        )
    except sqlite3.IntegrityError:
        pass

    # Store filename as project conversation
    h2 = make_hash(f"project:{filename}")
    try:
        conn.execute("INSERT INTO project_memory (title, hash) VALUES (?, ?)", (filename, h2))
    except sqlite3.IntegrityError:
        pass

    conn.commit()
    rebuild_fts(conn)
    conn.close()

    print(f"  ✅ Personality samples : {personality_count[0]}")
    print(f"  ✅ Best exchanges      : {exchange_count[0]}")
    print(f"  ✅ TB receipts         : {receipt_count[0]}")


def print_stats():
    conn = get_conn()
    print("\n  Memory totals:")
    for table, label in [
        ("profile_memory",         "Profile facts"),
        ("personality_samples",    "Personality samples"),
        ("exchanges",              "Best exchanges"),
        ("receipts",               "TB receipts"),
        ("conversation_summaries", "Conversation summaries"),
        ("project_memory",         "Project conversations"),
    ]:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"    {label:<28} {n:>6}")
        except Exception:
            pass
    conn.close()


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python txt_parser.py file1.txt file2.txt ...")
        print("       python txt_parser.py --selective file1.txt  (keyword filtered)")
        sys.exit(1)

    selective = False
    if "--selective" in args:
        selective = True
        args = [a for a in args if a != "--selective"]

    files = args
    print(f"\n⚡ Gary Memory — Text File Ingestion ({'selective' if selective else 'FULL'} mode)")
    print(f"   Files to process: {len(files)}")

    for filepath in files:
        if not os.path.exists(filepath):
            print(f"  ⚠ File not found: {filepath}")
            continue
        if not filepath.endswith(".txt"):
            print(f"  ⚠ Skipping non-txt file: {filepath}")
            continue
        process_file(filepath, selective=selective)

    print_stats()
    print("\n✅ Done! Gary's memory has been updated. 🇬🇧")


if __name__ == "__main__":
    main()
