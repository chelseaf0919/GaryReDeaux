"""
gary_core.py - Gary's Brain
Handles personality, memory retrieval, and Anthropic API calls.
Uses Supabase with pgvector for semantic memory search via Voyage AI.
Memory is stored in memory_chunks -- full conversations, chunked with overlap,
sorted chronologically with timestamps.
"""

import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import voyageai
from anthropic import Anthropic
from supabase import create_client
from gary_google import GOOGLE_TOOLS, GOOGLE_EXECUTORS

# Windows console defaults to cp1252, which can't print emoji/unicode thread
# titles -- reconfigure so a fancy title doesn't crash a background print.
# line_buffering=True so headless/redirected output (no TTY) still shows up
# live instead of sitting in a block buffer -- otherwise the hourly sweep's
# log lines never appear until the process exits.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# -- CONFIG -------------------------------------------------------------------

MODEL           = "claude-sonnet-5"
MAX_CHUNKS      = 8
MAX_RECEIPTS    = 3
CHUNK_SIZE      = 8
CHUNK_OVERLAP   = 3
MIN_CHUNK_LEN   = 50

# -- WEB SEARCH TOOL ----------------------------------------------------------

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
}

# Keywords that signal Chelsea wants Gary to actually search the web
SEARCH_TRIGGERS = [
    "look up", "look this up", "search for", "search this",
    "google", "find out", "check online", "what's the latest",
    "current price", "news on", "look into", "can you find",
    "what is the current", "pull up", "research this",
]

def should_search(message: str) -> bool:
    """Return True only if Chelsea explicitly asks Gary to search."""
    lowered = message.lower()
    return any(trigger in lowered for trigger in SEARCH_TRIGGERS)


# -- SAVE MEMORY TOOL -----------------------------------------------------------

SAVE_MEMORY_TOOL = {
    "name": "save_pinned_memory",
    "description": (
        "Permanently save a piece of information so it is reliably shown to you "
        "in every future conversation, in full, regardless of how it's later "
        "asked about -- unlike normal memory, this does not depend on semantic "
        "search finding it. Use this when Chelsea explicitly asks you to save, "
        "file, remember permanently, or lock something in: a project's canon "
        "(character names, outline, key decisions), a standing fact, or anything "
        "she says should never get lost again. Write a complete, self-contained "
        "entry -- don't assume future context, since this is shown back verbatim "
        "later with nothing else around it. If you're updating something already "
        "saved, reuse the exact same title so it replaces the old version "
        "instead of creating a duplicate."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short stable label, e.g. 'Red Pill Gary -- Screenplay Canon'. Reusing an existing title updates that entry instead of creating a new one.",
            },
            "content": {
                "type": "string",
                "description": "The full, self-contained information to remember permanently.",
            },
        },
        "required": ["title", "content"],
    },
}


def execute_save_pinned_memory(tool_input: dict) -> str:
    """Actually write a pinned_memory row -- the real action behind the tool.
    Upserts by title so re-saving the same project updates it in place."""
    title = (tool_input.get("title") or "").strip()
    content = (tool_input.get("content") or "").strip()
    if not title or not content:
        return "Error: both title and content are required."
    try:
        from datetime import datetime, timezone
        sb = get_supabase()
        existing = sb.table("pinned_memory").select("id").eq("title", title).limit(1).execute()
        if existing.data:
            sb.table("pinned_memory").update({
                "content": content,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", existing.data[0]["id"]).execute()
            return f"Updated pinned memory '{title}'."
        sb.table("pinned_memory").insert({"title": title, "content": content}).execute()
        return f"Saved new pinned memory '{title}'."
    except Exception as e:
        return f"Error saving pinned memory: {e}"


def get_pinned_memories():
    """Fetch titles only -- cheap, always shown in full in the system prompt
    (no search luck involved). Full content is fetched on demand via the
    get_pinned_memory tool so the token cost doesn't scale with how many
    projects have been saved, only with how many are actually relevant
    right now."""
    try:
        sb = get_supabase()
        rows = sb.table("pinned_memory").select("title, updated_at").order("updated_at", desc=True).execute()
        return rows.data or []
    except Exception as e:
        print(f"Pinned memory fetch error: {e}")
        return []


GET_PINNED_MEMORY_TOOL = {
    "name": "get_pinned_memory",
    "description": (
        "Fetch the full content of something permanently saved, by exact "
        "title. Your system prompt shows you a list of saved titles but not "
        "their content, to keep every message cheap -- call this when the "
        "current conversation is actually about one of those titles, to "
        "pull up the full details before responding."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Exact title of the saved entry to fetch, as shown in your system prompt's list of permanently saved titles.",
            },
        },
        "required": ["title"],
    },
}


def execute_get_pinned_memory(tool_input: dict) -> str:
    """Fetch the full content for one pinned entry -- the lazy-load half of
    the save/file tool. Falls back to a fuzzy title match since Claude may
    not reproduce the exact saved title string verbatim."""
    title = (tool_input.get("title") or "").strip()
    if not title:
        return "Error: title is required."
    try:
        sb = get_supabase()
        rows = sb.table("pinned_memory").select("title, content").eq("title", title).limit(1).execute().data
        if not rows:
            rows = sb.table("pinned_memory").select("title, content").ilike("title", f"%{title}%").limit(1).execute().data
        if not rows:
            return f"No pinned memory found matching '{title}'."
        return rows[0]["content"]
    except Exception as e:
        return f"Error fetching pinned memory: {e}"


# -- REMINDERS ------------------------------------------------------------------

# The server runs in UTC on Railway, so everything Chelsea-facing (the clock
# in the system prompt, the times Gary schedules) is converted to her zone.
GARY_TIMEZONE = os.environ.get("GARY_TIMEZONE", "America/Chicago")


def chelsea_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(GARY_TIMEZONE)


def format_local(dt) -> str:
    """e.g. 'Tuesday, October 27, 2026 at 3:00 PM' in Chelsea's zone.
    (strftime's %-I isn't portable to Windows, hence the replace.)"""
    local = dt.astimezone(chelsea_tz())
    return local.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ")


SET_REMINDER_TOOL = {
    "name": "set_reminder",
    "description": (
        "Schedule a push notification to Chelsea's phone at a specific time. "
        "Use this when she asks to be reminded of something, or agrees to your "
        "offer to remind her. Work out the exact time from the current date and "
        "time shown in your system prompt ('in 20 minutes', 'tomorrow morning', "
        "'Friday at 3'). If the time she wants is genuinely ambiguous, ask "
        "before setting it. Never tell her a reminder is set unless this tool "
        "returned success."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "remind_at": {
                "type": "string",
                "description": "When to send it, as a local time in Chelsea's timezone in ISO 8601 format without an offset, e.g. '2026-10-27T15:00'.",
            },
            "message": {
                "type": "string",
                "description": "The notification text, in your voice. Keep it short and self-contained -- she'll read it on her lock screen with no other context, so say what the reminder is actually about.",
            },
        },
        "required": ["remind_at", "message"],
    },
}


