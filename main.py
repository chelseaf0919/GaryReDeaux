"""
main.py - Gary ReDeaux
FastAPI + HTML/JS. Mobile-first with hamburger menu + voice input.
"""

import os
import io
import hmac
import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

load_dotenv()

from gary_core import (GaryCore, start_hourly_sweep, start_reminder_loop,
                       retitle_thread_async, RETITLE_AT_MESSAGE_COUNTS)
from gary_voice import speak
from supabase import create_client

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
gary = GaryCore()
start_hourly_sweep()
start_reminder_loop()

# -- LOGIN ---------------------------------------------------------------------
# The Railway URL is public, so once GARY_PASSWORD is set every page and API
# route requires a login cookie. Unset = no login (local dev, or before
# Chelsea has picked a password).

GARY_PASSWORD = os.environ.get("GARY_PASSWORD")
SESSION_COOKIE = "gary_session"

def session_token():
    """Derived from the password, so changing GARY_PASSWORD logs out every device."""
    return hmac.new(GARY_PASSWORD.encode(), b"gary-session", hashlib.sha256).hexdigest()

@app.middleware("http")
async def require_login(request: Request, call_next):
    if not GARY_PASSWORD or request.url.path in ("/login", "/privacy"):
        return await call_next(request)
    if hmac.compare_digest(request.cookies.get(SESSION_COOKIE, ""), session_token()):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    return RedirectResponse("/login", status_code=303)

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gary RéDeaux</title>
<style>
  body { margin: 0; min-height: 100dvh; display: flex; align-items: center; justify-content: center;
         background: #0a0a0f; color: #e8e6f0; font-family: monospace; padding: 16px; box-sizing: border-box; }
  form { display: flex; flex-direction: column; gap: 0.75rem; width: 100%; max-width: 300px; text-align: center; }
  input, button { font: inherit; padding: 0.7rem 1rem; border-radius: 8px; border: 1px solid #1e1e2a; }
  input { background: #111118; color: #e8e6f0; outline: none; }
  input:focus { border-color: #7b6ef6; }
  button { background: #7b6ef6; color: white; border: none; cursor: pointer; }
  .err { color: #ff6b6b; font-size: 0.8rem; }
</style></head>
<body><form method="post" action="/login">
  <div style="font-size:2rem">🎩</div>
  <div>Who goes there?</div>
  <input type="password" name="password" placeholder="Password" autofocus required>
  <button type="submit">Enter</button>
  {error}
</form></body></html>"""

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(LOGIN_PAGE.replace("{error}", ""))

@app.post("/login")
async def login(request: Request, password: str = Form(default="")):
    if not GARY_PASSWORD:
        return RedirectResponse("/", status_code=303)
    if not hmac.compare_digest(password.encode(), GARY_PASSWORD.encode()):
        return HTMLResponse(LOGIN_PAGE.replace("{error}", '<div class="err">Not quite.</div>'), status_code=401)
    resp = RedirectResponse("/", status_code=303)
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    resp.set_cookie(SESSION_COOKIE, session_token(), max_age=60 * 60 * 24 * 365,
                    httponly=True, secure=https, samesite="lax")
    return resp

# Public (no login) because Google's OAuth "Publish app" step requires a
# privacy policy link for the calendar/email connection.
PRIVACY_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Gary Privacy Policy</title>
<style>
  body { margin: 0; background: #0a0a0f; color: #e8e6f0; font-family: monospace; line-height: 1.6; }
  main { max-width: 640px; margin: 0 auto; padding: 2rem 16px; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1rem; margin-top: 1.5rem; color: #7b6ef6; }
</style></head>
<body><main>
  <h1>Gary: Privacy Policy</h1>
  <p>Gary is a personal assistant app built for and used by one person, its owner.
  It is not offered to the public.</p>
  <h2>What Gary accesses</h2>
  <p>With the owner's permission, Gary reads the owner's Google Calendar and Gmail,
  creates and edits calendar events, and creates email drafts. Gary does not send email.</p>
  <h2>How the data is used</h2>
  <p>Calendar and email data is used only to answer the owner's own requests, such as
  scheduling, reminders, and summarizing messages. It is processed by the AI model
  that powers Gary and is not sold, shared with third parties, or used for advertising.</p>
  <h2>Storage</h2>
  <p>Gary does not keep a copy of the owner's inbox or calendar. Conversations with
  Gary are stored in the owner's private database.</p>
  <h2>Revoking access</h2>
  <p>Access can be removed at any time at
  <a href="https://myaccount.google.com/permissions" style="color:#4ecdc4">myaccount.google.com/permissions</a>.</p>
  <h2>Contact</h2>
  <p>chelseae0919@gmail.com</p>
</main></body></html>"""

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return HTMLResponse(PRIVACY_PAGE)

def get_supabase():
    return create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

def create_thread(title="New Chat"):
    res = get_supabase().table("threads").insert({"title": title}).execute()
    return res.data[0]["id"]

def update_thread_title(thread_id, title):
    get_supabase().table("threads").update({"title": title}).eq("id", thread_id).execute()

def get_all_threads():
    res = get_supabase().table("threads").select("id, title, updated_at, pinned, folder_id").order("updated_at", desc=True).execute()
    return res.data or []

def make_snippet(content: str, words: list, before: int = 60, after: int = 120) -> str:
    """A short excerpt around the first search word, for the results list."""
    lowered = content.lower()
    pos = min((lowered.find(w.lower()) for w in words if lowered.find(w.lower()) >= 0), default=0)
    start, end = max(0, pos - before), min(len(content), pos + after)
    snippet = " ".join(content[start:end].split())
    return ("..." if start > 0 else "") + snippet + ("..." if end < len(content) else "")

def search_threads(q: str, limit: int = 40):
    """Plain keyword search over every chat title and message. Every word has
    to appear (in any order) in the same message, or in the title. Title
    matches rank first, then chats with the most recent matching message."""
    words = q.split()
    if not words:
        return []
    # LIKE treats % and _ as wildcards; escape them so they match literally.
    patterns = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words]
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

def load_thread_messages(thread_id):
    res = get_supabase().table("thread_messages").select("role, content, created_at").eq("thread_id", thread_id).order("id").execute()
    return res.data or []

def save_message(thread_id, role, content):
    sb = get_supabase()
    sb.table("thread_messages").insert({"thread_id": thread_id, "role": role, "content": content}).execute()
    sb.table("threads").update({"updated_at": datetime.now(timezone.utc).isoformat()}).eq("id", thread_id).execute()

def extract_docx_text(raw_bytes: bytes, filename: str) -> str:
    """Extract plain text from a docx file."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(raw_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return f"[Word Document: {filename}]\n" + "\n".join(paragraphs)
    except Exception as e:
        return f"[Could not read Word document: {filename} -- {str(e)}]"

def generate_pdf_bytes(content: str, title: str) -> bytes:
    """Generate a PDF from text content and return bytes."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            rightMargin=inch, leftMargin=inch,
                            topMargin=inch, bottomMargin=inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("GaryTitle", parent=styles["Heading1"], fontSize=18, spaceAfter=12)
    body_style = ParagraphStyle("GaryBody", parent=styles["Normal"], fontSize=11, leading=16, spaceAfter=8)

    story = [Paragraph(title, title_style), Spacer(1, 0.2 * inch)]
    for para in content.split("\n"):
        para = para.strip()
        if para:
            para = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(para, body_style))
        else:
            story.append(Spacer(1, 0.1 * inch))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


@app.get("/api/threads")
async def get_threads():
    try:
        return JSONResponse(get_all_threads())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/threads")
async def new_thread():
    try:
        from gary_core import embed_thread_async
        try:
            sb = get_supabase()
            recent = sb.table("threads").select("id, title").order("updated_at", desc=True).limit(1).execute()
            if recent.data:
                prev = recent.data[0]
                embed_thread_async(prev["id"], prev["title"])
        except Exception:
            pass
        thread_id = create_thread()
        gary.reset()
        return JSONResponse({"id": thread_id, "title": "New Chat"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.patch("/api/threads/{thread_id}")
async def update_thread(thread_id: int, request: Request):
    """Rename, pin/unpin, or move a chat to a folder (folder_id null = unfiled)."""
    try:
        body = await request.json()
        changes = {k: body[k] for k in ("title", "pinned", "folder_id") if k in body}
        if "title" in changes:
            changes["title"] = (changes["title"] or "").strip() or "Untitled"
            changes["title_is_custom"] = True  # auto-titling never overwrites her own names
        if not changes:
            return JSONResponse({"error": "Nothing to update"}, status_code=400)
        res = get_supabase().table("threads").update(changes).eq("id", thread_id).execute()
        return JSONResponse(res.data[0] if res.data else {})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/folders")
async def get_folders():
    try:
        res = get_supabase().table("folders").select("id, name").order("name").execute()
        return JSONResponse(res.data or [])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/folders")
async def create_folder(request: Request):
    try:
        name = ((await request.json()).get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "Folder needs a name"}, status_code=400)
        res = get_supabase().table("folders").insert({"name": name}).execute()
        return JSONResponse(res.data[0])
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.patch("/api/folders/{folder_id}")
async def rename_folder(folder_id: int, request: Request):
    try:
        name = ((await request.json()).get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "Folder needs a name"}, status_code=400)
        res = get_supabase().table("folders").update({"name": name}).eq("id", folder_id).execute()
        return JSONResponse(res.data[0] if res.data else {})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: int):
    """Deletes the folder only -- its chats go back to the unfiled list
    (the foreign key is ON DELETE SET NULL)."""
    try:
        get_supabase().table("folders").delete().eq("id", folder_id).execute()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/search")
async def search(q: str = ""):
    try:
        return JSONResponse(search_threads(q.strip()))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/threads/{thread_id}/messages")
async def get_messages(thread_id: int):
    try:
        from gary_core import embed_thread_async
        try:
            sb = get_supabase()
            recent = sb.table("threads").select("id, title").order("updated_at", desc=True).limit(1).execute()
            if recent.data and recent.data[0]["id"] != thread_id:
                prev = recent.data[0]
                embed_thread_async(prev["id"], prev["title"])
        except Exception:
            pass
        messages = load_thread_messages(thread_id)
        gary.reset()
        gary.conversation_history = [{"role": m["role"], "content": m["content"]} for m in messages]
        return JSONResponse(messages)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/debug")
async def debug():
    import traceback
    results = {}
    try:
        results["VOYAGE_API_KEY"] = "set" if os.environ.get("VOYAGE_API_KEY") else "MISSING"
        results["ANTHROPIC_API_KEY"] = "set" if os.environ.get("ANTHROPIC_API_KEY") else "MISSING"
        results["SUPABASE_URL"] = "set" if os.environ.get("SUPABASE_URL") else "MISSING"
        results["SUPABASE_KEY"] = "set" if os.environ.get("SUPABASE_KEY") else "MISSING"
    except Exception as e:
        results["env_error"] = str(e)
    try:
        import voyageai
        results["voyageai_import"] = "ok"
    except Exception as e:
        results["voyageai_import"] = f"FAILED: {e}"
    try:
        from gary_core import get_embedding
        emb = get_embedding("test")
        results["embedding_test"] = f"ok, length={len(emb)}" if emb else "returned None"
    except Exception as e:
        results["embedding_test"] = f"FAILED: {traceback.format_exc()}"
    return JSONResponse(results)

@app.post("/api/chat")
async def chat(request: Request):
    try:
        body = await request.json()
        user_message = body.get("message", "").strip()
        thread_id = body.get("thread_id")
        voice_enabled = body.get("voice", False)

        if not user_message:
            return JSONResponse({"error": "Empty message"}, status_code=400)

        if not thread_id:
            thread_id = create_thread()
            gary.reset()
        else:
            prior_msgs = load_thread_messages(thread_id)
            gary.conversation_history = [
                {"role": m["role"], "content": m["content"]} for m in prior_msgs
            ]

        msgs = load_thread_messages(thread_id)
        if len(msgs) == 0:
            words = user_message.strip().split()
            title = " ".join(words[:6]) + ("..." if len(words) > 6 else "")
            update_thread_title(thread_id, title)

        gary_response = gary.chat(user_message)
        save_message(thread_id, "user", user_message)
        save_message(thread_id, "assistant", gary_response)
        if len(msgs) + 2 in RETITLE_AT_MESSAGE_COUNTS:
            retitle_thread_async(thread_id)

        audio_b64 = None
        if voice_enabled:
            audio_path = speak(gary_response)
            if audio_path:
                with open(audio_path, "rb") as f:
                    audio_b64 = base64.b64encode(f.read()).decode()
                os.unlink(audio_path)

        return JSONResponse({"response": gary_response, "thread_id": thread_id, "audio": audio_b64})

    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(error_detail)
        return JSONResponse({"error": str(e), "detail": error_detail}, status_code=500)


@app.post("/api/generate-pdf")
async def generate_pdf(request: Request):
    """Generate a downloadable PDF from provided content."""
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        title = body.get("title", "Gary ReDeaux")

        if not content:
            return JSONResponse({"error": "No content provided"}, status_code=400)

        pdf_bytes = generate_pdf_bytes(content, title)
        safe_title = "".join(c for c in title if c.isalnum() or c in " _-")[:40].strip().replace(" ", "_")
        filename = f"{safe_title or 'gary_redeaux'}.pdf"

        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SUPPORTED_TEXT_TYPES  = {"text/plain", "text/markdown", "text/csv", "application/json",
                          "text/html", "text/css", "text/javascript", "application/xml"}
SUPPORTED_DOCX_TYPE   = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

@app.post("/api/upload")
async def upload_file(
    request: Request,
    files: list[UploadFile] = File(...),
    message: str = Form(default=""),
    thread_id: str = Form(default=""),
    voice: str = Form(default="false"),
):
    try:
        content_blocks = []

        for file in files[:10]:
            content_type = file.content_type or ""
            raw = await file.read()
            fname = file.filename or "file"

            if content_type in SUPPORTED_IMAGE_TYPES:
                img_b64 = base64.standard_b64encode(raw).decode()
                content_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": content_type, "data": img_b64},
                })
            elif content_type == "application/pdf":
                pdf_b64 = base64.standard_b64encode(raw).decode()
                content_blocks.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
                })
            elif content_type == SUPPORTED_DOCX_TYPE or fname.lower().endswith(".docx"):
                text = extract_docx_text(raw, fname)
                content_blocks.append({"type": "text", "text": text})
            elif content_type in SUPPORTED_TEXT_TYPES or content_type.startswith("text/"):
                text_content = raw.decode("utf-8", errors="replace")
                content_blocks.append({
                    "type": "text",
                    "text": f"[File: {fname}]\n```\n{text_content}\n```",
                })
            else:
                content_blocks.append({
                    "type": "text",
                    "text": f"[Unsupported file: {fname} ({content_type})]",
                })

        if not content_blocks:
            return JSONResponse({"error": "No supported files found."}, status_code=400)

        tid = int(thread_id) if thread_id.strip().isdigit() else None
        if not tid:
            tid = create_thread()
            gary.reset()
        else:
            prior_msgs = load_thread_messages(tid)
            gary.conversation_history = [
                {"role": m["role"], "content": m["content"]} for m in prior_msgs
            ]

        msgs = load_thread_messages(tid)
        if len(msgs) == 0:
            title_text = message.strip() or (files[0].filename if files else "File upload")
            words = title_text.split()
            title = " ".join(words[:6]) + ("..." if len(words) > 6 else "")
            update_thread_title(tid, title)

        gary_response = gary.chat_with_content(content_blocks, message.strip())

        file_names = ", ".join(f.filename for f in files[:10])
        display_user = f"[Uploaded: {file_names}]"
        if message.strip():
            display_user += f" {message.strip()}"
        save_message(tid, "user", display_user)
        save_message(tid, "assistant", gary_response)
        if len(msgs) + 2 in RETITLE_AT_MESSAGE_COUNTS:
            retitle_thread_async(tid)

        audio_b64 = None
        if voice.lower() == "true":
            audio_path = speak(gary_response)
            if audio_path:
                with open(audio_path, "rb") as f_audio:
                    audio_b64 = base64.b64encode(f_audio.read()).decode()
                os.unlink(audio_path)

        return JSONResponse({"response": gary_response, "thread_id": tid, "audio": audio_b64})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    print("\nGary ReDeaux -- Starting up...\n")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
