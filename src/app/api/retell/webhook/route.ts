import crypto from 'crypto';
import { waitUntil } from '@vercel/functions';
import { NextRequest, NextResponse } from 'next/server';
import { config } from '@/lib/config';
import { logError, logInfo, logWarn } from '@/lib/logger';
import { supabase } from '@/lib/supabase';
import { RetellWebhookPayloadSchema } from '@/types';
import type { RetellCallObject, RetellWebhookPayload } from '@/types';

export const maxDuration = 60;

const DISCONNECTION_REASON_OUTCOME_MAP: Record<string, string> = {
  agent_bye: 'connected',
  user_hangup: 'connected',
  call_transfer: 'connected',
  voicemail_reached: 'voicemail',
  inactivity: 'no_answer',
  machine_detected: 'no_answer',
  concurrency_limit_reached: 'error',
  dial_busy: 'busy',
  dial_failed: 'error',
  dial_no_answer: 'no_answer',
};

export async function POST(req: NextRequest) {
  try {
    const rawBody = await req.text();
    const signature = req.headers.get('x-retell-signature');

    if (!verifyRetellSignature(rawBody, signature, config.retell.apiKey)) {
      logWarn('Retell webhook: invalid signature');
      return NextResponse.json({ error: 'Invalid signature' }, { status: 401 });
    }

    let rawPayload: unknown;
    try {
      rawPayload = JSON.parse(rawBody);
    } catch {
      logWarn('Retell webhook: invalid JSON payload');
      return NextResponse.json({ error: 'Invalid JSON' }, { status: 400 });
    }

    const parsed = RetellWebhookPayloadSchema.safeParse(rawPayload);
    if (!parsed.success) {
      logWarn('Retell webhook: invalid payload shape', { validationError: parsed.error.message });
      return NextResponse.json({ error: 'Invalid payload' }, { status: 400 });
    }

    waitUntil(
      processWebhook(parsed.data).catch((err) => {
        logError('Retell webhook: background processing failed', err, {
          callId: parsed.data.call.call_id,
          event: parsed.data.event,
        });
      })
    );

    return new NextResponse(null, { status: 204 });
  } catch (err) {
    logError('Retell webhook: unhandled error', err);
    return new NextResponse(null, { status: 204 });
  }
}

async function processWebhook(payload: RetellWebhookPayload): Promise<void> {
  switch (payload.event) {
    case 'call_started':
      await handleCallStarted(payload.call);
      return;
    case 'call_ended':
      await handleCallEnded(payload.call);
      return;
    case 'call_analyzed':
      await handleCallAnalyzed(payload.call);
      return;
    default:
      logWarn('Retell webhook: unhandled event', { event: payload.event });
  }
}

async function handleCallStarted(call: RetellCallObject): Promise<void> {
  const callId = call.call_id;
  const prospectId = getProspectIdFromMetadata(call.metadata);
  const startedAt = timestampToIso(call.start_timestamp) || new Date().toISOString();

  const upsertPayload: Record<string, unknown> = {
    retell_call_id: callId,
    phone: normalizePhone(call.to_number),
    started_at: startedAt,
  };
  if (prospectId) upsertPayload.prospect_id = prospectId;

  const { error: upsertError } = await supabase
    .from('calls')
    .upsert(upsertPayload, { onConflict: 'retell_call_id' });

  if (upsertError) {
    logError('Retell webhook call_started: call upsert failed', upsertError, { callId, prospectId });
  }

  if (prospectId) {
    const { error: prospectError } = await supabase
      .from('prospects')
      .update({ status: 'dialing', updated_at: new Date().toISOString() })
      .eq('id', prospectId);

    if (prospectError) {
      logError('Retell webhook call_started: prospect status update failed', prospectError, { callId, prospectId });
    }
  }

  logInfo('Retell webhook call_started processed', { callId, prospectId });
}

