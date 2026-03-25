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
import struct

from starlette.websockets import WebSocket

logger = logging.getLogger("twilio-adapter")


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
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._reader_task = None

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
                payload = msg.get("media", {}).get("payload", "")
                if not payload:
                    continue

                # Decode base64 mulaw → PCM 16kHz
                mulaw_bytes = base64.b64decode(payload)
                pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
                pcm_16k = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)[0]

                try:
                    self._audio_queue.put_nowait(pcm_16k)
                except asyncio.QueueFull:
                    pass  # Drop oldest if queue full

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
            return

        # Wait for Twilio "start" event (max 5s) — greeting arrives before streamSid
        if not self.stream_sid:
            try:
                await asyncio.wait_for(self._started.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for Twilio streamSid")
                return

        try:
            # Resample 24kHz → 8kHz
            pcm_8k = audioop.ratecv(data, 2, 1, 24000, 8000, None)[0]

            # Convert linear PCM to mulaw
            mulaw_bytes = audioop.lin2ulaw(pcm_8k, 2)

            # Base64 encode
            payload = base64.b64encode(mulaw_bytes).decode("ascii")

            # Send as Twilio Media Stream JSON
            await self.twilio_ws.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {
                    "payload": payload,
                },
            }))
        except Exception as e:
            if not self._closed:
                logger.warning("Failed to send audio to Twilio: %s", e)

    async def send_text(self, data: str) -> None:
        """Drop text events — Twilio doesn't understand them."""
        # WebPipeline sends JSON events (transcript, agent_talking, etc.)
        # These are for the browser UI. On phone calls, just ignore them.
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
