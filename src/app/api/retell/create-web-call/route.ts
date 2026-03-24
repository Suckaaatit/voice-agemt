import { NextRequest, NextResponse } from "next/server";
import { randomUUID } from "crypto";

export const maxDuration = 60;

export async function POST(_: NextRequest) {
  try {
    const sessionId = randomUUID();
    // Voice server WebSocket URL — uses custom pipeline, not Retell
    const voiceServerUrl = process.env.VOICE_SERVER_URL || "wss://voice-agemt.onrender.com";
    const wsUrl = `${voiceServerUrl}/ws/web-call/${sessionId}`;

    return NextResponse.json({
      session_id: sessionId,
      ws_url: wsUrl,
    });
  } catch {
    return NextResponse.json({ error: "Failed to create web call session" }, { status: 500 });
  }
}
