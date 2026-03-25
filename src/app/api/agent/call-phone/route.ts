import { NextRequest, NextResponse } from "next/server";

export const maxDuration = 60;

/**
 * POST /api/agent/call-phone
 * Initiates a phone call via the ngrok-tunneled local voice server.
 * Separate from the Render-based web "Talk to Agent" pipeline.
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

    // Use ngrok URL for phone calls (local server)
    const voiceServerUrl =
      process.env.NGROK_VOICE_SERVER_URL ||
      process.env.VOICE_SERVER_URL ||
      "https://voice-agemt.onrender.com";
    const agentSecret = process.env.AGENT_SECRET || "";

    const response = await fetch(`${voiceServerUrl}/api/calls/initiate`, {
      method: "POST",
      headers: {
        "x-agent-secret": agentSecret,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        to: phone,
        prospect_id: `quick-dial-${Date.now()}`,
        prospect_name: name || "Prospect",
        property_name: "Demo Property",
      }),
    });

    if (!response.ok) {
      const errBody = await response.text();
      return NextResponse.json(
        { error: `Call failed: ${errBody}` },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json({
      success: true,
      call_id: data.call_id,
      twilio_sid: data.twilio_sid,
    });
  } catch (err: any) {
    return NextResponse.json(
      { error: err.message || "Call failed" },
      { status: 500 }
    );
  }
}