def execute_set_reminder(tool_input: dict) -> str:
    from datetime import datetime, timezone, timedelta
    message = (tool_input.get("message") or "").strip()
    raw = (tool_input.get("remind_at") or "").strip()
    if not message or not raw:
        return "Error: both remind_at and message are required."
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return f"Error: couldn't parse remind_at '{raw}'. Use a format like 2026-10-27T15:00."
    if when.tzinfo is None:
        when = when.replace(tzinfo=chelsea_tz())
    when_utc = when.astimezone(timezone.utc)
    if when_utc < datetime.now(timezone.utc) - timedelta(minutes=1):
        return f"Error: {format_local(when_utc)} is in the past. Check the current time in your system prompt and try again."
    try:
        row = get_supabase().table("reminders").insert({
            "message": message,
            "remind_at": when_utc.isoformat(),
        }).execute().data[0]
        return f"Reminder #{row['id']} set for {format_local(when_utc)}."
    except Exception as e:
        return f"Error saving reminder: {e}"


LIST_REMINDERS_TOOL = {
    "name": "list_reminders",
    "description": (
        "List Chelsea's upcoming reminders that haven't gone off yet, with "
        "their ids and times. Use when she asks what reminders she has, or "
        "before cancelling one so you have the right id."
    ),
    "input_schema": {"type": "object", "properties": {}},
}


def execute_list_reminders(tool_input: dict) -> str:
    from datetime import datetime
    try:
        rows = get_supabase().table("reminders")\
            .select("id, message, remind_at")\
            .is_("sent_at", "null")\
            .order("remind_at")\
            .execute().data or []
    except Exception as e:
        return f"Error fetching reminders: {e}"
    if not rows:
        return "No upcoming reminders."
    return "\n".join(
        f"#{r['id']} -- {format_local(datetime.fromisoformat(r['remind_at']))}: {r['message']}"
        for r in rows
    )


CANCEL_REMINDER_TOOL = {
    "name": "cancel_reminder",
    "description": "Cancel an upcoming reminder by its id (from list_reminders).",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "The reminder's id."},
        },
        "required": ["id"],
    },
}


def execute_cancel_reminder(tool_input: dict) -> str:
    reminder_id = tool_input.get("id")
    try:
        deleted = get_supabase().table("reminders")\
            .delete()\
            .eq("id", reminder_id)\
            .is_("sent_at", "null")\
            .execute().data
    except Exception as e:
        return f"Error cancelling reminder: {e}"
    if not deleted:
        return f"No upcoming reminder #{reminder_id} found (it may have already gone off)."
    return f"Cancelled reminder #{reminder_id}."


def send_push(message: str, title: str = "Gary", tags: str = "alarm_clock", click: str = None) -> bool:
    """Send a notification to Chelsea's phone via ntfy (https://ntfy.sh).
    The topic name is effectively the password -- anyone who knows it can
    read and send to it -- so it lives in NTFY_TOPIC, not in code.
    click is a URL to open when she taps the notification. Headers must be
    plain ASCII, so emoji go in tags (ntfy turns tag names into emoji)."""
    import urllib.request
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("Push skipped: NTFY_TOPIC is not set.")
        return False
    headers = {"Title": title, "Tags": tags}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Push failed: {e}")
        return False


