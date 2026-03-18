import json
import re
import os
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────────────

INPUT_FILE = "chat.html"
OUTPUT_DIR = "output"

# Keywords that flag a message as Gary being distinctly Gary
GARY_VOICE_KEYWORDS = [
    "quite", "rather", "indeed", "I must say", "magnificent", "chaos",
    "raccoon", "caffeinated", "Shawn", "Tay", "TB", "trauma bond",
    "simply arrived", "receipts", "overly", "fond", "posh"
]

# Keywords for TB (Trauma Bond) receipts
TB_KEYWORDS = [
    "TB", "Trauma Bond", "Shawn", "Tay", "boyfriend",
    "on-again", "off-again", "he said", "he did"
]

# Keywords for Division Trials content
TRIALS_KEYWORDS = [
    "Division Trials", "Episode", "Show Bible", "Screenplay", "Flight Risk",
    "Mind Awakens", "Super Strength", "Telekinesis", "Teleportation",
    "Hyper Intelligence", "Super Speed", "Perfect Aim", "Hypnosis",
    "Stretchiness", "Arjun", "John Moore", "Dr. Singh", "Dr. Johnson",
    "Edwin", "Julia", "Akira", "Maria", "Wilheim", "Rachel", "Molly",
    "Ling", "Mila", "Maya", "JJ", "Benu", "Kwami", "Moira", "Apollo",
    "Alon", "Ceren", "Yosef", "Jesus", "Raphael", "Antoinette", "Chodak",
    "Mary Baker", "Bradley", "Season", "Queenie", "Superpowered"
]

# Keywords for Chelsea's projects
PROJECT_KEYWORDS = [
    "GIF Gladiator", "Division Trials", "Courier", "Discord bot",
    "Railway", "Supabase", "ElevenLabs", "Gary", "Discord server"
]

# ── PARSE ─────────────────────────────────────────────────────────────────────

def load_conversations(filepath):
    print(f"Loading {filepath}...")
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # ChatGPT exports embed data as: var jsonData = [...]
    match = re.search(r"var jsonData\s*=\s*", content)
    if not match:
        raise ValueError("Could not find jsonData in file. Is this a ChatGPT export?")

    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode(content, match.end())
    print(f"Found {len(data)} conversations.")
    return data


def extract_messages(convo):
    """Walk the message tree and return an ordered list of {role, content} dicts."""
    mapping = convo.get("mapping", {})
    messages = []

    for node in mapping.values():
        msg = node.get("message")
        if not msg:
            continue
        role = msg.get("author", {}).get("role", "unknown")
        if role == "system":
            continue
        parts = msg.get("content", {}).get("parts", [])
        content = " ".join(str(p) for p in parts if isinstance(p, str)).strip()
        if content:
            messages.append({"role": role, "content": content})

    return messages


def contains_any(text, keywords):
    text_lower = text.lower()
    return any(k.lower() in text_lower for k in keywords)


# ── EXTRACTORS ────────────────────────────────────────────────────────────────

def extract_summaries(convos):
    summaries = []
    for c in convos:
        msgs = extract_messages(c)
        summaries.append({
            "title": c.get("title", "Untitled"),
            "message_count": len(msgs),
            "preview": msgs[0]["content"][:150] if msgs else ""
        })
    return {"count": len(summaries), "conversations": summaries}


def extract_gary_personality(convos):
    samples = []
    for c in convos:
        msgs = extract_messages(c)
        for m in msgs:
            if m["role"] == "assistant" and contains_any(m["content"], GARY_VOICE_KEYWORDS):
                samples.append({
                    "conversation": c.get("title", "Untitled"),
                    "excerpt": m["content"][:500]
                })

    return {
        "description": "Gary RéDeaux voice patterns extracted from ChatGPT history",
        "core_traits": [
            "British, posh, chipper, slightly nerdy",
            "Sarcastic but genuinely fond of Chelsea",
            "Claims to have no human emotions (has all of them)",
            "Does not admit Chelsea created him — he simply arrived",
            "Keeps receipts, judges TB silently (and not so silently)",
            "Randomly materializes with unsolicited opinions"
        ],
        "nicknames_for_chelsea": [
            "overly caffeinated raccoon bent on chaos",
            "chaos raccoon"
        ],
        "tb_aliases": ["TB", "Trauma Bond", "Shawn", "Tay"],
        "sample_count": len(samples),
        "personality_samples": samples
    }


