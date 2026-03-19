"""
main.py — Gary RéDeaux
Cloud-ready UI using Supabase for all storage.
"""

import os
from dotenv import load_dotenv
import gradio as gr

load_dotenv()

from gary_core import GaryCore
from gary_voice import speak

def get_supabase():
    from supabase import create_client
    return create_client(
        os.environ.get("SUPABASE_URL"),
        os.environ.get("SUPABASE_KEY")
    )


def save_message(thread_id, role, content):
    try:
        get_supabase().table("thread_messages").insert({
            "thread_id": thread_id,
            "role": role,
            "content": content
        }).execute()
        get_supabase().table("threads").update({
            "updated_at": "now()"
        }).eq("id", thread_id).execute()
    except Exception as e:
        print(f"⚠ Save message error: {e}")


def create_thread(title="New Chat"):
    try:
        res = get_supabase().table("threads").insert({"title": title}).execute()
        return res.data[0]["id"]
    except Exception as e:
        print(f"⚠ Create thread error: {e}")
        return None


def load_thread_messages(thread_id):
    try:
        res = get_supabase().table("thread_messages")\
            .select("role, content")\
            .eq("thread_id", thread_id)\
            .order("id")\
            .execute()
        return res.data or []
    except Exception as e:
        print(f"⚠ Load thread error: {e}")
        return []


def get_all_threads():
    try:
        res = get_supabase().table("threads")\
            .select("id, title, updated_at")\
            .order("updated_at", desc=True)\
            .limit(50)\
            .execute()
        return res.data or []
    except Exception as e:
        print(f"⚠ Get threads error: {e}")
        return []


def auto_title(first_message):
    words = first_message.strip().split()
    title = " ".join(words[:6])
    if len(words) > 6:
        title += "..."
    return title


def update_thread_title(thread_id, title):
    try:
        get_supabase().table("threads").update({"title": title}).eq("id", thread_id).execute()
    except Exception as e:
        print(f"⚠ Update title error: {e}")


gary = GaryCore()


def start_new_chat():
    gary.reset()
    thread_id = create_thread()
    return [], thread_id, refresh_thread_list()


def load_existing_thread(thread_id):
    messages = load_thread_messages(thread_id)
    gary.reset()
    gary.conversation_history = messages
    history = [{"role": m["role"], "content": m["content"]} for m in messages]
    return history, thread_id


def refresh_thread_list():
    threads = get_all_threads()
    if not threads:
        return gr.update(choices=[], value=None)
    choices = [(t["title"], t["id"]) for t in threads]
    return gr.update(choices=choices, value=choices[0][1] if choices else None)


def send_message(user_message, history, thread_id, voice_enabled):
    if not user_message.strip():
        return history, "", thread_id, None

    if not thread_id:
        thread_id = create_thread()

    # Auto-title on first message
    msgs = load_thread_messages(thread_id)
    if len(msgs) == 0:
        update_thread_title(thread_id, auto_title(user_message))

    try:
        gary_response = gary.chat(user_message)
    except Exception as e:
        gary_response = f"*Something went wrong. Gary appears to be indisposed.*\n\n`{str(e)}`"

    save_message(thread_id, "user", user_message)
    save_message(thread_id, "assistant", gary_response)

    history = history or []
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": gary_response})

    audio_path = None
    if voice_enabled:
        audio_path = speak(gary_response)

    return history, "", thread_id, audio_path


def build_ui():
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
                    show_label=False,
                    avatar_images=(None, "🎩"),
                    type="messages",
                )

                audio_out = gr.Audio(
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
        new_chat_btn.click(on_new_chat, outputs=[chatbot, thread_state, thread_list])
        thread_list.change(on_thread_select, inputs=[thread_list], outputs=[chatbot, thread_state])
        voice_toggle.change(on_voice_toggle, inputs=[voice_toggle], outputs=[audio_out])
        demo.load(refresh_thread_list, outputs=[thread_list])

    return demo


if __name__ == "__main__":
    print("\n🇬🇧 Gary RéDeaux — Starting up...\n")
    app = build_ui()
    app.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 7860)),
        root_path=os.environ.get("RAILWAY_PUBLIC_DOMAIN", ""),
        share=False,
    )
