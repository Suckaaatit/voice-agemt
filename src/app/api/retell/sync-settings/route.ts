import { NextRequest, NextResponse } from "next/server";
import { config } from "@/lib/config";
import { RETELL_AGENT_SETTINGS } from "@/lib/retell-agent-settings";
import { logInfo, logError } from "@/lib/logger";

export const maxDuration = 60;

/**
 * POST /api/retell/sync-settings
 *
 * Re-applies the canonical agent settings to Retell.
 * Protected by INTERNAL_API_SECRET — call from postbuild hook or dashboard proxy.
 */
export async function POST(req: NextRequest) {
  const authHeader = req.headers.get("authorization") || "";
  const token = authHeader.replace(/^Bearer\s+/i, "").trim();

  if (!token || token !== config.app.internalSecret) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
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
      logError("retell sync-settings failed", new Error(errText));
      return NextResponse.json(
        { error: "Retell API error", detail: errText },
        { status: response.status }
      );
    }

    const data = await response.json();
    const synced = Object.keys(RETELL_AGENT_SETTINGS);
    logInfo("retell agent settings synced", { agentId: agentId.slice(-6), synced });

    return NextResponse.json({
      ok: true,
      synced,
      values: RETELL_AGENT_SETTINGS,
      agent_name: data.agent_name ?? null,
    });
  } catch (err) {
    logError("retell sync-settings exception", err);
    return NextResponse.json(
      { error: "Failed to sync settings" },
      { status: 500 }
    );
  }
}
