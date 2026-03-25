import { NextRequest, NextResponse } from "next/server";
import { randomUUID } from "crypto";

export const maxDuration = 60;

export async function POST(_: NextRequest) {
  try {
    const sessionId = randomUUID();
    // Voice server WebSocket URL — custom pipeline
    const voiceServerUrl = process.env.VOICE_SERVER_URL || "wss://voice-agemt.onrender.com";

    // Wake up Render if sleeping (free tier cold start ~50s)
    const httpUrl = voiceServerUrl.replace("wss://", "https://").replace("ws://", "http://");
    try {
      await fetch(`${httpUrl}/health`, { signal: AbortSignal.timeout(55000) });
    } catch {
      // Health check failed — return URL anyway, client will retry
    }

    const wsUrl = `${voiceServerUrl}/ws/web-call/${sessionId}`;

    return NextResponse.json({
      session_id: sessionId,
      ws_url: wsUrl,
    });
  } catch {
    return NextResponse.json({ error: "Failed to create web call session" }, { status: 500 });
  }
}
