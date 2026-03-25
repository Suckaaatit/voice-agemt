"""WebPipeline — browser-based voice pipeline for Talk to Agent testing.

Browser audio (PCM 16kHz) ↔ Deepgram STT ↔ LLM (Groq) ↔ ElevenLabs TTS ↔ browser playback.
No Twilio dependency. Reuses pattern matching and LLM logic from the phone pipeline.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from typing import Optional

import aiohttp
import websockets

from config import config
from pipeline import (
    HARD_NO_PATTERNS, BUSY_PATTERNS, CHECK_LATER_PATTERNS,
    CONNECTORS, NEGATION_PREFIXES, AVAILABILITY_CONNECTORS,
    CallState, LLM_TOOLS, MAX_RESPONSE_CHARS, SLIDING_WINDOW,
    CLAUSE_SPLIT_RE, MIN_CLAUSE_LEN,
)

logger = logging.getLogger("web-pipeline")

GREETING = "Hey, how's it going? This is Adam with God's Cleaning Crew."

# ─── Filler phrases (played while LLM thinks) ──────────────────
import random
FILLERS = [
    "Yeah so...",
    "Right, so...",
    "Sure, so...",
    "Got it, so...",
    "Yeah...",
    "So...",
    "Hmm...",
]

# ─── Backchannel words (don't trigger LLM) ──────────────────────
BACKCHANNELS = {
    "uh huh", "uh-huh", "yeah", "right", "okay", "ok", "mm hmm",
    "mmhmm", "mm-hmm", "sure", "got it", "yep", "yup", "mhm",
    "go on", "i see", "ah", "oh", "hmm",
}

# ─── Emotion tag mapping for Cartesia ────────────────────────────
EMOTION_MAP = {
    "[calm]": ["positivity:low", "curiosity:low"],
    "[warm]": ["positivity:high", "curiosity:medium"],
    "[steady]": ["positivity:low", "curiosity:low"],
    "[excited]": ["positivity:high", "curiosity:high"],
}
EMOTION_TAG_RE = re.compile(r'^\[(calm|warm|steady|excited)\]\s*')


class WebPipeline:
    """Voice pipeline for browser-based calls via WebSocket."""

    def __init__(self, ws, call_id: str, http_session: aiohttp.ClientSession):
        self.ws = ws  # FastAPI WebSocket
        self.call_id = call_id
        self.http_session = http_session

        # State
        self.state = CallState.GREETING
        self.conversation: list[dict] = []
        self.is_first_response = True
        self.prospect_name = "Test Caller"
        self.property_name = "Demo Property"
        self.captured_email = ""
        self.captured_phone = ""
        self.key_facts: list[str] = []

        # LLM
        self.use_openai = False
        self.groq_failures = 0
        self._email_attempt_count = 0  # Track email collection attempts (max 1)

        # Tool call dedup — prevent double emails/SMS in same turn
        self._executed_tools: set = set()  # (name, args) tuples per turn
        # Recovery nudge — if LLM asks confirmation instead of acting
        self._pending_tool_nudge: bool = False
        # Response dedup — prevent repeating same response
        self._last_3_responses: list[str] = []

        # Audio connections
        self.dg_ws = None  # Deepgram
        self.el_ws = None  # ElevenLabs

        # Control
        self._shutdown = asyncio.Event()
        self._speaking = asyncio.Event()
        self._interrupt = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._cleaned_up = False
        self._last_speech = time.time()
        self._last_agent_text = ""
        self._turn_count = 0

        # TTS playback flag from browser (echo cancellation)
        self._tts_playing_on_client = False

        # Greeting protection — block barge-in for first 8 seconds
        self._call_start_time = time.time()
        self._greeting_protection_ms = 8000

        # Barge-in suppression — after interrupt, suppress agent audio for 1200ms
        self._barge_in_suppress_until: float = 0.0
        self._barge_in_cooldown_until: float = 0.0  # 500ms cooldown between barge-ins

        # Speculative generation
        self._speculative_task: Optional[asyncio.Task] = None
        self._speculative_transcript = ""
        self._speculative_result: Optional[str] = None
        self._partial_stable_time: float = 0.0
        self._last_partial: str = ""

        # Sequence ID for response invalidation (Bolna pattern)
        self._response_seq: int = 0
        self._valid_seqs: set = set()

        # Backchanneling — play "mm-hmm" after 5s of continuous user speech
        self._user_speaking_since: float = 0.0
        self._backchannel_played: bool = False

        # Per-component latency tracking
        self._latency: dict = {"stt": [], "llm": [], "tts": [], "total": []}

        # Deepgram utterance timeout — force-finalize if stuck
        self._last_interim_text: str = ""
        self._last_interim_time: float = 0.0
        self._dg_utterance_timeout: float = 8.0  # seconds — give user time to finish

    async def run(self):
        """Main pipeline loop."""
        try:
            await self._connect_deepgram()
            await self._send_event("ready", {})

            # Send greeting
            await self._speak(GREETING)
            self.conversation.append({"role": "assistant", "content": GREETING})
            await self._send_event("transcript", {"role": "agent", "text": GREETING})

            # Start background tasks
            self._tasks.append(asyncio.create_task(self._receive_browser_audio()))
            self._tasks.append(asyncio.create_task(self._process_deepgram()))
            self._tasks.append(asyncio.create_task(self._silence_watchdog()))
            self._tasks.append(asyncio.create_task(self._backchanneling_monitor()))
            self._tasks.append(asyncio.create_task(self._utterance_timeout_monitor()))

            # Wait for shutdown
            await self._shutdown.wait()

        except Exception as e:
            logger.error("WebPipeline error: %s", e, exc_info=True)
            await self._send_event("error", {"message": str(e)})
        finally:
            await self.cleanup()

    # ─── Browser Audio ─────────────────────────────────────────────

    async def _receive_browser_audio(self):
        """Receive PCM audio from browser and forward to Deepgram."""
        try:
            while not self._shutdown.is_set():
                try:
                    msg = await self.ws.receive()
                except Exception:
                    break

                msg_type = msg.get("type", "")

                if msg_type == "websocket.disconnect":
                    break

                if msg_type == "websocket.receive":
                    # Binary audio data (1-byte TTS flag + PCM Int16)
                    audio = msg.get("bytes")
                    if audio and len(audio) > 1 and self.dg_ws:
                        # First byte = isTTSPlaying flag from browser
                        self._tts_playing_on_client = (audio[0] == 1)
                        pcm_data = audio[1:]
                        # Send all audio to Deepgram — echo handled by barge-in threshold
                        try:
                            await self.dg_ws.send(pcm_data)
                            if not hasattr(self, '_audio_log_count'):
                                self._audio_log_count = 0
                            self._audio_log_count += 1
                            if self._audio_log_count <= 3:
                                logger.info("Audio to DG: %d bytes, first 4: %s", len(pcm_data), pcm_data[:4].hex())
                        except Exception as e:
                            logger.warning("Failed to send to Deepgram: %s", e)
                        continue

                    # Text command
                    text = msg.get("text")
                    if text:
                        try:
                            data = json.loads(text)
                            if data.get("type") == "end_call":
                                self._shutdown.set()
                                break
                        except json.JSONDecodeError:
                            pass
        except Exception as e:
            if not self._shutdown.is_set():
                logger.error("Browser audio receive error: %s", e)
        self._shutdown.set()

    # ─── Deepgram STT ──────────────────────────────────────────────

    async def _connect_deepgram(self):
        """Connect to Deepgram streaming STT."""
        url = (
            "wss://api.deepgram.com/v1/listen?"
            "encoding=linear16&sample_rate=16000&channels=1"
            "&model=nova-3&smart_format=true&endpointing=500"
            "&interim_results=true&utterance_end_ms=2000"
        )
        self.dg_ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {config['deepgram_api_key']}"},
            ping_interval=8,
        )
        logger.info("Deepgram connected: call_id=%s", self.call_id)

    async def _process_deepgram(self):
        """Process Deepgram transcript results."""
        try:
            dg_msg_count = 0
            async for msg in self.dg_ws:
                if self._shutdown.is_set():
                    break
                dg_msg_count += 1
                if dg_msg_count <= 3:
                    logger.info("DG raw msg #%d: %s", dg_msg_count, str(msg)[:200])
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    continue

                # Skip non-result messages (metadata, UtteranceEnd, etc.)
                msg_type = data.get("type", "")
                if msg_type == "UtteranceEnd":
                    continue

                channel = data.get("channel")
                if not channel or not isinstance(channel, dict):
                    continue

                alternatives = channel.get("alternatives")
                if not alternatives or not isinstance(alternatives, list):
                    continue

                alt = alternatives[0]
                if not isinstance(alt, dict):
                    continue

                transcript = alt.get("transcript", "").strip()
                if not transcript:
                    continue

                is_final = data.get("is_final", False)
                speech_final = data.get("speech_final", False)

                logger.debug("DG transcript: final=%s speech_final=%s text='%s'",
                             is_final, speech_final, transcript)

                # ── Barge-in with greeting protection + echo protection + cooldown ──
                now = time.time()

                # 1. Greeting protection — don't allow barge-in during first N ms
                in_greeting_protection = (now - self._call_start_time) * 1000 < self._greeting_protection_ms

                # 2. Cooldown — don't allow rapid-fire barge-ins
                in_cooldown = now < self._barge_in_cooldown_until

                # While agent is speaking, only allow barge-in on FINAL transcripts
                # that are long enough to be real user speech (not echo)
                # Echo from speakers is typically short fragments. Real interrupts are longer.
                if self._speaking.is_set():
                    # Time guard: no barge-in within 2s of agent starting to speak
                    agent_speaking_for = now - getattr(self, '_agent_speak_start', now)
                    if is_final and speech_final and len(transcript) > 35 and agent_speaking_for > 2.0:
                        # Check if this is echo (matches agent's last speech) or real user
                        is_echo = False
                        if self._last_agent_text:
                            agent_words = set(self._last_agent_text.lower().split())
                            user_words = set(transcript.lower().split())
                            if agent_words and user_words:
                                overlap = len(agent_words & user_words) / len(user_words)
                                if overlap > 0.3:  # Even 30% overlap is likely echo
                                    is_echo = True
                                    logger.debug("Echo detected (%.0f%% overlap): '%s'", overlap * 100, transcript[:40])

                        if not is_echo:
                            # Real user interrupt — stop agent and process
                            logger.info("Barge-in (real): '%s'", transcript[:50])
                            self._interrupt.set()
                            self._speaking.clear()
                            await self._send_event("agent_talking", {"value": False})
                            self.is_first_response = False
                            self._last_speech = time.time()
                            await self._send_event("transcript", {"role": "user", "text": transcript})
                            await self._handle_user_input(transcript)
                        else:
                            # Echo — just stop audio but don't process as user input
                            logger.info("Barge-in suppressed (echo): '%s'", transcript[:50])
                    # Skip all other transcripts while agent speaks
                    continue

                # ── Track interim for utterance timeout + backchanneling ──
                if not is_final and transcript:
                    self._last_interim_text = transcript
                    self._last_interim_time = time.time()
                    # Track continuous user speech for backchanneling
                    if not self._user_speaking_since:
                        self._user_speaking_since = time.time()

                # ── Speculative LLM on stable partials ──
                # Start LLM before user finishes speaking if partial is stable
                if not is_final and transcript:
                    now = time.time()
                    similarity = self._text_similarity(transcript, self._last_partial)
                    if similarity > 0.85:
                        # Partial hasn't changed much — track stability
                        if self._partial_stable_time == 0:
                            self._partial_stable_time = now
                        elif (now - self._partial_stable_time) > 0.6:
                            # Stable for 300ms — start speculative generation
                            if (not self._speculative_task or self._speculative_task.done()):
                                if self._text_similarity(transcript, self._speculative_transcript) < 0.9:
                                    self._speculative_transcript = transcript
                                    self._speculative_task = asyncio.create_task(
                                        self._speculative_llm(transcript)
                                    )
                                    logger.debug("Speculative LLM started for: '%s'", transcript[:50])
                    else:
                        # Partial changed — reset stability, cancel stale speculation
                        self._partial_stable_time = now
                        if self._speculative_task and not self._speculative_task.done():
                            self._speculative_task.cancel()
                            self._speculative_result = None
                    self._last_partial = transcript

                # ── Process final transcript ──
                if is_final and speech_final:
                    # Greeting echo dedup: skip if transcript matches the greeting
                    greeting_words = set(GREETING.lower().split())
                    t_words = set(transcript.lower().split())
                    if greeting_words and t_words:
                        greeting_overlap = len(greeting_words & t_words) / max(len(t_words), 1)
                        if greeting_overlap > 0.5:
                            logger.info("Greeting echo filtered: '%s'", transcript[:40])
                            continue

                    self.is_first_response = False
                    self._last_speech = time.time()
                    self._stt_done_time = time.time()  # latency tracking
                    # Reset backchannel + utterance timeout tracking
                    self._user_speaking_since = 0.0
                    self._backchannel_played = False
                    self._last_interim_text = ""
                    self._last_interim_time = 0.0
                    await self._send_event("transcript", {"role": "user", "text": transcript})
                    await self._handle_user_input(transcript)
                elif is_final:
                    self._last_speech = time.time()

        except websockets.exceptions.ConnectionClosed:
            if not self._shutdown.is_set():
                logger.warning("Deepgram connection closed unexpectedly")
        except Exception as e:
            if not self._shutdown.is_set():
                logger.error("Deepgram processing error: %s", e, exc_info=True)
        self._shutdown.set()

    # ─── Text Similarity (for speculative dedup) ─────────────────

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """Weighted text similarity — 70% weight on last 5 words, 30% overall."""
        if not a or not b:
            return 0.0
        a_lower, b_lower = a.lower().strip(), b.lower().strip()
        if a_lower == b_lower:
            return 1.0

        # Overall character-level similarity (Jaccard on words)
        a_words, b_words = set(a_lower.split()), set(b_lower.split())
        if not a_words or not b_words:
            return 0.0
        overall = len(a_words & b_words) / len(a_words | b_words)

        # Last 5 words similarity (more important)
        a_tail = " ".join(a_lower.split()[-5:])
        b_tail = " ".join(b_lower.split()[-5:])
        a_tw, b_tw = set(a_tail.split()), set(b_tail.split())
        tail_sim = len(a_tw & b_tw) / len(a_tw | b_tw) if (a_tw | b_tw) else 0.0

        return 0.3 * overall + 0.7 * tail_sim

    # ─── Speculative LLM ────────────────────────────────────────

    async def _speculative_llm(self, text: str):
        """Run LLM speculatively on partial transcript. Result is cached."""
        try:
            result = await self._call_llm(text)
            self._speculative_result = result
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Speculative LLM failed (expected): %s", e)

    # ─── User Input Handling ───────────────────────────────────────

    async def _handle_user_input(self, text: str):
        """Process final transcript: pattern match first, then LLM."""
        text_lower = text.lower().strip()
        if not text_lower:
            return

        # ── Backchannel detection — "uh huh", "yeah", "okay" aren't turns ──
        word_count = len(text_lower.split())
        if word_count <= 3 and text_lower.rstrip(".,!?") in BACKCHANNELS:
            logger.info("Backchannel detected, ignoring: '%s'", text_lower)
            # Don't trigger LLM, don't add to conversation
            # Just let the agent continue or wait
            return

        # Recovery nudge — if LLM asked "shall I send?" and user confirms
        if self._pending_tool_nudge:
            confirm_words = {"yes", "yeah", "yep", "sure", "go ahead", "do it", "okay", "ok", "yea", "please"}
            if text_lower.strip().rstrip(".!,") in confirm_words or any(w in text_lower for w in confirm_words):
                self._pending_tool_nudge = False
                # Inject "Yes, go ahead" to trigger the tool call
                self.conversation.append({"role": "user", "content": "Yes, go ahead."})
                logger.info("Recovery nudge: user confirmed, injecting 'Yes, go ahead'")
                try:
                    await self._respond_streaming("Yes, go ahead.")
                except Exception:
                    response = await self._call_llm("Yes, go ahead.")
                    if response:
                        await self._respond(response)
                return
            else:
                self._pending_tool_nudge = False

        self.conversation.append({"role": "user", "content": text})

        # Deterministic pattern matching (<10ms)
        pattern_result = self._check_patterns(text_lower)
        if pattern_result:
            # Cancel any speculative generation
            if self._speculative_task and not self._speculative_task.done():
                self._speculative_task.cancel()
            action, response_text = pattern_result
            logger.info("Pattern match: %s call_id=%s", action, self.call_id)
            if action in ("voicemail", "call_screening"):
                await self._send_event("call_ended", {"reason": action})
                self._shutdown.set()
                return
            if response_text:
                await self._respond(response_text)
            if action == "hard_no":
                await self._send_event("call_ended", {"reason": "hard_no"})
                self._shutdown.set()
            elif action == "busy":
                await self._send_event("call_ended", {"reason": "busy"})
                self._shutdown.set()
            elif action == "check_later":
                await self._send_event("call_ended", {"reason": "check_later"})
                self._shutdown.set()
            return

        # ── Check if speculative result is usable ──
        if (self._speculative_result and self._speculative_task
                and self._speculative_task.done()
                and self._text_similarity(text, self._speculative_transcript) > 0.85):
            logger.info("Using speculative LLM result (saved ~200ms)")
            response = self._speculative_result
            self._speculative_result = None
            self._speculative_transcript = ""
            await self._respond(response)
            return

        # Cancel stale speculation
        if self._speculative_task and not self._speculative_task.done():
            self._speculative_task.cancel()
        self._speculative_result = None

        # ── Play filler while LLM thinks (cuts perceived latency from 1.2s to ~200ms) ──
        t0 = time.time()
        filler = random.choice(FILLERS)
        filler_task = asyncio.create_task(self._speak(filler))

        # Start LLM in parallel
        response = await self._call_llm(text)
        llm_ms = (time.time() - t0) * 1000
        logger.info("⚡ LLM total: %.0fms | Response: '%s'", llm_ms, (response or "")[:60])

        # Wait for filler to finish (it's short, ~300ms)
        try:
            await asyncio.wait_for(filler_task, timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass

        if response:
            t1 = time.time()
            await self._respond(response)
            tts_ms = (time.time() - t1) * 1000
            total_ms = (time.time() - t0) * 1000
            logger.info("⚡ TTS: %.0fms | Total turn: %.0fms", tts_ms, total_ms)

    async def _respond(self, text: str):
        """Send full response via TTS and update conversation (non-streaming)."""
        # Dedup — only block CONSECUTIVE identical responses
        if self._last_3_responses and text.lower().strip() == self._last_3_responses[-1].lower().strip():
            logger.warning("Skipping consecutive duplicate: '%s'", text[:50])
            return
        self._last_3_responses.append(text)
        if len(self._last_3_responses) > 3:
            self._last_3_responses.pop(0)

        # Strip emotion tags before storing/displaying (TTS handles them separately)
        clean_text = EMOTION_TAG_RE.sub("", text).strip()
        self.conversation.append({"role": "assistant", "content": clean_text})
        # Trim conversation to keep latency constant across turns
        if len(self.conversation) > SLIDING_WINDOW:
            self.conversation = self.conversation[-SLIDING_WINDOW:]
        await self._send_event("transcript", {"role": "agent", "text": clean_text})
        await self._speak(text)  # TTS gets original with tags for emotion
        self._turn_count += 1

    async def _respond_streaming(self, user_text: str):
        """Stream LLM → Cartesia TTS — single continuous context, no gaps."""
        self._interrupt.clear()
        self._speaking.set()
        await self._send_event("agent_talking", {"value": True})

        self._response_seq += 1
        my_seq = self._response_seq
        self._valid_seqs.add(my_seq)

        turn_start = time.time()
        llm_first_token_time = None
        tts_first_audio_time = None
        full_response = ""

        try:
            cartesia_key = config.get("cartesia_api_key", "")
            voice_id = config.get("cartesia_voice_id", config.get("elevenlabs_voice_id", ""))
            url = (
                f"wss://api.cartesia.ai/tts/websocket"
                f"?api_key={cartesia_key}&cartesia_version=2025-04-16"
            )
            context_id = f"ctx-{self.call_id[:8]}-{self._turn_count}"

            async with websockets.connect(url) as cart_ws:
                audio_done = asyncio.Event()

                # ── Audio drain — runs in background, sends audio to browser ──
                async def drain_audio():
                    nonlocal tts_first_audio_time
                    try:
                        async for msg in cart_ws:
                            if self._interrupt.is_set() or my_seq not in self._valid_seqs:
                                try:
                                    await cart_ws.send(json.dumps({
                                        "context_id": context_id, "cancel": True
                                    }))
                                except Exception:
                                    pass
                                break
                            if self._shutdown.is_set():
                                break
                            data = json.loads(msg)
                            if data.get("type") == "chunk" and data.get("data"):
                                if tts_first_audio_time is None:
                                    tts_first_audio_time = time.time()
                                    tts_lat = (tts_first_audio_time - (llm_first_token_time or turn_start)) * 1000
                                    total_lat = (tts_first_audio_time - turn_start) * 1000
                                    self._latency["tts"].append(tts_lat)
                                    self._latency["total"].append(total_lat)
                                    logger.info("⚡ TTS first audio: %.0fms | Total: %.0fms", tts_lat, total_lat)
                                audio_bytes = base64.b64decode(data["data"])
                                await self._send_audio(audio_bytes)
                            elif data.get("type") == "error":
                                logger.warning("Cartesia error: %s", data.get("error"))
                                break
                    except websockets.exceptions.ConnectionClosed:
                        pass
                    except Exception as e:
                        if not self._shutdown.is_set():
                            logger.warning("Audio drain error: %s", e)
                    finally:
                        audio_done.set()

                drain_task = asyncio.create_task(drain_audio())

                # ── Send FIRST chunk with full voice config ──
                first_sent = False

                def _make_first_msg(text: str) -> str:
                    return json.dumps({
                        "model_id": "sonic-3",
                        "transcript": text,
                        "voice": {
                            "mode": "id",
                            "id": voice_id,
                            "__experimental_controls": {
                                "speed": "slowest",
                                "emotion": ["positivity:medium", "curiosity:low"],
                            },
                        },
                        "language": "en",
                        "context_id": context_id,
                        "output_format": {
                            "container": "raw",
                            "encoding": "pcm_s16le",
                            "sample_rate": 24000,
                        },
                        "continue": False,
                    })

                def _make_continue_msg(text: str) -> str:
                    """Continuation — same context, Cartesia appends seamlessly."""
                    return json.dumps({
                        "model_id": "sonic-3",
                        "transcript": text,
                        "voice": {"mode": "id", "id": voice_id},
                        "language": "en",
                        "context_id": context_id,
                        "output_format": {
                            "container": "raw",
                            "encoding": "pcm_s16le",
                            "sample_rate": 24000,
                        },
                        "continue": True,
                    })

                # ── TWO-PHASE TTS: quick first sentence + bulk remainder ──
                # Phase 1: Buffer until first sentence ends → send immediately
                # Phase 2: Accumulate ALL remaining text → send as ONE chunk
                # This gives fast start + zero gaps in the rest.
                SENTENCE_ENDERS = {".", "!", "?"}
                phase = 1  # 1 = looking for first sentence, 2 = accumulating rest
                first_sentence = ""
                remainder = ""

                async for token in self._stream_llm(user_text):
                    if self._interrupt.is_set() or self._shutdown.is_set():
                        break
                    if my_seq not in self._valid_seqs:
                        break

                    if llm_first_token_time is None:
                        llm_first_token_time = time.time()
                        llm_lat = (llm_first_token_time - turn_start) * 1000
                        self._latency["llm"].append(llm_lat)
                        logger.info("⚡ LLM first token: %.0fms", llm_lat)

                    full_response += token

                    if phase == 1:
                        first_sentence += token
                        stripped = first_sentence.rstrip()
                        if len(stripped) >= 15 and stripped[-1] in SENTENCE_ENDERS:
                            # Got first sentence — send to TTS NOW
                            try:
                                await cart_ws.send(_make_first_msg(first_sentence))
                                first_sent = True
                                logger.info("⚡ Phase 1: sent first sentence (%d chars) at %.0fms",
                                           len(first_sentence),
                                           (time.time() - turn_start) * 1000)
                            except Exception:
                                break
                            phase = 2  # Now just accumulate the rest
                    else:
                        remainder += token

                # Phase 2: Send ALL remaining text as ONE chunk — zero gaps
                if remainder.strip() and not self._interrupt.is_set():
                    try:
                        await cart_ws.send(_make_continue_msg(remainder))
                        logger.info("⚡ Phase 2: sent remainder (%d chars)", len(remainder))
                    except Exception:
                        pass

                # Edge case: LLM response was just one sentence (no remainder)
                if not first_sent and full_response.strip() and not self._interrupt.is_set():
                    try:
                        await cart_ws.send(_make_first_msg(full_response))
                    except Exception:
                        pass

                # Wait for all audio to finish playing
                try:
                    await asyncio.wait_for(audio_done.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    logger.warning("Audio drain timeout")
                drain_task.cancel()
                try:
                    await drain_task
                except (asyncio.CancelledError, Exception):
                    pass

        except Exception as e:
            logger.warning("Streaming TTS failed: %s", e)
            if not full_response:
                full_response = await self._call_llm(user_text) or ""
                if full_response:
                    await self._speak(full_response)
        finally:
            self._speaking.clear()
            self._last_speech = time.time()
            self._last_agent_text = full_response
            await self._send_event("agent_talking", {"value": False})

        if full_response:
            resp_lower = full_response.lower().strip()
            if resp_lower not in [r.lower().strip() for r in self._last_3_responses]:
                self._last_3_responses.append(full_response)
                if len(self._last_3_responses) > 3:
                    self._last_3_responses.pop(0)
                clean_response = EMOTION_TAG_RE.sub("", full_response).strip()
                self.conversation.append({"role": "assistant", "content": clean_response})
                # Trim conversation to keep latency constant
                if len(self.conversation) > SLIDING_WINDOW:
                    self.conversation = self.conversation[-SLIDING_WINDOW:]
                await self._send_event("transcript", {"role": "agent", "text": clean_response})
                self._turn_count += 1
            else:
                logger.warning("Skipping duplicate response: '%s'", full_response[:50])

    # ─── STT Fuzzy Normalization ────────────────────────────────────

    STT_CORRECTIONS = {
        # Common Deepgram misheards → correct intent
        "hand up": "hang up",
        "and the call": "end the call",
        "and call": "end call",
        "hang up call": "hang up",
        "dot come": "dot com",
        "dot calm": "dot com",
        "add sign": "at sign",
        "at the rate": "@",
        "no thanks bye": "no thanks",
        "i'm not interesting": "i'm not interested",
        "we're all sit": "we're all set",
        "we are all sit": "we are all set",
    }

    @classmethod
    def _normalize_stt(cls, text: str) -> str:
        """Fix common STT transcription errors."""
        normalized = text.lower().strip()
        for wrong, right in cls.STT_CORRECTIONS.items():
            normalized = normalized.replace(wrong, right)
        return normalized

    # ─── Hangup Guardrail ────────────────────────────────────────

    # Words that indicate the USER wants to end the call (not the agent deciding)
    HANGUP_INTENT_PHRASES = [
        "not interested", "stop calling", "don't call", "do not call",
        "remove me", "take me off", "no thanks", "no thank you",
        "goodbye", "bye", "hang up", "end the call", "we're all set",
        "we are all set", "pass", "i'm good", "we're good",
    ]

    def _user_wants_hangup(self, text: str) -> bool:
        """Check if the user's last message indicates end-of-call intent."""
        text_lower = text.lower()
        return any(phrase in text_lower for phrase in self.HANGUP_INTENT_PHRASES)

    # ─── Pattern Matching (reuse from pipeline.py) ─────────────────

    def _check_patterns(self, text: str) -> Optional[tuple]:
        """Deterministic pattern matching. Returns (action, response) or None."""
        # Apply STT normalization first
        text = self._normalize_stt(text)

        for pattern in HARD_NO_PATTERNS:
            idx = text.find(pattern)
            if idx == -1:
                continue
            after = text[idx + len(pattern):].lstrip()
            if len(after) > 5 and after[0].isalpha():
                return None
            for conn in CONNECTORS:
                if after.startswith(conn):
                    return None
            return ("hard_no", "Understood. Appreciate your time. Take care.")

        for pattern in BUSY_PATTERNS:
            if pattern not in text:
                continue
            for neg in NEGATION_PREFIXES:
                if neg in text and text.index(neg) < text.index(pattern):
                    return None
            for conn in AVAILABILITY_CONNECTORS:
                if conn in text:
                    return None
            return ("busy", "Oh my bad. I'll call you tomorrow at 3 PM. If I catch ya, I catch ya.")

        if self.state == CallState.WAITING_PAYMENT:
            for pattern in CHECK_LATER_PATTERNS:
                if pattern in text:
                    return ("check_later", "No rush at all. I'll follow up in a couple days.")

        return None

    # ─── LLM ───────────────────────────────────────────────────────

    def _build_messages(self, user_text: str = None) -> list:
        """Build LLM messages. Full prompt on first turn, compact on subsequent."""
        state_msg = f"State: {self.state.value} | Prospect: {self.prospect_name} | Property: {self.property_name}"
        if self.key_facts:
            state_msg += f" | Facts: {', '.join(self.key_facts[-5:])}"

        # Show last 3 agent responses so LLM can see its own patterns and vary
        recent_starters = []
        for msg in reversed(self.conversation):
            if msg.get("role") == "assistant" and len(recent_starters) < 3:
                first_word = msg["content"].split(",")[0].split()[0] if msg["content"].split() else ""
                recent_starters.append(first_word)
        if recent_starters:
            state_msg += f" | Your last response starters: {', '.join(recent_starters)}. DO NOT start with the same word again. Vary your openers: Yeah, Look, Honestly, Right, Hmm, or start directly without a filler."

        # Two-layer prompt: full on first turn (learn tone), compact on Turn 2+ (speed)
        if self._turn_count <= 1:
            prompt = config["system_prompt"]  # Full reference with all 60 objections
        else:
            prompt = config.get("compact_prompt") or config["system_prompt"]  # Compact guidelines
        # Inject critical runtime rules
        prompt += """

CRITICAL RUNTIME RULES:
1. You ALREADY introduced yourself at the start of the call. NEVER introduce yourself again. NEVER say "this is Adam" or "I'm from God's Cleaning Crew" again after the first greeting. Go straight to the conversation.
2. NEVER repeat yourself. If you already said it, move forward to the next point.
3. MAX 1-2 short sentences per turn. Think texting, not emailing.
4. Sound like a REAL person on the phone — not a script reader.
5. When handling objections, understand the CONTEXT of what they said. Don't give a generic response — address their SPECIFIC concern.

HOW TO SOUND HUMAN (this is the most important part):

PACING RULE — THIS IS CRITICAL:
Every sentence you say MUST be broken into short clauses separated by commas.
Think of each comma as a breath. Each clause should be 3-8 words max.

PERFECT EXAMPLE (copy this rhythm for EVERYTHING):
"We've got a couple of plans, $650 a year covers one incident, and $1,100 covers two, and that's it, no surprise bills, and if something happens, we're there within 4 hours, guaranteed."

Notice: short clause, comma, short clause, comma, short clause. Every clause is a bite-sized thought. This is how real people talk on the phone.

BAD (run-on, sounds robotic):
"So the reason I am calling is we work with buildings like yours condos retirement homes hotels that kind of thing and we set up biohazard cleanup coverage before anything actually happens."

GOOD (broken into clauses, sounds natural):
"So the reason I'm calling, we work with buildings like yours, condos, retirement homes, that kind of thing, and basically, we set up cleanup coverage, before anything actually happens."

MORE EXAMPLES:
BAD: "Yeah most properties don't have anything in place and when something happens like a death everyone is scrambling and costs go through the roof"
GOOD: "Yeah, most places don't have anything set up, and when something goes down, like a death in a unit, everyone's scrambling, costs go through the roof, it's a mess."

RULES:
- Every response MUST have at least 3 commas per sentence.
- No clause longer than 8 words without a comma.
- Use fillers: "yeah", "so", "look", "honestly", "you know"
- React FIRST: "Oh nice." "Ha, fair enough." "Right, right."
- Keep it simple and casual. You're chatting, not presenting.
- NEVER use tools unless user EXPLICITLY asks for email/payment.
- When asked "what's the plan?" — EXPLAIN it. Don't send an email.

EMOTION TAGS — prefix your response with a tone tag:
- [calm] when prospect sounds frustrated or annoyed
- [warm] when closing or building rapport
- [steady] when handling objections
- [excited] when prospect shows interest
Example: "[warm] Yeah, that's great, so you've already got something in place, then, right?"
Only one tag per response. Tag goes FIRST, before the text.
"""
        messages = [
            {"role": "system", "content": prompt},
            {"role": "system", "content": state_msg},
        ]

        messages.extend(self.conversation[-SLIDING_WINDOW:])
        return messages

    async def _call_llm(self, user_text: str) -> Optional[str]:
        """Call LLM (non-streaming). Groq primary, Gemini fallback."""
        messages = self._build_messages(user_text)

        # Primary: Groq
        try:
            return await self._call_groq_blocking(messages, config.get("llm_model", "llama-3.3-70b-versatile"))
        except Exception as e:
            logger.warning("Groq failed: %s", e)

        return "Sorry, can you say that one more time?"

    async def _stream_llm(self, user_text: str):
        """Streaming LLM — Groq primary, Gemini fallback. Yields tokens."""
        messages = self._build_messages(user_text)
        model = config.get("llm_model", "llama-3.3-70b-versatile")
        payload = {
            "model": model,
            "messages": messages,
            # No tools — agent just talks, all actions done manually
            "temperature": 0.4,
            "max_tokens": 250,
            "stream": True,
        }

        # Primary: Groq
        api_url = "https://api.groq.com/openai/v1/chat/completions"
        api_key = config["groq_api_key"]

        try:
            async with self.http_session.post(
                api_url,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"LLM stream {resp.status}")

                async for line in resp.content:
                    line = line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    token = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if token:
                        yield token

        except Exception as e:
            logger.error("Groq streaming failed: %s", e)
            yield "Sorry, give me one sec."

    async def _call_groq_blocking(self, messages: list, model: str) -> Optional[str]:
        payload = {
            "model": model,
            "messages": messages,
            # No tools — conversation only
            "temperature": 0.4,
            "max_tokens": 250,
        }
        async with self.http_session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {config['groq_api_key']}"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status == 429:
                raise Exception("rate limited")
            if resp.status == 413:
                raise Exception("prompt too large")
            if resp.status != 200:
                body = await resp.text()
                raise Exception(f"{resp.status}: {body[:200]}")
            data = await resp.json()
            return self._parse_response(data)

    async def _call_gemini_blocking(self, messages: list) -> Optional[str]:
        payload = {
            "model": "gemini-2.0-flash",
            "messages": messages,
            # No tools — conversation only
            "temperature": 0.4,
            "max_tokens": 250,
        }
        async with self.http_session.post(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {config['gemini_api_key']}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise Exception(f"Gemini {resp.status}: {body[:300]}")
            data = await resp.json()
            return self._parse_response(data)

    async def _call_openai_blocking(self, messages: list) -> Optional[str]:
        payload = {
            "model": "gpt-4o-mini",
            "messages": messages,
            # No tools — conversation only
            "temperature": 0.4,
            "max_tokens": 250,
        }
        async with self.http_session.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {config['openai_api_key']}"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise Exception(f"OpenAI {resp.status}: {body[:200]}")
            data = await resp.json()
            return self._parse_response(data)

    def _parse_response(self, data: dict) -> Optional[str]:
        choice = data.get("choices", [{}])[0]
        content = (choice.get("message", {}).get("content") or "").strip()

        # Truncate to max chars at sentence boundary
        if content and len(content) > MAX_RESPONSE_CHARS:
            for end in [". ", "! ", "? "]:
                idx = content.rfind(end, 0, MAX_RESPONSE_CHARS)
                if idx > 0:
                    content = content[:idx + 1]
                    break
            else:
                content = content[:MAX_RESPONSE_CHARS]

        # Safety: if response was cut mid-word by max_tokens, trim to last sentence
        if content and not content.rstrip().endswith(('.', '!', '?', '"')):
            for end in ['. ', '! ', '? ']:
                idx = content.rfind(end)
                if idx > 20:  # keep at least 20 chars
                    content = content[:idx + 1]
                    break

        return content if content else None

    # ─── ElevenLabs TTS ────────────────────────────────────────────

    async def _speak(self, text: str):
        """Generate TTS and send audio to browser/phone."""
        logger.info("Speaking: '%s'", text[:60])
        self._interrupt.clear()
        self._speaking.set()
        self._agent_speak_start = time.time()
        self._last_agent_text = text
        await self._send_event("agent_talking", {"value": True})
        # Start suppressing inbound audio NOW (before TTS chunks arrive)
        # Prevents echo from loudspeaker during Cartesia generation time
        if hasattr(self.ws, '_suppress_inbound'):
            self.ws._suppress_inbound = True

        try:
            # Try Cartesia WebSocket first, fall back to HTTP
            try:
                await self._tts_websocket(text)
                logger.info("TTS WS completed for: '%s'", text[:40])
            except Exception as e:
                logger.warning("Cartesia WS failed, using HTTP: %s", e)
                try:
                    await self._tts_http(text)
                    logger.info("TTS HTTP completed for: '%s'", text[:40])
                except Exception as e2:
                    logger.error("TTS HTTP also failed: %s", e2)
        except Exception as e:
            logger.error("TTS failed completely: %s", e)
        finally:
            self._speaking.clear()
            # Reset silence timer AFTER agent finishes — gives user time to respond
            self._last_speech = time.time()
            await self._send_event("agent_talking", {"value": False})
            # Tell TwilioAdapter to resume inbound audio (echo tail delay)
            if hasattr(self.ws, 'mark_tts_done'):
                self.ws.mark_tts_done()

    async def _tts_websocket(self, text: str):
        """Stream text to Cartesia WS and forward audio chunks to browser."""
        cartesia_key = config.get("cartesia_api_key", "")
        voice_id = config.get("cartesia_voice_id", config.get("elevenlabs_voice_id", ""))
        url = (
            f"wss://api.cartesia.ai/tts/websocket"
            f"?api_key={cartesia_key}&cartesia_version=2025-04-16"
        )
        context_id = f"ctx-{self.call_id[:8]}-{self._turn_count}"

        # Parse emotion tag from text (e.g., "[warm] Yeah, that's great...")
        emotion = ["positivity:low", "curiosity:low"]  # default calm
        tag_match = EMOTION_TAG_RE.match(text)
        if tag_match:
            tag = f"[{tag_match.group(1)}]"
            emotion = EMOTION_MAP.get(tag, emotion)
            text = text[tag_match.end():]  # Strip tag from text
            logger.debug("Emotion tag: %s → %s", tag, emotion)

        async with websockets.connect(url) as cart_ws:
            # Send generation request
            context_id = f"ctx-{self.call_id[:8]}-{self._turn_count}"
            request_payload = {
                "model_id": "sonic-3",
                "transcript": text,
                "voice": {
                    "mode": "id",
                    "id": voice_id,
                },
                "language": "en",
                "context_id": context_id,
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_s16le",
                    "sample_rate": 24000,
                },
            }
            logger.debug("TTS request: voice_id=%s text='%s'", voice_id, text[:40])
            await cart_ws.send(json.dumps(request_payload))

            # Receive audio chunks — stream to browser/phone immediately
            first_chunk = True
            tts_start = time.time()
            chunk_count = 0
            logger.info("TTS: waiting for Cartesia chunks (interrupt=%s shutdown=%s)",
                        self._interrupt.is_set(), self._shutdown.is_set())
            async for msg in cart_ws:
                if self._interrupt.is_set() or self._shutdown.is_set():
                    logger.warning("TTS: interrupted (interrupt=%s shutdown=%s) after %d chunks",
                                   self._interrupt.is_set(), self._shutdown.is_set(), chunk_count)
                    break
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    logger.warning("TTS: non-JSON message from Cartesia")
                    continue
                msg_type = data.get("type", "unknown")
                if msg_type == "chunk" and data.get("data"):
                    chunk_count += 1
                    if first_chunk:
                        logger.info("⚡ TTS first chunk: %dms", (time.time() - tts_start) * 1000)
                        first_chunk = False
                    audio_bytes = base64.b64decode(data["data"])
                    try:
                        await self.ws.send_bytes(audio_bytes)
                        logger.debug("TTS chunk %d: sent %d bytes", chunk_count, len(audio_bytes))
                    except Exception as send_err:
                        logger.error("Failed to send TTS chunk %d to WS: %s", chunk_count, send_err)
                        break
                elif msg_type == "done" or data.get("done"):
                    logger.info("TTS: done after %d chunks", chunk_count)
                    break
                elif msg_type == "error":
                    logger.error("Cartesia error: %s", data.get("error", "unknown"))
                    raise Exception(f"Cartesia error: {data.get('error')}")
                else:
                    logger.debug("TTS: unknown msg type: %s", msg_type)

    async def _tts_http(self, text: str):
        """HTTP fallback TTS via Cartesia."""
        cartesia_key = config.get("cartesia_api_key", "")
        voice_id = config.get("cartesia_voice_id", config.get("elevenlabs_voice_id", ""))
        url = "https://api.cartesia.ai/tts/bytes"

        # Split into clauses for faster first-chunk
        clauses = CLAUSE_SPLIT_RE.split(text)
        clauses = [c.strip() for c in clauses if len(c.strip()) >= MIN_CLAUSE_LEN]
        if not clauses:
            clauses = [text]

        for clause in clauses:
            if self._interrupt.is_set() or self._shutdown.is_set():
                break
            async with self.http_session.post(
                url,
                json={
                    "model_id": "sonic-3",
                    "transcript": clause,
                    "voice": {
                        "mode": "id",
                        "id": voice_id,
                        "__experimental_controls": {
                            "speed": "normal",
                            "emotion": ["positivity:low", "curiosity:low"],
                        },
                    },
                    "language": "en",
                    "output_format": {
                        "container": "raw",
                        "encoding": "pcm_s16le",
                        "sample_rate": 24000,
                    },
                },
                headers={
                    "Cartesia-Version": "2025-04-16",
                    "X-API-Key": cartesia_key,
                },
            ) as resp:
                if resp.status != 200:
                    logger.error("Cartesia HTTP %d", resp.status)
                    continue
                async for chunk in resp.content.iter_chunked(4800):  # 100ms at 24kHz 16-bit
                    if self._interrupt.is_set():
                        break
                    try:
                        await self._send_audio(chunk)
                    except Exception:
                        return

    # ─── Silence Watchdog ──────────────────────────────────────────

    async def _silence_watchdog(self):
        """Re-prompt after silence, exit after extended silence."""
        reprompted = False
        while not self._shutdown.is_set():
            await asyncio.sleep(2)
            # Don't count silence while agent is talking
            if self._speaking.is_set():
                continue

            silence = time.time() - self._last_speech

            if not reprompted and silence > 18 and not self._speaking.is_set():
                reprompted = True
                await self._respond("Just wanted to make sure I didn't lose you.")

            if reprompted and silence > 35:
                await self._respond("No worries — I can send the info over if that's easier.")
                await self._send_event("call_ended", {"reason": "silence_timeout"})
                self._shutdown.set()

            if silence < 5:
                reprompted = False

    # ─── Backchanneling Monitor ─────────────────────────────────────

    BACKCHANNEL_PHRASES = ["mm-hmm.", "right.", "yeah.", "got it."]

    async def _backchanneling_monitor(self):
        """Play 'mm-hmm' / 'yeah' after 5s of continuous user speech."""
        while not self._shutdown.is_set():
            await asyncio.sleep(1)
            if self._speaking.is_set() or not self._user_speaking_since:
                continue
            if self._backchannel_played:
                continue

            speaking_duration = time.time() - self._user_speaking_since
            if speaking_duration >= 5.0:
                # User has been talking for 5+ seconds — play acknowledgment
                import random
                phrase = random.choice(self.BACKCHANNEL_PHRASES)
                self._backchannel_played = True
                logger.debug("Backchanneling: '%s' after %.1fs of user speech", phrase, speaking_duration)
                try:
                    await self._speak(phrase)
                except Exception:
                    pass

    # ─── Deepgram Utterance Timeout Monitor ──────────────────────

    async def _utterance_timeout_monitor(self):
        """Force-finalize transcript if Deepgram gets stuck (no speech_final)."""
        while not self._shutdown.is_set():
            await asyncio.sleep(1)
            if not self._last_interim_text or not self._last_interim_time:
                continue
            if self._speaking.is_set():
                continue

            elapsed = time.time() - self._last_interim_time
            if elapsed >= self._dg_utterance_timeout:
                # Deepgram hasn't finalized — force process
                text = self._last_interim_text
                self._last_interim_text = ""
                self._last_interim_time = 0.0
                logger.warning("Utterance timeout: force-finalizing '%s' after %.1fs", text[:50], elapsed)
                self._last_speech = time.time()
                self._user_speaking_since = 0.0
                self._backchannel_played = False
                await self._send_event("transcript", {"role": "user", "text": text})
                await self._handle_user_input(text)

    # ─── Helpers ───────────────────────────────────────────────────

    async def _send_audio(self, audio_bytes: bytes):
        """Send audio to browser/phone. Only block on shutdown, never on interrupt."""
        if self._shutdown.is_set():
            return
        try:
            await self.ws.send_bytes(audio_bytes)
        except Exception as e:
            logger.warning("_send_audio failed: %s", e)

    async def _send_event(self, event_type: str, data: dict):
        """Send a JSON event to the browser."""
        try:
            await self.ws.send_text(json.dumps({"type": event_type, **data}))
        except Exception:
            pass

    async def cleanup(self):
        """Clean up all connections."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._shutdown.set()

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.dg_ws:
            try:
                await self.dg_ws.close()
            except Exception:
                pass

        logger.info("WebPipeline cleaned up: call_id=%s", self.call_id)
