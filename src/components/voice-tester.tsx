"use client";

const FUNCTIONS = [
  "send_payment_email",
  "log_objection",
  "schedule_followup",
  "confirm_payment",
  "mark_do_not_call",
];

export default function VoiceTester() {
  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] p-4">
        <p className="text-sm text-white">Voice server function list</p>
        <div className="mt-3 grid grid-cols-1 gap-2 text-xs text-[var(--text-muted)] md:grid-cols-2">
          {FUNCTIONS.map((name) => (
            <div
              className="rounded-lg border border-[var(--line)] bg-[rgba(0,0,0,0.25)] px-3 py-2"
              key={name}
            >
              <p data-mono="true">{name}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="rounded-xl border border-[var(--line)] bg-[rgba(255,255,255,0.02)] p-4 text-xs text-[var(--text-muted)]">
        Use the browser call widget above for live testing. You can also dial from
        <span className="mx-1 text-white">Dashboard &rarr; Prospects &rarr; Call Now</span>
        or by executing
        <span className="mx-1 text-white" data-mono="true">
          node scripts/batch-dial.js 1
        </span>
        after setting the voice server environment variables.
      </div>
    </div>
  );
}