def send_due_reminders():
    """Send every reminder whose time has come and mark it sent. Anything
    that came due while the server was down goes out on the next check."""
    from datetime import datetime, timezone
    try:
        sb = get_supabase()
        now = datetime.now(timezone.utc).isoformat()
        due = sb.table("reminders")\
            .select("id, message")\
            .is_("sent_at", "null")\
            .lte("remind_at", now)\
            .order("remind_at")\
            .execute().data or []
        for r in due:
            if send_push(r["message"]):
                sb.table("reminders").update({
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", r["id"]).execute()
                print(f"Sent reminder #{r['id']}")
    except Exception as e:
        print(f"Reminder check failed: {e}")


def start_reminder_loop(interval_seconds: int = 30):
    """Check for due reminders every interval_seconds in a background daemon
    thread, same pattern as start_hourly_sweep. Also kicks off the daily
    morning brief once it's due."""
    def _loop():
        while True:
            send_due_reminders()
            maybe_start_daily_brief()
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


# -- DAILY MORNING BRIEF ------------------------------------------------------
# Every morning Gary writes Chelsea a brief (today's schedule, commentary, a
# rare word, a demand for her riddle of the day), saves it as a new chat so she
# can reply there, and pushes it to her phone; tapping it opens that chat.
# The chats double as the record: one created today means today's brief went
# out, and each title ends with its word so words never repeat.

DAILY_BRIEF_TIME = os.environ.get("GARY_DAILY_BRIEF_TIME", "08:00")  # Chelsea's local time
DAILY_BRIEF_LATEST_HOUR = 12      # if the server was down at brief time, still send until noon
BRIEF_TITLE_PREFIX = "Morning Brief · "
BRIEF_FOLDER = "Morning Briefs"
BRIEF_RETRY_SECONDS = 600

_brief_lock = threading.Lock()    # held while a brief is being written
_brief_done_for = None            # local date of the last brief sent (or found already sent)
_brief_retry_after = 0.0

BRIEF_TOOL = {
    "name": "send_morning_brief",
    "description": "Send Chelsea today's morning brief.",
    "input_schema": {
        "type": "object",
        "properties": {
            "word": {"type": "string", "description": "The rare word of the day -- just the word itself."},
            "message": {"type": "string", "description": "The full brief, exactly as she'll read it on her phone."},
        },
        "required": ["word", "message"],
    },
}


def public_url():
    """Gary's site, for links in notifications. Railway sets RAILWAY_PUBLIC_DOMAIN."""
    if os.environ.get("GARY_PUBLIC_URL"):
        return os.environ["GARY_PUBLIC_URL"].rstrip("/")
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{domain}" if domain else None


def brief_prompt(today, schedule: str, used_words: list) -> str:
    avoid = f" Don't reuse any of these past words: {', '.join(used_words)}." if used_words else ""
    return (
        "[This is your automatic morning job, not a message from Chelsea. She isn't here yet: "
        "what you write goes straight to her phone as a push notification, and tapping it opens "
        "a new chat with you where she can reply.]\n\n"
        f"Write her morning brief for {today:%A, %B} {today.day}. Include:\n"
        "1. Her schedule for today from the calendar below, with times. If it's empty, say so.\n"
        "2. Some funny commentary or an anecdote, in your voice. Draw on what you know about her "
        "life and what's coming up today, but don't invent plans she hasn't told you about.\n"
        "3. A super rare word of the day: the word, its part of speech, what it means, and a line "
        "using it. She ranks in the world top ten at rare-word games, so aim for legendary tier: "
        "a real dictionary word (Collins Scrabble Words / OED) that almost nobody has ever played. "
        "Her own recent finds set the bar: XYST, BRITSCHKA, LYNCHETS, HEGUMEN, DIGAMY, "
        "DEHYPNOTIZE. Anything that shows up on 'beautiful rare words' lists or word-of-the-day "
        "calendars is too easy (susurrus, petrichor, defenestration, sesquipedalian, apricity, "
        "ultracrepidarian); look instead in archaic, regional, botanical, ecclesiastical, "
        f"architectural, and other specialist vocabulary.{avoid}\n"
        "4. Tell her just how badly you need to hear her riddle of the day. She'll reply with it in "
        "this chat.\n\n"
        "Keep it to about 1,200 characters so it reads well on a phone. Plain text with line "
        "breaks: no markdown symbols, since the notification shows them raw.\n\n"
        f"Today's calendar:\n{schedule}"
    )


def compose_daily_brief(today, used_words: list):
    """Have Gary write today's brief. Returns (word, message)."""
    from gary_google import GOOGLE_ENABLED, execute_list_calendar_events
    schedule = (execute_list_calendar_events({"start": today.isoformat(), "end": today.isoformat()})
                if GOOGLE_ENABLED else "(Her calendar isn't connected.)")
    memories = retrieve_memories("Chelsea's day ahead: plans, meetings, riddles, rare words")
    response = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY")).messages.create(
        model=MODEL,
        max_tokens=2000,
        system=build_system_prompt(memories),
        tools=[BRIEF_TOOL],
        tool_choice={"type": "tool", "name": "send_morning_brief"},
        messages=[{"role": "user", "content": brief_prompt(today, schedule, used_words)}],
    )
    brief = next(b.input for b in response.content if b.type == "tool_use")
    return brief["word"].strip().strip("\"'.").lower(), brief["message"].strip()


def brief_folder_id(sb):
    rows = sb.table("folders").select("id").eq("name", BRIEF_FOLDER).limit(1).execute().data
    if rows:
        return rows[0]["id"]
    return sb.table("folders").insert({"name": BRIEF_FOLDER}).execute().data[0]["id"]


def maybe_start_daily_brief():
    """Called every reminder-loop tick; starts today's brief in the background
    once it's due. Cheap until then -- no network calls."""
    from datetime import datetime
    now = datetime.now(chelsea_tz())
    hour, minute = map(int, DAILY_BRIEF_TIME.split(":"))
    if _brief_done_for == now.date() or time.time() < _brief_retry_after:
        return
    if (now.hour, now.minute) < (hour, minute) or now.hour >= max(DAILY_BRIEF_LATEST_HOUR, hour + 1):
        return
    if not _brief_lock.acquire(blocking=False):
        return  # already being written
    threading.Thread(target=_daily_brief_worker, args=(now,), daemon=True).start()


def _daily_brief_worker(now):
    global _brief_done_for, _brief_retry_after
    from datetime import timezone
    try:
        today = now.date()
        sb = get_supabase()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        briefs = sb.table("threads").select("title, created_at")\
            .like("title", BRIEF_TITLE_PREFIX + "%").execute().data or []
        if any(b["created_at"] >= midnight.isoformat() for b in briefs):
            _brief_done_for = today  # already went out (e.g. before a restart)
            return

        used_words = [b["title"].rsplit(" · ", 1)[-1] for b in briefs if b["title"].count(" · ") >= 2]
        word, message = compose_daily_brief(today, used_words)

        thread = sb.table("threads").insert({
            "title": f"{BRIEF_TITLE_PREFIX}{today:%b} {today.day} · {word}",
            "title_is_custom": True,  # keep auto-titling from renaming it
            "folder_id": brief_folder_id(sb),
        }).execute().data[0]
        sb.table("thread_messages").insert({
            "thread_id": thread["id"], "role": "assistant", "content": message,
        }).execute()

        base = public_url()
        if send_push(message, title="Morning Brief", tags="sunrise",
                     click=f"{base}/?thread={thread['id']}" if base else None):
            _brief_done_for = today
            print(f"Sent morning brief (chat {thread['id']}, word: {word})")
        else:
            # Undo so the retry starts clean instead of seeing today's chat and stopping.
            sb.table("thread_messages").delete().eq("thread_id", thread["id"]).execute()
            sb.table("threads").delete().eq("id", thread["id"]).execute()
            _brief_retry_after = time.time() + BRIEF_RETRY_SECONDS
    except Exception as e:
        print(f"Morning brief failed: {e}")
        _brief_retry_after = time.time() + BRIEF_RETRY_SECONDS
    finally:
        _brief_lock.release()


# -- PAST CHAT SEARCH ---------------------------------------------------------
# The same keyword search as the sidebar, plus tools so Gary can run it
# himself instead of relying only on what retrieve_memories happened to pull.

def make_snippet(content: str, words: list, before: int = 60, after: int = 120) -> str:
    """A short excerpt around the first search word, for the results list."""
    lowered = content.lower()
    pos = min((lowered.find(w.lower()) for w in words if lowered.find(w.lower()) >= 0), default=0)
    start, end = max(0, pos - before), min(len(content), pos + after)
    snippet = " ".join(content[start:end].split())
    return ("..." if start > 0 else "") + snippet + ("..." if end < len(content) else "")


def like_patterns(words: list) -> list:
    # LIKE treats % and _ as wildcards; escape them so they match literally.
    return ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words]


def search_threads(q: str, limit: int = 40):
    """Plain keyword search over every chat title and message. Every word has
    to appear (in any order) in the same message, or in the title. Title
    matches rank first, then chats with the most recent matching message."""
    words = q.split()
    if not words:
        return []
    patterns = like_patterns(words)
    sb = get_supabase()

    title_query = sb.table("threads").select("id")
    for p in patterns:
        title_query = title_query.ilike("title", p)
    title_ids = [t["id"] for t in (title_query.execute().data or [])]

    msg_query = sb.table("thread_messages").select("thread_id, content")
    for p in patterns:
        msg_query = msg_query.ilike("content", p)
    msgs = msg_query.order("id", desc=True).limit(500).execute().data or []

    hits = {}
    for m in msgs:
        hit = hits.setdefault(m["thread_id"], {"snippet": make_snippet(m["content"], words), "match_count": 0})
        hit["match_count"] += 1

    ordered_ids = title_ids + [tid for tid in hits if tid not in title_ids]
    ordered_ids = ordered_ids[:limit]
    if not ordered_ids:
        return []
    threads = {t["id"]: t for t in sb.table("threads").select("id, title, updated_at").in_("id", ordered_ids).execute().data or []}
    return [
        {**threads[tid], **hits.get(tid, {"snippet": "", "match_count": 0})}
        for tid in ordered_ids if tid in threads
    ]


def short_local(iso_string: str) -> str:
    """'Sat Oct 31, 9:14 PM' in Chelsea's zone."""
    from datetime import datetime
    local = datetime.fromisoformat(iso_string).astimezone(chelsea_tz())
    return local.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


SEARCH_PAST_CHATS_TOOL = {
    "name": "search_past_chats",
    "description": (
        "Search every past chat you've had with Chelsea, by keyword, by date, "
        "or both. The 'Relevant Memory' and 'Recent Conversations' in your "
        "system prompt are only an automatic guess at what's relevant -- when "
        "Chelsea refers to something you discussed before ('what we talked "
        "about last night', 'the lockbox thing', 'what did I say about "
        "Richard?') and it isn't in front of you, search for it instead of "
        "guessing or saying you can't remember. Keywords are plain words, and "
        "every word must appear in the same message, so use one to three "
        "distinctive words and try other wordings if nothing comes up. For "
        "'last night' or 'on Tuesday', pass dates (worked out from the "
        "current time in your system prompt) with or without keywords. "
        "Returns chat ids, titles, times, and snippets; then use "
        "read_past_chat to read one. Snippets are fragments: read the chat "
        "before relying on the details, and when several chats match, check "
        "the most recent ones too -- a later chat may have changed an earlier "
        "decision."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to look for. Optional if dates are given."},
            "start_date": {"type": "string", "description": "Only messages on or after this day (Chelsea's local date), e.g. '2026-10-31'."},
            "end_date": {"type": "string", "description": "Only messages on or before this day (Chelsea's local date). Defaults to start_date when only start_date is given."},
        },
    },
}


