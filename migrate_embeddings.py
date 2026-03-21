"""
migrate_embeddings.py — Gary's Memory Embedding Migration
Loops through all rows in exchanges, personality_samples, and conversation_summaries,
generates embeddings via Voyage AI, and stores them in Supabase.

Run this ONCE from your local machine:
    pip install voyageai supabase python-dotenv
    python migrate_embeddings.py

Requires .env with:
    VOYAGE_API_KEY=...
    SUPABASE_URL=...
    SUPABASE_KEY=...
"""

import os
import time
import voyageai
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

vo = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def get_embedding(text: str):
    """Get a 1024-dim embedding for a piece of text."""
    text = text[:4000].strip()
    if not text:
        return None
    result = vo.embed([text], model="voyage-3", input_type="document")
    return result.embeddings[0]


def embed_table(table: str, text_fields: list, id_field: str = "id"):
    """Embed all rows in a table that don't have embeddings yet."""
    print(f"\n── {table} ──────────────────────────────")

    rows = sb.table(table)\
        .select(f"{id_field}, {', '.join(text_fields)}, embedding")\
        .is_("embedding", "null")\
        .execute()

    total = len(rows.data or [])
    print(f"Found {total} rows to embed...")

    if total == 0:
        print("Nothing to do.")
        return

    success = 0
    failed = 0

    for i, row in enumerate(rows.data):
        parts = []
        for field in text_fields:
            val = row.get(field, "") or ""
            if val:
                parts.append(val.strip())
        combined = " | ".join(parts)

        try:
            embedding = get_embedding(combined)
            if embedding is None:
                print(f"  [{i+1}/{total}] Row {row[id_field]} — skipped (empty text)")
                failed += 1
                continue

            sb.table(table)\
                .update({"embedding": embedding})\
                .eq(id_field, row[id_field])\
                .execute()

            success += 1
            if (i + 1) % 25 == 0:
                print(f"  [{i+1}/{total}] {success} done, {failed} failed...")

            # Rate limiting — be gentle with the API
            time.sleep(0.1)

        except Exception as e:
            print(f"  [{i+1}/{total}] Row {row[id_field]} FAILED: {e}")
            failed += 1
            time.sleep(1)

    print(f"  Done: {success} embedded, {failed} failed")


if __name__ == "__main__":
    print("Gary's Memory Embedding Migration")
    print("=====================================")

    embed_table(
        table="exchanges",
        text_fields=["user_msg", "gary_msg"],
    )

    embed_table(
        table="personality_samples",
        text_fields=["excerpt"],
    )

    embed_table(
        table="conversation_summaries",
        text_fields=["preview", "title"],
    )

    print("\nMigration complete. Gary's memory is now semantic.")
