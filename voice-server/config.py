"""Environment configuration loader with validation."""

import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("voice-server")

REQUIRED_VARS = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "DEEPGRAM_API_KEY",
    "GROQ_API_KEY",
    "LLM_MODEL",
    "VERCEL_API_URL",
    "AGENT_SECRET",
]

# These have fallbacks so they're not strictly required
OPTIONAL_FALLBACK_VARS = {
    "ELEVENLABS_API_KEY": "",       # ElevenLabs TTS (backup)
    "ELEVENLABS_VOICE_ID": "",      # ElevenLabs voice ID
    "OPENAI_API_KEY": "",           # fallback LLM — Groq is primary
    "UPSTASH_REDIS_URL": "",        # falls back to Supabase polling
    "SERVER_BASE_URL": "http://localhost:8080",  # ngrok URL for prod
    "CARTESIA_API_KEY": "",         # Cartesia TTS (primary)
    "CARTESIA_VOICE_ID": "",        # Cartesia voice ID
    "GEMINI_API_KEY": "",           # Google Gemini (fallback LLM)
    "SAMBANOVA_API_KEY": "",        # SambaNova (fallback LLM)
    "RESEND_API_KEY": "",           # Resend email
    "STRIPE_WEBHOOK_SECRET": "",    # Stripe payments
}

OPTIONAL_DEFAULTS = {
    "LOG_LEVEL": "INFO",
    "MAX_CONCURRENT_CALLS": "10",
    "MAX_CALL_DURATION": "300",
    "SILENCE_TIMEOUT": "12",
    "SILENCE_EXIT_TIMEOUT": "20",
}


def _load_prompts() -> tuple[str, str]:
    """Load full + compact system prompts.

    Returns (full_prompt, compact_prompt).
    Full prompt used on first turn, compact on subsequent to save tokens.
    """
    full_prompt = ""
    compact_prompt = ""

    # Load full prompt
    prompt_path = Path(__file__).parent / "prompt.txt"
    if prompt_path.exists():
        text = prompt_path.read_text(encoding="utf-8").strip()
        if text and not text.startswith("# Paste"):
            full_prompt = text
            logger.info("Loaded full prompt (%d chars, ~%d tokens)", len(text), len(text) // 4)

    # Load compact prompt (check prompt_v2.txt first, then prompt_compact.txt)
    for compact_name in ["prompt_v2.txt", "prompt_compact.txt"]:
        compact_path = Path(__file__).parent / compact_name
        if compact_path.exists():
            text = compact_path.read_text(encoding="utf-8").strip()
            if text:
                compact_prompt = text
                logger.info("Loaded compact prompt from %s (%d chars, ~%d tokens)",
                           compact_name, len(text), len(text) // 4)
                break

    # Fallbacks
    if not full_prompt:
        full_prompt = compact_prompt or "You are Adam, an AI sales assistant for God's Cleaning Crew."
        logger.warning("prompt.txt not found or empty — using fallback")
    if not compact_prompt:
        compact_prompt = full_prompt

    return full_prompt, compact_prompt


def _validate() -> dict:
    """Validate env vars and return config dict."""
    missing = [v for v in REQUIRED_VARS if not os.getenv(v, "").strip()]
    if missing:
        print(f"FATAL: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    cfg = {}
    for var in REQUIRED_VARS:
        cfg[var.lower()] = os.getenv(var).strip()

    for var, default in OPTIONAL_FALLBACK_VARS.items():
        val = os.getenv(var, default).strip()
        cfg[var.lower()] = val
        if not val:
            logger.warning("%s not set — fallback will be used", var)

    for var, default in OPTIONAL_DEFAULTS.items():
        cfg[var.lower()] = os.getenv(var, default).strip()

    # Parse integers
    cfg["max_concurrent_calls"] = int(cfg["max_concurrent_calls"])
    cfg["max_call_duration"] = int(cfg["max_call_duration"])
    cfg["silence_timeout"] = int(cfg["silence_timeout"])
    cfg["silence_exit_timeout"] = int(cfg["silence_exit_timeout"])

    # Strip trailing slash from URLs
    cfg["vercel_api_url"] = cfg["vercel_api_url"].rstrip("/")
    cfg["server_base_url"] = cfg["server_base_url"].rstrip("/")

    # Load prompts (full + compact)
    full_prompt, compact_prompt = _load_prompts()
    cfg["system_prompt"] = full_prompt
    cfg["compact_prompt"] = compact_prompt

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, cfg["log_level"].upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    return cfg


config = _validate()
