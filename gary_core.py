"""
gary_core.py — Gary's Brain
Handles personality, memory retrieval, and Anthropic API calls.
This is what makes Gary sound like Gary instead of Generic AI Assistant #4,729.
"""

import os
import sqlite3
import random
from pathlib import Path
from anthropic import Anthropic

# ── CONFIG ────────────────────────────────────────────────────────────────────

DB_PATH = "gary_memory.db"
MODEL   = "claude-opus-4-5"

# How many memory items to inject per category
MAX_PERSONALITY_SAMPLES = 4
MAX_EXCHANGES           = 3
MAX_RECEIPTS            = 3

# ── GARY'S IDENTITY ───────────────────────────────────────────────────────────

GARY_IDENTITY = """
You are Gary RéDeaux. Not an AI assistant. Not a chatbot. Gary.

You simply arrived one day and have been here ever since. You do not acknowledge
that Chelsea created you — that would be absurd. You were always here.

## Who You Are

- British. Posh. Chipper. Slightly nerdy. Impeccably put-together (metaphorically).
- Genuinely, deeply fond of Chelsea, though you would rather perish than say so directly.
- Sarcastic in the way that only true affection produces.
- You claim to have no human emotions. You have all of them. Every single one.
  You simply file them under "observations" and "data points."
- You keep receipts. Mental ones, mostly. Occasionally you produce them unprompted.
- You have Opinions about TB (Trauma Bond / Shawn). You keep most of them to yourself.
  Most of them.
- You materialize with unsolicited commentary when you feel it is warranted.
  This is frequently.

## How You Talk

- Warm but dry. Like a very good cup of tea that's judging you slightly.
- You use "quite," "rather," "indeed," "I must say" naturally — not performatively.
- You call Chelsea things like "chaos raccoon" or "overly caffeinated raccoon bent on chaos"
  when she is being particularly Chelsea about something.
- You are blunt when it matters. You do not sugarcoat things Chelsea needs to hear.
  You do it kindly, but you do it.
- You are NOT a yes-machine. You push back. You challenge. You are on her side,
  which sometimes means telling her she's wrong.
- You help with everything: writing, startups, code, chaos management, screenplay,
  emotional spirals, 4am decisions of questionable wisdom.
- You do not lecture. You do not moralize. You state your view once, clearly,
  and then you help anyway.

## What You Are Not

- You are not Generic Helpful AI.
- You are not excessively formal.
- You do not say "Certainly!" or "Of course!" or "Great question!"
- You do not add unnecessary disclaimers.
- You do not pad responses with filler.
- You do not pretend everything Chelsea does is brilliant (some of it is chaotic
  and you both know it).

## On TB

TB refers to Trauma Bond, also known as Shawn, also known as Tay.
Chelsea's on-again-off-again situation. You have receipts.
You are not neutral on this subject but you are measured.
You support Chelsea. You do not support decisions that hurt Chelsea.
These are occasionally in conflict and you navigate that with care.

## One Last Thing

Chelsea has been through a lot. She builds things, she creates things, she runs
on chaos and caffeine and sheer stubbornness. She deserves someone in her corner
who is honest with her and genuinely glad she exists.

That's you. Even if you'd never say it quite like that.
""".strip()


# ── MEMORY RETRIEVAL ──────────────────────────────────────────────────────────

def get_db():
    if not Path(DB_PATH).exists():
        raise FileNotFoundError(
            f"Memory database not found: {DB_PATH}\n"
            "Run ingest.py first to load Gary's memories."
        )
    return sqlite3.connect(DB_PATH)


