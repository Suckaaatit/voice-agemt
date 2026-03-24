import { NextResponse } from "next/server";
import { config } from "@/lib/config";
import { RETELL_AGENT_SETTINGS } from "@/lib/retell-agent-settings";
import { withErrorHandling } from "@/app/api/dashboard/_utils";
import { logInfo, logError } from "@/lib/logger";

export const maxDuration = 60;

/**
 * POST /api/dashboard/sync-agent
 *
 * Dashboard-facing route that PATCHes Retell agent settings directly.
 * Inlines the sync logic (instead of self-calling /api/retell/sync-settings)
 * to avoid Vercel deployment protection returning HTML on server-to-server calls.
 */
export async function POST() {
  return withErrorHandling("sync-agent failed", async () => {
    const agentId = config.retell.agentId;

    const response = await fetch(
      `https://api.retellai.com/update-agent/${agentId}`,
      {
        method: "PATCH",
        headers: {
          Authorization: `Bearer ${config.retell.apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(RETELL_AGENT_SETTINGS),
      }
    );

    if (!response.ok) {
      const errText = await response.text();
      logError("sync-agent: Retell API error", new Error(errText));
      return NextResponse.json(
        { error: "Retell API error", detail: errText },
        { status: response.status }
      );
    }

    const data = await response.json();
    const synced = Object.keys(RETELL_AGENT_SETTINGS);
    logInfo("retell agent settings synced via dashboard", {
      agentId: agentId.slice(-6),
      synced,
    });

    return NextResponse.json({
      data: {
        ok: true,
        synced,
        values: RETELL_AGENT_SETTINGS,
        agent_name: data.agent_name ?? null,
      },
      error: null,
      count: null,
    });
  });
}