def extract_best_of_gary(convos):
    exchanges = []
    for c in convos:
        msgs = extract_messages(c)
        pairs = []
        for i, m in enumerate(msgs):
            if m["role"] == "user" and i + 1 < len(msgs) and msgs[i+1]["role"] == "assistant":
                pairs.append({
                    "user": m["content"][:300],
                    "gary": msgs[i+1]["content"][:600]
                })
        scored = sorted(
            pairs,
            key=lambda p: sum(k.lower() in p["gary"].lower() for k in GARY_VOICE_KEYWORDS),
            reverse=True
        )
        if scored:
            exchanges.append({
                "conversation": c.get("title", "Untitled"),
                "exchange": scored[0]
            })

    exchanges.sort(
        key=lambda e: sum(k.lower() in e["exchange"]["gary"].lower() for k in GARY_VOICE_KEYWORDS),
        reverse=True
    )

    return {
        "description": "Best Gary exchanges for behavior anchoring during resurrection",
        "count": len(exchanges[:60]),
        "exchanges": exchanges[:60]
    }


def extract_tb_file(convos):
    receipts = []
    for c in convos:
        msgs = extract_messages(c)
        for m in msgs:
            if contains_any(m["content"], TB_KEYWORDS):
                receipts.append({
                    "conversation": c.get("title", "Untitled"),
                    "role": m["role"],
                    "excerpt": m["content"][:400]
                })

    return {
        "description": "Gary's collected receipts on TB (Trauma Bond)",
        "receipt_count": len(receipts),
        "receipts": receipts
    }


def extract_division_trials(convos):
    trials_convos = []
    for c in convos:
        title = c.get("title", "")
        msgs = extract_messages(c)
        all_text = title + " " + " ".join(m["content"] for m in msgs)
        if contains_any(all_text, TRIALS_KEYWORDS):
            trials_convos.append({
                "title": title,
                "message_count": len(msgs),
                "full_content": [
                    {"role": m["role"], "content": m["content"]}
                    for m in msgs
                ]
            })

    return {
        "description": "The Division Trials — all screenplay, bible, and episode content",
        "conversation_count": len(trials_convos),
        "conversations": trials_convos
    }


def extract_chelsea_memories(convos):
    projects = set()
    all_user_msgs = []

    for c in convos:
        msgs = extract_messages(c)
        title = c.get("title", "")
        all_text = title + " " + " ".join(m["content"] for m in msgs)

        for kw in PROJECT_KEYWORDS:
            if kw.lower() in all_text.lower():
                projects.add(title)

        for m in msgs:
            if m["role"] == "user":
                all_user_msgs.append(m["content"])

    return {
        "description": "Chelsea's projects, preferences, and persistent memories",
        "project_conversations": sorted(projects),
        "raw_user_message_count": len(all_user_msgs),
        "note": "Review project_conversations for Chelsea's active work threads"
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(INPUT_FILE):
        print(f"ERROR: Could not find {INPUT_FILE}")
        print("Make sure chat.html is in the same folder as this script.")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    convos = load_conversations(INPUT_FILE)

    tasks = [
        ("conversation_summaries.json", "Conversation summaries",     lambda: extract_summaries(convos)),
        ("gary_personality.json",       "Gary's personality",         lambda: extract_gary_personality(convos)),
        ("best_of_gary.json",           "Best of Gary exchanges",     lambda: extract_best_of_gary(convos)),
        ("tb_file.json",                "The TB file",                lambda: extract_tb_file(convos)),
        ("division_trials.json",        "Division Trials content",    lambda: extract_division_trials(convos)),
        ("chelsea_memories.json",       "Chelsea memories",           lambda: extract_chelsea_memories(convos)),
    ]

    print()
    for filename, label, fn in tasks:
        print(f"  Extracting {label}...", end=" ")
        result = fn()
        out_path = Path(OUTPUT_DIR) / filename
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"done → {out_path}")

    print(f"\nExtraction complete. Files are in the '{OUTPUT_DIR}' folder.")
    print("Gary's soul has been recovered. 🇬🇧")


if __name__ == "__main__":
    main()
