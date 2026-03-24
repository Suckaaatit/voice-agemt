import { createHash, createHmac, timingSafeEqual } from 'crypto';
import { NextRequest, NextResponse } from 'next/server';
import { config } from '@/lib/config';
import { logError, logInfo, logWarn } from '@/lib/logger';
import { supabase } from '@/lib/supabase';
import { RetellToolCallPayloadSchema } from '@/types';
import type { ProcessPaymentPayload } from '@/types';

export const maxDuration = 60;

const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const VALID_OBJECTION_TYPES = [
  'not_interested',
  'too_expensive',
  'send_info',
  'call_later',
  'has_provider',
  'busy_moment',
  'other',
] as const;

export async function POST(req: NextRequest) {
  try {
    const rawBody = await req.text();
    const signature = req.headers.get('x-retell-signature');
    if (signature && !verifyRetellSignature(rawBody, signature, config.retell.apiKey)) {
      logWarn('Retell actions: invalid signature');
      return NextResponse.json({ error: 'Invalid signature' }, { status: 401 });
    }

    let rawPayload: unknown;
    try {
      rawPayload = JSON.parse(rawBody);
    } catch {
      logWarn('Retell actions: invalid JSON payload');
      return NextResponse.json({ error: 'Invalid payload' }, { status: 400 });
    }

    const parsed = RetellToolCallPayloadSchema.safeParse(rawPayload);

    if (!parsed.success) {
      logWarn('Retell actions: invalid payload', { validationError: parsed.error.message });
      return NextResponse.json({ error: 'Invalid payload' }, { status: 400 });
    }

    const payload = parsed.data;
    const callId = payload.call.call_id;
    const metadata = normalizeMetadata(payload.call.metadata);
    const functionName = payload.name;
    const args = payload.args || {};

    const dedupKey = createHash('sha256')
      .update(`${callId}:${functionName}:${JSON.stringify(args)}`)
      .digest('hex');

    const { data: existing, error: dedupLookupError } = await supabase
      .from('processed_tool_calls')
      .select('response_text')
      .eq('tool_call_id', dedupKey)
      .maybeSingle();

    if (dedupLookupError) {
      logError('Retell actions: dedup lookup failed', dedupLookupError, {
        callId,
        functionName,
        toolCallId: dedupKey,
      });
    }

    if (existing) {
      return NextResponse.json({ response: existing.response_text || 'Processed.' });
    }

    const response = await handleFunction(functionName, args, callId, metadata);

    const { error: dedupInsertError } = await supabase
      .from('processed_tool_calls')
      .upsert(
        {
          tool_call_id: dedupKey,
          function_name: functionName,
          response_text: response,
        },
        { onConflict: 'tool_call_id' }
      );

    if (dedupInsertError) {
      logError('Retell actions: dedup insert failed', dedupInsertError, {
        callId,
        functionName,
        toolCallId: dedupKey,
      });
    }

    return NextResponse.json({ response });
  } catch (err) {
    logError('Retell actions: unhandled error', err);
    return NextResponse.json({ response: 'An error occurred, please try again.' });
  }
}

function verifyRetellSignature(rawBody: string, signature: string, apiKey: string): boolean {
  if (!signature || !apiKey) return false;

  const normalized = signature.replace(/^sha256=/i, '').trim();
  if (!/^[a-f0-9]{64}$/i.test(normalized)) return false;

  const hash = createHmac('sha256', apiKey).update(rawBody).digest('hex');
  const expected = Buffer.from(hash, 'hex');
  const received = Buffer.from(normalized, 'hex');

  if (expected.length !== received.length) return false;
  return timingSafeEqual(expected, received);
}

async function handleFunction(
  name: string,
  args: Record<string, unknown>,
  callId: string,
  metadata: Record<string, string>
): Promise<string> {
  switch (name) {
    case 'send_payment_email':
      return handleSendPaymentEmail(args, callId, metadata);
    case 'log_objection':
      return handleLogObjection(args, callId);
    case 'schedule_followup':
      return handleScheduleFollowup(args, callId, metadata);
    case 'confirm_payment':
      return handleConfirmPayment(callId, metadata);
    case 'mark_do_not_call':
      return handleDoNotCall(args, callId, metadata);
    default:
      logWarn('Retell actions: unknown function called', { callId, functionName: name });
      return `Unknown function: ${name}`;
  }
}