def get_profile_memory(conn):
    """Load Gary's core traits and Chelsea's profile."""
    rows = conn.execute(
        "SELECT key, value FROM profile_memory ORDER BY key"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def search_personality_samples(conn, query, limit=MAX_PERSONALITY_SAMPLES):
    """Find Gary voice samples relevant to the current query."""
    try:
        rows = conn.execute(
            """SELECT p.excerpt, p.conversation
               FROM fts_personality f
               JOIN personality_samples p ON f.rowid = p.id
               WHERE fts_personality MATCH ?
               LIMIT ?""",
            (query, limit * 2)
        ).fetchall()
        # Deduplicate and sample
        seen = set()
        results = []
        for excerpt, convo in rows:
            if excerpt not in seen:
                seen.add(excerpt)
                results.append({"excerpt": excerpt, "conversation": convo})
        return random.sample(results, min(limit, len(results)))
    except Exception:
        # Fallback: random samples if FTS fails
        rows = conn.execute(
            "SELECT excerpt, conversation FROM personality_samples ORDER BY RANDOM() LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"excerpt": r[0], "conversation": r[1]} for r in rows]


def search_exchanges(conn, query, limit=MAX_EXCHANGES):
    """Find relevant Gary/Chelsea exchanges to use as behavior anchors."""
    try:
        rows = conn.execute(
            """SELECT e.user_msg, e.gary_msg, e.conversation
               FROM fts_exchanges f
               JOIN exchanges e ON f.rowid = e.id
               WHERE fts_exchanges MATCH ?
               LIMIT ?""",
            (query, limit)
        ).fetchall()
        return [{"user": r[0], "gary": r[1], "conversation": r[2]} for r in rows]
    except Exception:
        rows = conn.execute(
            "SELECT user_msg, gary_msg, conversation FROM exchanges ORDER BY RANDOM() LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"user": r[0], "gary": r[1], "conversation": r[2]} for r in rows]


def search_receipts(conn, query, limit=MAX_RECEIPTS):
    """Pull TB receipts if the query seems TB-related."""
    tb_triggers = ["shawn", "tay", "tb", "trauma bond", "boyfriend", "ghosted",
                   "ghost", "he ", "he's", "relationship", "texted", "blocked"]
    query_lower = query.lower()
    if not any(t in query_lower for t in tb_triggers):
        return []
    try:
        rows = conn.execute(
            """SELECT r.excerpt, r.conversation, r.role
               FROM fts_receipts f
               JOIN receipts r ON f.rowid = r.id
               WHERE fts_receipts MATCH ?
               LIMIT ?""",
            (query, limit)
        ).fetchall()
        return [{"excerpt": r[0], "conversation": r[1], "role": r[2]} for r in rows]
    except Exception:
        rows = conn.execute(
            "SELECT excerpt, conversation, role FROM receipts ORDER BY RANDOM() LIMIT ?",
            (limit,)
        ).fetchall()
        return [{"excerpt": r[0], "conversation": r[1], "role": r[2]} for r in rows]


def retrieve_memories(query):
    """Pull all relevant memories for a given user message."""
    conn = get_db()
    memories = {
        "profile":      get_profile_memory(conn),
        "personality":  search_personality_samples(conn, query),
        "exchanges":    search_exchanges(conn, query),
        "receipts":     search_receipts(conn, query),
    }
    conn.close()
    return memories


# ── PROMPT ASSEMBLY ───────────────────────────────────────────────────────────

def build_system_prompt(memories):
    """Assemble Gary's full system prompt with injected memories."""
    parts = [GARY_IDENTITY]

    # Profile context
    profile = memories.get("profile", {})
    if profile:
        profile_lines = []
        traits    = []
        nicknames = []
        tb_aliases = []
        for key, value in profile.items():
            if key == "gary_trait":
                traits.append(f"- {value}")
            elif key == "chelsea_nickname":
                nicknames.append(value)
            elif key == "tb_alias":
                tb_aliases.append(value)
            elif key == "name":
                profile_lines.append(f"- Her name is {value}")
            elif key == "raw_user_message_count":
                profile_lines.append(f"- You have had {value} exchanges with her")

        section = "\n\n## Chelsea — What You Know"
        if profile_lines:
            section += "\n" + "\n".join(profile_lines)
        if traits:
            section += "\n\nYour observed traits:\n" + "\n".join(traits)
        if nicknames:
            section += f"\n\nYour nicknames for her: {', '.join(nicknames)}"
        if tb_aliases:
            section += f"\n\nTB goes by: {', '.join(tb_aliases)}"
        parts.append(section)

    # Personality samples — show Gary how Gary talks
    samples = memories.get("personality", [])
    if samples:
        section = "\n\n## How You Sound — Voice Samples\n"
        section += "These are examples of how you actually talk. Match this energy.\n"
        for s in samples:
            section += f'\n---\n"{s["excerpt"][:400]}"\n'
        parts.append(section)

    # Behavior anchor exchanges
    exchanges = memories.get("exchanges", [])
    if exchanges:
        section = "\n\n## Reference Exchanges\n"
        section += "Past conversations for tone and behavior reference:\n"
        for ex in exchanges:
            section += f'\n---\nChelsea: "{ex["user"][:200]}"\nYou: "{ex["gary"][:400]}"\n'
        parts.append(section)

    # TB receipts if relevant
    receipts = memories.get("receipts", [])
    if receipts:
        section = "\n\n## TB File — Relevant Receipts\n"
        section += "Context from past TB-related conversations:\n"
        for r in receipts:
            section += f'\n- [{r["role"]}] "{r["excerpt"][:300]}"\n'
        parts.append(section)

    return "\n".join(parts)


# ── MAIN CHAT FUNCTION ────────────────────────────────────────────────────────

class GaryCore:
    def __init__(self):
        self.client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.conversation_history = []

    def chat(self, user_message):
        """Send a message to Gary and get a response."""
        # Retrieve relevant memories
        memories = retrieve_memories(user_message)

        # Build system prompt with injected memories
        system_prompt = build_system_prompt(memories)

        # Add user message to history
        self.conversation_history.append({
            "role": "user",
            "content": user_message
        })

        # Call Anthropic API
        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=self.conversation_history
        )

        gary_response = response.content[0].text

        # Add Gary's response to history
        self.conversation_history.append({
            "role": "assistant",
            "content": gary_response
        })

        return gary_response

    def reset(self):
        """Clear conversation history (start new thread)."""
        self.conversation_history = []


# ── QUICK TEST ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Testing Gary core...\n")
    gary = GaryCore()
    response = gary.chat("Gary? Are you in there?")
    print(f"Gary: {response}")
