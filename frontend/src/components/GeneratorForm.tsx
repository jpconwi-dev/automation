import { Clapperboard, Film, Lightbulb, Loader2, Music, Palette, Play, Presentation, Shapes, Sparkles, Square, Volume2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "../services/api";
import type { Aspect, Duration, FormValues, Mode, MusicStyle, Style, Tone, Voice } from "../types";

const MODES: { id: Mode; label: string; hint: string; icon: ReactNode }[] = [
  { id: "explainer", label: "Explainer AI", hint: "Topic + audience, explained in simple steps", icon: <Lightbulb size={18} /> },
  { id: "prompt", label: "Prompt to video", hint: "Describe any video in your own words", icon: <Film size={18} /> },
];
const STYLES: { id: Style; label: string; hint: string; icon: ReactNode }[] = [
  { id: "cinematic", label: "Cinematic", hint: "Dramatic light", icon: <Clapperboard size={18} /> },
  { id: "whiteboard", label: "Whiteboard", hint: "Marker sketches", icon: <Presentation size={18} /> },
  { id: "minimal", label: "Minimal", hint: "Flat and clean", icon: <Shapes size={18} /> },
  { id: "illustrated", label: "Illustrated", hint: "Storybook color", icon: <Palette size={18} /> },
];
const DURATIONS: { v: Duration; label: string; exact: string }[] = [
  { v: 30, label: "30 seconds", exact: "0:30" }, { v: 60, label: "60 seconds", exact: "1:00" },
  { v: 120, label: "2 minutes", exact: "2:00" }, { v: 300, label: "5 minutes", exact: "5:00" },
];
const TONES: { id: Tone; label: string; hint: string }[] = [
  { id: "friendly", label: "Friendly", hint: "Warm teacher" },
  { id: "professional", label: "Professional", hint: "Clear and polished" },
  { id: "energetic", label: "Energetic", hint: "Upbeat, fast" },
  { id: "calm", label: "Calm", hint: "Slow, soothing" },
  { id: "humorous", label: "Humorous", hint: "Light jokes" },
  { id: "dramatic", label: "Dramatic", hint: "Trailer voice" },
];
const MUSIC: { id: MusicStyle; label: string }[] = [
  { id: "auto", label: "Auto (match the tone)" }, { id: "calm", label: "Calm ambient" }, { id: "upbeat", label: "Upbeat" },
  { id: "cinematic", label: "Cinematic" }, { id: "playful", label: "Playful" },
];
const ASPECTS: Aspect[] = ["16:9", "9:16", "1:1"];
export const LANGUAGES = [
  ["en-us", "English (US)"], ["en-gb", "English (UK)"], ["es", "Spanish"], ["fr", "French"], ["hi", "Hindi"],
  ["it", "Italian"], ["ja", "Japanese"], ["pt-br", "Portuguese (Brazil)"], ["zh", "Chinese (Mandarin)"],
];
export const MAX_TOPIC = 500;
export const MAX_PROMPT = 2000;

export function validate(v: FormValues): Partial<Record<keyof FormValues, string>> {
  const e: Partial<Record<keyof FormValues, string>> = {};
  if (v.mode === "explainer") {
    if (v.topic.trim().length < 3) e.topic = "Describe the topic in at least 3 characters.";
    if (v.topic.length > MAX_TOPIC) e.topic = `Keep the topic under ${MAX_TOPIC} characters.`;
    if (v.audience.trim().length < 2) e.audience = "Say who the video is for, such as “grade 6 students”.";
  } else {
    if (v.prompt.trim().length < 10) e.prompt = "Describe the video you want in at least 10 characters.";
    if (v.prompt.length > MAX_PROMPT) e.prompt = `Keep the prompt under ${MAX_PROMPT} characters.`;
  }
  return e;
}

const field = "w-full rounded-xl border border-ink-600 bg-ink-900 px-3.5 py-2.5 text-sm text-white placeholder:text-zinc-500 transition focus:border-brand-magenta focus:outline-none disabled:opacity-60";
const label = "mb-1.5 block text-sm font-medium text-zinc-300";
const chip = (on: boolean) => `rounded-xl border px-3 py-2 text-left transition ${on
  ? "border-brand-magenta bg-brand-magenta/10 text-white" : "border-ink-600 bg-ink-900 text-zinc-300 hover:border-zinc-500"}`;

function Toggle({ on, onChange, children, disabled }: { on: boolean; onChange: (v: boolean) => void; children: ReactNode; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={on} disabled={disabled} onClick={() => onChange(!on)}
      className="flex w-full items-center justify-between gap-3 rounded-xl border border-ink-600 bg-ink-900 px-3.5 py-2.5 text-left text-sm text-zinc-200 transition hover:border-zinc-500 disabled:opacity-60">
      <span>{children}</span>
      <span className={`relative h-5 w-9 shrink-0 rounded-full transition ${on ? "bg-brand-magenta" : "bg-ink-600"}`}>
        <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${on ? "left-[18px]" : "left-0.5"}`} />
      </span>
    </button>
  );
}

type SetField = <K extends keyof FormValues>(k: K, v: FormValues[K]) => void;

/** Voice picker: loads the voices for the chosen language and plays a short sample with the current tone and speed. */
function VoicePicker({ values, set, busy }: { values: FormValues; set: SetField; busy: boolean }) {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [backend, setBackend] = useState("");
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    let live = true;
    api.voices(values.language).then((r) => {
      if (!live) return;
      setVoices(r.voices); setBackend(r.backend);
      if (values.voice !== "auto" && !r.voices.some((v) => v.id === values.voice)) set("voice", "auto");
    }).catch(() => { if (live) { setVoices([]); setBackend(""); } });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values.language]);

  useEffect(() => () => { audio.current?.pause(); }, []);

  const preview = async () => {
    if (playing) { audio.current?.pause(); setPlaying(false); return; }
    setMsg(null); setLoading(true);
    try {
      const blob = await api.voicePreview(values);
      const a = new Audio(URL.createObjectURL(blob));
      a.onended = () => { setPlaying(false); URL.revokeObjectURL(a.src); };
      audio.current = a;
      await a.play();
      setPlaying(true);
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Couldn't play the preview.");
    } finally { setLoading(false); }
  };

  const off = backend === "none";
  return (
    <div className="space-y-3">
      <div>
        <label htmlFor="voice" className={label}>Voice</label>
        <div className="flex gap-2">
          <select id="voice" disabled={busy || off} value={values.voice} onChange={(e) => set("voice", e.target.value)} className={field}>
            <option value="auto">Automatic</option>
            {voices.map((v) => <option key={v.id} value={v.id}>{v.label}{v.gender ? ` (${v.gender})` : ""}</option>)}
          </select>
          <button type="button" onClick={preview} disabled={busy || off || loading}
            className="flex shrink-0 items-center gap-1.5 rounded-xl border border-ink-600 bg-ink-900 px-3 text-sm text-zinc-200 transition hover:border-brand-magenta disabled:opacity-60"
            aria-label={playing ? "Stop voice preview" : "Play voice preview"}>
            {loading ? <Loader2 size={15} className="animate-spin" /> : playing ? <Square size={14} /> : <Play size={14} />}
            {playing ? "Stop" : "Preview"}
          </button>
        </div>
        {off && <p className="mt-1 text-xs text-zinc-500">Narration is turned off on this server (TTS_BACKEND=none).</p>}
        {msg && <p className="mt-1 text-xs text-rose-400" role="alert">{msg}</p>}
      </div>
      <div>
        <label htmlFor="speed" className={`${label} flex justify-between`}>
          <span>Speaking speed</span><span className="tabular-nums text-zinc-400">{values.voice_speed.toFixed(2)}×</span>
        </label>
        <input id="speed" type="range" min={0.8} max={1.3} step={0.05} disabled={busy || off} value={values.voice_speed}
          onChange={(e) => set("voice_speed", Number(e.target.value))} className="w-full accent-brand-magenta" />
      </div>
    </div>
  );
}

interface Props {
  values: FormValues; onChange: (v: FormValues) => void; errors: Partial<Record<keyof FormValues, string>>;
  busy: boolean; onSubmit: () => void;
}

export default function GeneratorForm({ values, onChange, errors, busy, onSubmit }: Props) {
  const set: SetField = (k, val) => onChange({ ...values, [k]: val });
  const exact = DURATIONS.find((d) => d.v === values.duration)?.exact;
  return (
    <form onSubmit={(e) => { e.preventDefault(); onSubmit(); }} className="space-y-5 rounded-3xl border border-ink-700 bg-ink-800/80 p-5 sm:p-6" noValidate>
      <fieldset disabled={busy}>
        <legend className="sr-only">What do you want to make?</legend>
        <div className="grid grid-cols-2 gap-2.5">
          {MODES.map((m) => (
            <button type="button" key={m.id} onClick={() => set("mode", m.id)} aria-pressed={values.mode === m.id} className={`flex items-start gap-3 ${chip(values.mode === m.id)}`}>
              <span className={`mt-0.5 ${values.mode === m.id ? "text-brand-pink" : "text-zinc-500"}`}>{m.icon}</span>
              <span><span className="block text-sm font-medium">{m.label}</span><span className="block text-xs text-zinc-500">{m.hint}</span></span>
            </button>
          ))}
        </div>
      </fieldset>

      {values.mode === "explainer" ? (
        <>
          <div>
            <label htmlFor="topic" className={label}>Topic</label>
            <textarea id="topic" rows={3} maxLength={MAX_TOPIC + 50} disabled={busy} value={values.topic}
              onChange={(e) => set("topic", e.target.value)} placeholder="How photosynthesis works"
              aria-invalid={!!errors.topic} className={`${field} resize-none`} />
            <div className="mt-1 flex justify-between text-xs">
              <span className="text-rose-400" role="alert">{errors.topic}</span>
              <span className={values.topic.length > MAX_TOPIC ? "text-rose-400" : "text-zinc-500"}>{values.topic.length}/{MAX_TOPIC}</span>
            </div>
          </div>
          <div>
            <label htmlFor="audience" className={label}>Target audience</label>
            <input id="audience" maxLength={200} disabled={busy} value={values.audience} onChange={(e) => set("audience", e.target.value)}
              placeholder="Curious middle school students" aria-invalid={!!errors.audience} className={field} />
            {errors.audience && <p className="mt-1 text-xs text-rose-400" role="alert">{errors.audience}</p>}
          </div>
        </>
      ) : (
        <>
          <div>
            <label htmlFor="prompt" className={label}>Describe your video</label>
            <textarea id="prompt" rows={6} maxLength={MAX_PROMPT + 100} disabled={busy} value={values.prompt}
              onChange={(e) => set("prompt", e.target.value)} aria-invalid={!!errors.prompt} className={`${field} resize-none`}
              placeholder="A cheerful video about a day in the life of a coffee farmer in Mindanao, from sunrise harvest to the first cup, with a hopeful ending." />
            <div className="mt-1 flex justify-between text-xs">
              <span className="text-rose-400" role="alert">{errors.prompt}</span>
              <span className={values.prompt.length > MAX_PROMPT ? "text-rose-400" : "text-zinc-500"}>{values.prompt.length}/{MAX_PROMPT}</span>
            </div>
          </div>
          <div>
            <label htmlFor="audience" className={label}>Audience <span className="font-normal text-zinc-500">(optional)</span></label>
            <input id="audience" maxLength={200} disabled={busy} value={values.audience} onChange={(e) => set("audience", e.target.value)}
              placeholder="Anyone who likes coffee" className={field} />
          </div>
        </>
      )}

      <fieldset disabled={busy}>
        <legend className={label}>Visual style</legend>
        <div className="grid grid-cols-2 gap-2.5">
          {STYLES.map((s) => (
            <button type="button" key={s.id} onClick={() => set("style", s.id)} aria-pressed={values.style === s.id} className={`flex items-center gap-3 ${chip(values.style === s.id)}`}>
              <span className={values.style === s.id ? "text-brand-pink" : "text-zinc-500"}>{s.icon}</span>
              <span><span className="block text-sm font-medium">{s.label}</span><span className="block text-xs text-zinc-500">{s.hint}</span></span>
            </button>
          ))}
        </div>
      </fieldset>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="duration" className={label}>Duration</label>
          <select id="duration" disabled={busy} value={values.duration} onChange={(e) => set("duration", Number(e.target.value) as Duration)} className={field}>
            {DURATIONS.map((d) => <option key={d.v} value={d.v}>{d.label}</option>)}
          </select>
          <p className="mt-1 text-xs text-zinc-500">Your video will be exactly {exact} long.</p>
        </div>
        <div>
          <label htmlFor="language" className={label}>Language</label>
          <select id="language" disabled={busy} value={values.language} onChange={(e) => onChange({ ...values, language: e.target.value, voice: "auto" })} className={field}>
            {LANGUAGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </div>
      </div>

      <fieldset disabled={busy}>
        <legend className={label}>Aspect ratio</legend>
        <div className="grid grid-cols-3 gap-2.5">
          {ASPECTS.map((a) => (
            <button type="button" key={a} onClick={() => set("aspect_ratio", a)} aria-pressed={values.aspect_ratio === a}
              className={`rounded-xl border py-2 text-sm transition ${values.aspect_ratio === a
                ? "border-brand-magenta bg-brand-magenta/10 text-white" : "border-ink-600 bg-ink-900 text-zinc-300 hover:border-zinc-500"}`}>{a}</button>
          ))}
        </div>
      </fieldset>

      <section className="space-y-4 rounded-2xl border border-ink-700 bg-ink-900/50 p-4" aria-labelledby="voice-h">
        <h3 id="voice-h" className="flex items-center gap-2 font-display text-sm font-semibold text-zinc-100"><Volume2 size={16} className="text-brand-pink" /> Voice and tone</h3>
        <fieldset disabled={busy}>
          <legend className={label}>Tone of the AI narrator</legend>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {TONES.map((t) => (
              <button type="button" key={t.id} onClick={() => set("tone", t.id)} aria-pressed={values.tone === t.id} className={chip(values.tone === t.id)}>
                <span className="block text-sm font-medium">{t.label}</span><span className="block text-xs text-zinc-500">{t.hint}</span>
              </button>
            ))}
          </div>
          <p className="mt-1.5 text-xs text-zinc-500">Changes how the script is written and how the voice sounds.</p>
        </fieldset>
        <VoicePicker values={values} set={set} busy={busy} />
      </section>

      <section className="space-y-3 rounded-2xl border border-ink-700 bg-ink-900/50 p-4" aria-labelledby="sound-h">
        <h3 id="sound-h" className="flex items-center gap-2 font-display text-sm font-semibold text-zinc-100"><Music size={16} className="text-brand-pink" /> Sound</h3>
        <Toggle on={values.background_music} onChange={(v) => set("background_music", v)} disabled={busy}>Background music</Toggle>
        {values.background_music && (
          <div>
            <label htmlFor="music" className={label}>Music style</label>
            <select id="music" disabled={busy} value={values.music_style} onChange={(e) => set("music_style", e.target.value as MusicStyle)} className={field}>
              {MUSIC.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
            </select>
            <p className="mt-1 text-xs text-zinc-500">Original music made for each video. It gets quieter while the narrator speaks.</p>
          </div>
        )}
        <Toggle on={values.sound_effects} onChange={(v) => set("sound_effects", v)} disabled={busy}>Sound effects <span className="text-zinc-500">(whooshes, pops, closing chime)</span></Toggle>
      </section>

      <button type="submit" disabled={busy}
        className="flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-brand-pink via-brand-magenta to-brand-purple px-4 py-3 font-semibold text-white shadow-lg shadow-brand-magenta/20 transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60">
        {busy ? <><Loader2 className="animate-spin" size={18} /> Generating…</> : <><Sparkles size={18} /> {values.mode === "prompt" ? "Generate video" : "Generate explainer"}</>}
      </button>
    </form>
  );
}