async function handleSendPaymentEmail(
  args: Record<string, unknown>,
  callId: string,
  metadata: Record<string, string>
): Promise<string> {
  const rawEmail = String(args.email || args.recipient_email || '');
  const email = sanitizeEmail(rawEmail);

  if (!email || !EMAIL_REGEX.test(email)) {
    logWarn('send_payment_email: invalid email after sanitization', { callId, rawEmail });
    return "I didn't catch a valid email. Could you spell that out for me one more time?";
  }

  const planSelection = parsePlanSelection(args);
  const prospectName = safeTextArg(args, ['prospect_name', 'name', 'contact_name']);
  const companyName = safeTextArg(args, ['company_name', 'property_name', 'property']);

  let dbCallId: string | null = null;
  let prospectId: string | null = metadata.prospect_id || null;

  try {
    const { data: callData, error: callLookupError } = await supabase
      .from('calls')
      .select('id, prospect_id')
      .eq('retell_call_id', callId)
      .maybeSingle();

    if (callLookupError && callLookupError.code !== 'PGRST116') {
      logError('send_payment_email: call lookup failed', callLookupError, { callId });
    }

    if (callData?.id) {
      dbCallId = callData.id;
      prospectId = callData.prospect_id || prospectId;
    } else if (prospectId) {
      const { data: upsertedCall, error: upsertError } = await supabase
        .from('calls')
        .upsert(
          {
            retell_call_id: callId,
            prospect_id: prospectId,
            phone: metadata.phone || null,
            started_at: new Date().toISOString(),
          },
          { onConflict: 'retell_call_id' }
        )
        .select('id, prospect_id')
        .maybeSingle();

      if (upsertError) {
        logError('send_payment_email: call upsert failed', upsertError, { callId, prospectId });
      } else {
        dbCallId = upsertedCall?.id || null;
        prospectId = upsertedCall?.prospect_id || prospectId;
      }
    }
  } catch (err) {
    logError('send_payment_email: call/prospect lookup failed', err, { callId });
  }

  if (prospectId) {
    const { error: emailUpdateError } = await supabase
      .from('prospects')
      .update({ email, updated_at: new Date().toISOString() })
      .eq('id', prospectId);

    if (emailUpdateError) {
      logError('send_payment_email: prospect email update failed', emailUpdateError, { callId, prospectId });
    }
  }

  if (dbCallId && prospectId) {
    const { data: existingPayment, error: paymentLookupError } = await supabase
      .from('payments')
      .select('id')
      .eq('call_id', dbCallId)
      .order('created_at', { ascending: false })
      .limit(1)
      .maybeSingle();

    if (paymentLookupError) {
      logError('send_payment_email: payment lookup failed', paymentLookupError, { callId, prospectId });
    } else if (!existingPayment) {
      const { error: placeholderInsertError } = await supabase.from('payments').insert({
        call_id: dbCallId,
        prospect_id: prospectId,
        status: 'pending',
        email_sent: false,
      });

      if (placeholderInsertError) {
        logError('send_payment_email: placeholder payment insert failed', placeholderInsertError, { callId, prospectId });
      }
    }
  }

  fireBackgroundPayment({
    call_id: dbCallId || undefined,
    prospect_id: prospectId || undefined,
    email,
    retell_call_id: callId || `agent-mode-${Date.now()}`,
    secret: config.app.internalSecret,
    plan_tier: planSelection.planTier,
    plan_label: planSelection.planLabel,
    price_id: planSelection.priceId,
    prospect_name: prospectName || undefined,
    company_name: companyName || undefined,
  });

  logInfo('send_payment_email: background payment fired', { callId, prospectId });
  return "I'm sending that payment link to your email right now.";
}

function fireBackgroundPayment(payload: ProcessPaymentPayload): void {
  const url = `${config.app.url}/api/internal/process-payment`;
  const dashboardUser = config.app.dashboardBasicUser || '';
  const dashboardPass = config.app.dashboardBasicPass || '';
  const basicAuth =
    dashboardUser && dashboardUser.length > 0
      ? `Basic ${Buffer.from(`${dashboardUser}:${dashboardPass}`).toString('base64')}`
      : null;

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'x-internal-secret': config.app.internalSecret,
  };
  if (basicAuth) headers.Authorization = basicAuth;

  fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  }).catch((err) => {
    logError('fireBackgroundPayment: self-call failed', err, {
      callId: payload.call_id,
      prospectId: payload.prospect_id,
    });
  });
}

