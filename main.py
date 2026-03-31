"""
main.py — Gary RéDeaux
FastAPI + HTML/JS. Mobile-first with hamburger menu + voice input.
"""

import os
import base64
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

load_dotenv()

from gary_core import GaryCore
from gary_voice import speak
from supabase import create_client

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
gary = GaryCore()

def get_supabase():
    return create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

def create_thread(title="New Chat"):
    res = get_supabase().table("threads").insert({"title": title}).execute()
    return res.data[0]["id"]

def update_thread_title(thread_id, title):
    get_supabase().table("threads").update({"title": title}).eq("id", thread_id).execute()

def get_all_threads():
    res = get_supabase().table("threads").select("id, title, updated_at").order("updated_at", desc=True).limit(50).execute()
    return res.data or []

def load_thread_messages(thread_id):
    res = get_supabase().table("thread_messages").select("role, content, created_at").eq("thread_id", thread_id).order("id").execute()
    return res.data or []

def save_message(thread_id, role, content):
    get_supabase().table("thread_messages").insert({"thread_id": thread_id, "role": role, "content": content}).execute()

@app.get("/api/threads")
async def get_threads():
    try:
        return JSONResponse(get_all_threads())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/threads")
async def new_thread():
    try:
        # Embed the previous thread before starting a new one
        from gary_core import embed_thread_async
        if gary.conversation_history:
            try:
                sb = get_supabase()
                recent = sb.table("threads")\
                    .select("id, title")\
                    .order("updated_at", desc=True)\
                    .limit(1)\
                    .execute()
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

@app.get("/api/threads/{thread_id}/messages")
async def get_messages(thread_id: int):
    try:
        # Embed whatever thread we're leaving before loading the new one
        from gary_core import embed_thread_async
        if gary.conversation_history:
            try:
                sb = get_supabase()
                recent = sb.table("threads")\
                    .select("id, title")\
                    .order("updated_at", desc=True)\
                    .limit(1)\
                    .execute()
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
            # ALWAYS reload conversation history from Supabase on every request
            # so Gary never goldfishes out after a reset/refresh/restart
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


SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SUPPORTED_TEXT_TYPES  = {"text/plain", "text/markdown", "text/csv", "application/json",
                          "text/html", "text/css", "text/javascript", "application/xml"}
