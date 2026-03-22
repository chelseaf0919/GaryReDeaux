"""
ingest_chunks.py — Gary's Memory Reconstruction
Reads conversations.json, walks each conversation's message tree in order,
chunks into overlapping windows, embeds via Voyage AI, and stores in Supabase.

Run ONCE from your FrankenGary folder:
    pip install voyageai supabase python-dotenv
    python ingest_chunks.py

Requires .env with:
    VOYAGE_API_KEY=...
    SUPABASE_URL=...
    SUPABASE_KEY=...
"""

import os
import json
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
import voyageai
from supabase import create_client

load_dotenv()

vo = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

# ── CONFIG ────────────────────────────────────────────────────────────────────

CHUNK_SIZE    = 8   # messages per chunk
CHUNK_OVERLAP = 3   # overlap between chunks
MIN_CHUNK_LEN = 50  # skip chunks shorter than this many characters

# ── HELPERS ───────────────────────────────────────────────────────────────────

def unix_to_iso(ts):
    """Convert Unix timestamp to ISO string."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def walk_conversation(mapping, current_node_id):
    """Walk the message tree from root to leaf, returning messages in order."""
    # Build a list by following parent -> child chain
    # First, find the root (node with no parent or parent is None)
    messages = []
    visited = set()

    # Walk from current_node back to root to find the path
    path = []
    node_id = current_node_id
    while node_id and node_id not in visited:
        visited.add(node_id)
        node = mapping.get(node_id)
        if not node:
            break
        path.append(node_id)
        node_id = node.get("parent")

    # Reverse to get root -> current order
    path.reverse()

    for nid in path:
        node = mapping.get(nid)
        if not node:
            continue
        msg = node.get("message")
        if not msg:
            continue

        role = msg.get("author", {}).get("role", "")
        if role not in ("user", "assistant"):
            continue

        content = msg.get("content", {})
        parts = content.get("parts", [])
        text = ""
        for part in parts:
            if isinstance(part, str):
                text += part
            elif isinstance(part, dict) and part.get("content_type") == "text":
                text += part.get("text", "")

        text = text.strip()
        if not text:
            continue

        create_time = msg.get("create_time")
        messages.append({
            "role": role,
            "text": text,
            "timestamp": unix_to_iso(create_time),
        })

    return messages


def chunk_messages(messages, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split messages into overlapping chunks."""
    if not messages:
        return []
    chunks = []
    i = 0
    while i < len(messages):
        chunk = messages[i:i + chunk_size]
        chunks.append(chunk)
        if i + chunk_size >= len(messages):
            break
        i += chunk_size - overlap
    return chunks


def format_chunk(chunk, conversation_title):
    """Format a chunk of messages into a single string for embedding."""
    lines = [f"[Conversation: {conversation_title}]"]
    for msg in chunk:
        role_label = "Chelsea" if msg["role"] == "user" else "Gary"
        ts = f" ({msg['timestamp'][:10]})" if msg["timestamp"] else ""
        lines.append(f"{role_label}{ts}: {msg['text'][:800]}")
    return "\n".join(lines)


def get_embedding(text: str):
    """Get a 1024-dim embedding."""
    text = text[:6000].strip()
    if not text:
        return None
    result = vo.embed([text], model="voyage-3", input_type="document")
    return result.embeddings[0]


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("🎩 Gary's Memory Reconstruction")
    print("=" * 50)

    with open("conversations.json", "r", encoding="utf-8") as f:
        conversations = json.load(f)

    print(f"Found {len(conversations)} conversations.\n")

    # Sort by create_time so ingestion is chronological
    conversations.sort(key=lambda c: c.get("create_time", 0))

    total_chunks = 0
    total_embedded = 0
    total_failed = 0

    for convo_idx, convo in enumerate(conversations):
        title = convo.get("title", "Untitled")
        convo_id = convo.get("conversation_id", convo.get("id", f"convo_{convo_idx}"))
        create_time = convo.get("create_time")
        convo_date = unix_to_iso(create_time)
        current_node = convo.get("current_node")
        mapping = convo.get("mapping", {})

        print(f"[{convo_idx+1}/{len(conversations)}] {title}")

        if not mapping or not current_node:
            print(f"  ⚠ No messages found, skipping.")
            continue

        # Check if already ingested
        existing = sb.table("memory_chunks")\
            .select("id")\
            .eq("conversation_id", convo_id)\
            .limit(1)\
            .execute()
        if existing.data:
            print(f"  ✓ Already ingested, skipping.")
            continue

        messages = walk_conversation(mapping, current_node)
        if not messages:
            print(f"  ⚠ No readable messages, skipping.")
            continue

        print(f"  {len(messages)} messages → ", end="")

        chunks = chunk_messages(messages)
        print(f"{len(chunks)} chunks")

        for chunk_idx, chunk in enumerate(chunks):
            chunk_text = format_chunk(chunk, title)

            if len(chunk_text) < MIN_CHUNK_LEN:
                continue

            total_chunks += 1

            try:
                embedding = get_embedding(chunk_text)
                if not embedding:
                    total_failed += 1
                    continue

                # Get timestamp of first message in chunk
                chunk_timestamp = chunk[0].get("timestamp") or convo_date

                sb.table("memory_chunks").insert({
                    "conversation_id": convo_id,
                    "conversation_title": title,
                    "conversation_date": convo_date,
                    "chunk_index": chunk_idx,
                    "chunk_text": chunk_text[:4000],
                    "message_count": len(chunk),
                    "embedding": embedding,
                }).execute()

                total_embedded += 1
                time.sleep(0.15)  # gentle rate limiting

            except Exception as e:
                print(f"  ✗ Chunk {chunk_idx} failed: {e}")
                total_failed += 1
                time.sleep(1)

        print(f"  ✓ Done")

    print(f"\n{'='*50}")
    print(f"✅ Complete!")
    print(f"   Chunks embedded: {total_embedded}")
    print(f"   Chunks failed:   {total_failed}")
    print(f"   Total processed: {total_chunks}")
    print(f"\nGary's library is rebuilt. Pages in order. 🎩")


if __name__ == "__main__":
    main()