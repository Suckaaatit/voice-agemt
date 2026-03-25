import { NextRequest, NextResponse } from "next/server";

export const maxDuration = 10;

/**
 * POST /api/voice/call-phone
 * Quick-dial: call a phone number without creating a prospect.
 * Client wakes Render first, then calls this route.
 */
export async function POST(req: NextRequest) {
  try {
    const { phone, name } = await req.json();

    if (!phone || !/^\+[1-9]\d{6,14}$/.test(phone)) {
      return NextResponse.json(
        { error: "Invalid phone number. Use E.164 format (e.g. +14155551234)" },
        { status: 400 }
      );
    }

    const voiceServerUrl =
      process.env.VOICE_SERVER_URL || "https://voice-agemt.onrender.com";
    const agentSecret = process.env.AGENT_SECRET || "";

    const response = await fetch(`${voiceServerUrl}/api/calls/initiate`, {
      method: "POST",
      headers: {
        "x-agent-secret": agentSecret,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        to: phone,
        prospect_id: `quick-${Date.now()}`,
        prospect_name: name || "Unknown",
        property_name: "Quick Dial",
      }),
    });

    if (!response.ok) {
      const errBody = await response.text();
      return NextResponse.json(
        { error: `Voice server error: ${errBody}` },
        { status: 502 }
      );
    }

    const data = await response.json();
    return NextResponse.json({
      success: true,
      call_id: data.call_id,
      twilio_sid: data.twilio_sid,
    });
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