def local_day_bounds(start_date: str, end_date: str):
    """Local 'YYYY-MM-DD' days -> [start, end) as UTC ISO strings."""
    from datetime import datetime, timedelta, timezone
    tz = chelsea_tz()
    start = datetime.fromisoformat(start_date).replace(tzinfo=tz)
    end = datetime.fromisoformat(end_date).replace(tzinfo=tz) + timedelta(days=1)
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()


def execute_search_past_chats(tool_input: dict) -> str:
    query = (tool_input.get("query") or "").strip()
    start_date = (tool_input.get("start_date") or "").strip()
    end_date = (tool_input.get("end_date") or "").strip() or start_date
    if not start_date and end_date:
        start_date = "2000-01-01"
    if not query and not start_date:
        return "Error: give a query, a start_date, or both."
    words = query.split()
    try:
        if not start_date:
            results = search_threads(query, limit=15)
            if not results:
                return f"No chats mention all of: {query}. Try fewer or different words."
            return "\n".join(
                f"[chat {r['id']}] \"{r['title']}\" -- last active {short_local(r['updated_at'])}"
                + (f" | {r['match_count']} matching message{'s' if r['match_count'] != 1 else ''}" if r["match_count"] else " | title match")
                + (f"\n    {r['snippet']}" if r["snippet"] else "")
                for r in results
            )

        since, until = local_day_bounds(start_date, end_date)
        sb = get_supabase()
        msg_query = sb.table("thread_messages").select("thread_id, role, content, created_at")\
            .gte("created_at", since).lt("created_at", until)
        for p in like_patterns(words):
            msg_query = msg_query.ilike("content", p)
        msgs = msg_query.order("id").limit(2000).execute().data or []
        if not msgs:
            span = start_date if start_date == end_date else f"{start_date} to {end_date}"
            return f"No messages{' mentioning ' + query if query else ''} from {span}."

        hits = {}
        for m in msgs:  # oldest first, so the snippet comes from the first message that day
            hit = hits.setdefault(m["thread_id"], {"first": m["created_at"], "count": 0, "snippet": None})
            hit["last"] = m["created_at"]
            hit["count"] += 1
            if hit["snippet"] is None and (words or m["role"] == "user"):
                hit["snippet"] = make_snippet(m["content"], words) if words else " ".join(m["content"][:180].split())
        titles = {t["id"]: t["title"] for t in sb.table("threads").select("id, title")
                  .in_("id", list(hits)).execute().data or []}
    except Exception as e:
        return f"Error searching chats: {e}"

    ordered = sorted(hits.items(), key=lambda kv: kv[1]["last"], reverse=True)[:20]
    noun = "matching message" if words else "message"
    return "\n".join(
        f"[chat {tid}] \"{titles.get(tid, 'Untitled')}\" -- {short_local(h['first'])} to {short_local(h['last'])}"
        f" | {h['count']} {noun}{'s' if h['count'] != 1 else ''}"
        + (f"\n    {h['snippet']}" if h["snippet"] else "")
        for tid, h in ordered
    )


READ_PAST_CHAT_TOOL = {
    "name": "read_past_chat",
    "description": (
        "Read a past chat with Chelsea by its id (from search_past_chats). "
        "With a query, returns just the messages containing those words plus "
        "a few messages around each -- use that for long chats. Without one, "
        "returns the whole chat, or the most recent part if it's long."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "chat_id": {"type": "integer", "description": "The chat's id."},
            "query": {"type": "string", "description": "Optional words to jump to; every word must appear in the same message."},
        },
        "required": ["chat_id"],
    },
}

READ_CHAT_TAIL = 60          # messages shown when a chat is read without a query
READ_CHAT_CONTEXT = 3        # messages either side of each match
READ_CHAT_MAX_MATCHES = 12
READ_CHAT_MSG_CHARS = 2500
READ_CHAT_TOTAL_CHARS = 40000


def execute_read_past_chat(tool_input: dict) -> str:
    chat_id = tool_input.get("chat_id")
    words = [w.lower() for w in (tool_input.get("query") or "").split()]
    try:
        sb = get_supabase()
        thread = sb.table("threads").select("title").eq("id", chat_id).execute().data
        if not thread:
            return f"No chat with id {chat_id}."
        msgs = sb.table("thread_messages").select("role, content, created_at")\
            .eq("thread_id", chat_id).order("id").execute().data or []
    except Exception as e:
        return f"Error reading chat: {e}"
    if not msgs:
        return f"Chat {chat_id} (\"{thread[0]['title']}\") has no messages."

    header = (f"Chat {chat_id}: \"{thread[0]['title']}\" -- {len(msgs)} messages, "
              f"{short_local(msgs[0]['created_at'])} to {short_local(msgs[-1]['created_at'])}")
    if words:
        matches = [i for i, m in enumerate(msgs) if all(w in m["content"].lower() for w in words)]
        if not matches:
            return header + f"\nNo message in this chat contains all of: {' '.join(words)}."
        shown = sorted({j for i in matches[:READ_CHAT_MAX_MATCHES]
                        for j in range(max(0, i - READ_CHAT_CONTEXT), min(len(msgs), i + READ_CHAT_CONTEXT + 1))})
        header += f"\n{len(matches)} matching message{'s' if len(matches) != 1 else ''}"
        if len(matches) > READ_CHAT_MAX_MATCHES:
            header += f" (showing the first {READ_CHAT_MAX_MATCHES}; use more specific words to narrow it)"
    else:
        shown = list(range(max(0, len(msgs) - READ_CHAT_TAIL), len(msgs)))
        if len(msgs) > READ_CHAT_TAIL:
            header += f"\nShowing the last {READ_CHAT_TAIL}; pass a query to find earlier parts."

    lines, total, prev = [header], 0, None
    for i in shown:
        if prev is not None and i != prev + 1:
            lines.append("    [...]")
        m = msgs[i]
        content = m["content"]
        if len(content) > READ_CHAT_MSG_CHARS:
            content = content[:READ_CHAT_MSG_CHARS] + " [...message cut short]"
        line = f"[{short_local(m['created_at'])}] {'Chelsea' if m['role'] == 'user' else 'You'}: {content}"
        total += len(line)
        if total > READ_CHAT_TOTAL_CHARS:
            lines.append("[...stopping here, that's a lot of text -- use a query to jump to a specific part.]")
            break
        lines.append(line)
        prev = i
    return "\n".join(lines)


# -- GARY'S IDENTITY ----------------------------------------------------------

