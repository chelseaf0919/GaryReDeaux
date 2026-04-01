"""
main.py - Gary ReDeaux
FastAPI + HTML/JS. Mobile-first with hamburger menu + voice input.
"""

import os
import io
import base64
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
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
        if gary.conversation_history:
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

@app.get("/api/threads/{thread_id}/messages")
async def get_messages(thread_id: int):
    try:
        from gary_core import embed_thread_async
        if gary.conversation_history:
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