async function handleLogObjection(args: Record<string, unknown>, callId: string): Promise<string> {
  try {
    const { data: callData, error: callLookupError } = await supabase
      .from('calls')
      .select('id')
      .eq('retell_call_id', callId)
      .maybeSingle();

    if (callLookupError) {
      logError('log_objection: call lookup failed', callLookupError, { callId });
      return '';
    }

    if (!callData?.id) {
      return '';
    }

    const objectionType = String(args.type || args.objection_type || 'other');
    const safeType = VALID_OBJECTION_TYPES.includes(objectionType as (typeof VALID_OBJECTION_TYPES)[number])
      ? objectionType
      : 'other';

    const { error: insertError } = await supabase.from('objections').insert({
      call_id: callData.id,
      objection_type: safeType,
      prospect_statement: String(args.verbatim || args.prospect_statement || ''),
      ai_response: String(args.ai_response || ''),
      resolved: false,
    });

    if (insertError) {
      logError('log_objection: insert failed', insertError, { callId, objectionType: safeType });
    }
  } catch (err) {
    logError('log_objection: unhandled error', err, { callId });
  }

  return '';
}

async function handleScheduleFollowup(
  args: Record<string, unknown>,
  callId: string,
  metadata: Record<string, string>
): Promise<string> {
  const prospectId = metadata.prospect_id;
  if (!prospectId) {
    return "I've noted to call you back at tomorrow 3:00 PM EST.";
  }

  let dbCallId: string | null = null;
  try {
    const { data: callData } = await supabase
      .from('calls')
      .select('id')
      .eq('retell_call_id', callId)
      .maybeSingle();
    dbCallId = callData?.id || null;
  } catch (err) {
    logWarn('schedule_followup: call lookup skipped', { callId, error: String(err) });
  }

  const scheduledAt = parseSuggestedTime(String(args.suggested_time || '').trim());
  const followupReason = String(args.reason || 'Prospect asked for callback');

  const { error: insertError } = await supabase.from('followups').insert({
    prospect_id: prospectId,
    call_id: dbCallId,
    scheduled_at: scheduledAt.toISOString(),
    reason: followupReason,
    status: 'pending',
  });

  if (insertError) {
    logError('schedule_followup: insert failed', insertError, { callId, prospectId });
  }

  const { error: prospectUpdateError } = await supabase
    .from('prospects')
    .update({ status: 'followup', updated_at: new Date().toISOString() })
    .eq('id', prospectId);

  if (prospectUpdateError) {
    logError('schedule_followup: prospect update failed', prospectUpdateError, { callId, prospectId });
  }

  const spokenTime = scheduledAt.toLocaleString('en-US', {
    timeZone: 'America/New_York',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
    timeZoneName: 'short',
  });
  return `I've noted to call you back at ${spokenTime}.`;
}

async function handleConfirmPayment(callId: string, metadata: Record<string, string>): Promise<string> {
  let prospectId = metadata.prospect_id || null;

  if (!prospectId) {
    const { data: callData, error: callLookupError } = await supabase
      .from('calls')
      .select('prospect_id')
      .eq('retell_call_id', callId)
      .maybeSingle();

    if (callLookupError) {
      logError('confirm_payment: call lookup failed', callLookupError, { callId });
    }

    prospectId = callData?.prospect_id || null;
  }

  if (!prospectId) {
    return "I'm not seeing the payment yet. Try refreshing.";
  }

  const { data: paidPayment, error: paymentError } = await supabase
    .from('payments')
    .select('id')
    .eq('prospect_id', prospectId)
    .eq('status', 'paid')
    .limit(1)
    .maybeSingle();

  if (paymentError) {
    logError('confirm_payment: payment lookup failed', paymentError, { callId, prospectId });
    return "I'm not seeing the payment yet. Try refreshing.";
  }

  if (paidPayment?.id) {
    return "Payment confirmed. You're all set.";
  }

  return "I'm not seeing the payment yet. Try refreshing.";
}

async function handleDoNotCall(
  args: Record<string, unknown>,
  callId: string,
  metadata: Record<string, string>
): Promise<string> {
  let prospectId =
    (typeof args.prospect_id === 'string' ? args.prospect_id.trim() : '') ||
    metadata.prospect_id ||
    '';

  if (!prospectId && callId) {
    const { data: callData } = await supabase
      .from('calls')
      .select('prospect_id')
      .eq('retell_call_id', callId)
      .maybeSingle();
    prospectId = callData?.prospect_id || '';
  }

  if (!prospectId) {
    logWarn('mark_do_not_call: no prospect found', { callId });
    return "Done. You've been removed from our list.";
  }

  const { error } = await supabase
    .from('prospects')
    .update({ status: 'do_not_call', updated_at: new Date().toISOString() })
    .eq('id', prospectId);

  if (error) {
    logError('mark_do_not_call: update failed', error, { callId, prospectId });
  }

  return "Done. You've been removed from our list.";
}

