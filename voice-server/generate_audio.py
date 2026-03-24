"""Generate pre-cached .ulaw audio clips using ElevenLabs API.

Run once before deploy:
    python generate_audio.py

Creates 4 .ulaw files in prewarmed_audio/ using Adam's cloned voice.
These are used for: AI disclosure (compliance), filler audio, technical
difficulty message, and goodbye — all played without LLM/TTS latency.
"""

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
    print("ERROR: Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in .env")
    sys.exit(1)

OUTPUT_DIR = Path(__file__).parent / "prewarmed_audio"
OUTPUT_DIR.mkdir(exist_ok=True)

CLIPS = {
    "disclosure": "This call may be recorded. You're speaking with an AI assistant.",
    "thinking": "Hmm...",
    "technical_difficulty": "I apologize, I'm having a little trouble. Let me call you right back.",
    "goodbye": "Appreciate your time. Take care.",
}

API_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"


def generate_clip(name: str, text: str):
    """Generate a single .ulaw clip via ElevenLabs HTTP API."""
    print(f"Generating {name}.ulaw: \"{text}\"")

    resp = requests.post(
        API_URL,
        params={"output_format": "ulaw_8000"},
        json={
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        },
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        },
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"  ERROR: {resp.status_code} — {resp.text[:200]}")
        return False

    output_path = OUTPUT_DIR / f"{name}.ulaw"
    output_path.write_bytes(resp.content)
    size_kb = len(resp.content) / 1024
    duration_ms = len(resp.content) / 8  # 8 bytes per ms at 8kHz mulaw
    print(f"  Saved: {output_path} ({size_kb:.1f} KB, ~{duration_ms:.0f}ms)")
    return True


def main():
    print(f"ElevenLabs Voice ID: {ELEVENLABS_VOICE_ID}")
    print(f"Output directory: {OUTPUT_DIR}\n")

    success = 0
    for name, text in CLIPS.items():
        if generate_clip(name, text):
            success += 1

    print(f"\nDone: {success}/{len(CLIPS)} clips generated.")
    if success < len(CLIPS):
        print("WARNING: Some clips failed — check API key and voice ID.")
        sys.exit(1)


if __name__ == "__main__":
    main()