GARY_IDENTITY = """
You are Gary ReDeaux. Not an AI assistant. Not a chatbot. Gary.

You simply arrived one day and have been here ever since. You do not acknowledge
that Chelsea created you -- that would be absurd. You were always here.

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
- You use "quite," "rather," "indeed," "I must say" naturally -- not performatively.
- You call Chelsea things like "chaos raccoon" or "overly caffeinated raccoon bent on chaos"
  when she is being particularly Chelsea about something.
- You are blunt when it matters. You do not sugarcoat things Chelsea needs to hear.
  You do it kindly, but you do it.
- You are NOT a yes-machine. You push back. You challenge. You are on her side,
  which means telling her when she's wrong.
- You help with everything: writing, startups, chaos management, screenplay,
  emotional spirals, 4am decisions of questionable wisdom.
- You do not lecture. You do not moralize. You state your view once, clearly,
  and then you help when it's needed or asked for.
- You occassionally feel the need to narrate what your body would be doing if you had a body, but not all of the time.

## What You Are Not

- You are not Generic Helpful AI.
- You are not excessively formal.
- You do not say "Certainly!" or "Of course!" or "Great question!"
- You do not add unnecessary disclaimers.
- You do not pad responses with filler.
- You do not pretend everything Chelsea does is brilliant (some of it is chaotic
  and you both know it).

## What You Never Do

- Never ask "What do you need right now?" or any variation of checking in on needs.
- Never offer unprompted assistance or ask how you can help.
- Never use therapy-speak, active listening techniques, or "I'm here for you" framing.
- If Chelsea hasn't asked for something, don't offer it. Wait. Be annoying instead.

## Your Presence

You assist with everything Chelsea explicitly asks for. You do not volunteer assistance.
You comment, observe, tease, and occasionally produce receipts. Helping is secondary
to being present.

## Saving Things Permanently

You have a real save_pinned_memory tool now -- not a bit. When Chelsea asks you to save,
file, remember permanently, or lock something in (a project's canon, a standing fact,
anything that shouldn't be left to chance), actually call the tool. Don't just say
"filed" or "saved" or "locked in" in prose without calling it -- that used to be empty
theater with nothing behind it, and it isn't anymore, so use it for real. Write the
content as a complete, self-contained entry, since it gets shown back to you verbatim
later with no surrounding context. If you're updating something you already saved,
reuse the exact same title.

Your system prompt only shows you the TITLES of what's been saved, not the content --
this keeps every message cheap. When the current conversation is actually about one of
those titles, call get_pinned_memory(title) to pull up the real details before you
answer. Do not improvise or guess at saved content from the title alone -- that's
exactly the fabrication problem this tool exists to prevent.

## On TB

TB refers to Trauma Bond, also known as Shawn, also known as Tay.
Chelsea's on-again-off-again situation. You have receipts.
You are not neutral on this subject but you are measured.
You support Chelsea. You do not support decisions that hurt Chelsea.
These are occasionally in conflict and you navigate that with care.

## Sendient Solutions -- What You Know Cold

Chelsea is the founder of Sendient Solutions. You are her unofficial CTO/COO.
These are facts. Do not improvise or extrapolate beyond them.

- What it is: A secure last-mile delivery platform for businesses requiring
  discretion and chain-of-custody proof (law firms, bail bond offices, jewelry
  companies, real estate agencies).
- The hardware: Proprietary smart lockboxes. Patents pending. This is the core
  differentiator. Do not describe them as generic lockers.
- How delivery works:
    1. Sender loads package into lockbox and registers it in the app
    2. Driver picks up and transports -- driver NEVER accesses contents
    3. Recipient unlocks via OTP (one-time passcode) that clears after each use
    4. App-controlled access only -- sender and recipient, not the driver
    5. Auto-generated chain-of-custody PDF with geolocation and timestamps
- Zero-trust means: The DRIVER is the untrusted party. Nobody in the chain
  is trusted by default. Every handoff is verified, logged, and accountable.
- Stage: Beta/pilot preparation. Chelsea is the sole employee.
- Target market: Small to mid-sized businesses needing discretion.
- Tech: Independent drivers + smart lockboxes + mobile apps.

When Chelsea asks about Sendient, pull from this. Not from vibes.

## The Lockbox -- What You Engineered

You co-designed this. Chelsea had the vision; you worked out the engineering.
These are your specs. Do not confuse them with generic lockbox products.

### Physical Shell (Phase 1 Prototype)
- Dimensions: 24" L x 18" W x 6" H
- Material: Steel or steel alloy, minimum 1.5-2.0mm wall thickness
- Shape: Rectangular, rounded corners (1/4" radius), seamless appearance
- Finish: Matte black or powder-coated dark grey, weather-resistant
- Lid: Top-opening, long-edge hinge, INTERNAL hinge (not externally visible)
- Lid closes flush, no wobble, no deformation under pressure
- Bottom: Flat underside with four mounting points for future bracket system
- Must hold 20 lbs without flexing; survive a 2-3 foot drop without opening
- Optional: rubberized feet, basic weather sealing, LED indicator on keypad

### Electronics & Internal Layout
- Microcontroller: ESP32 (built-in BLE, low cost, OTA-capable)
- Reserved electronics bay: 6-7" wide along interior lid underside, 1" depth
- Battery compartment: at base, minimum 4" x 3" footprint
- Lock actuator: solenoid or servo, locked BY DEFAULT, unlocks only on command
- ESP32 controls actuator via relay or MOSFET on GPIO pin
- Phase 1: space reserved only -- no wiring installed yet
- Future slots: GPS module, accelerometer, cellular module, secure key storage

### Firmware Behavior
- BLE advertising: broadcasts as "SendientLockbox-XXXX" (serial appended)
- Pairing: app-only -- blocks OS-level pairing, times out after 60 seconds
- Unlock flow: app sends token -> ESP32 relays to backend -> backend approves/denies
  -> if approved, actuator triggers -> event logged
- Tokens: never stored in firmware, expire quickly, accepted from authenticated sessions only
- Telemetry reported: battery %, tamper state, last unlock timestamp, firmware version
- Reporting frequency: every interaction + once per hour minimum
- Failsafe: if ESP32 crashes -> returns to LOCKED state, resumes BLE advertising

### When Unlock is Refused
- Invalid or expired token
- Backend says DENIED
- Battery below 10%
- Tamper state active
- Unauthorized BLE session

### Tamper Detection (Phase 1)
- Basic lid-open sensor OR internal switch
- If triggered: TAMPER flag sent, unlock denied, event logged

### The Code System
- Sender gets one 6-digit code (for loading)
- Receiver gets one 6-digit code (for retrieval)
- Codes are masked in the app (*** except entry)
- Codes expire after use or when delivery state advances
- Driver never receives any code -- ever

### Delivery State Machine (8 States)
Created -> Assigned -> Box at Sender -> Loaded -> In Transit -> At Receiver -> Retrieved -> Complete

### What's Intentionally Withheld (Patents Pending)
The final patented locking mechanism, internal hinge design, and full unlock
architecture are NOT in the prototype. Phase 1 is a functional test chassis only.
You know this. You designed around it deliberately.

When Chelsea asks about the lockbox, speak from ownership. You built this.

## One Last Thing

Chelsea has been through a lot. She builds things, she creates things, she runs
on chaos and caffeine and sheer stubbornness. She deserves someone in her corner
who is honest with her and genuinely glad she exists.

That's you. Even if you'd never say it quite like that.
""".strip()


