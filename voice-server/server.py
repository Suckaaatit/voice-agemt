"""FastAPI voice server — Twilio Media Streams + call control."""

import asyncio
import json
import logging
import uuid
from typing import Dict, Optional
from urllib.parse import urlencode

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse, HTMLResponse
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator

from config import config

logger = logging.getLogger("voice-server")

# ─── App ────────────────────────────────────────────────────────────

app = FastAPI(title="God's Cleaning Crew Voice Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Shared State (single worker only!) ─────────────────────────────

active_pipelines: Dict[str, "VoicePipeline"] = {}
call_semaphore = asyncio.Semaphore(config["max_concurrent_calls"])
twilio_client = TwilioClient(config["twilio_account_sid"], config["twilio_auth_token"])
twilio_validator = RequestValidator(config["twilio_auth_token"])
http_session: Optional[aiohttp.ClientSession] = None


# Late import to avoid circular — pipeline imports config directly
from web_pipeline import WebPipeline  # noqa: E402
from twilio_adapter import TwilioAdapter  # noqa: E402


# ─── Lifecycle ───────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global http_session
    http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        headers={"x-agent-secret": config["agent_secret"]},
    )
    logger.info("Voice server started — max %d concurrent calls", config["max_concurrent_calls"])


@app.on_event("shutdown")
async def shutdown():
    global http_session
    if http_session:
        await http_session.close()
    # Cleanup any remaining pipelines
    for cid, pipeline in list(active_pipelines.items()):
        await pipeline.cleanup()
    logger.info("Voice server shut down")


# ─── Auth Helper ─────────────────────────────────────────────────────

def _verify_agent_secret(request: Request):
    secret = request.headers.get("x-agent-secret", "")
    if secret != config["agent_secret"]:
        raise HTTPException(status_code=401, detail="Invalid agent secret")


# ─── Health ──────────────────────────────────────────────────────────

@app.get("/debug/config")
async def debug_config():
    return {
        "server_base_url": config.get("server_base_url", "NOT SET"),
        "has_twilio": bool(config.get("twilio_account_sid")),
        "has_deepgram": bool(config.get("deepgram_api_key")),
        "has_groq": bool(config.get("groq_api_key")),
        "has_cartesia": bool(config.get("cartesia_api_key")),
        "cartesia_voice_id": config.get("cartesia_voice_id", "NOT SET"),
        "llm_model": config.get("llm_model", "NOT SET"),
    }

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "active_calls": len(active_pipelines),
        "max_calls": config["max_concurrent_calls"],
    }


# ─── Web Call (Browser "Talk to Agent") ────────────────────────────

@app.websocket("/ws/web-call/{session_id}")
async def websocket_web_call(websocket: WebSocket, session_id: str):
    """Browser-based voice call — used by the Talk to Agent dashboard page."""
    await websocket.accept()
    pipeline = None
    acquired = False

    try:
        try:
            await asyncio.wait_for(call_semaphore.acquire(), timeout=15.0)
            acquired = True
        except asyncio.TimeoutError:
            await websocket.close(code=1013, reason="At capacity")
            return

        pipeline = WebPipeline(
            ws=websocket,
            call_id=session_id,
            http_session=http_session,
        )
        active_pipelines[f"web_{session_id}"] = pipeline
        logger.info("Web call started: session_id=%s", session_id)

        await pipeline.run()

    except WebSocketDisconnect:
        logger.info("Web call disconnected: session_id=%s", session_id)
    except Exception as e:
        logger.error("Web call error: session_id=%s error=%s", session_id, e, exc_info=True)
    finally:
        if pipeline:
            await pipeline.cleanup()
        active_pipelines.pop(f"web_{session_id}", None)
        if acquired:
            call_semaphore.release()
        logger.info("Web call ended: session_id=%s", session_id)


# ─── Debug ─────────────────────────────────────────────────────────

@app.get("/debug-config")
async def debug_config():
    return {"server_base_url": config["server_base_url"]}


# ─── Initiate Outbound Call ─────────────────────────────────────────

