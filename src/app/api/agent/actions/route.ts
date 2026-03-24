import { NextRequest, NextResponse } from "next/server";
import { supabase } from "@/lib/supabase";
import { stripe } from "@/lib/stripe";
import { config } from "@/lib/config";
import { logInfo, logError } from "@/lib/logger";

export const maxDuration = 15;

/**
 * POST /api/agent/actions
 *
 * Idempotent tool execution endpoint called by the voice server.
 * Handles: send_payment_sms, send_payment_email, schedule_followup,
 *          mark_do_not_call, log_objection
 */
export async function POST(req: NextRequest) {
  try {
    // Verify agent secret
    const secret = req.headers.get("x-agent-secret");
    if (secret !== process.env.AGENT_SECRET) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const body = await req.json();
    const { call_id, prospect_id, tool, arguments: args, idempotency_key } = body;

    if (!tool) {
      return NextResponse.json({ error: "Missing tool name" }, { status: 400 });
    }

    // Idempotency check — atomic upsert
    if (idempotency_key) {
      const { data: existing } = await supabase
        .from("processed_tool_calls")
        .select("result, status")
        .eq("idempotency_key", idempotency_key)
        .maybeSingle();

      if (existing?.status === "completed") {
        logInfo("Tool call deduped", { idempotency_key, tool });
        return NextResponse.json(existing.result);
      }

      // Mark as processing
      await supabase.from("processed_tool_calls").upsert(
        {
          idempotency_key,
          call_id,
          tool,
          status: "processing",
          created_at: new Date().toISOString(),
        },
        { onConflict: "idempotency_key", ignoreDuplicates: true }
      );
    }

    let result: Record<string, unknown>;

    switch (tool) {
      case "send_payment_sms":
        result = await handleSendPaymentSms(call_id, prospect_id, args);
        break;
      case "send_payment_email":
        result = await handleSendPaymentEmail(call_id, prospect_id, args);
        break;
      case "schedule_followup":
        result = await handleScheduleFollowup(call_id, prospect_id, args);
        break;
      case "mark_do_not_call":
        result = await handleMarkDNC(prospect_id, args);
        break;
      case "log_objection":
        result = await handleLogObjection(call_id, prospect_id, args);
        break;
      default:
        result = { success: false, error: `Unknown tool: ${tool}` };
    }

    // Mark as completed
    if (idempotency_key) {
      await supabase
        .from("processed_tool_calls")
        .update({ result, status: "completed" })
        .eq("idempotency_key", idempotency_key);
    }

    return NextResponse.json(result);
  } catch (err) {
    logError("Agent action failed", err);
    return NextResponse.json({ success: false, error: "Internal error" }, { status: 500 });
  }
}

// ─── Tool Handlers ───────────────────────────────────────────────

async function handleSendPaymentSms(
  callId: string,
  prospectId: string,
  args: { phone: string; plan: string }
): Promise<Record<string, unknown>> {
  const { phone, plan } = args;
  if (!phone || phone.length < 10) {
    return { success: false, error: "Invalid phone number" };
  }

  try {
    // Create Stripe Checkout Session
    const priceAmount = plan === "double" ? 110000 : 65000;
    const session = await stripe.checkout.sessions.create({
      payment_method_types: ["card"],
      line_items: [
        {
          price_data: {
            currency: "usd",
            product_data: {
              name: plan === "double" ? "Biohazard Response Plan (2 incidents)" : "Biohazard Response Plan (1 incident)",
            },
            unit_amount: priceAmount,
          },
          quantity: 1,
        },
      ],
      mode: "payment",
      success_url: `${config.app.url}/payment/success`,
      cancel_url: `${config.app.url}/payment/cancel`,
      metadata: { call_id: callId, prospect_id: prospectId },
    });

    // Send SMS via Twilio
    const twilio = require("twilio")(
      process.env.TWILIO_ACCOUNT_SID,
      process.env.TWILIO_AUTH_TOKEN
    );
    const message = await twilio.messages.create({
      body: `Here's your biohazard response plan payment link: ${session.url}`,
      from: process.env.TWILIO_PHONE_NUMBER,
      to: phone,
      statusCallback: `${config.app.url}/api/agent/sms-status`,
    });

    logInfo("SMS sent", { sid: message.sid, phone, callId });
    return { success: true, sid: message.sid, checkout_url: session.url };
  } catch (err: any) {
    logError("SMS send failed", err);
    return { success: false, error: err.message };
  }
}

