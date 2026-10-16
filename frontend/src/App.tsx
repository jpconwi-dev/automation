import { AlertTriangle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import AccessGate from "./components/AccessGate";
import GeneratorForm, { validate } from "./components/GeneratorForm";
import PreviewPanel from "./components/PreviewPanel";
import ProgressPanel from "./components/ProgressPanel";
import { useGeneration } from "./hooks/useGeneration";
import { accessKey, api } from "./services/api";
import type { FormValues } from "./types";

const INITIAL: FormValues = {
  mode: "explainer", topic: "", audience: "", prompt: "", style: "illustrated", duration: 60, language: "en-us", aspect_ratio: "16:9",
  tone: "friendly", voice: "auto", voice_speed: 1, background_music: true, music_style: "auto", sound_effects: true,
};

export default function App() {
  const [values, setValues] = useState<FormValues>(INITIAL);
  const [touched, setTouched] = useState(false);
  const [issues, setIssues] = useState<string[]>([]);
  const [locked, setLocked] = useState(false);
  const { project, job, error, busy, start, cancel } = useGeneration();
  const errors = useMemo(() => (touched ? validate(values) : {}), [touched, values]);

  const loadHealth = () =>
    api.health().then((h) => {
      setLocked(!!h.auth_required && !accessKey.get());
      setIssues(Object.entries(h.dependencies).filter(([, d]) => !d.ok).map(([k, d]) => `${k}: ${d.detail}`));
    }).catch(() => setIssues(["backend: not reachable. A free Render server takes about a minute to wake up, so reload shortly."]));
  useEffect(() => { loadHealth(); }, []);
  useEffect(() => { if (error?.code === "unauthorized") { accessKey.set(""); setLocked(true); } }, [error]);

  if (locked) return <AccessGate onUnlocked={() => { setLocked(false); loadHealth(); }} />;

  const submit = () => {
    setTouched(true);
    if (Object.keys(validate(values)).length === 0) start(values);
  };

  return (
    <div className="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:py-12">
      <header className="mb-8 flex items-center gap-2 font-display text-lg font-bold">
        <span className="h-3 w-3 rounded-full bg-gradient-to-br from-brand-pink to-brand-purple" /> Clickcast
      </header>
      {issues.length > 0 && (
        <div className="mb-6 flex gap-3 rounded-2xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-100" role="status">
          <AlertTriangle size={18} className="mt-0.5 shrink-0" />
          <div><p className="font-medium">Some tools need setup before videos can be made.</p>
            <ul className="mt-1 list-disc pl-5 text-amber-200/80">{issues.map((i) => <li key={i}>{i}</li>)}</ul>
            <p className="mt-1 text-amber-200/70">See DEPLOY.md for setup steps.</p></div>
        </div>
      )}
      <div className="grid gap-8 lg:grid-cols-2 lg:gap-12">
        <section className="lg:sticky lg:top-8 lg:self-start">
          <h1 className="font-display text-4xl font-extrabold leading-[1.05] tracking-tight sm:text-5xl">Explainers that make things click.</h1>
          <p className="mb-8 mt-4 max-w-md text-lg text-zinc-400">AI explainer videos that teach in simple steps.</p>
          <PreviewPanel project={project} job={job} aspect={values.aspect_ratio} />
        </section>
        <section className="space-y-5">
          <GeneratorForm values={values} onChange={setValues} errors={errors} busy={busy} onSubmit={submit} />
          {error && (
            <div className="rounded-xl border border-rose-500/40 bg-rose-500/10 p-3 text-sm text-rose-200" role="alert">
              {error.message}
              {error.details?.map((d) => <div key={d.field} className="text-rose-300/80">{d.field}: {d.message}</div>)}
            </div>
          )}
          {job && <ProgressPanel job={job} onCancel={cancel} />}
        </section>
      </div>
    </div>
  );
}
