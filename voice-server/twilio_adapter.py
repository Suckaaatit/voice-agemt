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

    async def receive(self) -> dict:
        """Receive audio from Twilio, convert to PCM 16kHz for WebPipeline."""
        while not self._closed:
            try:
                raw = await self.twilio_ws.receive_text()
            except Exception:
                self._closed = True
                return {"type": "websocket.disconnect"}

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event = msg.get("event", "")

            if event == "start":
                self.stream_sid = msg.get("start", {}).get("streamSid", "")
                logger.info("Twilio stream started: streamSid=%s", self.stream_sid)
                self._started.set()
                continue

            elif event == "media":
                payload = msg.get("media", {}).get("payload", "")
                if not payload:
                    continue

                # Decode base64 mulaw
                mulaw_bytes = base64.b64decode(payload)

                # Convert mulaw to linear PCM 16-bit (8kHz)
                pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)

                # Resample 8kHz → 16kHz (double each sample)
                pcm_16k = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)[0]

                return {"type": "websocket.receive", "bytes": pcm_16k}

            elif event == "stop":
                logger.info("Twilio stream stopped")
                self._closed = True
                return {"type": "websocket.disconnect"}

            elif event == "mark":
                continue

        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data: bytes) -> None:
        """Convert PCM 24kHz from WebPipeline to mulaw 8kHz and send to Twilio."""
        if self._closed or not self.stream_sid:
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
                # Send clear to stop any playing audio
                if self.stream_sid:
                    await self.twilio_ws.send_text(json.dumps({
                        "event": "clear",
                        "streamSid": self.stream_sid,
                    }))
                await self.twilio_ws.close(code=code)
            except Exception:
                pass