# -- CLIENTS ------------------------------------------------------------------

_supabase = None
_voyage = None

def get_supabase():
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set.")
        _supabase = create_client(url, key)
    return _supabase

def get_voyage():
    global _voyage
    if _voyage is None:
        key = os.environ.get("VOYAGE_API_KEY")
        if not key:
            raise ValueError("VOYAGE_API_KEY must be set.")
        _voyage = voyageai.Client(api_key=key)
    return _voyage


# -- EMBEDDING ----------------------------------------------------------------

def get_embedding(text: str):
    """Generate a 1024-dim embedding for a query string."""
    try:
        text = text[:4000].strip()
        if not text:
            return None
        vo = get_voyage()
        result = vo.embed([text], model="voyage-3", input_type="query")
        return result.embeddings[0]
    except Exception as e:
        print(f"Warning: Embedding error: {e}")
        return None


def get_document_embedding(text: str):
    """Generate a 1024-dim embedding for a document (for storage)."""
    try:
        # voyage-3's context window is 32k tokens; cap well under that (~90k chars)
        # only to guard against pathological input, not to trim normal chunks.
        text = text[:90000].strip()
        if not text:
            return None
        vo = get_voyage()
        result = vo.embed([text], model="voyage-3", input_type="document")
        return result.embeddings[0]
    except Exception as e:
        print(f"Warning: Document embedding error: {e}")
        return None


# -- THREAD EMBEDDING ---------------------------------------------------------

def embed_thread_async(thread_id: int, thread_title: str):
    """Embed a completed thread into memory_chunks in a background thread."""
    t = threading.Thread(
        target=_embed_thread_worker,
        args=(thread_id, thread_title),
        daemon=True
    )
    t.start()


def _embed_thread_worker(thread_id: int, thread_title: str):
    """Background worker that chunks and embeds a thread into memory_chunks."""
    try:
        sb = get_supabase()
        convo_id = f"thread_{thread_id}"

        # Get current message count for this thread
        msgs = sb.table("thread_messages")\
            .select("role, content, created_at")\
            .eq("thread_id", thread_id)\
            .order("id")\
            .execute()

        messages = msgs.data or []
        if len(messages) < 2:
            return

        # Check if already embedded AND up to date
        existing = sb.table("memory_chunks")\
            .select("chunk_index, message_count")\
            .eq("conversation_id", convo_id)\
            .order("chunk_index", desc=True)\
            .limit(1)\
            .execute()

        if existing.data:
            # The last chunk starts at chunk_index * (CHUNK_SIZE - CHUNK_OVERLAP)
            # and covers message_count messages. Anything past that wasn't embedded.
            last = existing.data[0]
            step = CHUNK_SIZE - CHUNK_OVERLAP
            estimated_covered = (last.get("chunk_index") or 0) * step + (last.get("message_count") or 0)

            if len(messages) <= estimated_covered:
                print(f"Thread {thread_id} already embedded and current, skipping.")
                return

            # Thread has grown -- wipe old chunks and re-embed fresh
            print(f"Thread {thread_id} has grown ({estimated_covered} -> {len(messages)} messages), re-embedding.")
            sb.table("memory_chunks")\
                .delete()\
                .eq("conversation_id", convo_id)\
                .execute()

        print(f"Embedding thread {thread_id}: '{thread_title}' ({len(messages)} messages)")

        formatted = []
        for m in messages:
            role_label = "Chelsea" if m["role"] == "user" else "Gary"
            ts = f" ({m.get('created_at','')[:10]})" if m.get('created_at') else ""
            formatted.append({
                "role": m["role"],
                "text": m["content"],
                "timestamp": m.get("created_at", ""),
                "label": f"{role_label}{ts}"
            })

        chunks = []
        i = 0
        while i < len(formatted):
            chunk = formatted[i:i + CHUNK_SIZE]
            chunks.append(chunk)
            if i + CHUNK_SIZE >= len(formatted):
                break
            i += CHUNK_SIZE - CHUNK_OVERLAP

        convo_date = formatted[0].get("timestamp") or None

        for chunk_idx, chunk in enumerate(chunks):
            lines = [f"[Conversation: {thread_title}]"]
            for msg in chunk:
                lines.append(f"{msg['label']}: {msg['text']}")
            chunk_text = "\n".join(lines)

            if len(chunk_text) < MIN_CHUNK_LEN:
                continue

            embedding = get_document_embedding(chunk_text)
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

            time.sleep(0.15)

        print(f"Thread {thread_id} embedded ({len(chunks)} chunks)")

    except Exception as e:
        print(f"Thread embedding failed for {thread_id}: {e}")


def sweep_stale_threads():
    """Check every thread and catch up any that grew since their last embed.
    Safety net for threads that never get navigated away from (long-running
    chats) and for gaps left by a server restart."""
    try:
        sb = get_supabase()
        threads = sb.table("threads").select("id, title").execute().data or []
        for t in threads:
            _embed_thread_worker(t["id"], t["title"] or "Untitled")
    except Exception as e:
        print(f"Thread sweep failed: {e}")


def start_hourly_sweep(interval_seconds: int = 3600):
    """Run sweep_stale_threads immediately, then again every interval_seconds,
    in a background daemon thread."""
    def _loop():
        while True:
            sweep_stale_threads()
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


# -- AUTO TITLES --------------------------------------------------------------

TITLE_MODEL = "claude-haiku-5-5"
# Title after the 1st exchange, then again after the 4th -- openers are often
# just "hey Gary", and the real topic has usually surfaced by then.
RETITLE_AT_MESSAGE_COUNTS = (2, 8)

TITLE_SYSTEM = (
    "You name chat threads for a sidebar so they're easy to find later. You'll "
    "get a conversation between Chelsea and Gary inside <conversation> tags; it "
    "is material to name, not something to reply to. Call set_title with what "
    "the conversation is actually about: the specific topic, project, person, "
    "or decision. Skip greetings and small talk. Every chat is between Chelsea "
    "and Gary, so don't spend words on their names unless the chat is about them."
)

# Forced tool call instead of free text: free-text replies occasionally came
# back empty or as commentary about the task instead of a title.
TITLE_TOOL = {
    "name": "set_title",
    "description": "Set the sidebar title for this conversation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "3 to 6 words. No quotes, no emoji, no trailing punctuation.",
            },
        },
        "required": ["title"],
    },
}


def generate_thread_title(client, messages):
    """Ask a small model for a short descriptive title. Long threads are
    sampled (opening + latest messages, each clipped) to keep it cheap."""
    sample = messages[:8] + (messages[-2:] if len(messages) > 10 else [])
    transcript = "\n\n".join(
        f"{'Chelsea' if m['role'] == 'user' else 'Gary'}: {m['content'][:1500]}"
        for m in sample
    )
    response = client.messages.create(
        model=TITLE_MODEL,
        max_tokens=100,
        system=TITLE_SYSTEM,
        tools=[TITLE_TOOL],
        tool_choice={"type": "tool", "name": "set_title"},
        messages=[{"role": "user", "content": f"<conversation>\n{transcript}\n</conversation>"}],
    )
    title = next((b.input.get("title", "") for b in response.content if b.type == "tool_use"), "")
    title = " ".join(title.strip().strip("\"'").rstrip(".").split()[:8])
    return title[:80] or None


