"""VoicePipeline — one instance per active call.

Manages: Twilio audio ↔ Deepgram STT ↔ LLM (Groq/OpenAI) ↔ ElevenLabs TTS,
deterministic pattern matching, tool execution, payment listener, barge-in,
filler audio, silence/call watchdogs, and idempotent cleanup.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import aiohttp
import redis.asyncio as aioredis
import websockets

from config import config

logger = logging.getLogger("voice-server.pipeline")

# ─── Constants ───────────────────────────────────────────────────────

AUDIO_DIR = Path(__file__).parent / "prewarmed_audio"
CHUNK_SIZE = 640          # 20ms of mulaw 8kHz
CHUNK_INTERVAL = 0.02     # 20ms pacing
FILLER_THRESHOLD = 1.2    # seconds before playing filler
MAX_RESPONSE_CHARS = 350  # LLM response truncation limit (increased to avoid mid-word cuts)
SLIDING_WINDOW = 8        # conversation history size (messages) — 4 turns, keeps latency constant
DEEPGRAM_HEARTBEAT_S = 8  # KeepAlive interval
EL_RECONNECT_TURNS = 20   # reconnect ElevenLabs every N turns
EL_RECONNECT_IDLE_S = 60  # or after N seconds idle
CLAUSE_SPLIT_RE = re.compile(r'(?<=[,;:\u2014\u2013])\s+')
MIN_CLAUSE_LEN = 20
PAYMENT_POLL_INTERVAL = 2  # seconds


# ─── Call States ─────────────────────────────────────────────────────

class CallState(str, Enum):
    PENDING = "PENDING"
    GREETING = "GREETING"
    PITCH = "PITCH"
    QUALIFYING = "QUALIFYING"
    OBJECTION_HANDLING = "OBJECTION_HANDLING"
    CLOSING = "CLOSING"
    PAYMENT_SENT = "PAYMENT_SENT"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    GOODBYE = "GOODBYE"
    FOLLOWUP_SCHEDULED = "FOLLOWUP_SCHEDULED"


# ─── Deterministic Patterns ─────────────────────────────────────────

HARD_NO_PATTERNS = [
    "not interested", "no thanks", "no thank you", "remove me",
    "stop calling", "don't call again", "do not call", "take me off",
    "we're good", "pass", "we're all set", "no need",
]

BUSY_PATTERNS = [
    "busy right now", "in a meeting", "driving right now",
    "eating right now", "at lunch", "at dinner", "can't talk right now",
    "not a good time", "in the middle of something", "on the road",
    "dropping off", "picking up", "at the store", "running errands",
    "can you call back",
]

CHECK_LATER_PATTERNS = [
    "check later", "look at it later", "do it later",
    "get to it", "when i get a chance", "i'll take a look",
]

CALL_SCREENING_PATTERNS = [
    "state your name", "who is calling", "screening",
    "press 1", "press one", "para español", "please say your name",
]

VOICEMAIL_KEYWORDS = [
    "leave a message", "after the beep", "after the tone",
    "not available", "voicemail", "mailbox", "record your message",
    "please leave", "no one is available", "office hours",
]

CONNECTORS = {"but", "actually", "though", "however", "although"}

NEGATION_PREFIXES = ["i'm not", "i am not", "not really", "no i'm not"]

AVAILABILITY_CONNECTORS = [
    "but free now", "but i can talk", "but i'm free",
    "but available", "but go ahead",
]

# ─── LLM Tool Schemas ───────────────────────────────────────────────

LLM_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_payment_sms",
            "description": "Send a Stripe payment link via SMS to the prospect's phone number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Prospect phone number in E.164 format"},
                    "plan": {"type": "string", "enum": ["single", "double"], "description": "single=$650/1 incident, double=$1100/2 incidents"},
                },
                "required": ["phone", "plan"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_payment_email",
            "description": "Send a Stripe payment link via email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Prospect email address"},
                    "plan": {"type": "string", "enum": ["single", "double"], "description": "single=$650/1 incident, double=$1100/2 incidents"},
                },
                "required": ["email", "plan"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_followup",
            "description": "Schedule a follow-up callback with the prospect.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Follow-up date/time description"},
                    "reason": {"type": "string", "description": "Reason for follow-up"},
                },
                "required": ["date", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_objection",
            "description": "Log a prospect objection for analytics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["not_interested", "too_expensive", "send_info", "call_later", "has_provider", "busy_moment", "other"],
                        "description": "Objection category",
                    },
                    "statement": {"type": "string", "description": "Prospect's verbatim objection"},
                },
                "required": ["type", "statement"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_do_not_call",
            "description": "Mark prospect as do-not-call in the CRM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Reason for DNC"},
                },
                "required": ["reason"],
            },
        },
    },
]


# ═════════════════════════════════════════════════════════════════════
#  VoicePipeline
# ═════════════════════════════════════════════════════════════════════

class VoicePipeline:
    """Full-duplex voice pipeline for a single call."""

    def __init__(
        self,
        call_id: str,
        call_sid: str,
        twilio_ws: "WebSocket",
        http_session: aiohttp.ClientSession,
        prospect_id: str = "",
        prospect_name: str = "",
        property_name: str = "",
    ):
        # Identity
        self.call_id = call_id
        self.call_sid = call_sid
        self.prospect_id = prospect_id
        self.prospect_name = prospect_name
        self.property_name = property_name

        # Connections
        self.twilio_ws = twilio_ws
        self.http_session = http_session
        self.stream_sid: str = ""

        # Deepgram
        self.dg_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.dg_lock = asyncio.Lock()

        # ElevenLabs
        self.el_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.el_turn_count = 0
        self.el_last_used = time.time()

        # LLM
        self.groq_failures = 0
        self.use_openai = False  # per-call circuit breaker
        self.conversation: list = []  # sliding window
        self.key_facts: list = []  # deterministic fact extraction

        # State
        self.state = CallState.PENDING
        self.amd_result: str = ""
        self.is_first_response = True
        self.captured_email: str = ""
        self.captured_phone: str = ""

        # Audio control
        self._tts_task: Optional[asyncio.Task] = None
        self._is_speaking = False  # TTS currently playing
        self._interrupt = asyncio.Event()
        self._tts_started = asyncio.Event()

        # Timers
        self._last_speech_time = time.time()
        self._silence_prompted = False
        self._call_start_time = time.time()

        # Cleanup
        self._cleaned_up = False
        self._twilio_alive = True
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

        # Events buffer for batch flush
        self._events: list[dict] = []

        # Redis
        self._redis: Optional["aioredis.Redis"] = None
        self._pubsub = None

        # Speculative LLM
        self._speculative_task: Optional[asyncio.Task] = None
        self._speculative_transcript: str = ""
        self._last_partial: str = ""
        self._partial_stable_since: float = 0.0

    # ─── Main Loop ───────────────────────────────────────────────────

    async def run(self):
        """Main pipeline loop — process Twilio WebSocket messages."""
        # Connect to external services
        await self._connect_deepgram()
        # ElevenLabs disabled — Cartesia is primary TTS (HTTP, no persistent connection needed)
        self.el_ws = None
        await self._start_redis_listener()

        # Start background tasks
        self._tasks.append(asyncio.create_task(self._deepgram_receiver()))
        self._tasks.append(asyncio.create_task(self._deepgram_heartbeat()))
        self._tasks.append(asyncio.create_task(self._silence_watchdog()))
        self._tasks.append(asyncio.create_task(self._call_watchdog()))
        self._tasks.append(asyncio.create_task(self._prewarm_llm()))

        # Process Twilio messages
        try:
            while not self._shutdown.is_set():
                try:
                    msg = await asyncio.wait_for(
                        self.twilio_ws.receive_text(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    continue

                data = json.loads(msg)
                event = data.get("event")

                if event == "connected":
                    logger.info("Twilio stream connected: call_id=%s", self.call_id)

                elif event == "start":
                    self.stream_sid = data.get("start", {}).get("streamSid", "")
                    logger.info("Twilio stream started: streamSid=%s", self.stream_sid)
                    # Play disclosure + greeting immediately
                    self.state = CallState.GREETING
                    asyncio.create_task(self._play_greeting())

                elif event == "media":
                    # Forward audio to Deepgram
                    payload = data.get("media", {}).get("payload", "")
                    if payload:
                        audio_bytes = base64.b64decode(payload)
                        await self._send_to_deepgram(audio_bytes)

                elif event == "stop":
                    logger.info("Twilio stream stopped: call_id=%s", self.call_id)
                    self._shutdown.set()

        except Exception as e:
            if not self._shutdown.is_set():
                logger.error("Twilio message loop error: %s", e, exc_info=True)

    # ─── Greeting ────────────────────────────────────────────────────

    async def _play_greeting(self):
        """Play AI disclosure then greet immediately (no AMD delay)."""
        await self._play_cached_audio("disclosure")
        # Generate and speak the greeting via LLM
        greeting_prompt = self._build_greeting_context()
        self.conversation.append({"role": "user", "content": "[CALL_CONNECTED]"})
        response = await self._call_llm("[CALL_CONNECTED]")
        if response:
            await self._speak(response)
        self.state = CallState.PITCH

    def _build_greeting_context(self) -> str:
        """Build context injection for greeting."""
        parts = ["Current state: GREETING — deliver AI disclosure and introduction."]
        if self.prospect_name:
            parts.append(f"Prospect name: {self.prospect_name}")
        if self.property_name:
            parts.append(f"Property: {self.property_name}")
        return "\n".join(parts)

    # ─── Deepgram STT ────────────────────────────────────────────────

    async def _connect_deepgram(self):
        """Connect to Deepgram Nova-2 WebSocket."""
        url = (
            "wss://api.deepgram.com/v1/listen?"
            "model=nova-3&encoding=mulaw&sample_rate=8000&channels=1"
            "&smart_format=true&endpointing=300&interim_results=true"
            "&utterance_end_ms=1000"
        )
        headers = {"Authorization": f"Token {config['deepgram_api_key']}"}
        self.dg_ws = await websockets.connect(url, additional_headers=headers)
        logger.info("Deepgram connected: call_id=%s", self.call_id)

    async def _send_to_deepgram(self, audio: bytes):
        """Send audio to Deepgram (lock-protected)."""
        if not self.dg_ws:
            return
        async with self.dg_lock:
            try:
                await self.dg_ws.send(audio)
            except Exception as e:
                logger.error("Deepgram send error: %s", e)
                await self._reconnect_deepgram()

    async def _deepgram_receiver(self):
        """Receive and process Deepgram transcripts."""
        while not self._shutdown.is_set():
            try:
                if not self.dg_ws:
                    await asyncio.sleep(0.1)
                    continue
                msg = await asyncio.wait_for(self.dg_ws.recv(), timeout=1.0)
                data = json.loads(msg)
                await self._process_transcript(data)
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                logger.warning("Deepgram WS closed, reconnecting...")
                await self._reconnect_deepgram()
            except Exception as e:
                if not self._shutdown.is_set():
                    logger.error("Deepgram receiver error: %s", e)
                    await asyncio.sleep(0.5)

    async def _process_transcript(self, data: dict):
        """Process a Deepgram transcript message."""
        # Skip non-result messages (UtteranceEnd, metadata, etc.)
        msg_type = data.get("type", "")
        if msg_type == "UtteranceEnd":
            return

        channel = data.get("channel")
        if not channel or not isinstance(channel, dict):
            return

        alternatives = channel.get("alternatives")
        if not alternatives or not isinstance(alternatives, list):
            return

        alt = alternatives[0]
        if not isinstance(alt, dict):
            return

        transcript = alt.get("transcript", "").strip()
        if not transcript:
            return

        is_final = data.get("is_final", False)
        speech_final = data.get("speech_final", False)

        self._last_speech_time = time.time()
        self._silence_prompted = False

        if not is_final:
            # Partial transcript — handle barge-in
            if self._is_speaking:
                await self._barge_in()

            # Speculative LLM: track partial stability
            if transcript != self._last_partial:
                self._last_partial = transcript
                self._partial_stable_since = time.time()
                # Cancel stale speculative task
                if self._speculative_task and not self._speculative_task.done():
                    self._speculative_task.cancel()
                    self._speculative_task = None
            elif (time.time() - self._partial_stable_since > 0.3
                  and not self._speculative_task
                  and not self._is_speaking):
                # Stable partial + 300ms silence — start speculative LLM
                self._speculative_transcript = transcript
                self._speculative_task = asyncio.create_task(
                    self._speculative_llm(transcript)
                )
            return

        # Final transcript
        if not transcript:
            return

        logger.info("STT [%s]: %s", self.call_id[:8], transcript)

        # Extract key facts
        self._extract_facts(transcript)

        # Check if speculative result is usable
        speculative_response = None
        if (self._speculative_task
                and self._speculative_task.done()
                and self._speculative_transcript == transcript):
            try:
                speculative_response = self._speculative_task.result()
            except Exception:
                pass

        # Cancel speculative task if transcript changed
        if self._speculative_task and not self._speculative_task.done():
            self._speculative_task.cancel()
        self._speculative_task = None
        self._speculative_transcript = ""

        # Process the final transcript
        await self._handle_transcript(transcript, speculative_response)

    async def _speculative_llm(self, transcript: str) -> Optional[str]:
        """Speculative LLM generation on stable partial."""
        try:
            return await self._call_llm(transcript)
        except asyncio.CancelledError:
            return None

    async def _handle_transcript(self, transcript: str, speculative_response: Optional[str] = None):
        """Route transcript through patterns or LLM."""
        text_lower = transcript.lower().strip()

        # 1. Deterministic pattern matching (< 10ms)
        pattern_result = self._check_patterns(text_lower)
        if pattern_result:
            action, response_text = pattern_result
            await self._handle_pattern_action(action, response_text, transcript)
            return

        # 2. AMD voicemail check (first response only)
        if self.is_first_response and self.amd_result in ("machine_start", "machine_end_beep"):
            logger.info("AMD voicemail detected: call_id=%s", self.call_id)
            await self._hangup("Voicemail detected (AMD)")
            return

        self.is_first_response = False

        # 3. Use speculative response if available, otherwise call LLM
        if speculative_response:
            logger.info("Using speculative LLM response: call_id=%s", self.call_id)
            response = speculative_response
        else:
            self.conversation.append({"role": "user", "content": transcript})
            response = await self._call_llm(transcript)

        if response:
            self.conversation.append({"role": "assistant", "content": response})
            self._trim_conversation()
            await self._speak(response)

    def _extract_facts(self, transcript: str):
        """Extract key facts from transcript deterministically."""
        text = transcript.lower()
        # Email detection
        email_match = re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', transcript)
        if email_match:
            self.captured_email = self._correct_stt_email(email_match.group())
        # Phone detection (10+ digits)
        phone_match = re.search(r'[\d\s\-().]{10,}', transcript)
        if phone_match:
            digits = re.sub(r'\D', '', phone_match.group())
            if len(digits) >= 10:
                self.captured_phone = f"+1{digits[-10:]}" if len(digits) == 10 else f"+{digits}"

    def _correct_stt_email(self, email: str) -> str:
        """Apply STT corrections to email addresses."""
        email = email.lower().strip()
        email = email.replace(" dot com", ".com")
        email = email.replace(" dot ", ".")
        email = email.replace(" at ", "@")
        return email

    # ─── Deepgram Heartbeat & Reconnect ──────────────────────────────

    async def _deepgram_heartbeat(self):
        """Send KeepAlive to Deepgram every 8s (non-blocking)."""
        while not self._shutdown.is_set():
            await asyncio.sleep(DEEPGRAM_HEARTBEAT_S)
            if not self.dg_ws or self._shutdown.is_set():
                continue
            # Non-blocking: skip if audio is streaming
            if self.dg_lock.locked():
                continue
            try:
                async with self.dg_lock:
                    await self.dg_ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception as e:
                logger.warning("Deepgram heartbeat failed: %s — reconnecting", e)
                await self._reconnect_deepgram()

    async def _reconnect_deepgram(self):
        """Atomic reconnect: new WS first, then close old."""
        old_ws = self.dg_ws
        try:
            await self._connect_deepgram()
            logger.info("Deepgram reconnected: call_id=%s", self.call_id)
        except Exception as e:
            logger.error("Deepgram reconnect failed: %s", e)
            # Play technical difficulty and hang up
            await self._play_cached_audio("technical_difficulty")
            await self._hangup("Deepgram reconnect failed")
            return
        finally:
            if old_ws:
                try:
                    await old_ws.close()
                except Exception:
                    pass

    # ─── Deterministic Pattern Matching ──────────────────────────────

    def _check_patterns(self, text: str) -> Optional[tuple]:
        """Check text against deterministic patterns. Returns (action, response) or None."""

        # Hard No (from any state)
        for pattern in HARD_NO_PATTERNS:
            idx = text.find(pattern)
            if idx == -1:
                continue
            after = text[idx + len(pattern):]
            # Guard: >5 trailing chars that aren't punctuation
            after_stripped = after.lstrip()
            if len(after_stripped) > 5 and after_stripped[0].isalpha():
                return None  # LLM handles
            # Guard: connector follows
            for conn in CONNECTORS:
                if after_stripped.startswith(conn):
                    return None  # LLM handles
            return ("hard_no", "Understood. Appreciate your time. Take care.")

        # Busy (from any state)
        for pattern in BUSY_PATTERNS:
            if pattern not in text:
                continue
            # Guard: negation prefix
            for neg in NEGATION_PREFIXES:
                if neg in text and text.index(neg) < text.index(pattern):
                    return None  # LLM handles
            # Guard: availability connector
            for conn in AVAILABILITY_CONNECTORS:
                if conn in text:
                    return None  # LLM handles
            return ("busy", "Oh my bad. I'll call you tomorrow at 3 PM. If I catch ya, I catch ya.")

        # Check Later (WAITING_PAYMENT state only)
        if self.state == CallState.WAITING_PAYMENT:
            for pattern in CHECK_LATER_PATTERNS:
                if pattern in text:
                    return ("check_later", "No rush at all. I'll follow up in a couple days.")

        # Call Screening (first response only)
        if self.is_first_response:
            for pattern in CALL_SCREENING_PATTERNS:
                if pattern in text:
                    return ("call_screening", None)

        # Voicemail (first response only, 2+ keyword hits)
        if self.is_first_response:
            hits = sum(1 for kw in VOICEMAIL_KEYWORDS if kw in text)
            if hits >= 2:
                return ("voicemail", None)

        return None

    async def _handle_pattern_action(self, action: str, response_text: Optional[str], transcript: str):
        """Execute the action from a deterministic pattern match."""
        logger.info("Pattern match: action=%s call_id=%s", action, self.call_id)

        if action == "hard_no":
            # Log and execute DNC
            asyncio.create_task(self._execute_tool(
                "mark_do_not_call", {"reason": transcript}
            ))
            if response_text:
                await self._speak(response_text)
            self.state = CallState.GOODBYE
            await self._hangup("Hard no detected")

        elif action == "busy":
            asyncio.create_task(self._execute_tool(
                "schedule_followup", {"date": "tomorrow 3pm", "reason": f"Busy: {transcript}"}
            ))
            if response_text:
                await self._speak(response_text)
            self.state = CallState.FOLLOWUP_SCHEDULED
            await self._hangup("Busy — follow-up scheduled")

        elif action == "check_later":
            asyncio.create_task(self._execute_tool(
                "schedule_followup", {"date": "in 2 days", "reason": "Will check payment later"}
            ))
            if response_text:
                await self._speak(response_text)
            self.state = CallState.GOODBYE
            await self._hangup("Check later — follow-up scheduled")

        elif action == "call_screening":
            logger.info("Call screening detected — hanging up + reschedule")
            asyncio.create_task(self._execute_tool(
                "schedule_followup", {"date": "tomorrow", "reason": "Call screening detected"}
            ))
            await self._hangup("Call screening detected")

        elif action == "voicemail":
            logger.info("Voicemail detected (2+ keywords) — hanging up")
            await self._hangup("Voicemail detected")

    # ─── LLM (Groq primary, OpenAI fallback) ─────────────────────────

    async def _call_llm(self, user_text: str) -> Optional[str]:
        """Call LLM with function calling. Returns response text or None."""
        messages = self._build_llm_messages(user_text)

        if not self.use_openai:
            try:
                return await self._call_groq(messages)
            except Exception as e:
                self.groq_failures += 1
                logger.warning("Groq failed (%d/3): %s", self.groq_failures, e)
                if self.groq_failures >= 3:
                    self.use_openai = True
                    logger.info("Circuit breaker: switching to OpenAI for call %s", self.call_id)

        # Gemini fallback
        gemini_key = config.get("gemini_api_key", "")
        if gemini_key:
            try:
                return await self._call_gemini(messages)
            except Exception as e:
                logger.warning("Gemini also failed: %s", e)

        # OpenAI fallback
        try:
            return await self._call_openai(messages)
        except Exception as e:
            logger.error("All LLMs failed: %s", e)
            return None

    async def _call_gemini(self, messages: list) -> Optional[str]:
        """Call Google Gemini API (OpenAI-compatible endpoint)."""
        api_key = config.get("gemini_api_key", "")
        # Convert tools to Gemini-compatible format (no function calling for now)
        gemini_messages = []
        for msg in messages:
            role = msg["role"]
            if role == "system":
                gemini_messages.append({"role": "user", "parts": [{"text": f"[System]: {msg['content']}"}]})
                gemini_messages.append({"role": "model", "parts": [{"text": "Understood."}]})
            elif role == "user":
                gemini_messages.append({"role": "user", "parts": [{"text": msg["content"]}]})
            elif role == "assistant":
                gemini_messages.append({"role": "model", "parts": [{"text": msg["content"]}]})

        payload = {
            "contents": gemini_messages,
            "generationConfig": {
                "temperature": 0.4,
                "maxOutputTokens": 200,
            }
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

        async with self.http_session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise ValueError(f"Gemini {resp.status}: {body[:200]}")
            data = await resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    text = parts[0].get("text", "").strip()
                    # Truncate to ~220 chars at sentence boundary
                    if len(text) > 220:
                        for i in range(220, 0, -1):
                            if text[i] in ".!?":
                                text = text[:i+1]
                                break
                    self.conversation.append({"role": "assistant", "content": text})
                    return text
            return None

    def _build_llm_messages(self, user_text: str) -> list:
        """Build message list with system prompt, state context, and sliding window."""
        messages = [
            {"role": "system", "content": config["system_prompt"]},
            {"role": "system", "content": self._build_state_context()},
        ]
        # Sliding window of conversation
        messages.extend(self.conversation[-SLIDING_WINDOW:])
        # Current user message (if not already in conversation)
        if not self.conversation or self.conversation[-1].get("content") != user_text:
            messages.append({"role": "user", "content": user_text})
        return messages

    def _build_state_context(self) -> str:
        """Build state context injection for LLM."""
        parts = [
            f"Current state: {self.state.value}",
            f"Call ID: {self.call_id}",
        ]
        if self.prospect_name:
            parts.append(f"Prospect: {self.prospect_name}")
        if self.property_name:
            parts.append(f"Property: {self.property_name}")
        if self.captured_email:
            parts.append(f"Captured email: {self.captured_email}")
        if self.captured_phone:
            parts.append(f"Captured phone: {self.captured_phone}")
        if self.key_facts:
            parts.append(f"Key facts: {'; '.join(self.key_facts[-5:])}")
        return "\n".join(parts)

    async def _call_groq(self, messages: list) -> Optional[str]:
        """Call Groq API with function calling."""
        payload = {
            "model": config["llm_model"],
            "messages": messages,
            "tools": LLM_TOOLS,
            "temperature": 0.4,
            "max_tokens": 200,
        }
        async with self.http_session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {config['groq_api_key']}"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status == 429:
                raise Exception("Groq rate limited (429)")
            if resp.status != 200:
                body = await resp.text()
                raise Exception(f"Groq error {resp.status}: {body[:200]}")
            data = await resp.json()
            return await self._parse_llm_response(data)

    async def _call_openai(self, messages: list) -> Optional[str]:
        """Call OpenAI API (fallback)."""
        payload = {
            "model": "gpt-4o-mini",
            "messages": messages,
            "tools": LLM_TOOLS,
            "temperature": 0.4,
            "max_tokens": 200,
        }
        async with self.http_session.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {config['openai_api_key']}"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise Exception(f"OpenAI error {resp.status}: {body[:200]}")
            data = await resp.json()
            return await self._parse_llm_response(data)

    async def _parse_llm_response(self, data: dict) -> Optional[str]:
        """Parse LLM response: handle tool calls and text."""
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls", [])
        content = message.get("content", "")

        # Handle tool calls
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            idempotency_key = tc.get("id", f"{self.call_id}:{tool_name}:{hashlib.md5(json.dumps(args).encode()).hexdigest()}")

            # Validate before execution
            if not self._validate_tool_args(tool_name, args):
                logger.warning("Invalid tool args: %s %s", tool_name, args)
                continue

            # Execute tool (non-blocking for most)
            asyncio.create_task(self._execute_tool(tool_name, args, idempotency_key))

            # State transitions from tool calls
            if tool_name == "send_payment_sms":
                self.state = CallState.PAYMENT_SENT
            elif tool_name == "send_payment_email":
                if self.state not in (CallState.PAYMENT_SENT, CallState.WAITING_PAYMENT):
                    self.state = CallState.PAYMENT_SENT
            elif tool_name == "schedule_followup":
                self.state = CallState.FOLLOWUP_SCHEDULED
            elif tool_name == "mark_do_not_call":
                self.state = CallState.GOODBYE

        # Truncate response
        if content:
            content = self._truncate_response(content)

        return content or None

    def _validate_tool_args(self, tool_name: str, args: dict) -> bool:
        """Validate tool arguments before execution."""
        if tool_name == "send_payment_email":
            email = args.get("email", "")
            if not email or "@" not in email:
                return False
        elif tool_name == "send_payment_sms":
            phone = args.get("phone", "")
            digits = re.sub(r'\D', '', phone)
            if len(digits) < 10:
                return False
        elif tool_name == "schedule_followup":
            if not args.get("date"):
                return False
        return True

    def _truncate_response(self, text: str) -> str:
        """Truncate LLM response to ~220 chars at sentence boundary."""
        if len(text) <= MAX_RESPONSE_CHARS:
            return text
        # Find last sentence boundary before limit
        truncated = text[:MAX_RESPONSE_CHARS]
        for sep in [". ", "! ", "? "]:
            idx = truncated.rfind(sep)
            if idx > 0:
                return truncated[:idx + 1]
        # No sentence boundary found — truncate at last space
        idx = truncated.rfind(" ")
        if idx > 0:
            return truncated[:idx] + "."
        return truncated + "."

    def _trim_conversation(self):
        """Keep conversation to sliding window size."""
        if len(self.conversation) > SLIDING_WINDOW:
            self.conversation = self.conversation[-SLIDING_WINDOW:]

    async def _prewarm_llm(self):
        """Fire a throwaway request to warm Groq's container."""
        try:
            await self._call_groq([
                {"role": "system", "content": config["system_prompt"]},
                {"role": "user", "content": "ready"},
            ])
        except Exception:
            pass  # warmup failure is harmless

    # ─── Tool Execution ──────────────────────────────────────────────

    async def _execute_tool(self, tool_name: str, args: dict, idempotency_key: str = ""):
        """Execute a tool via Vercel API (non-blocking, with retries)."""
        if not idempotency_key:
            idempotency_key = f"{self.call_id}:{tool_name}:{hashlib.md5(json.dumps(args).encode()).hexdigest()}"

        payload = {
            "call_id": self.call_id,
            "prospect_id": self.prospect_id,
            "tool": tool_name,
            "arguments": args,
            "idempotency_key": idempotency_key,
        }

        for attempt in range(3):
            try:
                async with self.http_session.post(
                    f"{config['vercel_api_url']}/api/agent/actions",
                    json=payload,
                    headers={
                        "x-agent-secret": config["agent_secret"],
                        "x-idempotency-key": idempotency_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    result = await resp.json()

                    if result.get("success"):
                        logger.info("Tool executed: %s call_id=%s", tool_name, self.call_id)

                        # SMS → email auto-fallback
                        if tool_name == "send_payment_sms" and self.state == CallState.PAYMENT_SENT:
                            self.state = CallState.WAITING_PAYMENT
                        return result

                    # SMS failed — auto-fallback to email
                    if tool_name == "send_payment_sms" and not result.get("success"):
                        logger.warning("SMS failed — falling back to email: call_id=%s", self.call_id)
                        if self.captured_email:
                            await self._execute_tool(
                                "send_payment_email",
                                {"email": self.captured_email, "plan": args.get("plan", "single")},
                            )
                            # Inject system message so LLM knows
                            self.conversation.append({
                                "role": "system",
                                "content": "EVENT: SMS failed. Payment link sent via email instead.",
                            })
                        return result

                    logger.warning("Tool failed: %s attempt=%d result=%s", tool_name, attempt + 1, result)

            except Exception as e:
                logger.error("Tool execution error: %s attempt=%d error=%s", tool_name, attempt + 1, e)

            if attempt < 2:
                await asyncio.sleep(1)

        logger.error("Tool failed after 3 retries: %s call_id=%s", tool_name, self.call_id)
        self._events.append({
            "type": "tool_failed",
            "tool": tool_name,
            "call_id": self.call_id,
            "args": args,
        })

    # ─── ElevenLabs TTS ──────────────────────────────────────────────

    async def _connect_elevenlabs(self):
        """Connect to ElevenLabs WebSocket (try /stream-input, then /ws)."""
        voice_id = config["elevenlabs_voice_id"]
        api_key = config["elevenlabs_api_key"]

        # Try /stream-input path first
        for path in ["stream-input", "ws"]:
            url = (
                f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/"
                f"{path}?model_id=eleven_turbo_v2_5&output_format=ulaw_8000"
            )
            try:
                self.el_ws = await websockets.connect(
                    url,
                    additional_headers={"xi-api-key": api_key},
                )
                # Send BOS (beginning of stream)
                await self.el_ws.send(json.dumps({
                    "text": " ",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                    },
                    "xi_api_key": api_key,
                }))
                logger.info("ElevenLabs connected via /%s: call_id=%s", path, self.call_id)
                return
            except Exception as e:
                logger.warning("ElevenLabs /%s failed: %s", path, e)

        logger.error("ElevenLabs WebSocket connection failed — will use HTTP fallback")
        self.el_ws = None

    async def _reconnect_elevenlabs(self):
        """Reconnect ElevenLabs between turns."""
        old_ws = self.el_ws
        try:
            await self._connect_elevenlabs()
        except Exception as e:
            logger.error("ElevenLabs reconnect failed: %s", e)
        finally:
            if old_ws:
                try:
                    await old_ws.close()
                except Exception:
                    pass

    async def _speak(self, text: str):
        """Convert text to speech and stream to Twilio via Cartesia."""
        if not text or self._shutdown.is_set():
            return

        self._is_speaking = True
        self._interrupt.clear()
        self._tts_started.clear()

        # Start filler audio task (cancelled when real audio arrives)
        filler_task = asyncio.create_task(self._maybe_play_filler())

        try:
            await self._tts_cartesia(text)
        except Exception as e:
            logger.error("Cartesia TTS failed: %s", e)
        finally:
            filler_task.cancel()
            self._is_speaking = False

    async def _tts_cartesia(self, text: str):
        """Cartesia HTTP TTS — returns mulaw 8kHz for Twilio."""
        import audioop
        api_key = config.get("cartesia_api_key", "")
        voice_id = config.get("cartesia_voice_id", "")
        if not api_key or not voice_id:
            raise ValueError("Cartesia API key or voice ID not configured")

        clauses = self._split_clauses(text)
        for clause in clauses:
            if self._interrupt.is_set():
                break
            try:
                async with self.http_session.post(
                    "https://api.cartesia.ai/tts/bytes",
                    json={
                        "model_id": "sonic-3",
                        "transcript": clause,
                        "voice": {"mode": "id", "id": voice_id},
                        "output_format": {
                            "container": "raw",
                            "encoding": "pcm_s16le",
                            "sample_rate": 8000,
                        },
                        "language": "en",
                    },
                    headers={
                        "X-API-Key": api_key,
                        "Cartesia-Version": "2025-04-16",
                        "Content-Type": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        pcm_bytes = await resp.read()
                        # Convert 16-bit PCM to mulaw for Twilio
                        mulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)
                        self._tts_started.set()
                        await self._stream_audio_to_twilio(mulaw_bytes)
                    else:
                        body = await resp.text()
                        logger.error("Cartesia HTTP %d: %s", resp.status, body[:200])
                        raise ValueError(f"Cartesia HTTP {resp.status}")
            except asyncio.TimeoutError:
                logger.error("Cartesia TTS timeout for clause")
                raise

    async def _tts_websocket(self, text: str):
        """Stream text to ElevenLabs WebSocket and forward audio to Twilio."""
        if not self.el_ws:
            return

        # Send text
        await self.el_ws.send(json.dumps({"text": text + " "}))
        # Send EOS to flush
        await self.el_ws.send(json.dumps({"text": ""}))

        # Receive audio chunks
        while not self._interrupt.is_set():
            try:
                msg = await asyncio.wait_for(self.el_ws.recv(), timeout=5.0)
                data = json.loads(msg)

                audio_b64 = data.get("audio")
                if audio_b64:
                    self._tts_started.set()
                    audio_bytes = base64.b64decode(audio_b64)
                    await self._stream_audio_to_twilio(audio_bytes)

                if data.get("isFinal"):
                    break

            except asyncio.TimeoutError:
                logger.warning("ElevenLabs WS timeout — breaking")
                break
            except websockets.ConnectionClosed:
                logger.warning("ElevenLabs WS closed during TTS")
                self.el_ws = None
                break

    async def _tts_http(self, text: str):
        """ElevenLabs HTTP fallback with clause splitting."""
        voice_id = config["elevenlabs_voice_id"]
        api_key = config["elevenlabs_api_key"]
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=ulaw_8000"

        # Split into clauses for faster first-audio
        clauses = self._split_clauses(text)

        for clause in clauses:
            if self._interrupt.is_set():
                break
            try:
                async with self.http_session.post(
                    url,
                    json={
                        "text": clause,
                        "model_id": "eleven_turbo_v2_5",
                        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                    },
                    headers={"xi-api-key": api_key},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        audio_bytes = await resp.read()
                        self._tts_started.set()
                        await self._stream_audio_to_twilio(audio_bytes)
                    else:
                        logger.error("ElevenLabs HTTP error: %d", resp.status)
            except Exception as e:
                logger.error("ElevenLabs HTTP error: %s", e)

    def _split_clauses(self, text: str) -> list:
        """Split text at clause boundaries for HTTP TTS."""
        parts = CLAUSE_SPLIT_RE.split(text)
        # Merge short clauses
        result = []
        current = ""
        for part in parts:
            if current and len(current) >= MIN_CLAUSE_LEN:
                result.append(current.strip())
                current = part
            else:
                current = current + " " + part if current else part
        if current.strip():
            result.append(current.strip())
        return result if result else [text]

    async def _stream_audio_to_twilio(self, audio_bytes: bytes):
        """Stream audio bytes to Twilio WebSocket in 640-byte chunks."""
        for i in range(0, len(audio_bytes), CHUNK_SIZE):
            if self._interrupt.is_set() or not self._twilio_alive:
                break
            chunk = audio_bytes[i:i + CHUNK_SIZE]
            encoded = base64.b64encode(chunk).decode("ascii")
            await self._send_to_twilio({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": encoded},
            })
            await asyncio.sleep(CHUNK_INTERVAL)

    async def _maybe_play_filler(self):
        """Play filler audio if TTS takes >1.2s."""
        try:
            await asyncio.wait_for(self._tts_started.wait(), timeout=FILLER_THRESHOLD)
        except asyncio.TimeoutError:
            # TTS hasn't started — play filler
            if not self._tts_started.is_set() and not self._interrupt.is_set():
                await self._play_cached_audio("thinking")

    # ─── Pre-cached Audio Playback ───────────────────────────────────

    async def _play_cached_audio(self, name: str):
        """Play a pre-cached .ulaw file to Twilio."""
        path = AUDIO_DIR / f"{name}.ulaw"
        if not path.exists():
            logger.warning("Cached audio not found: %s", path)
            return

        audio_bytes = path.read_bytes()
        await self._stream_audio_to_twilio(audio_bytes)

    # ─── Barge-In ────────────────────────────────────────────────────

    async def _barge_in(self):
        """Interrupt current TTS when prospect speaks."""
        self._interrupt.set()
        # Cancel TTS task if running
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        # Clear Twilio audio buffer
        await self._send_to_twilio({
            "event": "clear",
            "streamSid": self.stream_sid,
        })
        self._is_speaking = False
        logger.info("Barge-in: call_id=%s", self.call_id)

    # ─── Twilio WebSocket Sends ──────────────────────────────────────

    async def _send_to_twilio(self, message: dict):
        """Send a message to Twilio WebSocket (with alive guard)."""
        if not self._twilio_alive or self._shutdown.is_set():
            return
        try:
            await self.twilio_ws.send_text(json.dumps(message))
        except Exception as e:
            logger.warning("Twilio send failed: %s", e)
            self._twilio_alive = False
            self._shutdown.set()

    def _twilio_ws_alive(self) -> bool:
        return self._twilio_alive and not self._shutdown.is_set()

    # ─── Silence Watchdog ────────────────────────────────────────────

    async def _silence_watchdog(self):
        """Monitor for silence: 12s → re-prompt, 20s → exit."""
        while not self._shutdown.is_set():
            await asyncio.sleep(1)
            if self._is_speaking:
                continue

            elapsed = time.time() - self._last_speech_time
            silence_timeout = config["silence_timeout"]
            exit_timeout = config["silence_exit_timeout"]

            if not self._silence_prompted and elapsed >= silence_timeout:
                self._silence_prompted = True
                logger.info("Silence %ds — re-prompting: call_id=%s", silence_timeout, self.call_id)
                await self._speak("Just wanted to make sure I didn't lose you.")

            elif self._silence_prompted and elapsed >= silence_timeout + exit_timeout:
                logger.info("Extended silence — graceful exit: call_id=%s", self.call_id)
                await self._speak("No worries — I can send the info over if that's easier.")
                await self._hangup("Extended silence")

    # ─── Call Watchdog ───────────────────────────────────────────────

    async def _call_watchdog(self):
        """End call after max duration."""
        max_duration = config["max_call_duration"]
        while not self._shutdown.is_set():
            await asyncio.sleep(5)
            elapsed = time.time() - self._call_start_time
            if elapsed >= max_duration:
                logger.info("Call watchdog: %ds elapsed — ending call: call_id=%s",
                            max_duration, self.call_id)
                await self._speak("I appreciate your time. Let me send over the information so you can review it at your convenience.")
                await self._hangup("Max duration reached")
                return

    # ─── Payment Listener ────────────────────────────────────────────

    async def _start_redis_listener(self):
        """Start Redis pub/sub listener for payment events."""
        try:
            self._redis = await aioredis.from_url(
                config["upstash_redis_url"],
                health_check_interval=30,
            )
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe("payments")
            self._tasks.append(asyncio.create_task(self._redis_listener()))
            logger.info("Redis payment listener started: call_id=%s", self.call_id)
        except Exception as e:
            logger.error("Redis connection failed: %s — using poll fallback", e)
            self._tasks.append(asyncio.create_task(self._payment_poll_fallback()))

    async def _redis_listener(self):
        """Listen for payment events on Redis pub/sub."""
        while not self._shutdown.is_set():
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0,
                )
                if message and message.get("type") == "message":
                    raw = message["data"]
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    data = json.loads(raw)
                    received_call_id = data.get("call_id", "").strip()
                    if received_call_id == self.call_id:
                        await self._handle_payment_confirmed(data)
                    elif received_call_id:
                        logger.debug("Payment event for other call: %s (ours: %s)",
                                     received_call_id, self.call_id)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                if not self._shutdown.is_set():
                    logger.error("Redis listener error: %s", e)
                    await asyncio.sleep(1)

    async def _payment_poll_fallback(self):
        """Poll Vercel for payment status (fallback if Redis unavailable)."""
        while not self._shutdown.is_set():
            await asyncio.sleep(PAYMENT_POLL_INTERVAL)
            if self.state not in (CallState.WAITING_PAYMENT, CallState.PAYMENT_SENT):
                continue
            try:
                async with self.http_session.get(
                    f"{config['vercel_api_url']}/api/agent/actions",
                    params={"call_id": self.call_id, "tool": "check_payment_status"},
                    headers={"x-agent-secret": config["agent_secret"]},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("paid"):
                            await self._handle_payment_confirmed(data)
            except Exception as e:
                logger.debug("Payment poll error: %s", e)

    async def _handle_payment_confirmed(self, data: dict):
        """Handle confirmed payment — interrupt and confirm."""
        if self.state == CallState.PAYMENT_CONFIRMED:
            return  # Already handled

        logger.info("Payment confirmed! call_id=%s amount=%s",
                     self.call_id, data.get("amount"))

        self.state = CallState.PAYMENT_CONFIRMED

        # Interrupt current audio
        if self._is_speaking:
            await self._barge_in()

        # Confirm payment verbally
        await self._speak("I can see that just came through. You're all set. Welcome aboard.")
        self.state = CallState.GOODBYE
        await asyncio.sleep(1)
        await self._play_cached_audio("goodbye")
        await self._hangup("Payment confirmed")

    # ─── Call Control ────────────────────────────────────────────────

    async def _hangup(self, reason: str):
        """Gracefully end the call."""
        logger.info("Hanging up: reason=%s call_id=%s", reason, self.call_id)
        self._shutdown.set()

        # Close Deepgram to stop STT
        if self.dg_ws:
            try:
                await self.dg_ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass

        # Small delay for final audio to play
        await asyncio.sleep(0.5)

    # ─── Outcome & Transcript ────────────────────────────────────────

    def get_outcome(self) -> str:
        """Map final state to call outcome."""
        state_outcome = {
            CallState.PAYMENT_CONFIRMED: "closed",
            CallState.GOODBYE: "connected",
            CallState.FOLLOWUP_SCHEDULED: "followup",
        }
        return state_outcome.get(self.state, "connected")

    def get_transcript(self) -> list:
        """Return conversation history."""
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self.conversation
            if m["role"] in ("user", "assistant")
        ]

    # ─── Cleanup ─────────────────────────────────────────────────────

    async def cleanup(self):
        """Idempotent cleanup — safe to call multiple times."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._shutdown.set()

        logger.info("Cleaning up pipeline: call_id=%s", self.call_id)

        # Flush remaining events
        if self._events:
            try:
                await self._flush_events()
            except Exception as e:
                logger.error("Event flush error: %s", e)

        # Cancel background tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Close Deepgram
        if self.dg_ws:
            try:
                await self.dg_ws.close()
            except Exception:
                pass
            self.dg_ws = None

        # Close ElevenLabs
        if self.el_ws:
            try:
                await self.el_ws.close()
            except Exception:
                pass
            self.el_ws = None

        # Unsubscribe Redis
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe("payments")
            except Exception:
                pass
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass

        logger.info("Pipeline cleanup complete: call_id=%s", self.call_id)

    async def _flush_events(self):
        """Flush buffered events to Vercel."""
        if not self._events or not self.http_session:
            return
        events = self._events[:]
        self._events.clear()
        for event in events:
            try:
                await self.http_session.post(
                    f"{config['vercel_api_url']}/api/agent/events",
                    json=event,
                    headers={"x-agent-secret": config["agent_secret"]},
                    timeout=aiohttp.ClientTimeout(total=5),
                )
            except Exception as e:
                logger.error("Event flush failed: %s", e)
