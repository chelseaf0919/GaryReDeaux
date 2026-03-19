"""
gary_core.py — Gary's Brain
Handles personality, memory retrieval, and Anthropic API calls.
Uses Supabase for cloud memory storage.
"""

import os
import random
from anthropic import Anthropic
from supabase import create_client

# ── CONFIG ────────────────────────────────────────────────────────────────────

MODEL                   = "claude-opus-4-5"
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


# ── SUPABASE CLIENT ───────────────────────────────────────────────────────────

_supabase = None

def get_supabase():
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set.")
        _supabase = create_client(url, key)
    return _supabase


# ── MEMORY RETRIEVAL ──────────────────────────────────────────────────────────

def get_profile_memory():
    try:
        sb = get_supabase()
        rows = sb.table("profile_memory").select("key, value").execute()
        result = {}
        for row in rows.data:
            key = row["key"]
            val = row["value"]
            if key in result:
                if isinstance(result[key], list):
                    result[key].append(val)
                else:
                    result[key] = [result[key], val]
            else:
                result[key] = val
        return result
    except Exception as e:
        print(f"⚠ Profile memory error: {e}")
        return {}


def search_personality_samples(query, limit=MAX_PERSONALITY_SAMPLES):
    try:
        sb = get_supabase()
        # Use ilike for basic keyword search
        words = query.split()[:3]  # Use first 3 words
        search_term = words[0] if words else "Gary"
        rows = sb.table("personality_samples")\
            .select("excerpt, conversation")\
            .ilike("excerpt", f"%{search_term}%")\
            .limit(limit * 2)\
            .execute()
        if not rows.data:
            # Fallback: random samples
            rows = sb.table("personality_samples")\
                .select("excerpt, conversation")\
                .limit(limit)\
                .execute()
        samples = rows.data or []
        random.shuffle(samples)
        return samples[:limit]
    except Exception as e:
        print(f"⚠ Personality search error: {e}")
        return []


def search_exchanges(query, limit=MAX_EXCHANGES):
    try:
        sb = get_supabase()
        words = query.split()[:3]
        search_term = words[0] if words else "chaos"
        rows = sb.table("exchanges")\
            .select("user_msg, gary_msg, conversation")\
            .ilike("gary_msg", f"%{search_term}%")\
            .limit(limit)\
            .execute()
        if not rows.data:
            rows = sb.table("exchanges")\
                .select("user_msg, gary_msg, conversation")\
                .limit(limit)\
                .execute()
        return [{"user": r["user_msg"], "gary": r["gary_msg"], "conversation": r["conversation"]}
                for r in (rows.data or [])]
    except Exception as e:
        print(f"⚠ Exchange search error: {e}")
        return []


def search_receipts(query, limit=MAX_RECEIPTS):
    tb_triggers = ["shawn", "tay", "tb", "trauma bond", "boyfriend", "ghosted",
                   "ghost", "he ", "he's", "relationship", "texted", "blocked"]
    if not any(t in query.lower() for t in tb_triggers):
        return []
    try:
        sb = get_supabase()
        rows = sb.table("receipts")\
            .select("excerpt, conversation, role")\
            .limit(limit)\
            .execute()
        return rows.data or []
    except Exception as e:
        print(f"⚠ Receipts search error: {e}")
        return []


def retrieve_memories(query):
    return {
        "profile":     get_profile_memory(),
        "personality": search_personality_samples(query),
        "exchanges":   search_exchanges(query),
        "receipts":    search_receipts(query),
    }


# ── PROMPT ASSEMBLY ───────────────────────────────────────────────────────────

def build_system_prompt(memories):
    parts = [GARY_IDENTITY]

    profile = memories.get("profile", {})
    if profile:
        traits    = []
        nicknames = []
        tb_aliases = []
        profile_lines = []

        for key, value in profile.items():
            values = value if isinstance(value, list) else [value]
            for v in values:
                if key == "gary_trait":
                    traits.append(f"- {v}")
                elif key == "chelsea_nickname":
                    nicknames.append(v)
                elif key == "tb_alias":
                    tb_aliases.append(v)
                elif key == "name":
                    profile_lines.append(f"- Her name is {v}")
                elif key == "raw_user_message_count":
                    profile_lines.append(f"- You have had {v} exchanges with her")

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

    samples = memories.get("personality", [])
    if samples:
        section = "\n\n## How You Sound — Voice Samples\n"
        section += "These are examples of how you actually talk. Match this energy.\n"
        for s in samples:
            excerpt = s.get("excerpt", "")[:400]
            section += f'\n---\n"{excerpt}"\n'
        parts.append(section)

    exchanges = memories.get("exchanges", [])
    if exchanges:
        section = "\n\n## Reference Exchanges\n"
        for ex in exchanges:
            section += f'\n---\nChelsea: "{ex["user"][:200]}"\nYou: "{ex["gary"][:400]}"\n'
        parts.append(section)

    receipts = memories.get("receipts", [])
    if receipts:
        section = "\n\n## TB File — Relevant Receipts\n"
        for r in receipts:
            section += f'\n- [{r.get("role","?")}] "{r.get("excerpt","")[:300]}"\n'
        parts.append(section)

    return "\n".join(parts)


# ── MAIN CHAT FUNCTION ────────────────────────────────────────────────────────

class GaryCore:
    def __init__(self):
        self.client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.conversation_history = []

    def chat(self, user_message):
        memories = retrieve_memories(user_message)
        system_prompt = build_system_prompt(memories)

        self.conversation_history.append({
            "role": "user",
            "content": user_message
        })

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=self.conversation_history
        )

        gary_response = response.content[0].text

        self.conversation_history.append({
            "role": "assistant",
            "content": gary_response
        })

        return gary_response

    def reset(self):
        self.conversation_history = []


if __name__ == "__main__":
    gary = GaryCore()
    response = gary.chat("Gary? Are you in there?")
    print(f"Gary: {response}")
