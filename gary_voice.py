"""
gary_voice.py — Gary's Mouth
Handles ElevenLabs text-to-speech for Gary.
"""

import os
import base64
import tempfile
from pathlib import Path

GARY_VOICE_ID = "1BUhH8aaMvGMUdGAmWVM"
ELEVENLABS_MODEL = "eleven_turbo_v2_5"  # Fast, high quality


def speak(text):
    """
    Convert text to Gary's voice using ElevenLabs.
    Returns a base64-encoded audio string for Gradio, or None if it fails.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("⚠ ELEVENLABS_API_KEY not set — voice disabled.")
        return None

    try:
        from elevenlabs.client import ElevenLabs
        from elevenlabs import VoiceSettings

        client = ElevenLabs(api_key=api_key)

        audio = client.text_to_speech.convert(
            voice_id=GARY_VOICE_ID,
            text=text,
            model_id=ELEVENLABS_MODEL,
            voice_settings=VoiceSettings(
                stability=0.5,
                similarity_boost=0.75,
                style=0.3,
                use_speaker_boost=True,
            ),
            output_format="mp3_44100_128",
        )

        # Collect audio chunks
        audio_bytes = b"".join(audio)

        # Save to temp file and return path for Gradio
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(audio_bytes)
        tmp.close()
        return tmp.name

    except Exception as e:
        print(f"⚠ Voice error: {e}")
        return None