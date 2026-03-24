/**
 * Canonical Retell agent settings — single source of truth.
 *
 * These get re-applied:
 *   1. Automatically after every Vercel deploy (postbuild hook)
 *   2. Manually via the "Sync Agent Settings" button on /dashboard/agent
 *
 * Edit HERE to change agent behaviour. Do not change settings in the
 * Retell dashboard — they will be overwritten on next sync.
 */
export const RETELL_AGENT_SETTINGS = {
  voice_temperature: 0.2,
  voice_speed: 0.92,
  responsiveness: 1.0,
  interruption_sensitivity: 0.3,
  enable_backchannel: true,
  backchannel_frequency: 0.4,
  enable_dynamic_responsiveness: false,
  enable_dynamic_voice_speed: true,
  denoising_mode: "noise-cancellation",
  normalize_for_speech: true,
  stt_mode: "fast",
} as const;

export type RetellAgentSettings = typeof RETELL_AGENT_SETTINGS;