async function handleCallEnded(call: RetellCallObject): Promise<void> {
  const callId = call.call_id;
  let prospectId = getProspectIdFromMetadata(call.metadata);

  if (!prospectId) {
    const { data: existingCall, error: lookupError } = await supabase
      .from('calls')
      .select('prospect_id')
      .eq('retell_call_id', callId)
      .maybeSingle();

    if (lookupError) {
      logError('Retell webhook call_ended: prospect lookup failed', lookupError, { callId });
    } else {
      prospectId = existingCall?.prospect_id || null;
    }
  }

  const startIso = timestampToIso(call.start_timestamp);
  const endIso = timestampToIso(call.end_timestamp) || new Date().toISOString();
  const durationSeconds = calculateDurationSeconds(call.start_timestamp, call.end_timestamp);
  const outcome = mapDisconnectionReasonToOutcome(call.disconnection_reason);
  const transcriptObject =
    Array.isArray(call.transcript_object) && call.transcript_object.length > 0
      ? call.transcript_object
      : call.transcript
        ? [{ role: 'user', content: call.transcript }]
        : [];

  const upsertPayload: Record<string, unknown> = {
    retell_call_id: callId,
    phone: normalizePhone(call.to_number),
    transcript: transcriptObject,
    recording_url: call.recording_url || null,
    summary: call.call_analysis?.call_summary || null,
    duration_seconds: durationSeconds,
    started_at: startIso,
    ended_at: endIso,
    outcome,
  };
  if (prospectId) upsertPayload.prospect_id = prospectId;

  const { error: upsertError } = await supabase
    .from('calls')
    .upsert(upsertPayload, { onConflict: 'retell_call_id' });

  if (upsertError) {
    logError('Retell webhook call_ended: call upsert failed', upsertError, { callId, prospectId, outcome });
  }

  if (prospectId) {
    const nextProspectStatus = mapOutcomeToProspectStatus(outcome);
    const { error: prospectError } = await supabase
      .from('prospects')
      .update({ status: nextProspectStatus, updated_at: new Date().toISOString() })
      .eq('id', prospectId);

    if (prospectError) {
      logError('Retell webhook call_ended: prospect status update failed', prospectError, {
        callId,
        prospectId,
        outcome,
        nextProspectStatus,
      });
    }
  }

  logInfo('Retell webhook call_ended processed', { callId, prospectId, outcome, durationSeconds });
}

async function handleCallAnalyzed(call: RetellCallObject): Promise<void> {
  const callId = call.call_id;
  const prospectId = getProspectIdFromMetadata(call.metadata);
  const summary = call.call_analysis?.call_summary || null;

  const updatePayload: Record<string, unknown> = {
    retell_call_id: callId,
  };
  if (summary) updatePayload.summary = summary;
  if (prospectId) updatePayload.prospect_id = prospectId;

  const { error } = await supabase
    .from('calls')
    .upsert(updatePayload, { onConflict: 'retell_call_id' });

  if (error) {
    logError('Retell webhook call_analyzed: call update failed', error, { callId, prospectId });
  } else {
    logInfo('Retell webhook call_analyzed processed', { callId, prospectId });
  }
}

function verifyRetellSignature(rawBody: string, signature: string | null, apiKey: string): boolean {
  if (!signature || !apiKey) return false;

  const normalized = signature.replace(/^sha256=/i, '').trim();
  if (!/^[a-f0-9]{64}$/i.test(normalized)) return false;

  const hash = crypto.createHmac('sha256', apiKey).update(rawBody).digest('hex');
  const expected = Buffer.from(hash, 'hex');
  const received = Buffer.from(normalized, 'hex');

  if (expected.length !== received.length) return false;
  return crypto.timingSafeEqual(expected, received);
}

function mapDisconnectionReasonToOutcome(reason?: string): string {
  if (!reason) return 'connected';
  if (reason.startsWith('error_')) return 'error';
  return DISCONNECTION_REASON_OUTCOME_MAP[reason] || 'connected';
}

function mapOutcomeToProspectStatus(outcome: string): 'contacted' | 'no_answer' | 'failed' {
  if (outcome === 'voicemail' || outcome === 'no_answer' || outcome === 'busy') return 'no_answer';
  if (outcome === 'error') return 'failed';
  return 'contacted';
}

function timestampToIso(timestamp?: number): string | null {
  if (typeof timestamp !== 'number' || !Number.isFinite(timestamp) || timestamp <= 0) {
    return null;
  }
  return new Date(timestamp).toISOString();
}

function calculateDurationSeconds(startTimestamp?: number, endTimestamp?: number): number | null {
  if (
    typeof startTimestamp !== 'number' ||
    typeof endTimestamp !== 'number' ||
    !Number.isFinite(startTimestamp) ||
    !Number.isFinite(endTimestamp) ||
    endTimestamp < startTimestamp
  ) {
    return null;
  }
  return Math.floor((endTimestamp - startTimestamp) / 1000);
}

function normalizePhone(value?: string): string | null {
  if (!value) return null;
  const next = value.trim();
  return next.length > 0 ? next : null;
}

function getProspectIdFromMetadata(metadata?: Record<string, unknown>): string | null {
  const value = metadata?.prospect_id;
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}
