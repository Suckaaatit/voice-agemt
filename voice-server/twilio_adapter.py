"""TwilioAdapter — bridges Twilio Media Streams to WebPipeline's WebSocket interface.

Twilio sends mulaw 8kHz audio as base64 JSON. WebPipeline expects raw PCM 16kHz binary.
This adapter translates between the two formats so WebPipeline can handle phone calls
with the exact same code path as browser calls.
"""

import asyncio
import audioop
import base64
import json
import logging

from starlette.websockets import WebSocket

logger = logging.getLogger("twilio-adapter")

# Twilio expects 20ms audio frames (160 bytes of mulaw at 8kHz)
TWILIO_FRAME_SIZE = 160


class TwilioAdapter:
    """Wraps a Twilio Media Stream WebSocket to look like a browser WebSocket.

    WebPipeline calls:
        - adapter.receive()     → returns {"type": "websocket.receive", "bytes": pcm_16khz}
        - adapter.send_bytes()  → converts PCM 24kHz to mulaw 8kHz, sends to Twilio
        - adapter.send_text()   → silently drops (Twilio doesn't understand JSON events)
    """

    def __init__(self, twilio_ws: WebSocket, call_sid: str):
        self.twilio_ws = twilio_ws
        self.call_sid = call_sid
        self.stream_sid: str = ""
        self._started = asyncio.Event()
        self._closed = False
        # Large queue — 2000 frames ≈ 40 seconds of audio at 20ms/frame
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._reader_task = None
        # Echo suppression: don't queue inbound audio while TTS is playing
        self._suppress_inbound = False
        self._suppress_until: float = 0.0

    async def start_reading(self):
        """Start background task to read Twilio messages and buffer audio.
        Must be called before WebPipeline.run() so "start" event is captured."""
        self._reader_task = asyncio.create_task(self._read_twilio_loop())
        # Wait for Twilio "start" event (streamSid) before returning
        try:
            await asyncio.wait_for(self._started.wait(), timeout=10.0)
            logger.info("Twilio adapter ready: streamSid=%s", self.stream_sid)
        except asyncio.TimeoutError:
            logger.error("Timeout waiting for Twilio start event")

    async def _read_twilio_loop(self):
        """Background: read Twilio messages, extract audio, queue for receive()."""
        import time

        while not self._closed:
            try:
                raw = await self.twilio_ws.receive_text()
            except Exception:
                self._closed = True
                await self._audio_queue.put(None)  # Signal disconnect
                return

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event = msg.get("event", "")

            if event == "start":
                self.stream_sid = msg.get("start", {}).get("streamSid", "")
                logger.info("Twilio stream started: streamSid=%s", self.stream_sid)
                self._started.set()

            elif event == "media":
                # Suppress inbound audio while TTS is playing to prevent echo loop
                # (loudspeaker mode picks up agent's voice → Deepgram transcribes it → infinite loop)
                now = time.time()
                if self._suppress_inbound or now < self._suppress_until:
                    continue

                payload = msg.get("media", {}).get("payload", "")
                if not payload:
                    continue

                # Decode base64 mulaw → PCM 16kHz
                mulaw_bytes = base64.b64decode(payload)
                pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
                pcm_16k = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)[0]
                # Prepend 0x00 flag byte (not TTS playing) — matches browser format
                # WebPipeline expects: [1 byte TTS flag] + [PCM data]
                pcm_16k = b'\x00' + pcm_16k

                try:
                    self._audio_queue.put_nowait(pcm_16k)
                except asyncio.QueueFull:
                    # Drop oldest frame, keep newest (preserves current speech)
                    try:
                        self._audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._audio_queue.put_nowait(pcm_16k)
                    except asyncio.QueueFull:
                        pass

            elif event == "stop":
                logger.info("Twilio stream stopped")
                self._closed = True
                await self._audio_queue.put(None)
                return

    async def receive(self) -> dict:
        """Return next audio frame from queue (called by WebPipeline)."""
        data = await self._audio_queue.get()
        if data is None:
            return {"type": "websocket.disconnect"}
        return {"type": "websocket.receive", "bytes": data}

    async def send_bytes(self, data: bytes) -> None:
        """Convert PCM 24kHz from WebPipeline to mulaw 8kHz and send to Twilio."""
        if self._closed:
            logger.debug("send_bytes: closed, skipping %d bytes", len(data))
            return

        # streamSid must be set by now (start_reading() waited for it)
        if not self.stream_sid:
            logger.warning("streamSid not set, skipping %d bytes audio", len(data))
            return

        logger.debug("send_bytes: %d bytes PCM → Twilio (streamSid=%s)", len(data), self.stream_sid[:10])

        # Suppress inbound audio while sending TTS (echo protection for loudspeaker)
        self._suppress_inbound = True

        try:
            # Resample 24kHz → 8kHz
            pcm_8k = audioop.ratecv(data, 2, 1, 24000, 8000, None)[0]

            # Convert linear PCM to mulaw
            mulaw_bytes = audioop.lin2ulaw(pcm_8k, 2)

            # Send as single chunk — Twilio handles buffering
            payload = base64.b64encode(mulaw_bytes).decode("ascii")
            await self.twilio_ws.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {
                    "payload": payload,
                },
            }))
            logger.debug("Sent %d bytes mulaw to Twilio (from %d bytes PCM)", len(mulaw_bytes), len(data))

        except Exception as e:
            if not self._closed:
                logger.warning("Failed to send audio to Twilio: %s", e)

    def mark_tts_done(self):
        """Called by WebPipeline when TTS finishes. Resume inbound audio after 1s echo tail."""
        import time
        self._suppress_inbound = False
        self._suppress_until = time.time() + 0.5  # 0.5s echo tail — minimal, let barge-in handle the rest

    async def send_text(self, data: str) -> None:
        """Drop text events — Twilio doesn't understand them."""
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the Twilio WebSocket."""
        if not self._closed:
            self._closed = True
            try:
                if self.stream_sid:
                    await self.twilio_ws.send_text(json.dumps({
                        "event": "clear",
                        "streamSid": self.stream_sid,
                    }))
                await self.twilio_ws.close(code=code)
            except Exception:
                pass
            if self._reader_task:
                self._reader_task.cancel()
