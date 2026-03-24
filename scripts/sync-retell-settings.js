/**
 * Post-build hook: re-applies Retell agent settings after every Vercel deploy.
 * Runs after `next build` completes.
 *
 * Calls the Retell API directly (not our own app endpoint) to avoid
 * Vercel deployment protection returning HTML instead of JSON.
 */

const RETELL_AGENT_SETTINGS = {
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
};

const apiKey = process.env.RETELL_API_KEY;
const agentId = process.env.RETELL_AGENT_ID;

if (!apiKey || !agentId) {
  console.log("[sync-retell] Skipping: missing RETELL_API_KEY or RETELL_AGENT_ID");
  process.exit(0);
}

async function sync() {
  try {
    console.log(`[sync-retell] Syncing agent ${agentId.slice(-6)} settings directly via Retell API`);
    const res = await fetch(`https://api.retellai.com/update-agent/${agentId}`, {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(RETELL_AGENT_SETTINGS),
    });

    if (res.ok) {
      const data = await res.json();
      console.log("[sync-retell] ✅ Success — agent:", data.agent_name || agentId.slice(-6));
      console.log("[sync-retell] Synced keys:", Object.keys(RETELL_AGENT_SETTINGS).join(", "));
    } else {
      const errText = await res.text();
      console.warn("[sync-retell] ⚠️ Retell API error:", res.status, errText);
    }
  } catch (err) {
    console.warn("[sync-retell] Failed (non-fatal):", err.message || err);
  }
}

sync();
