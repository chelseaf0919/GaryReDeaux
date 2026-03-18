"""
main.py — Gary RéDeaux
The actual interface. Talk to Gary here.

Usage:
    python main.py
"""

import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
import gradio as gr

# Load .env file if present (local dev)
load_dotenv()

from gary_core import GaryCore
from gary_voice import speak

# ── THREAD STORAGE ────────────────────────────────────────────────────────────

DB_PATH = "gary_memory.db"

def init_threads_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT 'New Chat',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thread_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (thread_id) REFERENCES threads(id)
        )
    """)
    conn.commit()
    conn.close()


def save_message(thread_id, role, content):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO thread_messages (thread_id, role, content) VALUES (?, ?, ?)",
        (thread_id, role, content)
    )
    conn.execute(
        "UPDATE threads SET updated_at = datetime('now') WHERE id = ?",
        (thread_id,)
    )
    conn.commit()
    conn.close()


def create_thread(title="New Chat"):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("INSERT INTO threads (title) VALUES (?)", (title,))
    thread_id = cur.lastrowid
    conn.commit()
    conn.close()
    return thread_id


def load_thread_messages(thread_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT role, content FROM thread_messages WHERE thread_id = ? ORDER BY id",
        (thread_id,)
    ).fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


def get_all_threads():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title, updated_at FROM threads ORDER BY updated_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return rows


def auto_title(first_message):
    words = first_message.strip().split()
    title = " ".join(words[:6])
    if len(words) > 6:
        title += "..."
    return title


def update_thread_title(thread_id, title):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE threads SET title = ? WHERE id = ?", (title, thread_id))
    conn.commit()
    conn.close()


# ── CHAT LOGIC ────────────────────────────────────────────────────────────────

gary = GaryCore()
current_thread_id = None


def start_new_chat():
    global current_thread_id
    gary.reset()
    current_thread_id = create_thread()
    return [], current_thread_id, refresh_thread_list()


def load_existing_thread(thread_id):
    global current_thread_id
    current_thread_id = thread_id
    messages = load_thread_messages(thread_id)
    gary.reset()
    gary.conversation_history = messages

    history = []
    for i, msg in enumerate(messages):
        if msg["role"] == "user":
            gary_msg = messages[i + 1]["content"] if i + 1 < len(messages) else None
            history.append((msg["content"], gary_msg))

    return history, thread_id


def refresh_thread_list():
    threads = get_all_threads()
    if not threads:
        return gr.update(choices=[], value=None)
    choices = [(f"{t[1]}", t[0]) for t in threads]
    return gr.update(choices=choices, value=choices[0][1] if choices else None)


def send_message(user_message, history, thread_id, voice_enabled):
    global current_thread_id

    if not user_message.strip():
        return history, "", thread_id, None

    if not thread_id:
        thread_id = create_thread()
        current_thread_id = thread_id

    conn = sqlite3.connect(DB_PATH)
    msg_count = conn.execute(
        "SELECT COUNT(*) FROM thread_messages WHERE thread_id = ?", (thread_id,)
    ).fetchone()[0]
    conn.close()

    if msg_count == 0:
        update_thread_title(thread_id, auto_title(user_message))

    try:
        gary_response = gary.chat(user_message)
    except Exception as e:
        gary_response = f"*Something went wrong. Gary appears to be indisposed.*\n\n`{str(e)}`"

    save_message(thread_id, "user", user_message)
    save_message(thread_id, "assistant", gary_response)

    history = history or []
    history.append((user_message, gary_response))

    # Handle voice
    audio_path = None
    if voice_enabled:
        audio_path = speak(gary_response)

    return history, "", thread_id, audio_path


# ── UI ────────────────────────────────────────────────────────────────────────

def build_ui():
    init_threads_table()

    with gr.Blocks(
        title="Gary RéDeaux",
        theme=gr.themes.Base(
            primary_hue="slate",
            neutral_hue="slate",
            font=gr.themes.GoogleFont("Inter"),
        ),
        css="""
        #chatbox { height: 500px; }
        #send-btn { min-width: 80px; }
        #new-chat-btn { width: 100%; }
        .thread-list label { font-size: 0.8rem; color: #888; }
        #voice-toggle { margin-top: 0.5rem; }
        """
    ) as demo:

        thread_state = gr.State(None)

        with gr.Row():
            # ── Sidebar ──────────────────────────────────────────────────────
            with gr.Column(scale=1, min_width=220):
                gr.HTML("""
                    <div style="padding: 1rem 0 0.5rem 0;">
                        <div style="font-size:1.1rem; font-weight:700;">🇬🇧 Gary</div>
                        <div style="font-size:0.75rem; color:#888;">He simply arrived.</div>
                    </div>
                """)

                new_chat_btn = gr.Button("+ New Chat", variant="primary", elem_id="new-chat-btn")

                voice_toggle = gr.Checkbox(
                    label="🔊 Voice (Gary speaks)",
                    value=False,
                    elem_id="voice-toggle",
                    info="Toggle Gary's voice on/off"
                )

                thread_list = gr.Radio(
                    label="Past Chats",
                    choices=[],
                    value=None,
                    elem_classes=["thread-list"],
                    interactive=True,
                )

            # ── Main chat ─────────────────────────────────────────────────────
            with gr.Column(scale=4):
                gr.HTML("""
                    <div style="text-align:center; padding: 1.5rem 0 0.5rem 0;">
                        <div style="font-size:1.8rem; font-weight:700; letter-spacing:-0.02em;">
                            Gary RéDeaux
                        </div>
                        <div style="color:#888; font-size:0.85rem; margin-top:0.25rem;">
                            British · Posh · Keeping receipts since 2024
                        </div>
                    </div>
                """)

                chatbot = gr.Chatbot(
                    elem_id="chatbox",
                    bubble_full_width=False,
                    show_label=False,
                    avatar_images=(None, "🎩"),
                )

                # Audio output — hidden when voice is off
                audio_out = gr.Audio(
                    label="Gary's Voice",
                    autoplay=True,
                    visible=False,
                    show_label=False,
                )

                with gr.Row():
                    msg_input = gr.Textbox(
                        placeholder="Say something to Gary...",
                        show_label=False,
                        scale=5,
                        container=False,
                        autofocus=True,
                    )
                    send_btn = gr.Button("Send", variant="primary", elem_id="send-btn", scale=1)

        # ── Events ───────────────────────────────────────────────────────────

        def on_send(message, history, thread_id, voice_enabled):
            history, cleared, thread_id, audio = send_message(message, history, thread_id, voice_enabled)
            threads = refresh_thread_list()
            return history, cleared, thread_id, threads, audio

        def on_new_chat():
            history, thread_id, threads = start_new_chat()
            return history, thread_id, threads

        def on_thread_select(thread_id):
            if thread_id is None:
                return [], None
            history, tid = load_existing_thread(thread_id)
            return history, tid

        def on_voice_toggle(enabled):
            return gr.update(visible=enabled)

        send_btn.click(
            on_send,
            inputs=[msg_input, chatbot, thread_state, voice_toggle],
            outputs=[chatbot, msg_input, thread_state, thread_list, audio_out],
        )

        msg_input.submit(
            on_send,
            inputs=[msg_input, chatbot, thread_state, voice_toggle],
            outputs=[chatbot, msg_input, thread_state, thread_list, audio_out],
        )

        new_chat_btn.click(
            on_new_chat,
            outputs=[chatbot, thread_state, thread_list],
        )

        thread_list.change(
            on_thread_select,
            inputs=[thread_list],
            outputs=[chatbot, thread_state],
        )

        voice_toggle.change(
            on_voice_toggle,
            inputs=[voice_toggle],
            outputs=[audio_out],
        )

        demo.load(refresh_thread_list, outputs=[thread_list])

    return demo


# ── LAUNCH ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n🇬🇧 Gary RéDeaux — Starting up...\n")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠  WARNING: ANTHROPIC_API_KEY not set.")
        print("   Add it to your .env file or Railway Variables.\n")

    if not os.environ.get("ELEVENLABS_API_KEY"):
        print("⚠  WARNING: ELEVENLABS_API_KEY not set — voice disabled.")
        print("   Add it to your .env file or Railway Variables.\n")

    if not Path(DB_PATH).exists():
        print("⚠  WARNING: gary_memory.db not found.")
        print("   Run ingest.py first to load Gary's memories.\n")

    app = build_ui()
    app.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 7860)),
        share=False,
    )