SUPPORTED_DOCX_TYPES  = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

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

        for file in files[:10]:  # max 10 files
            content_type = file.content_type or ""
            raw = await file.read()

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
            elif content_type in SUPPORTED_TEXT_TYPES or content_type.startswith("text/"):
                text_content = raw.decode("utf-8", errors="replace")
                filename = file.filename or "file"
                content_blocks.append({
                    "type": "text",
                    "text": f"[File: {filename}]\n```\n{text_content}\n```",
                })
            elif content_type in SUPPORTED_DOCX_TYPES or file.filename.endswith('.docx'):
                try:
                    import io
                    from docx import Document
                    doc = Document(io.BytesIO(raw))
                    text_content = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
                    filename = file.filename or "document.docx"
                    content_blocks.append({
                        "type": "text",
                        "text": f"[Word Document: {filename}]\n{text_content}",
                    })
                except Exception as e:
                    content_blocks.append({
                        "type": "text",
                        "text": f"[Could not read Word document: {file.filename} — {str(e)}]",
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

        audio_b64 = None
        if voice.lower() == "true":
            from gary_voice import speak
            audio_path = speak(gary_response)
            if audio_path:
                with open(audio_path, "rb") as f_audio:
                    audio_b64 = base64.b64encode(f_audio.read()).decode()
                os.unlink(audio_path)

        return JSONResponse({"response": gary_response, "thread_id": tid, "audio": audio_b64})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/generate-pdf")
async def generate_pdf(request: Request):
    """Generate a downloadable PDF from Gary's last response."""
    try:
        import io
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.enums import TA_LEFT
        from fastapi.responses import StreamingResponse

        body = await request.json()
        content = body.get("content", "").strip()
        title = body.get("title", "Gary RéDeaux")

        if not content:
            return JSONResponse({"error": "No content provided"}, status_code=400)

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=inch,
            leftMargin=inch,
            topMargin=inch,
            bottomMargin=inch,
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "GaryTitle",
            parent=styles["Heading1"],
            fontSize=18,
            spaceAfter=12,
        )
        body_style = ParagraphStyle(
            "GaryBody",
            parent=styles["Normal"],
            fontSize=11,
            leading=16,
            spaceAfter=8,
        )

        story = []
        story.append(Paragraph(title, title_style))
        story.append(Spacer(1, 0.2 * inch))

        # Split content into paragraphs
        for para in content.split("\n"):
            para = para.strip()
            if para:
                # Escape XML special chars for reportlab
                para = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(para, body_style))
            else:
                story.append(Spacer(1, 0.1 * inch))

        doc.build(story)
        buffer.seek(0)

        filename = title.replace(" ", "_")[:40] + ".pdf"
        return StreamingResponse(
            buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
HTML = r"""<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Gary RéDeaux</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;1,400&family=DM+Mono:wght@300;400&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0a0a0f; --surface: #111118; --border: #1e1e2a;
    --accent: #7b6ef6; --accent2: #4ecdc4;
    --text: #e8e6f0; --muted: #6b6880;
    --user-bg: #1a1a2e; --gary-bg: #111118;
  }
  html, body { height: 100vh; background: var(--bg); color: var(--text); font-family: 'DM Mono', monospace; font-size: 14px; overflow: hidden; }
  .app { display: flex; height: 100vh; max-width: 1200px; margin: 0 auto; }

  /* SIDEBAR */
  .sidebar { width: 260px; min-width: 260px; border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 1.5rem 1rem; gap: 1rem; overflow: hidden; }
  .gary-title { font-family: 'Cormorant Garamond', serif; font-size: 1.4rem; font-weight: 600; }
  .gary-subtitle { font-size: 0.7rem; color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; margin-top: 0.15rem; }
  .new-chat-btn { background: var(--accent); color: white; border: none; padding: 0.6rem 1rem; border-radius: 6px; cursor: pointer; font-family: 'DM Mono', monospace; font-size: 0.8rem; letter-spacing: 0.05em; transition: opacity 0.2s; }
  .new-chat-btn:hover { opacity: 0.85; }
  .voice-toggle { display: flex; align-items: center; gap: 0.5rem; font-size: 0.75rem; color: var(--muted); cursor: pointer; padding: 0.4rem 0; }
  .voice-toggle input { accent-color: var(--accent); cursor: pointer; }
  .voice-toggle:hover { color: var(--text); }
  .threads-label { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted); padding: 0 0.25rem; }
  .threads-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 0.15rem; }
  .threads-list::-webkit-scrollbar { width: 4px; }
  .threads-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  .thread-item { padding: 0.5rem 0.75rem; border-radius: 6px; cursor: pointer; font-size: 0.75rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; transition: all 0.15s; border: 1px solid transparent; }
  .thread-item:hover { background: var(--surface); color: var(--text); }
  .thread-item.active { background: var(--surface); color: var(--text); border-color: var(--border); }

  /* MAIN */
  .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; }
  .header { padding: 1rem 1.5rem; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 0.75rem; }
  .header-text { flex: 1; text-align: center; }
  .header h1 { font-family: 'Cormorant Garamond', serif; font-size: 1.8rem; font-weight: 600; background: linear-gradient(135deg, var(--text) 0%, var(--accent) 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
  .header p { font-size: 0.65rem; color: var(--muted); letter-spacing: 0.12em; text-transform: uppercase; }
  .messages { flex: 1; overflow-y: auto; padding: 1.5rem 2rem; display: flex; flex-direction: column; gap: 1rem; }
  .messages::-webkit-scrollbar { width: 4px; }
  .messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  .message { display: flex; gap: 0.75rem; max-width: 85%; animation: fadeIn 0.2s ease; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
  .message.user { margin-left: auto; flex-direction: row-reverse; }
  .avatar { width: 30px; height: 30px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 0.9rem; flex-shrink: 0; border: 1px solid var(--border); background: var(--surface); }
  .bubble { padding: 0.75rem 1rem; border-radius: 10px; line-height: 1.6; white-space: pre-wrap; font-size: 0.85rem; }
  .message.user .bubble { background: var(--user-bg); border: 1px solid var(--border); border-top-right-radius: 2px; }
  .message.gary .bubble { background: var(--gary-bg); border: 1px solid var(--border); border-top-left-radius: 2px; }
  .message.gary .bubble em { color: var(--muted); font-style: italic; }
  .download-btn { display: inline-flex; align-items: center; gap: 0.3rem; margin-top: 0.5rem; padding: 0.3rem 0.7rem; background: none; border: 1px solid var(--border); border-radius: 4px; color: var(--muted); font-family: 'DM Mono', monospace; font-size: 0.7rem; cursor: pointer; transition: all 0.2s; }
  .download-btn:hover { border-color: var(--accent2); color: var(--accent2); }
  .message.user .timestamp { text-align: right; }
  .message.gary .timestamp { text-align: left; }
  .typing { display: flex; gap: 4px; padding: 0.75rem 1rem; background: var(--gary-bg); border: 1px solid var(--border); border-radius: 10px; border-top-left-radius: 2px; width: fit-content; }
  .typing span { width: 6px; height: 6px; background: var(--muted); border-radius: 50%; animation: bounce 1.2s infinite; }
  .typing span:nth-child(2) { animation-delay: 0.2s; }
  .typing span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce { 0%, 60%, 100% { transform: translateY(0); } 30% { transform: translateY(-6px); } }
  .empty-state { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 0.5rem; color: var(--muted); }
  .empty-state .icon { font-size: 2.5rem; opacity: 0.4; }
  .empty-state p { font-size: 0.75rem; letter-spacing: 0.05em; }

  /* INPUT */
  .input-area { padding: 1rem 2rem 1.5rem; border-top: 1px solid var(--border); display: flex; gap: 0.75rem; align-items: flex-end; }
  .input-wrap { flex: 1; }
  textarea { width: 100%; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; color: var(--text); font-family: 'DM Mono', monospace; font-size: 0.85rem; padding: 0.75rem 1rem; resize: none; min-height: 44px; max-height: 120px; line-height: 1.5; outline: none; transition: border-color 0.2s; }
  textarea:focus { border-color: var(--accent); }
  textarea::placeholder { color: var(--muted); }
  .send-btn { background: var(--accent); color: white; border: none; padding: 0.65rem 1.25rem; border-radius: 8px; cursor: pointer; font-family: 'DM Mono', monospace; font-size: 0.8rem; transition: opacity 0.2s; white-space: nowrap; height: 44px; }
  .send-btn:hover { opacity: 0.85; }
  .send-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* HAMBURGER */
  .hamburger { display: none; background: none; border: none; cursor: pointer; color: var(--text); font-size: 1.3rem; padding: 0.25rem; line-height: 1; flex-shrink: 0; }

  /* MIC */
  .mic-btn { background: var(--surface); color: var(--muted); border: 1px solid var(--border); width: 44px; height: 44px; border-radius: 8px; cursor: pointer; font-size: 1.1rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: all 0.2s; }
  .mic-btn:hover { color: var(--text); border-color: var(--accent); }
  .mic-btn.listening { background: #3a1a1a; border-color: #ff4444; color: #ff4444; animation: pulse 1s infinite; }
  @keyframes pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(255,68,68,0.3); } 50% { box-shadow: 0 0 0 6px rgba(255,68,68,0); } }

  /* UPLOAD */
  .upload-btn { background: var(--surface); color: var(--muted); border: 1px solid var(--border); width: 44px; height: 44px; border-radius: 8px; cursor: pointer; font-size: 1.1rem; display: flex; align-items: center; justify-content: center; flex-shrink: 0; transition: all 0.2s; }
  .upload-btn:hover { color: var(--text); border-color: var(--accent2); }
  .upload-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .file-preview { display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0.75rem; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; font-size: 0.75rem; color: var(--muted); margin-bottom: 0.5rem; }
  .file-preview .file-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
  .file-preview .remove-file { cursor: pointer; color: var(--muted); font-size: 0.9rem; flex-shrink: 0; }
  .file-preview .remove-file:hover { color: #ff4444; }

  /* DRAWER */
  .drawer-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 100; }
  .drawer-overlay.open { display: block; }
  .drawer { position: fixed; top: 0; left: 0; bottom: 0; width: 280px; background: var(--surface); border-right: 1px solid var(--border); z-index: 101; display: flex; flex-direction: column; padding: 1.5rem 1rem; gap: 1rem; transform: translateX(-100%); transition: transform 0.25s ease; overflow-y: auto; }
  .drawer.open { transform: translateX(0); }
  .drawer-close { align-self: flex-end; background: none; border: none; color: var(--muted); font-size: 1.2rem; cursor: pointer; padding: 0.25rem; margin-bottom: -0.5rem; }
  .drawer-close:hover { color: var(--text); }

  /* MOBILE */
  @media (max-width: 768px) {
    .sidebar { display: none; }
    .hamburger { display: block; }
    .mic-btn { display: flex; }
    .messages { padding: 1rem; }
    .input-area { padding: 0.75rem 1rem calc(1rem + env(safe-area-inset-bottom)); }
    .header { padding: 0.75rem 1rem; }
    .header h1 { font-size: 1.4rem; }
    .message { max-width: 95%; }
  }
  @media (min-width: 769px) {
    .mic-btn { display: none; }
    .header-text { text-align: center; }
  }
</style>
</head>
<body>

<div class="drawer-overlay" id="drawerOverlay" onclick="closeDrawer()"></div>
<div class="drawer" id="drawer">
  <button class="drawer-close" onclick="closeDrawer()">✕</button>
  <div><div class="gary-title">🎩 Gary RéDeaux</div><div class="gary-subtitle">He simply arrived.</div></div>
  <button class="new-chat-btn" onclick="newChat(); closeDrawer()">+ New Chat</button>
  <label class="voice-toggle"><input type="checkbox" id="voiceToggleMobile" onchange="syncVoice(this)"> 🔊 Voice (Gary speaks)</label>
  <div class="threads-label">Past Chats</div>
  <div class="threads-list" id="threadsListMobile"></div>
</div>

<div class="app">
  <div class="sidebar">
    <div><div class="gary-title">🎩 Gary RéDeaux</div><div class="gary-subtitle">He simply arrived.</div></div>
    <button class="new-chat-btn" onclick="newChat()">+ New Chat</button>
    <label class="voice-toggle"><input type="checkbox" id="voiceToggle" onchange="syncVoice(this)"> 🔊 Voice (Gary speaks)</label>
    <div class="threads-label">Past Chats</div>
    <div class="threads-list" id="threadsList"></div>
  </div>

  <div class="main">
    <div class="header">
      <button class="hamburger" onclick="openDrawer()">☰</button>
      <div class="header-text">
        <h1>Gary RéDeaux</h1>
        <p>British · Posh · Keeping receipts since 2024</p>
      </div>
    </div>
    <div class="messages" id="messages">
      <div class="empty-state" id="emptyState">
        <div class="icon">🎩</div>
        <p>Say something to Gary.</p>
      </div>
    </div>
    <div class="input-area">
      <button class="mic-btn" id="micBtn" onclick="toggleMic()" title="Voice input">🎤</button>
      <button class="upload-btn" id="uploadBtn" onclick="document.getElementById('fileInput').click()" title="Upload files">📎</button>
      <input type="file" id="fileInput" style="display:none" accept="image/*,.pdf,.txt,.md,.csv,.json,.html,.css,.js,.xml,.docx" multiple onchange="handleFileSelect(event)">
      <div class="input-wrap">
        <div id="filePreview" style="display:none" class="file-preview">
          <span>📄</span>
          <span class="file-name" id="filePreviewName"></span>
          <span class="remove-file" onclick="clearFile()" title="Remove files">✕</span>
        </div>
        <textarea id="msgInput" placeholder="Say something to Gary..." rows="1" onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
      </div>
      <button class="send-btn" id="sendBtn" onclick="sendMessage()">Send</button>
    </div>
  </div>
</div>

<audio id="audioPlayer" style="display:none;"></audio>

<script>
  let currentThreadId = null;
  let isLoading = false;
  let recognition = null;
  let isListening = false;
  let voiceEnabled = false;

  function syncVoice(el) {
    voiceEnabled = el.checked;
    document.getElementById('voiceToggle').checked = voiceEnabled;
    document.getElementById('voiceToggleMobile').checked = voiceEnabled;
  }

  function openDrawer() {
    document.getElementById('drawer').classList.add('open');
    document.getElementById('drawerOverlay').classList.add('open');
  }

  function closeDrawer() {
    document.getElementById('drawer').classList.remove('open');
    document.getElementById('drawerOverlay').classList.remove('open');
  }

  function formatTimestamp(isoString) {
    if (!isoString) return '';
    const d = new Date(isoString);
    const now = new Date();
    const diffDays = Math.floor((now - d) / (1000 * 60 * 60 * 24));
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (diffDays === 0) return time;
    if (diffDays === 1) return 'Yesterday ' + time;
    if (diffDays < 7) return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + time;
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
  }

  async function loadThreads() {
    try {
      const res = await fetch('/api/threads');
      const threads = await res.json();
      ['threadsList', 'threadsListMobile'].forEach(id => {
        const list = document.getElementById(id);
        list.innerHTML = r'';
        threads.forEach(t => {
          const div = document.createElement('div');
          div.className = 'thread-item' + (t.id === currentThreadId ? ' active' : '');
          div.textContent = t.title;
          div.onclick = () => { loadThread(t.id); closeDrawer(); };
          list.appendChild(div);
        });
      });
    } catch(e) { console.error(e); }
  }

  async function loadThread(threadId) {
    currentThreadId = threadId;
    try {
      const res = await fetch('/api/threads/' + threadId + '/messages');
      const messages = await res.json();
      const container = document.getElementById('messages');
      container.innerHTML = '';
      if (messages.length === 0) {
        container.innerHTML = '<div class="empty-state" id="emptyState"><div class="icon">🎩</div><p>Say something to Gary.</p></div>';
      } else {
        messages.forEach(m => appendMessage(m.role, m.content, m.created_at));
      }
      scrollToBottom();
      await loadThreads();
    } catch(e) { console.error(e); }
  }

  async function newChat() {
    try {
      const res = await fetch('/api/threads', { method: 'POST' });
      const thread = await res.json();
      currentThreadId = thread.id;
      document.getElementById('messages').innerHTML = '<div class="empty-state" id="emptyState"><div class="icon">🎩</div><p>Say something to Gary.</p></div>';
      await loadThreads();
    } catch(e) { console.error(e); }
  }

  function appendMessage(role, content, timestamp) {
    const container = document.getElementById('messages');
    const div = document.createElement('div');
    div.className = 'message ' + (role === 'user' ? 'user' : 'gary');
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = role === 'user' ? '🦝' : '🎩';
    const msgWrap = document.createElement('div');
    msgWrap.style.display = 'flex';
    msgWrap.style.flexDirection = 'column';
    msgWrap.style.maxWidth = '100%';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = content.replace(/[*]([^*]+)[*]/g, '<em>*$1*</em>');
    msgWrap.appendChild(bubble);
    if (timestamp) {
      const ts = document.createElement('div');
      ts.className = 'timestamp';
      ts.textContent = formatTimestamp(timestamp);
      msgWrap.appendChild(ts);
    }
    // Add PDF download button to Gary's messages
    if (role !== 'user') {
      const dlBtn = document.createElement('button');
      dlBtn.className = 'download-btn';
      dlBtn.innerHTML = 'Save as PDF';
      dlBtn.onclick = () => downloadPDF(content);
      msgWrap.appendChild(dlBtn);
    }
    div.appendChild(avatar);
    div.appendChild(msgWrap);
    container.appendChild(div);
    scrollToBottom();
  }

  async function downloadPDF(content) {
    try {
      const res = await fetch('/api/generate-pdf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, title: 'Gary ReDeaux' })
      });
      if (!res.ok) throw new Error('PDF generation failed');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'gary_redeaux.pdf';
      a.click();
      URL.revokeObjectURL(url);
    } catch(e) {
      console.error('PDF error:', e);
    }
  }

  function showTyping() {
    const container = document.getElementById('messages');
    const div = document.createElement('div');
    div.className = 'message gary'; div.id = 'typing';
    const avatar = document.createElement('div');
    avatar.className = 'avatar'; avatar.textContent = '🎩';
    const typing = document.createElement('div');
    typing.className = 'typing';
    typing.innerHTML = '<span></span><span></span><span></span>';
    div.appendChild(avatar); div.appendChild(typing);
    container.appendChild(div);
    scrollToBottom();
  }

  function hideTyping() { const el = document.getElementById('typing'); if (el) el.remove(); }
  function scrollToBottom() { const c = document.getElementById('messages'); c.scrollTop = c.scrollHeight; }
  function handleKey(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }
  function autoResize(el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 120) + 'px'; }

  // ── VOICE INPUT ──────────────────────────────────────────────────────────────
  // Uses non-continuous mode to avoid echo/repeat issues.
  // Collects the full final transcript then sends once on recognition end.

  let micSent = false;

  function toggleMic() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      alert('Voice input not supported in this browser. Try Chrome!');
      return;
    }

    if (isListening) {
      recognition.stop();
      return;
    }

    micSent = false;
    recognition = new SpeechRecognition();
    recognition.lang = 'en-US';
    recognition.continuous = false;      // single utterance — stops automatically
    recognition.interimResults = false;  // final results only — no repeating partials

    recognition.onstart = () => {
      isListening = true;
      document.getElementById('micBtn').classList.add('listening');
      document.getElementById('micBtn').textContent = '🔴';
      document.getElementById('msgInput').placeholder = 'Listening... tap 🔴 to stop';
    };

    recognition.onresult = (event) => {
      // Grab only the final transcript from this single utterance
      let transcript = '';
      for (let i = 0; i < event.results.length; i++) {
        if (event.results[i].isFinal) {
          transcript += event.results[i][0].transcript;
        }
      }
      if (transcript.trim()) {
        document.getElementById('msgInput').value = transcript.trim();
        autoResize(document.getElementById('msgInput'));
      }
    };

    recognition.onend = () => {
      isListening = false;
      document.getElementById('micBtn').classList.remove('listening');
      document.getElementById('micBtn').textContent = '🎤';
      document.getElementById('msgInput').placeholder = 'Say something to Gary...';
      // Send once, only if we have content and haven't already sent
      const msg = document.getElementById('msgInput').value.trim();
      if (msg && !micSent && !isLoading) {
        micSent = true;
        setTimeout(() => sendMessage(), 100);
      }
    };

    recognition.onerror = (e) => {
      if (e.error !== 'aborted') console.error('Speech error:', e.error);
      isListening = false;
      document.getElementById('micBtn').classList.remove('listening');
      document.getElementById('micBtn').textContent = '🎤';
      document.getElementById('msgInput').placeholder = 'Say something to Gary...';
    };

    recognition.start();
  }

  // ── FILE UPLOAD ──────────────────────────────────────────────────────────────

  let selectedFiles = [];

  function handleFileSelect(event) {
    const files = Array.from(event.target.files).slice(0, 10);
    if (!files.length) return;
    selectedFiles = files;
    const names = files.map(f => f.name).join(', ');
    document.getElementById('filePreviewName').textContent = 
      files.length === 1 ? files[0].name : `${files.length} files: ${names}`;
    document.getElementById('filePreview').style.display = 'flex';
    document.getElementById('msgInput').placeholder = 'Add a message (optional)...';
  }

  function clearFile() {
    selectedFiles = [];
    document.getElementById('fileInput').value = '';
    document.getElementById('filePreview').style.display = 'none';
    document.getElementById('msgInput').placeholder = 'Say something to Gary...';
  }

  async function sendMessage(messageOverride) {
    const input = document.getElementById('msgInput');
    const msg = messageOverride || input.value.trim();

    if (selectedFiles.length && !messageOverride) {
      await sendWithFiles(msg);
      return;
    }

    if (!msg || isLoading) return;

    isLoading = true;
    document.getElementById('sendBtn').disabled = true;
    document.getElementById('uploadBtn').disabled = true;
    if (!messageOverride) { input.value = ''; autoResize(input); }

    const empty = document.getElementById('emptyState');
    if (empty) empty.remove();

    const now = new Date().toISOString();
    appendMessage('user', msg, now);
    showTyping();

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg, thread_id: currentThreadId, voice: voiceEnabled })
      });
      const data = await res.json();
      hideTyping();

      if (data.error) {
        appendMessage('gary', '*Something went wrong. Gary appears to be indisposed.*', null);
        console.error('Gary error:', data.error, data.detail);
      } else {
        currentThreadId = data.thread_id;
        appendMessage('gary', data.response, new Date().toISOString());
        if (data.audio) {
          const audio = document.getElementById('audioPlayer');
          audio.src = 'data:audio/mp3;base64,' + data.audio;
          audio.play();
        }
        await loadThreads();
      }
    } catch(e) {
      hideTyping();
      appendMessage('gary', '*Something went wrong. Gary appears to be indisposed.*', null);
    }

    isLoading = false;
    document.getElementById('sendBtn').disabled = false;
    document.getElementById('uploadBtn').disabled = false;
    scrollToBottom();
  }

  async function sendWithFiles(caption) {
    if (!selectedFiles.length || isLoading) return;

    isLoading = true;
    document.getElementById('sendBtn').disabled = true;
    document.getElementById('uploadBtn').disabled = true;

    const empty = document.getElementById('emptyState');
    if (empty) empty.remove();

    const fileNames = selectedFiles.map(f => f.name).join(', ');
    const displayMsg = caption
      ? `[${selectedFiles.length > 1 ? selectedFiles.length + ' files' : selectedFiles[0].name}] ${caption}`
      : `[${selectedFiles.length > 1 ? selectedFiles.length + ' files: ' + fileNames : selectedFiles[0].name}]`;
    appendMessage('user', displayMsg, new Date().toISOString());
    showTyping();

    const formData = new FormData();
    selectedFiles.forEach(f => formData.append('files', f));
    formData.append('message', caption || '');
    formData.append('thread_id', currentThreadId || '');
    formData.append('voice', voiceEnabled ? 'true' : 'false');

    const fileInput = document.getElementById('msgInput');
    fileInput.value = '';
    autoResize(fileInput);
    clearFile();

    try {
      const res = await fetch('/api/upload', { method: 'POST', body: formData });
      const data = await res.json();
      hideTyping();

      if (data.error) {
        appendMessage('gary', `*Gary frowns at the files. ${data.error}*`, null);
      } else {
        currentThreadId = data.thread_id;
        appendMessage('gary', data.response, new Date().toISOString());
        if (data.audio) {
          const audio = document.getElementById('audioPlayer');
          audio.src = 'data:audio/mp3;base64,' + data.audio;
          audio.play();
        }
        await loadThreads();
      }
    } catch(e) {
      hideTyping();
      appendMessage('gary', '*Something went wrong. Gary appears to be indisposed.*', null);
    }

    isLoading = false;
    document.getElementById('sendBtn').disabled = false;
    document.getElementById('uploadBtn').disabled = false;
    scrollToBottom();
  }

  loadThreads();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML)


if __name__ == "__main__":
    print("\n🇬🇧 Gary RéDeaux — Starting up...\n")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))