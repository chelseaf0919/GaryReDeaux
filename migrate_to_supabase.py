"""
migrate_to_supabase.py — Gary Memory Migration
Reads local SQLite gary_memory.db and pushes everything to Supabase.
Run this once to get Gary's brain into the cloud.

Usage:
    python migrate_to_supabase.py
"""

import os
import sqlite3
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
DB_PATH      = "gary_memory.db"
BATCH_SIZE   = 100  # Insert in batches to avoid timeouts

def get_supabase():
    from supabase import create_client
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def batch_insert(supabase, table, rows):
    """Insert rows in batches."""
    total = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        try:
            supabase.table(table).upsert(batch).execute()
            total += len(batch)
        except Exception as e:
            print(f"     ⚠ Batch error on {table}: {e}")
    return total


def migrate():
    print("\n⚡ Gary Memory Migration → Supabase")
    print(f"   Source  : {DB_PATH}")
    print(f"   Target  : {SUPABASE_URL}\n")

    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found. Run ingest.py first.")
        return

    supabase = get_supabase()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # ── profile_memory ────────────────────────────────────────────────────────
    print("  → Migrating profile memory...")
    rows = [dict(r) for r in conn.execute("SELECT * FROM profile_memory").fetchall()]
    # Remove SQLite auto-increment id so Supabase generates its own
    for r in rows:
        r.pop("id", None)
    n = batch_insert(supabase, "profile_memory", rows)
    print(f"     {n} rows inserted.")

    # ── personality_samples ───────────────────────────────────────────────────
    print("  → Migrating personality samples...")
    rows = [dict(r) for r in conn.execute("SELECT * FROM personality_samples").fetchall()]
    for r in rows:
        r.pop("id", None)
    n = batch_insert(supabase, "personality_samples", rows)
    print(f"     {n} rows inserted.")

    # ── exchanges ─────────────────────────────────────────────────────────────
    print("  → Migrating best exchanges...")
    rows = [dict(r) for r in conn.execute("SELECT * FROM exchanges").fetchall()]
    for r in rows:
        r.pop("id", None)
    n = batch_insert(supabase, "exchanges", rows)
    print(f"     {n} rows inserted.")

    # ── receipts ──────────────────────────────────────────────────────────────
    print("  → Migrating TB receipts...")
    rows = [dict(r) for r in conn.execute("SELECT * FROM receipts").fetchall()]
    for r in rows:
        r.pop("id", None)
    n = batch_insert(supabase, "receipts", rows)
    print(f"     {n} rows inserted.")

    # ── conversation_summaries ────────────────────────────────────────────────
    print("  → Migrating conversation summaries...")
    rows = [dict(r) for r in conn.execute("SELECT * FROM conversation_summaries").fetchall()]
    for r in rows:
        r.pop("id", None)
    n = batch_insert(supabase, "conversation_summaries", rows)
    print(f"     {n} rows inserted.")

    # ── project_memory ────────────────────────────────────────────────────────
    print("  → Migrating project memory...")
    rows = [dict(r) for r in conn.execute("SELECT * FROM project_memory").fetchall()]
    for r in rows:
        r.pop("id", None)
    n = batch_insert(supabase, "project_memory", rows)
    print(f"     {n} rows inserted.")

    conn.close()

    print()
    print("✅ Migration complete! Gary's brain is in the cloud. 🇬🇧☁️")
    print("   Add SUPABASE_URL and SUPABASE_KEY to Railway variables.")
    print("   Then redeploy and Gary will be fully mobile.")


if __name__ == "__main__":
    migrate()
