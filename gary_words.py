"""
gary_words.py - Gary's rarity check for words he suggests in Outlier.

Outlier tiers come from how many players have found a word, which nobody outside
the game can see, so this only rules words out. Tested on 143 words Chelsea
reported, none used to set the cutoffs: at 1.5 and above (inflections borrow their
root's score, KVETCHED -> KVETCH), 12 of 14 were epic or lower. Below that the
score says little: 79-80% were legendary or outlier, near the list's 74% overall,
and 22 of the 37 epic or rare words scored zero. Her reported tiers are the real
evidence, so they're shown alongside and get better as she adds more.

Zero also covers words that don't exist, since wordfreq has no data on either.
So a word also has to be recognized: in data/word_list.txt.gz (ENABLE, Wordnik,
and dwyl's english-words combined) or in Chelsea's own tier reports, which
prove the game accepted it. About 1 in 5 of her real words aren't in the list,
so "unrecognized" means unverified, not fake.
"""

import re
import gzip
from pathlib import Path

from wordfreq import zipf_frequency

WORD_LIST = Path(__file__).parent / "data" / "word_list.txt.gz"
MAX_WORDS = 40

# Inflections only. Word-forming suffixes make a different, often much rarer
# word: VETCHLINGS and KITSCHIFY are outliers though VETCH and KITSCH aren't.
INFLECTIONS = [("iest", ["y"]), ("ier", ["y"]), ("ies", ["y"]), ("ied", ["y"]),
               ("est", ["", "e"]), ("er", ["", "e"]), ("es", ["", "e"]),
               ("ed", ["", "e"]), ("ing", ["", "e"]), ("s", [""])]

TIERS = "outlier|legendary|leg|epic|rare|uncommon|common"
# word (outlier), word(leg), word (leg/encore), word (outlier, but an encore)
# or a list, one per line or comma-separated: Word- outlier, Word-leg, Word: epic
TIER_REPORT = re.compile(
    rf"\b([A-Za-z]{{3,}})\s*\(({TIERS})\s*[),/]"
    rf"|(?:^|[,;])[ \t]*([A-Za-z]{{3,}})[ \t]*[-–—:][ \t]*({TIERS})[ \t]*(?=$|[,;])",
    re.I | re.M,
)
REPORT_SEARCH = ",".join(
    f'content.ilike."%{mark}{tier}%"'
    for tier in TIERS.split("|") if tier != "legendary"  # "leg" already covers it
    for mark in ("(", "-", "- ", ": ")
)


def frequency(word: str) -> float:
    return zipf_frequency(word, "en", wordlist="large")


def inflection_roots(word: str) -> set:
    roots = set()
    for suffix, endings in INFLECTIONS:
        stem = word[:-len(suffix)]
        if word.endswith(suffix) and len(stem) >= 3:
            roots.update(stem + ending for ending in endings)
            if stem[-1] == stem[-2]:  # KNOTTED -> KNOT
                roots.add(stem[:-1])
    return roots


def rarity_score(word: str):
    """(score, root): the word's frequency, or its root's if that's higher, on
    the Zipf scale (0 = no data, about 3 = uncommon, 5+ = everyday)."""
    best, root = frequency(word), None
    for candidate in inflection_roots(word):
        if frequency(candidate) > best:
            best, root = frequency(candidate), candidate
    return best, root


def rarity_label(score: float) -> str:
    if score >= 4:
        return "Common"
    if score >= 3:
        return "Uncommon"
    if score >= 2:
        return "Rare"
    if score > 0:
        return "Very Rare"
    return "Essentially Unseen"


def in_word_list(words) -> set:
    """Scans the file rather than holding ~450k strings in memory between calls."""
    with gzip.open(WORD_LIST, "rt", encoding="utf-8") as f:
        return set(words).intersection(line.strip() for line in f)


def reported_tiers() -> dict:
    """Word -> the tier Chelsea most recently reported for it."""
    from gary_core import get_supabase
    try:
        rows = get_supabase().table("thread_messages").select("content")\
            .eq("role", "user").or_(REPORT_SEARCH)\
            .order("id", desc=True).limit(1000).execute().data or []
    except Exception as e:
        print(f"Couldn't load reported tiers: {e}")
        return {}
    tiers = {}
    for row in rows:  # newest first, so the latest report wins
        for match in reversed(TIER_REPORT.findall(row["content"])):
            word, tier = match[0] or match[2], match[1] or match[3]
            tier = "legendary" if tier.lower() == "leg" else tier.lower()
            tiers.setdefault(word.lower(), tier)
    return tiers


def suffix_variants(word: str) -> set:
    base = word[:-1] if word.endswith("e") else word
    variants = {word + "s", word + "es", base + "ed", base + "er", base + "est", base + "ing"}
    if word.endswith("y"):
        stem = word[:-1]
        variants |= {stem + "ies", stem + "ied", stem + "ier", stem + "iest"}
    variants.discard(word)
    return variants


CHECK_RARITY_TOOL = {
    "name": "check_word_rarity",
    "description": (
        "Check candidate words for Chelsea's word game Outlier. Call this BEFORE "
        "suggesting any word to her, or calling one rare, with your candidates "
        "together in one call, then suggest only the ones that hold up. Your own "
        "sense of what's rare has been unreliable. Returns, rarest first, each "
        "word's frequency score, a label, a verdict, and the tier Chelsea last "
        "reported for it, if any. A reported tier beats everything else, though "
        "tiers shift daily. Without one, this can only rule words out: 'not ruled "
        "out' is no promise, and plenty of those turn out epic, so never claim a "
        "tier yourself. An unverified word may not be real, so say so if you offer "
        "it at all."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "words": {"type": "array", "items": {"type": "string"},
                      "description": f"Candidate words, up to {MAX_WORDS}."},
            "include_variants": {"type": "boolean",
                                 "description": "Also check recognized -s, -es, -ed, -er, -est, -ing, -ier, -iest forms of each word."},
        },
        "required": ["words"],
    },
}


def verdict(score: float, recognized: bool) -> str:
    if score >= 3:
        return "too common, skip it"
    if score >= 1.5:
        return "probably epic at best"
    if score > 0 or recognized:
        return "not ruled out: could be epic, legendary, or outlier"
    return "unverified, may not be a real word"


def execute_check_word_rarity(tool_input: dict) -> str:
    words = {w.strip().lower() for w in tool_input.get("words") or []}
    words = sorted(w for w in words if re.fullmatch(r"[a-z]+", w))[:MAX_WORDS]
    if not words:
        return "Error: give at least one word, letters only."
    variants = set()
    if tool_input.get("include_variants"):
        variants = {v for w in words for v in suffix_variants(w)} - set(words)

    tiers = reported_tiers()
    known = in_word_list(set(words) | variants) | (tiers.keys() & (set(words) | variants))
    # Naive suffixes mostly produce non-words, so only keep the real ones.
    checked = set(words) | (variants & known)

    results = []
    for word in checked:
        score, root = rarity_score(word)
        results.append((score, word not in known, word, root))
    results.sort()

    lines = []
    for score, unrecognized, word, root in results:
        line = (f"{word}: {score:.2f} ({rarity_label(score)}) -- {verdict(score, not unrecognized)}"
                + (f" | scored from its root, {root}" if root else ""))
        if word in tiers:
            line += f" | Chelsea last reported: {tiers[word]}"
        lines.append(line)
    return "\n".join(lines)
