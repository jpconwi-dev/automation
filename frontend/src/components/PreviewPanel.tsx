import { Download, Pause, Play, Volume2, VolumeX } from "lucide-react";
import { useRef, useState } from "react";
import { api } from "../services/api";
import type { Aspect, Job, Project } from "../types";

const RATIO: Record<Aspect, string> = { "16:9": "16 / 9", "9:16": "9 / 16", "1:1": "1 / 1" };
const pad = (i: number) => String(i + 1).padStart(2, "0");
const clock = (sec: number) => `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}`;

function Demo() {
  return (
    <svg viewBox="0 0 640 360" className="h-full w-full" role="img" aria-label="Sample explainer frame: sun, leaf and arrows">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#2a1646" /><stop offset="1" stopColor="#5b1d6b" /></linearGradient>
        <linearGradient id="s" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#ff4d9d" /><stop offset="1" stopColor="#ffb86b" /></linearGradient>
      </defs>
      <rect width="640" height="360" fill="url(#g)" />
      <circle cx="120" cy="90" r="46" fill="url(#s)" />
      {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => <line key={i} x1="120" y1="90" x2={120 + 82 * Math.cos((i * Math.PI) / 4)} y2={90 + 82 * Math.sin((i * Math.PI) / 4)} stroke="#ffb86b" strokeWidth="3" strokeLinecap="round" opacity=".5" />)}
      <path d="M300 280 C 320 190, 420 170, 520 190 C 500 270, 410 300, 300 280 Z" fill="#8b5cf6" opacity=".9" />
      <path d="M300 280 C 360 240, 420 220, 500 200" stroke="#f5d0fe" strokeWidth="3" fill="none" strokeLinecap="round" />
      <path d="M190 120 C 240 140, 280 160, 330 200" stroke="#f5d0fe" strokeWidth="3" strokeDasharray="6 8" fill="none" strokeLinecap="round" />
      <rect x="24" y="24" width="190" height="34" rx="10" fill="#0e0e11" opacity=".7" />
      <text x="40" y="47" fill="#fff" fontFamily="sans-serif" fontSize="16" fontWeight="700">Light becomes sugar</text>
      <rect x="150" y="304" width="340" height="34" rx="6" fill="#000" opacity=".6" />
      <text x="320" y="326" fill="#fff" fontFamily="sans-serif" fontSize="15" textAnchor="middle">Sample frame. Your video appears here.</text>
    </svg>
  );
}

function Player({ project }: { project: Project }) {
  const ref = useRef<HTMLVideoElement>(null);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const toggle = () => { const v = ref.current; if (!v) return; v.paused ? v.play() : v.pause(); };
  const btn = "grid h-9 w-9 place-items-center rounded-full bg-black/60 text-white backdrop-blur transition hover:bg-brand-magenta";
  return (
    <div className="relative h-full w-full">
      <video ref={ref} src={api.videoUrl(project.id)} className="h-full w-full bg-black object-contain" playsInline
        onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)} onClick={toggle} />
      <div className="absolute bottom-3 left-3 flex gap-2">
        <button className={btn} onClick={toggle} aria-label={playing ? "Pause" : "Play"}>{playing ? <Pause size={16} /> : <Play size={16} />}</button>
        <button className={btn} onClick={() => { setMuted(!muted); if (ref.current) ref.current.muted = !muted; }} aria-label={muted ? "Unmute" : "Mute"}>
          {muted ? <VolumeX size={16} /> : <Volume2 size={16} />}
        </button>
      </div>
      <a href={api.videoUrl(project.id, true)} className={`${btn} absolute bottom-3 right-3`} aria-label="Download MP4"><Download size={16} /></a>
    </div>
  );
}

function Facts({ project }: { project: Project }) {
  const facts = [
    project.video_seconds != null ? `Length ${clock(project.video_seconds)}` : null,
    project.silent || !project.tone ? null : `${project.tone[0].toUpperCase()}${project.tone.slice(1)} voice`,
    project.background_music ? "Music" : null,
    project.sound_effects ? "Sound effects" : null,
  ].filter(Boolean) as string[];
  return (
    <ul className="mt-4 flex flex-wrap justify-center gap-2 text-xs text-zinc-300" aria-label="Video details">
      {facts.map((f) => <li key={f} className="rounded-full border border-ink-600 bg-ink-800 px-3 py-1">{f}</li>)}
    </ul>
  );
}

function SceneNarration({ project }: { project: Project }) {
  const [playing, setPlaying] = useState<number | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  const play = (i: number) => {
    audio.current?.pause();
    if (playing === i) { setPlaying(null); return; }
    const a = new Audio(api.assetUrl(project.id, `scene_${pad(i)}.wav`));
    a.onended = () => setPlaying(null);
    a.play(); audio.current = a; setPlaying(i);
  };
  return (
    <div className="mt-5">
      <h3 className="mb-2 font-display text-base font-semibold">{project.script?.title}</h3>
      <ul className="space-y-2">
        {project.script?.scenes.map((s, i) => (
          <li key={i} className="flex items-start gap-3 rounded-xl border border-ink-700 bg-ink-800/60 p-3 text-sm">
            {!project.silent && (
              <button onClick={() => play(i)} aria-label={`${playing === i ? "Stop" : "Play"} narration for scene ${i + 1}`}
                className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full bg-brand-purple/25 text-brand-pink transition hover:bg-brand-purple/50">
                {playing === i ? <Pause size={13} /> : <Play size={13} />}
              </button>
            )}
            <div><p className="font-medium text-zinc-100">{s.on_screen_text || `Scene ${i + 1}`}</p><p className="text-zinc-400">{s.narration}</p></div>
          </li>
        ))}
      </ul>
      {project.silent && <p className="mt-2 text-xs text-zinc-500">Narration is turned off on this server (TTS_BACKEND=none), so this video is silent.</p>}
    </div>
  );
}

export default function PreviewPanel({ project, job, aspect }: { project: Project | null; job: Job | null; aspect: Aspect }) {
  const generating = job && (job.status === "queued" || job.status === "running") && job.scenes_total > 0 && job.stage !== "rendering";
  const ready = project?.video_ready;
  return (
    <div>
      <div className="mx-auto overflow-hidden rounded-3xl border border-ink-700 bg-ink-900 shadow-2xl shadow-black/40"
        style={{ aspectRatio: RATIO[aspect], maxHeight: "70vh" }}>
        {ready ? <Player project={project!} /> : generating && project ? (
          <div className="grid h-full w-full grid-cols-3 gap-1.5 overflow-auto p-1.5">
            {Array.from({ length: job!.scenes_total }, (_, i) => i < job!.images_done
              ? <img key={i} src={api.assetUrl(project.id, `scene_${pad(i)}.png?v=${job!.images_done}`)} alt={`Scene ${i + 1}`} className="aspect-square w-full rounded-lg object-cover" />
              : <div key={i} className="aspect-square animate-pulse rounded-lg bg-ink-700" aria-label={`Scene ${i + 1} pending`} />)}
          </div>
        ) : <Demo />}
      </div>
      {ready && <Facts project={project!} />}
      {ready && <SceneNarration project={project!} />}
    </div>
  );
}
