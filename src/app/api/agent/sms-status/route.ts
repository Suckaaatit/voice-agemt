import { NextRequest, NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { logInfo } from "@/lib/logger";

export const maxDuration = 10;

/**
 * POST /api/agent/sms-status
 * Twilio SMS delivery status callback
 */
export async function POST(req: NextRequest) {
  try {
    const formData = await req.formData();
    const messageSid = formData.get("MessageSid") as string;
    const status = formData.get("MessageStatus") as string;

    if (messageSid && status) {
      await supabase.from("sms_delivery").upsert(
        {
          message_sid: messageSid,
          status,
          updated_at: new Date().toISOString(),
        },
        { onConflict: "message_sid" }
      );
      logInfo("SMS status update", { messageSid, status });
    }

    return NextResponse.json({ ok: true });
  } catch {
    return NextResponse.json({ ok: true });
  }
}