async function handleSendPaymentEmail(
  callId: string,
  prospectId: string,
  args: { email: string; plan: string }
): Promise<Record<string, unknown>> {
  const { email, plan } = args;

  // STT corrections
  let cleanEmail = (email || "")
    .replace(/\s+dot\s+com/gi, ".com")
    .replace(/\s+dot\s+ca/gi, ".ca")
    .replace(/\s+dot\s+org/gi, ".org")
    .replace(/\s+at\s+/gi, "@")
    .trim();

  if (!cleanEmail || !cleanEmail.includes("@")) {
    return { success: false, error: "Invalid email address" };
  }

  try {
    // Create Stripe Checkout Session
    const priceAmount = plan === "double" ? 110000 : 65000;
    const session = await stripe.checkout.sessions.create({
      payment_method_types: ["card"],
      line_items: [
        {
          price_data: {
            currency: "usd",
            product_data: {
              name: plan === "double" ? "Biohazard Response Plan (2 incidents)" : "Biohazard Response Plan (1 incident)",
            },
            unit_amount: priceAmount,
          },
          quantity: 1,
        },
      ],
      mode: "payment",
      success_url: `${config.app.url}/payment/success`,
      cancel_url: `${config.app.url}/payment/cancel`,
      metadata: { call_id: callId, prospect_id: prospectId },
    });

    // Send via Resend
    const { Resend } = require("resend");
    const resend = new Resend(process.env.RESEND_API_KEY);
    const { data, error } = await resend.emails.send({
      from: "God's Cleaning Crew <noreply@godscleaningcrew.com>",
      to: [cleanEmail],
      subject: "Your Biohazard Response Plan",
      html: `
        <h2>Your Biohazard Response Plan</h2>
        <p>Thanks for chatting with us! Here's your secure payment link:</p>
        <p><a href="${session.url}" style="background:#10b981;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold;">Complete Payment</a></p>
        <p>Plan: ${plan === "double" ? "$1,100/year (2 incidents)" : "$650/year (1 incident)"}</p>
        <p>4-hour guaranteed response. No surprise billing.</p>
        <p>— God's Cleaning Crew</p>
      `,
    });

    if (error) {
      logError("Resend email failed", error);
      return { success: false, error: error.message };
    }

    logInfo("Email sent", { emailId: data?.id, email: cleanEmail, callId });
    return { success: true, email_id: data?.id, checkout_url: session.url };
  } catch (err: any) {
    logError("Email send failed", err);
    return { success: false, error: err.message };
  }
}

async function handleScheduleFollowup(
  callId: string,
  prospectId: string,
  args: { date: string; reason?: string }
): Promise<Record<string, unknown>> {
  try {
    const { error } = await supabase.from("followups").insert({
      call_id: callId,
      prospect_id: prospectId,
      scheduled_date: args.date || new Date(Date.now() + 2 * 86400000).toISOString(),
      reason: args.reason || "Follow up from cold call",
      status: "pending",
    });

    if (error) throw error;
    logInfo("Followup scheduled", { callId, prospectId, date: args.date });
    return { success: true };
  } catch (err: any) {
    logError("Schedule followup failed", err);
    return { success: false, error: err.message };
  }
}

async function handleMarkDNC(
  prospectId: string,
  args: { phone?: string }
): Promise<Record<string, unknown>> {
  try {
    if (prospectId) {
      await supabase
        .from("prospects")
        .update({ status: "dnc", updated_at: new Date().toISOString() })
        .eq("id", prospectId);
    }

    if (args.phone) {
      await supabase.from("dnc_list").upsert(
        { phone: args.phone, added_at: new Date().toISOString() },
        { onConflict: "phone", ignoreDuplicates: true }
      );
    }

    logInfo("DNC marked", { prospectId, phone: args.phone });
    return { success: true };
  } catch (err: any) {
    logError("DNC mark failed", err);
    return { success: false, error: err.message };
  }
}

async function handleLogObjection(
  callId: string,
  prospectId: string,
  args: { type: string; statement: string }
): Promise<Record<string, unknown>> {
  try {
    await supabase.from("objections").insert({
      call_id: callId,
      prospect_id: prospectId,
      type: args.type || "other",
      verbatim: args.statement || "",
    });
    return { success: true };
  } catch (err: any) {
    return { success: false, error: err.message };
  }
}
