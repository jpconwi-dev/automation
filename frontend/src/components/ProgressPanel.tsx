import { AlertTriangle, Check, Loader2, X } from "lucide-react";
import type { Job, Stage } from "../types";

const STAGES: { key: Stage; label: string }[] = [
  { key: "script", label: "Preparing script" },
  { key: "images", label: "Generating illustrations" },
  { key: "narration", label: "Creating narration" },
  { key: "audio", label: "Mixing music & sound effects" },
  { key: "rendering", label: "Rendering video" },
  { key: "completed", label: "Completed" },
];

export default function ProgressPanel({ job, onCancel }: { job: Job; onCancel: () => void }) {
  const idx = STAGES.findIndex((s) => s.key === job.stage);
  const active = job.status === "queued" || job.status === "running";
  return (
    <section className="rounded-3xl border border-ink-700 bg-ink-800/80 p-5" aria-live="polite">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-display text-lg font-semibold">{job.status === "completed" ? "Your video is ready" : "Generation progress"}</h2>
        <span className="text-sm tabular-nums text-zinc-400">{job.progress}%</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-ink-700" role="progressbar" aria-valuenow={job.progress} aria-valuemin={0} aria-valuemax={100}>
        <div className="h-full rounded-full bg-gradient-to-r from-brand-pink to-brand-purple transition-all duration-500" style={{ width: `${job.progress}%` }} />
      </div>
      <ol className="mt-4 space-y-2">
        {STAGES.map((s, i) => {
          const done = job.status === "completed" || i < idx;
          const current = active && i === idx;
          return (
            <li key={s.key} className={`flex items-center gap-2.5 text-sm ${done ? "text-zinc-300" : current ? "text-white" : "text-zinc-600"}`}>
              <span className={`grid h-5 w-5 place-items-center rounded-full ${done ? "bg-brand-purple/30 text-brand-pink" : current ? "text-brand-pink" : "border border-ink-600"}`}>
                {done ? <Check size={12} /> : current ? <Loader2 size={14} className="animate-spin" /> : null}
              </span>
              {s.label}
              {current && job.message && <span className="text-xs text-zinc-500">· {job.message}</span>}
            </li>
          );
        })}
      </ol>
      {job.status === "failed" && (
        <div className="mt-4 flex gap-2 rounded-xl border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-200" role="alert">
          <AlertTriangle size={16} className="mt-0.5 shrink-0" /><span className="break-words">{job.error}</span>
        </div>
      )}
      {job.status === "cancelled" && <p className="mt-4 text-sm text-zinc-400">Generation was cancelled. Change anything and generate again.</p>}
      {active && (
        <button onClick={onCancel} disabled={job.cancel_requested}
          className="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-ink-600 px-3 py-1.5 text-sm text-zinc-300 transition hover:border-rose-400 hover:text-rose-300 disabled:opacity-50">
          <X size={14} /> {job.cancel_requested ? "Cancelling…" : "Cancel"}
        </button>
      )}
    </section>
  );
}