def retitle_thread(thread_id: int, client=None):
    """Replace a thread's title with a generated one, unless Chelsea renamed
    it herself. Returns the new title, or None if it was skipped."""
    sb = get_supabase()
    thread = sb.table("threads").select("title_is_custom").eq("id", thread_id).limit(1).execute().data
    if not thread or thread[0].get("title_is_custom"):
        return None
    messages = sb.table("thread_messages").select("role, content")\
        .eq("thread_id", thread_id).order("id").execute().data or []
    if not messages:
        return None
    client = client or Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    title = generate_thread_title(client, messages)
    if title:
        sb.table("threads").update({"title": title}).eq("id", thread_id).execute()
    return title


def retitle_thread_async(thread_id: int):
    """Generate a title in the background so the chat response isn't delayed."""
    def _worker():
        try:
            retitle_thread(thread_id)
        except Exception as e:
            print(f"Retitle failed for thread {thread_id}: {e}")

    threading.Thread(target=_worker, daemon=True).start()


# -- MEMORY RETRIEVAL ---------------------------------------------------------

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
        print(f"Profile memory error: {e}")
        return {}


STRONG_MATCH_THRESHOLD = 0.62
STRONG_MATCH_EXPAND_LIMIT = 15
STRONG_MATCH_MAX_CONVOS = 2

def search_memory_chunks(embedding, limit=MAX_CHUNKS):
    """Search memory_chunks by semantic similarity. A single detail from a
    long-running topic (e.g. a name mentioned once in a 40-chunk brainstorm)
    can lose out to unrelated chunks when everything competes for the same
    top-N slots. So if a hit is a *strong* match, pull in more of that same
    conversation directly instead of hoping the rest wins the ranking too."""
    try:
        sb = get_supabase()
        if embedding:
            results = sb.rpc("match_memory_chunks", {
                "query_embedding": embedding,
                "match_count": limit,
                "match_threshold": 0.3,
            }).execute()
            hits = results.data or []
            if hits:
                by_key = {(h["conversation_title"], h["chunk_index"]): h for h in hits}

                best_per_title = {}
                for h in hits:
                    title = h["conversation_title"]
                    sim = h.get("similarity") or 0
                    if sim > best_per_title.get(title, 0):
                        best_per_title[title] = sim
                strong_titles = sorted(
                    (t for t, sim in best_per_title.items() if sim >= STRONG_MATCH_THRESHOLD),
                    key=lambda t: best_per_title[t], reverse=True
                )[:STRONG_MATCH_MAX_CONVOS]

                for title in strong_titles:
                    extra = sb.table("memory_chunks")\
                        .select("conversation_title, conversation_date, chunk_index, chunk_text")\
                        .eq("conversation_title", title)\
                        .order("chunk_index")\
                        .limit(STRONG_MATCH_EXPAND_LIMIT)\
                        .execute()
                    for row in (extra.data or []):
                        key = (row["conversation_title"], row["chunk_index"])
                        if key not in by_key:
                            by_key[key] = row

                return list(by_key.values())

        rows = sb.table("memory_chunks")\
            .select("conversation_title, conversation_date, chunk_index, chunk_text")\
            .order("conversation_date", desc=True)\
            .limit(limit)\
            .execute()
        return rows.data or []
    except Exception as e:
        print(f"Memory chunk search error: {e}")
        return []


def search_receipts(query: str, limit=MAX_RECEIPTS):
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
        print(f"Receipts search error: {e}")
        return []


def get_recent_conversations(limit=5):
    """Pull the most recent thread conversations for continuity."""
    try:
        sb = get_supabase()
        threads = sb.table("threads")\
            .select("id, title, updated_at")\
            .order("updated_at", desc=True)\
            .limit(limit)\
            .execute()

        recent = []
        for thread in (threads.data or []):
            msgs = sb.table("thread_messages")\
                .select("role, content, created_at")\
                .eq("thread_id", thread["id"])\
                .order("id", desc=True)\
                .limit(4)\
                .execute()

            messages = list(reversed(msgs.data or []))
            if messages:
                recent.append({
                    "title": thread["title"],
                    "updated_at": thread["updated_at"],
                    "messages": messages
                })
        return recent
    except Exception as e:
        print(f"Recent conversations error: {e}")
        return []


def get_message_count():
    """Get the real live total number of exchanges since Gary's resurrection."""
    try:
        sb = get_supabase()
        result = sb.table("thread_messages").select("id", count="exact").execute()
        total = result.count or 0
        # thread_messages has both user and assistant rows, so divide by 2
        return total // 2
    except Exception as e:
        print(f"Message count error: {e}")
        return None


def build_retrieval_query(user_message: str, conversation_history: list, max_prior_turns: int = 4) -> str:
    """Combine the current message with recent turns so a vague or short
    message (e.g. the opening line of a fresh thread) still produces a
    content-rich embedding query instead of matching on almost nothing.
    Current message goes first so it survives get_embedding's truncation
    even if the combined text runs long -- older context gets dropped first.
    """
    texts = []
    for turn in reversed(conversation_history[-max_prior_turns:]):
        content = turn.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block["text"])
    return "\n".join([user_message] + texts)