function normalizeMetadata(metadata?: Record<string, unknown>): Record<string, string> {
  if (!metadata) return {};
  return Object.entries(metadata).reduce<Record<string, string>>((acc, [key, value]) => {
    if (typeof value === 'string') acc[key] = value;
    else if (typeof value === 'number' || typeof value === 'boolean') acc[key] = String(value);
    return acc;
  }, {});
}

function sanitizeEmail(raw: string): string {
  if (!raw) return '';
  let email = raw.toLowerCase().trim();
  email = email.replace(/\s*@\s*/g, '@');
  email = email.replace(/\s*\.\s*/g, '.');
  email = email.replace(/\s+at\s+/g, '@');
  email = email.replace(/\s+dot\s+/g, '.');
  email = email.replace(/\bat\s+/g, '@');
  email = email.replace(/\s+at\b/g, '@');
  email = email.replace(/\bdot\s+/g, '.');
  email = email.replace(/\s+dot\b/g, '.');
  email = email.replace(/\s+/g, '');
  email = email.replace(/@@+/g, '@');
  email = email.replace(/\.\.+/g, '.');
  email = email.replace(/^\./, '');
  email = email.replace(/\.$/, '');
  return email;
}

function safeTextArg(args: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = args[key];
    if (typeof value === 'string' && value.trim().length > 0) return value.trim();
  }
  return '';
}

function parsePlanSelection(args: Record<string, unknown>): {
  planTier: 'one_incident' | 'two_incident';
  planLabel: string;
  priceId?: string;
} {
  const explicitPriceId = typeof args.price_id === 'string' ? args.price_id.trim() : '';
  if (explicitPriceId.startsWith('price_')) {
    const explicitPlan = safeTextArg(args, ['plan_tier', 'plan', 'plan_name', 'coverage']);
    const explicitTier =
      explicitPlan.includes('2') || explicitPlan.toLowerCase().includes('two')
        ? 'two_incident'
        : 'one_incident';
    return {
      planTier: explicitTier,
      planLabel:
        explicitTier === 'two_incident'
          ? 'Annual Biohazard Response - 2 Incident Coverage'
          : 'Annual Biohazard Response - 1 Incident Coverage',
      priceId: explicitPriceId,
    };
  }

  const rawPlan = safeTextArg(args, ['plan_tier', 'plan', 'plan_name', 'coverage']).toLowerCase();
  const incidentsRaw = String(args.incidents || args.incident_count || '').trim();
  const amountRaw = String(args.amount || args.amount_cents || '').trim();
  const likelyTwoIncident =
    rawPlan.includes('2') ||
    rawPlan.includes('two') ||
    incidentsRaw === '2' ||
    amountRaw === '1100' ||
    amountRaw === '110000';

  const planTier: 'one_incident' | 'two_incident' = likelyTwoIncident ? 'two_incident' : 'one_incident';
  return {
    planTier,
    planLabel:
      planTier === 'two_incident'
        ? 'Annual Biohazard Response - 2 Incident Coverage'
        : 'Annual Biohazard Response - 1 Incident Coverage',
  };
}

function parseSuggestedTime(rawSuggestedTime: string): Date {
  if (!rawSuggestedTime) return tomorrowAt3PmEst();

  const lower = rawSuggestedTime.toLowerCase();
  if (lower.includes('hour')) {
    const hours = Number.parseInt(rawSuggestedTime, 10);
    const safeHours = Number.isFinite(hours) && hours > 0 ? hours : 2;
    return new Date(Date.now() + safeHours * 60 * 60 * 1000);
  }
  if (lower.includes('minute')) {
    const minutes = Number.parseInt(rawSuggestedTime, 10);
    const safeMinutes = Number.isFinite(minutes) && minutes > 0 ? minutes : 30;
    return new Date(Date.now() + safeMinutes * 60 * 1000);
  }
  if (lower.includes('tomorrow')) {
    return tomorrowAt3PmEst();
  }

  const parsed = new Date(rawSuggestedTime);
  if (!Number.isNaN(parsed.getTime())) return parsed;
  return tomorrowAt3PmEst();
}

function tomorrowAt3PmEst(): Date {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1, 20, 0, 0, 0));
}
