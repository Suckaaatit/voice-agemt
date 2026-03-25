"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import VoiceTester from "@/components/voice-tester";
import { formatDuration, safeText } from "@/lib/utils";
import { toast } from "sonner";

type CallsResponse = {
  data: Array<{ duration_seconds: number | null; outcome: string | null; summary: string | null }> | null;
  error: string | null;
  count: number | null;
};

type DashboardResponse = {
  ok: boolean;
  summary: {
    prospects_total: number;
    prospects_closed: number;
  };
};

export default function AgentPage() {
  const [avgDuration, setAvgDuration] = useState("-");
  const [objectionRate, setObjectionRate] = useState("0%");
  const [closeRate, setCloseRate] = useState("0%");
  const [topObjection, setTopObjection] = useState("We already have someone (0)");
  const [callStatus, setCallStatus] = useState<"idle" | "connecting" | "active" | "ended">("idle");
  const [transcript, setTranscript] = useState("");
  const [agentTalking, setAgentTalking] = useState(false);
  const [callError, setCallError] = useState<string | null>(null);
  const [emailInput, setEmailInput] = useState("");
  const [planTier, setPlanTier] = useState<"one_incident" | "two_incident">("one_incident");
  const [sendingEmail, setSendingEmail] = useState(false);
  const [emailSent, setEmailSent] = useState(false);
  const [vadLog, setVadLog] = useState<Array<{ ts: number; event: string; detail?: string }>>([]);

  // Refs for WebSocket + Audio
  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const callStartTimeRef = useRef<number>(0);
  const nextPlayTimeRef = useRef<number>(0);
  const playbackCtxRef = useRef<AudioContext | null>(null);
  const isTTSPlayingRef = useRef<boolean>(false);

  // ─── Performance stats ─────────────────────────────────────────
  useEffect(() => {
    const loadPerformance = async () => {
      try {
        const [callsRes, dashboardRes] = await Promise.all([
          fetch("/api/dashboard/calls?page=1&limit=100&sortBy=created_at&sortDirection=desc", { cache: "no-store" }),
          fetch("/api/dashboard", { cache: "no-store" }),
        ]);

        const callsPayload = (await callsRes.json()) as CallsResponse;
        const dashboardPayload = (await dashboardRes.json()) as DashboardResponse;

        if (callsRes.ok && callsPayload.data) {
          const durations = callsPayload.data.map((call) => Number(call.duration_seconds || 0)).filter((v) => v > 0);
          const avg = durations.length ? Math.round(durations.reduce((s, v) => s + v, 0) / durations.length) : 0;
          setAvgDuration(formatDuration(avg));

          const objectionSignals = callsPayload.data.filter((c) => (c.summary || "").toLowerCase().includes("objection")).length;
          const handledSignals = callsPayload.data.filter(
            (c) => (c.summary || "").toLowerCase().includes("resolved") || (c.outcome || "").toLowerCase() === "closed"
          ).length;
          setObjectionRate(objectionSignals > 0 ? `${Math.round((Math.min(handledSignals, objectionSignals) / objectionSignals) * 100)}%` : "0%");

          const buckets: Record<string, number> = { "We already have someone": 0, "Too expensive": 0, "Not interested": 0 };
          for (const call of callsPayload.data) {
            const s = (call.summary || "").toLowerCase();
            if (s.includes("already have")) buckets["We already have someone"] += 1;
            if (s.includes("expensive") || s.includes("budget")) buckets["Too expensive"] += 1;
            if (s.includes("not interested")) buckets["Not interested"] += 1;
          }
          const top = Object.entries(buckets).sort((a, b) => b[1] - a[1])[0];
          setTopObjection(`${top[0]} (${top[1]})`);
        }

        if (dashboardRes.ok && dashboardPayload.ok) {
          const total = dashboardPayload.summary.prospects_total || 0;
          const closed = dashboardPayload.summary.prospects_closed || 0;
          setCloseRate(total > 0 ? `${Math.round((closed / total) * 100)}%` : "0%");
        }
      } catch {
        // stats failures are non-fatal
      }
    };

    void loadPerformance();
    const id = window.setInterval(() => void loadPerformance(), 30000);
    return () => window.clearInterval(id);
  }, []);

  // ─── VAD logger ────────────────────────────────────────────────
  const logVad = useCallback((event: string, detail?: string) => {
    const ts = Date.now() - callStartTimeRef.current;
    setVadLog((prev) => [...prev.slice(-49), { ts, event, detail }]);
  }, []);

  // ─── Start Call ────────────────────────────────────────────────
  const startCall = async () => {
    if (callStatus === "connecting" || callStatus === "active") return;
    setCallError(null);
    setTranscript("");
    setCallStatus("connecting");
    setEmailSent(false);
    setVadLog([]);
    callStartTimeRef.current = Date.now();

    try {
      // 1. Get WebSocket URL from API
      const res = await fetch("/api/voice/create-web-call", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error || "Failed to create session");

      const wsUrl = data.ws_url as string;

      // 2. Get mic access
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      streamRef.current = stream;

      // 3. Set up capture AudioContext (16kHz for Deepgram)
      const captureCtx = new AudioContext({ sampleRate: 16000 });
      audioCtxRef.current = captureCtx;
      const source = captureCtx.createMediaStreamSource(stream);

      // 4. Set up playback AudioContext (24kHz from ElevenLabs)
      playbackCtxRef.current = new AudioContext({ sampleRate: 24000 });
      nextPlayTimeRef.current = 0;

      // 5. Connect WebSocket
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.binaryType = "arraybuffer";

      ws.onopen = () => {
        setCallStatus("active");
        logVad("call_started");

        // Start capturing mic audio and sending to server
        // ScriptProcessorNode: capture PCM, convert Float32 → Int16, send
        const processor = captureCtx.createScriptProcessor(4096, 1, 1);
        processorRef.current = processor;

        processor.onaudioprocess = (e) => {
          if (ws.readyState !== WebSocket.OPEN) return;
          const float32 = e.inputBuffer.getChannelData(0);
          const int16 = new Int16Array(float32.length);
          for (let i = 0; i < float32.length; i++) {
            const s = Math.max(-1, Math.min(1, float32[i]));
            int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
          }
          // Prepend 1-byte TTS playback flag so server knows when to ignore echo
          const flag = new Uint8Array([isTTSPlayingRef.current ? 1 : 0]);
          const combined = new Uint8Array(flag.byteLength + int16.buffer.byteLength);
          combined.set(flag, 0);
          combined.set(new Uint8Array(int16.buffer), 1);
          ws.send(combined.buffer);
        };

        source.connect(processor);
        processor.connect(captureCtx.destination);
      };

      ws.binaryType = "arraybuffer";  // Force ArrayBuffer, not Blob

      ws.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          // Audio from Cartesia TTS — play it
          const pcmData = new Int16Array(event.data);
          if (pcmData.length === 0) return;

          const pCtx = playbackCtxRef.current;
          if (!pCtx) return;

          const float32 = new Float32Array(pcmData.length);
          for (let i = 0; i < pcmData.length; i++) {
            float32[i] = pcmData[i] / 32768;
          }

          const buffer = pCtx.createBuffer(1, float32.length, 24000);
          buffer.getChannelData(0).set(float32);
          const bufferSource = pCtx.createBufferSource();
          bufferSource.buffer = buffer;
          bufferSource.connect(pCtx.destination);

          const now = pCtx.currentTime;
          const playAt = Math.max(now, nextPlayTimeRef.current);
          bufferSource.start(playAt);
          nextPlayTimeRef.current = playAt + buffer.duration;
          // Track TTS playback state for echo cancellation
          isTTSPlayingRef.current = true;
          bufferSource.onended = () => {
            // Only mark as stopped if no more buffers are queued
            if (pCtx.currentTime >= nextPlayTimeRef.current - 0.05) {
              isTTSPlayingRef.current = false;
            }
          };
        } else {
          // JSON event from server
          try {
            const msg = JSON.parse(event.data as string);
            switch (msg.type) {
              case "ready":
                logVad("ready");
                break;
              case "transcript":
                setTranscript((prev) => {
                  const line = `${msg.role}: ${msg.text}`;
                  return prev ? `${prev}\n${line}` : line;
                });
                break;
              case "agent_talking":
                setAgentTalking(msg.value);
                logVad(msg.value ? "agent_start" : "agent_stop");
                break;
              case "call_ended":
                logVad("call_ended", msg.reason);
                setCallStatus("ended");
                setAgentTalking(false);
                break;
              case "error":
                setCallError(msg.message);
                break;
            }
          } catch {
            // ignore parse errors
          }
        }
      };

      ws.onerror = () => {
        setCallError("WebSocket connection failed. Is the voice server running on port 8080?");
        setCallStatus("ended");
      };

      ws.onclose = () => {
        if (callStatus !== "ended") {
          setCallStatus("ended");
          setAgentTalking(false);
          logVad("call_ended", "ws_closed");
        }
      };

    } catch (err) {
      console.error("Failed to start call:", err);
      setCallStatus("ended");
      const message = err instanceof Error ? err.message : "Unable to start the call.";
      setCallError(`${message} Please try again.`);
    }
  };

  // ─── End Call ──────────────────────────────────────────────────
  const endCall = () => {
    // Send end signal to server
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "end_call" }));
      wsRef.current.close();
    }
    wsRef.current = null;

    // Stop mic
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }

    // Disconnect audio
    if (processorRef.current) {
      processorRef.current.disconnect();
      processorRef.current = null;
    }
    if (audioCtxRef.current) {
      void audioCtxRef.current.close();
      audioCtxRef.current = null;
    }
    if (playbackCtxRef.current) {
      void playbackCtxRef.current.close();
      playbackCtxRef.current = null;
    }

    setCallStatus("ended");
    setAgentTalking(false);
    logVad("call_ended", "user_ended");
  };

  // ─── Send Payment Email ────────────────────────────────────────
  const sendPaymentEmail = async () => {
    const trimmed = emailInput.trim().toLowerCase();
    if (!trimmed || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) {
      toast.error("Please enter a valid email address.");
      return;
    }
    setSendingEmail(true);
    setEmailSent(false);
    try {
      const res = await fetch("/api/dashboard/send-payment-email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: trimmed, plan_tier: planTier }),
      });
      const payload = await res.json();
      if (!res.ok || payload.error) throw new Error(payload.error || "Failed to send email.");
      setEmailSent(true);
      toast.success(`Payment link sent to ${trimmed}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to send email.");
    } finally {
      setSendingEmail(false);
    }
  };

  // ─── Cleanup on unmount ────────────────────────────────────────
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      if (streamRef.current) {
        streamRef.current.getTracks().forEach((t) => t.stop());
      }
      if (audioCtxRef.current) {
        void audioCtxRef.current.close();
        audioCtxRef.current = null;
      }
      if (playbackCtxRef.current) {
        void playbackCtxRef.current.close();
        playbackCtxRef.current = null;
      }
    };
  }, []);

  // ─── Status labels ─────────────────────────────────────────────
  const statusLabel = callStatus === "idle" ? "Idle" : callStatus === "connecting" ? "Connecting" : callStatus === "active" ? "Active" : "Ended";
  const statusColor = callStatus === "active" ? "text-[var(--green)]" : callStatus === "connecting" ? "text-[var(--amber)]" : callStatus === "ended" ? "text-[var(--red)]" : "text-[var(--text-muted)]";

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-3xl font-bold text-white">Agent</h1>
        <p className="text-sm text-[var(--text-muted)]">Talk to Adam and run live voice tests before production dialing.</p>
      </div>

      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>Agent Profile</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-3 text-sm md:grid-cols-2">
            <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] px-3 py-2">
              <p className="text-xs text-[var(--text-muted)]">Name</p>
              <p>Adam</p>
            </div>
            <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] px-3 py-2">
              <p className="text-xs text-[var(--text-muted)]">Company</p>
              <p>God&apos;s Cleaning Crew</p>
            </div>
            <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] px-3 py-2">
              <p className="text-xs text-[var(--text-muted)]">Voice</p>
              <p>ElevenLabs Turbo V2.5</p>
            </div>
            <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] px-3 py-2">
              <p className="text-xs text-[var(--text-muted)]">LLM</p>
              <p>Groq (Llama 3.3 70B)</p>
            </div>
            <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] px-3 py-2 md:col-span-2">
              <p className="text-xs text-[var(--text-muted)]">Status</p>
              <div className="mt-1 flex items-center gap-2 text-[#9dffcf]">
                <span className="pulse-dot h-2.5 w-2.5 rounded-full bg-[var(--green)]" />
                Custom Voice Server
              </div>
            </div>
          </CardContent>
        </Card>

        <div className="glass-card flex min-h-[500px] w-full flex-col rounded-xl p-8">
          <h2 className="mb-1 text-xl font-bold text-white">Talk to Agent</h2>
          <p className="mb-8 text-sm text-gray-400">Talk to Adam directly in your browser. No phone number needed.</p>
          <div className="mb-8 rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] p-6">
            <div className="flex flex-col items-center gap-4">
              <div className="flex items-center gap-3">
                <span
                  className={`h-3 w-3 rounded-full ${
                    agentTalking ? "pulse-dot bg-[var(--green)]" : "bg-[rgba(255,255,255,0.15)]"
                  }`}
                />
                <div>
                  <p className="text-xs text-[var(--text-muted)]">Status</p>
                  <p className={`text-sm font-semibold ${statusColor}`}>{safeText(statusLabel)}</p>
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-center gap-3">
                {callStatus === "active" || callStatus === "connecting" ? (
                  <Button
                    className="h-12 min-w-[180px] bg-[var(--red)] text-white hover:bg-[#ff5a5a]"
                    onClick={() => endCall()}
                    size="lg"
                    type="button"
                  >
                    End Call
                  </Button>
                ) : (
                  <Button
                    className="h-12 min-w-[180px] bg-[var(--green)] text-black hover:bg-[#3dff9c]"
                    onClick={() => void startCall()}
                    size="lg"
                    type="button"
                  >
                    Start Call
                  </Button>
                )}
              </div>

              {callError ? <p className="text-xs text-[var(--red)]">{safeText(callError)}</p> : null}
            </div>

            <div className="mt-6 rounded-xl border border-[var(--line)] bg-[rgba(0,0,0,0.35)] p-4">
              <div className="mb-2 flex items-center justify-between text-xs text-[var(--text-muted)]">
                <span>Live transcript</span>
                <span>{agentTalking ? "Agent speaking" : "Listening"}</span>
              </div>
              <div className="min-h-[120px] whitespace-pre-wrap text-sm text-white">
                {safeText(transcript) || "Transcript will appear here once the call starts."}
              </div>
            </div>

            {vadLog.length > 0 ? (
              <div className="mt-4 rounded-xl border border-[var(--line)] bg-[rgba(0,0,0,0.35)] p-4">
                <div className="mb-2 flex items-center justify-between text-xs text-[var(--text-muted)]">
                  <span>Event Log</span>
                  <span>{vadLog.length} events</span>
                </div>
                <div className="max-h-[160px] overflow-y-auto font-mono text-xs leading-relaxed text-[var(--text-muted)]">
                  {vadLog.map((entry, i) => {
                    const isInterrupt =
                      entry.event === "agent_stop" &&
                      i > 0 &&
                      vadLog[i - 1]?.event === "agent_start" &&
                      entry.ts - vadLog[i - 1].ts < 2000;
                    return (
                      <div key={i} className={isInterrupt ? "text-[var(--red)]" : ""}>
                        <span className="text-[var(--text-muted)]">{(entry.ts / 1000).toFixed(2)}s</span>{" "}
                        <span className={
                          entry.event.startsWith("agent_start") ? "text-[var(--green)]" :
                          entry.event.startsWith("agent_stop") ? "text-[var(--amber)]" :
                          "text-white"
                        }>
                          {entry.event}
                        </span>
                        {entry.detail ? <span className="ml-2 text-[var(--text-muted)]">({entry.detail})</span> : null}
                        {isInterrupt ? <span className="ml-2 text-[var(--red)]">possible false interrupt</span> : null}
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : null}
          </div>

          {callStatus === "active" || callStatus === "ended" ? (
            <div className="mb-8 rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] p-6">
              <h3 className="mb-3 text-sm font-semibold text-white">Send Payment Link</h3>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
                <div className="flex-1">
                  <label className="mb-1 block text-xs text-[var(--text-muted)]" htmlFor="prospect-email">Email address</label>
                  <Input
                    disabled={sendingEmail}
                    id="prospect-email"
                    onChange={(e) => { setEmailInput(e.target.value); setEmailSent(false); }}
                    placeholder="prospect@example.com"
                    type="email"
                    value={emailInput}
                  />
                </div>
                <div className="w-full sm:w-48">
                  <label className="mb-1 block text-xs text-[var(--text-muted)]" htmlFor="plan-tier">Plan</label>
                  <Select
                    disabled={sendingEmail}
                    id="plan-tier"
                    onChange={(e) => setPlanTier(e.target.value as "one_incident" | "two_incident")}
                    value={planTier}
                  >
                    <option value="one_incident">1 Incident — $650/yr</option>
                    <option value="two_incident">2 Incidents — $1,100/yr</option>
                  </Select>
                </div>
                <Button
                  className="h-10 min-w-[160px] bg-[var(--brand-1)] text-white hover:bg-[#3da0ff]"
                  disabled={sendingEmail || !emailInput.trim()}
                  onClick={() => void sendPaymentEmail()}
                  type="button"
                >
                  {sendingEmail ? "Sending..." : "Send Payment Link"}
                </Button>
              </div>
              {emailSent ? <p className="mt-3 text-xs text-[var(--green)]">Payment link email sent!</p> : null}
            </div>
          ) : null}

          <VoiceTester />
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Performance Stats</CardTitle>
          </CardHeader>
          <CardContent className="grid grid-cols-1 gap-3 text-sm md:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] px-3 py-2">
              <p className="text-xs text-[var(--text-muted)]">Avg Call Duration</p>
              <p className="text-lg font-bold text-white">{avgDuration}</p>
            </div>
            <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] px-3 py-2">
              <p className="text-xs text-[var(--text-muted)]">Objection Handle Rate</p>
              <p className="text-lg font-bold text-white">{objectionRate}</p>
            </div>
            <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] px-3 py-2">
              <p className="text-xs text-[var(--text-muted)]">Close Rate</p>
              <p className="text-lg font-bold text-white">{closeRate}</p>
            </div>
            <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] px-3 py-2">
              <p className="text-xs text-[var(--text-muted)]">Top Objection</p>
              <p className="text-lg font-bold text-white">{topObjection}</p>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