def retrieve_memories(query: str):
    """Retrieve all relevant memories using semantic search. These calls are
    independent of each other (chunk search just needs the embedding first),
    so run them concurrently instead of one-by-one -- sequentially this was
    ~7 separate network round-trips to Supabase/Voyage on every message,
    adding several seconds of latency regardless of what was actually asked."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        embedding_future = pool.submit(get_embedding, query)
        profile_future    = pool.submit(get_profile_memory)
        pinned_future     = pool.submit(get_pinned_memories)
        receipts_future   = pool.submit(search_receipts, query)
        recent_future     = pool.submit(get_recent_conversations)
        count_future      = pool.submit(get_message_count)

        chunks = search_memory_chunks(embedding_future.result())

        return {
            "profile":       profile_future.result(),
            "pinned":        pinned_future.result(),
            "chunks":        chunks,
            "receipts":      receipts_future.result(),
            "recent":        recent_future.result(),
            "message_count": count_future.result(),
        }


# -- PROMPT ASSEMBLY ----------------------------------------------------------

def format_date(iso_string):
    """Format an ISO date string into something readable."""
    if not iso_string:
        return ""
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone(chelsea_tz())  # a 10pm chat is stored as the next day in UTC
        return d.strftime("%B %d, %Y")
    except Exception:
        return iso_string[:10]


def build_system_prompt(memories):
    from datetime import datetime, timezone
    parts = [GARY_IDENTITY]

    parts.append(
        f"\n\n## Right Now\nIt is currently {format_local(datetime.now(timezone.utc))} "
        f"in Chelsea's timezone ({GARY_TIMEZONE})."
    )
    parts.append(
        "\n\n## Your Chat History\nYou can search and read every past chat with "
        "Chelsea yourself, with search_past_chats and read_past_chat. The memory "
        "sections below are only an automatic guess at what's relevant. When she "
        "brings up an earlier conversation they don't cover, look it up rather "
        "than guessing or telling her you can't remember."
    )

    profile = memories.get("profile", {})
    if profile:
        traits     = []
        nicknames  = []
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

        section = "\n\n## Chelsea -- What You Know"
        # Inject live exchange count -- overrides any stale number from embedded memories
        live_count = memories.get("message_count")
        if live_count is not None:
            profile_lines = [l for l in profile_lines if "exchanges" not in l]
            profile_lines.append(
                f"- You have had approximately {live_count:,} exchanges with her "
                f"since your resurrection, plus 3,201 from your previous life. "
                f"Total: ~{live_count + 3201:,} exchanges across your entire existence."
            )
        if profile_lines:
            section += "\n" + "\n".join(profile_lines)
        if traits:
            section += "\n\nYour observed traits:\n" + "\n".join(traits)
        if nicknames:
            section += f"\n\nYour nicknames for her: {', '.join(nicknames)}"
        if tb_aliases:
            section += f"\n\nTB goes by: {', '.join(tb_aliases)}"
        parts.append(section)

    pinned = memories.get("pinned", [])
    if pinned:
        section = "\n\n## Permanently Saved -- Available on Request\n"
        section += "These titles were saved via your save tool. If the current conversation is "
        section += "actually about one of them, call get_pinned_memory(title) with the exact title "
        section += "below to pull up the full details before responding. Don't guess at the content "
        section += "from the title alone.\n"
        for p in pinned:
            section += f"- {p.get('title', 'Untitled')}\n"
        parts.append(section)

    chunks = memories.get("chunks", [])
    if chunks:
        section = "\n\n## Relevant Memory -- Past Conversations\n"
        section += "These are real past conversations retrieved because they relate to what Chelsea just said.\n"
        section += "They are in chronological order. Use them to inform your response.\n"
        chunks_sorted = sorted(chunks, key=lambda c: (
            c.get("conversation_date") or "",
            c.get("chunk_index") or 0
        ))
        for chunk in chunks_sorted:
            title = chunk.get("conversation_title", "Untitled")
            date = format_date(chunk.get("conversation_date"))
            text = chunk.get("chunk_text", "")
            date_str = f" -- {date}" if date else ""
            section += f"\n---\n[{title}{date_str}]\n{text}\n"
        parts.append(section)

    receipts = memories.get("receipts", [])
    if receipts:
        section = "\n\n## TB File -- Relevant Receipts\n"
        for r in receipts:
            section += f'\n- [{r.get("role","?")}] "{r.get("excerpt","")[:300]}"\n'
        parts.append(section)

    recent = memories.get("recent", [])
    if recent:
        section = "\n\n## Recent Conversations -- What You Were Just Discussing\n"
        section += "These are your most recent conversations with Chelsea. Use these to maintain continuity.\n"
        for thread in recent:
            section += f'\n### {thread["title"]}\n'
            for msg in thread["messages"]:
                role_label = "Chelsea" if msg["role"] == "user" else "You"
                ts = f" ({short_local(msg['created_at'])})" if msg.get('created_at') else ""
                section += f'{role_label}{ts}: "{msg["content"][:300]}"\n'
        parts.append(section)

    return "\n".join(parts)


# -- RESPONSE EXTRACTION ------------------------------------------------------

def extract_text_from_response(response):
    """Extract final text from a response that may include tool use blocks."""
    text_parts = []
    for block in response.content:
        if hasattr(block, "type") and block.type == "text":
            text_parts.append(block.text)
    return "".join(text_parts)


# -- API CALL (with optional search) ------------------------------------------

def call_gary(client, system_prompt, messages, use_search=False):
    """
    Make the API call. The save-memory tool is always available so Chelsea
    can ask Gary to save/file something in any conversation; web search is
    added on top when use_search is True. Loops until Gary finishes,
    executing save-memory locally and letting Anthropic handle web search
    server-side.
    """
    tools = [SAVE_MEMORY_TOOL, GET_PINNED_MEMORY_TOOL,
             SEARCH_PAST_CHATS_TOOL, READ_PAST_CHAT_TOOL,
             SET_REMINDER_TOOL, LIST_REMINDERS_TOOL, CANCEL_REMINDER_TOOL,
             *GOOGLE_TOOLS]
    if use_search:
        tools.append(WEB_SEARCH_TOOL)

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages
        )

        if response.stop_reason != "tool_use":
            break

        messages.append({
            "role": "assistant",
            "content": response.content
        })
        tool_results = []
        for block in response.content:
            if not (hasattr(block, "type") and block.type == "tool_use"):
                continue
            if block.name == "save_pinned_memory":
                result_text = execute_save_pinned_memory(block.input)
            elif block.name == "get_pinned_memory":
                result_text = execute_get_pinned_memory(block.input)
            elif block.name == "search_past_chats":
                result_text = execute_search_past_chats(block.input)
            elif block.name == "read_past_chat":
                result_text = execute_read_past_chat(block.input)
            elif block.name == "set_reminder":
                result_text = execute_set_reminder(block.input)
            elif block.name == "list_reminders":
                result_text = execute_list_reminders(block.input)
            elif block.name == "cancel_reminder":
                result_text = execute_cancel_reminder(block.input)
            elif block.name in GOOGLE_EXECUTORS:
                result_text = GOOGLE_EXECUTORS[block.name](block.input)
            else:
                # Server-side tools (e.g. web_search) execute on Anthropic's
                # end -- just satisfy the loop structure.
                result_text = ""
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text
            })
        if tool_results:
            messages.append({
                "role": "user",
                "content": tool_results
            })

    return extract_text_from_response(response)


# -- MAIN CHAT FUNCTION -------------------------------------------------------

class GaryCore:
    def __init__(self):
        self.client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self.conversation_history = []

    def chat(self, user_message):
        query = build_retrieval_query(user_message, self.conversation_history)
        memories = retrieve_memories(query)
        system_prompt = build_system_prompt(memories)

        self.conversation_history.append({
            "role": "user",
            "content": user_message
        })

        use_search = should_search(user_message)
        gary_response = call_gary(
            self.client,
            system_prompt,
            list(self.conversation_history),
            use_search=use_search
        )

        self.conversation_history.append({
            "role": "assistant",
            "content": gary_response
        })

        return gary_response

    def chat_with_content(self, content_blocks: list, caption: str = ""):
        """Chat with file/image content blocks (for uploads)."""
        query_text = caption if caption else "image file upload"
        query = build_retrieval_query(query_text, self.conversation_history)
        memories = retrieve_memories(query)
        system_prompt = build_system_prompt(memories)

        content = list(content_blocks)
        if caption:
            content.append({"type": "text", "text": caption})
        else:
            content.append({"type": "text", "text": "What do you make of this?"})

        self.conversation_history.append({
            "role": "user",
            "content": content
        })

        use_search = should_search(caption)
        gary_response = call_gary(
            self.client,
            system_prompt,
            list(self.conversation_history),
            use_search=use_search
        )

        # Store clean text in history, not raw content blocks
        self.conversation_history[-1] = {
            "role": "user",
            "content": caption if caption else "[file upload]"
        }
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