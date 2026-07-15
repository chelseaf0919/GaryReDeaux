"""
embed_all_threads.py — Embed all existing FrankenGary threads into memory_chunks
Safe to run anytime — re-embeds threads that have grown since last embed.
    python embed_all_threads.py

Requires .env with:
    VOYAGE_API_KEY=...
    SUPABASE_URL=...
    SUPABASE_KEY=...
"""

import os
import sys
import time
from dotenv import load_dotenv
import voyageai
from supabase import create_client

# Windows console defaults to cp1252, which can't print emoji/unicode thread
# titles -- reconfigure so a fancy title doesn't crash the whole run. Also
# force line buffering so progress shows up live when output is redirected.
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

load_dotenv()

vo = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

CHUNK_SIZE    = 8
CHUNK_OVERLAP = 3
MIN_CHUNK_LEN = 50


def get_embedding(text: str):
    # voyage-3's context window is 32k tokens; cap well under that (~90k chars)
    # only to guard against pathological input, not to trim normal chunks.
    text = text[:90000].strip()
    if not text:
        return None
    try:
        result = vo.embed([text], model="voyage-3", input_type="document")
        return result.embeddings[0]
    except Exception as e:
        print(f"  ! Embedding error, skipping chunk: {e}")
        return None


def embed_thread(thread_id, thread_title):
    convo_id = f"thread_{thread_id}"

    # Load messages first -- we need the count for the staleness check
    msgs = sb.table("thread_messages")\
        .select("role, content, created_at")\
        .eq("thread_id", thread_id)\
        .order("id")\
        .execute()

    messages = msgs.data or []
    if len(messages) < 2:
        print(f"  - Too short, skipping.")
        return 0

    # Check if already embedded AND up to date
    existing = sb.table("memory_chunks")\
        .select("chunk_index, message_count")\
        .eq("conversation_id", convo_id)\
        .order("chunk_index", desc=True)\
        .limit(1)\
        .execute()

    if existing.data:
        # Last chunk starts at chunk_index * (CHUNK_SIZE - CHUNK_OVERLAP)
        # and covers message_count messages. Anything past that wasn't embedded.
        last = existing.data[0]
        step = CHUNK_SIZE - CHUNK_OVERLAP
        estimated_covered = (last.get("chunk_index") or 0) * step + (last.get("message_count") or 0)

        if len(messages) <= estimated_covered:
            print(f"  = Already embedded and current, skipping.")
            return 0

        # Thread has grown -- wipe old chunks and re-embed fresh
        print(f"  ~ Thread grew ({estimated_covered} -> {len(messages)} messages), re-embedding...")
        sb.table("memory_chunks")\
            .delete()\
            .eq("conversation_id", convo_id)\
            .execute()

    # Format messages
    formatted = []
    for m in messages:
        role_label = "Chelsea" if m["role"] == "user" else "Gary"
        ts = f" ({m.get('created_at','')[:10]})" if m.get('created_at') else ""
        formatted.append({
            "label": f"{role_label}{ts}",
            "text": m["content"],
            "timestamp": m.get("created_at", ""),
        })

    # Chunk with overlap
    chunks = []
    i = 0
    while i < len(formatted):
        chunk = formatted[i:i + CHUNK_SIZE]
        chunks.append(chunk)
        if i + CHUNK_SIZE >= len(formatted):
            break
        i += CHUNK_SIZE - CHUNK_OVERLAP

    convo_date = formatted[0].get("timestamp") or None
    embedded = 0

    for chunk_idx, chunk in enumerate(chunks):
        lines = [f"[Conversation: {thread_title}]"]
        for msg in chunk:
            lines.append(f"{msg['label']}: {msg['text']}")
        chunk_text = "\n".join(lines)

        if len(chunk_text) < MIN_CHUNK_LEN:
            continue

        embedding = get_embedding(chunk_text)
        if not embedding:
            continue

        sb.table("memory_chunks").insert({
            "conversation_id": convo_id,
            "conversation_title": thread_title,
            "conversation_date": convo_date,
            "chunk_index": chunk_idx,
            "chunk_text": chunk_text,
            "message_count": len(chunk),
            "embedding": embedding,
        }).execute()

        embedded += 1
        time.sleep(0.15)

    return embedded


def main():
    print("Embedding all FrankenGary threads into memory_chunks")
    print("=" * 55)

    threads = sb.table("threads")\
        .select("id, title, updated_at")\
        .order("updated_at", desc=True)\
        .execute()

    all_threads = threads.data or []
    print(f"Found {len(all_threads)} threads.\n")

    total_chunks = 0
    refreshed = 0

    for i, thread in enumerate(all_threads):
        tid = thread["id"]
        title = thread["title"] or "Untitled"
        date = (thread.get("updated_at") or "")[:10]
        print(f"[{i+1}/{len(all_threads)}] '{title}' ({date})")

        chunks = embed_thread(tid, title)
        total_chunks += chunks
        if chunks:
            refreshed += 1
            print(f"  + {chunks} chunks embedded")

    print(f"\n{'='*55}")
    print(f"Done! {total_chunks} chunks embedded across {refreshed} threads.")
    print(f"Gary remembers everything now. For real this time.")


if __name__ == "__main__":
    main()