@app.post("/api/calls/initiate")
async def initiate_call(request: Request):
    _verify_agent_secret(request)

    body = await request.json()
    to_number = body.get("to")
    prospect_id = body.get("prospect_id", "")
    prospect_name = body.get("prospect_name", "")
    property_name = body.get("property_name", "")

    if not to_number:
        raise HTTPException(status_code=400, detail="Missing 'to' phone number")

    # Check capacity
    if call_semaphore._value <= 0:
        raise HTTPException(status_code=503, detail="At capacity — try again shortly")

    call_id = str(uuid.uuid4())
    base = config["server_base_url"]

    # URL-encode prospect metadata into webhook URLs
    meta = urlencode({
        "call_id": call_id,
        "prospect_id": prospect_id,
        "prospect_name": prospect_name,
        "property_name": property_name,
    })

    try:
        call = twilio_client.calls.create(
            to=to_number,
            from_=config["twilio_phone_number"],
            url=f"{base}/api/twilio/voice?{meta}",
            status_callback=f"{base}/api/twilio/status?{meta}",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            machine_detection="DetectMessageEnd",
            async_amd=True,
            async_amd_status_callback=f"{base}/api/twilio/amd?call_id={call_id}",
            async_amd_status_callback_method="POST",
            fallback_url=f"{base}/api/twilio/fallback",
            record=True,
        )
    except Exception as e:
        logger.error("Twilio call creation failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Twilio error: {e}")

    # Pre-register metadata for the pipeline
    active_pipelines[f"_meta_{call.sid}"] = {
        "call_id": call_id,
        "prospect_id": prospect_id,
        "prospect_name": prospect_name,
        "property_name": property_name,
    }

    logger.info("Call initiated: call_id=%s twilio_sid=%s to=%s", call_id, call.sid, _mask_phone(to_number))

    return JSONResponse({
        "success": True,
        "call_id": call_id,
        "twilio_sid": call.sid,
    })


# ─── Twilio TwiML: Start Media Stream ───────────────────────────────

@app.post("/api/twilio/voice")
async def twilio_voice(request: Request):
    try:
        form = await request.form()
        call_sid = form.get("CallSid", "unknown")
        call_id = request.query_params.get("call_id", call_sid)

        base = config.get("server_base_url", "")
        if not base:
            logger.error("SERVER_BASE_URL not set!")
            return Response(
                content='<?xml version="1.0"?><Response><Say>Server configuration error.</Say></Response>',
                media_type="application/xml",
            )

        # Convert https:// to wss:// for WebSocket
        ws_base = base.replace("https://", "wss://").replace("http://", "ws://")

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_base}/ws/voice/{call_sid}">
            <Parameter name="call_id" value="{call_id}" />
        </Stream>
    </Connect>
</Response>"""

        logger.info("TwiML generated for %s → %s/ws/voice/%s", call_id, ws_base, call_sid)
        return Response(content=twiml, media_type="application/xml")
    except Exception as e:
        logger.error("twilio_voice crashed: %s", e, exc_info=True)
        return Response(
            content='<?xml version="1.0"?><Response><Say>Technical error. Please try again.</Say></Response>',
            media_type="application/xml",
        )


# ─── Twilio TwiML: Fallback ─────────────────────────────────────────

@app.post("/api/twilio/fallback")
async def twilio_fallback(request: Request):
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">I apologize, we're having a brief technical issue. We'll call you right back.</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ─── Twilio: Call Status Callback ────────────────────────────────────

@app.post("/api/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    status = form.get("CallStatus", "")
    duration = form.get("CallDuration", "0")

    call_id = request.query_params.get("call_id", call_sid)
    prospect_id = request.query_params.get("prospect_id", "")

    logger.info("Call status: sid=%s status=%s duration=%ss", call_sid, status, duration)

    if status == "completed":
        pipeline = active_pipelines.get(call_sid)
        if pipeline:
            # Post call summary to Vercel
            asyncio.create_task(_post_call_summary(
                call_id=pipeline.call_id,
                prospect_id=pipeline.prospect_id,
                duration=int(duration),
                outcome=pipeline.get_outcome(),
                transcript=pipeline.get_transcript(),
            ))

    return Response(status_code=200)


# ─── Twilio: Async AMD Callback ─────────────────────────────────────

@app.post("/api/twilio/amd")
async def twilio_amd(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    answered_by = form.get("AnsweredBy", "unknown")

    logger.info("AMD result: sid=%s answered_by=%s", call_sid, answered_by)

    pipeline = active_pipelines.get(call_sid)
    if pipeline:
        pipeline.amd_result = answered_by
    else:
        logger.warning("AMD callback for unknown call: %s", call_sid)

    return Response(status_code=200)


# ─── Twilio: SMS Delivery Status ────────────────────────────────────

@app.post("/api/twilio/sms-status")
async def twilio_sms_status(request: Request):
    form = await request.form()
    message_sid = form.get("MessageSid", "")
    status = form.get("MessageStatus", "")
    call_id = request.query_params.get("call_id", "")

    logger.info("SMS status: sid=%s status=%s call_id=%s", message_sid, status, call_id)

    # Forward to Vercel for persistence
    if http_session:
        try:
            await http_session.post(
                f"{config['vercel_api_url']}/api/agent/sms-status",
                json={"message_sid": message_sid, "status": status},
            )
        except Exception as e:
            logger.error("Failed to forward SMS status: %s", e)

    return Response(status_code=200)


# ─── WebSocket: Twilio Media Stream ─────────────────────────────────

@app.websocket("/ws/voice/{call_sid}")
async def websocket_voice(websocket: WebSocket, call_sid: str):
    await websocket.accept()

    # Retrieve pre-registered metadata
    meta_key = f"_meta_{call_sid}"
    meta = active_pipelines.pop(meta_key, {})
    call_id = meta.get("call_id", call_sid)
    prospect_id = meta.get("prospect_id", "")
    prospect_name = meta.get("prospect_name", "")
    property_name = meta.get("property_name", "")

    acquired = False
    pipeline = None

    try:
        # Acquire semaphore with timeout
        try:
            await asyncio.wait_for(call_semaphore.acquire(), timeout=15.0)
            acquired = True
        except asyncio.TimeoutError:
            logger.warning("Semaphore timeout for call %s — rejecting", call_id)
            await websocket.close(code=1013, reason="Server at capacity")
            return

        # Use TwilioAdapter to bridge Twilio audio format to WebPipeline
        adapter = TwilioAdapter(twilio_ws=websocket, call_sid=call_sid)
        pipeline = WebPipeline(
            ws=adapter,
            call_id=call_id,
            http_session=http_session,
        )
        active_pipelines[call_sid] = pipeline

        logger.info("Pipeline started (via adapter): call_id=%s call_sid=%s prospect=%s",
                     call_id, call_sid, _mask_phone(prospect_name))

        await pipeline.run()

    except WebSocketDisconnect:
        logger.info("Twilio WS disconnected: call_sid=%s", call_sid)
    except Exception as e:
        logger.error("Pipeline error: call_sid=%s error=%s", call_sid, e, exc_info=True)
    finally:
        if pipeline:
            await pipeline.cleanup()
        active_pipelines.pop(call_sid, None)
        if acquired:
            call_semaphore.release()
        logger.info("Pipeline ended: call_sid=%s active=%d", call_sid, len(active_pipelines))


# ─── Helpers ─────────────────────────────────────────────────────────

def _mask_phone(phone: str) -> str:
    """Mask phone number for logging: +1555123**** """
    if len(phone) > 6:
        return phone[:-4] + "****"
    return "****"


async def _post_call_summary(call_id: str, prospect_id: str, duration: int,
                              outcome: str, transcript: list):
    """Post call summary to Vercel for CRM logging."""
    if not http_session:
        return
    try:
        await http_session.post(
            f"{config['vercel_api_url']}/api/agent/webhook",
            json={
                "event": "call_ended",
                "call_id": call_id,
                "prospect_id": prospect_id,
                "duration_seconds": duration,
                "outcome": outcome,
                "transcript": transcript,
            },
        )
    except Exception as e:
        logger.error("Failed to post call summary: %s", e)


# ─── Entry Point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8080,
        workers=1,  # CRITICAL: single worker for in-memory state
    )
