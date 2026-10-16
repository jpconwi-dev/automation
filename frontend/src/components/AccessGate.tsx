import { Lock } from "lucide-react";
import { useState } from "react";
import { accessKey, api, ApiError } from "../services/api";

/** Shown when the backend has ACCESS_KEY set and the browser doesn't have a valid key yet. */
export default function AccessGate({ onUnlocked }: { onUnlocked: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);

  const submit = async () => {
    setChecking(true); setError("");
    accessKey.set(value.trim());
    try {
      await api.voices("en-us"); // any protected endpoint works as a check
      onUnlocked();
    } catch (e) {
      accessKey.set("");
      setError(e instanceof ApiError && e.code === "unauthorized" ? "That key isn't right." : e instanceof ApiError ? e.message : "Something went wrong.");
    } finally { setChecking(false); }
  };

  return (
    <div className="mx-auto flex min-h-[70vh] max-w-sm flex-col justify-center gap-4 px-4">
      <div className="flex items-center gap-2 font-display text-lg font-bold"><Lock size={18} /> Private server</div>
      <p className="text-sm text-zinc-400">Enter the access key set on the backend to use this app.</p>
      <input type="password" autoFocus value={value} onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && value.trim() && !checking && submit()} aria-label="Access key"
        className="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm outline-none focus:border-brand-purple" />
      {error && <p className="text-sm text-rose-300" role="alert">{error}</p>}
      <button type="button" disabled={!value.trim() || checking} onClick={submit}
        className="rounded-xl bg-gradient-to-br from-brand-pink to-brand-purple px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">
        {checking ? "Checking…" : "Continue"}
      </button>
    </div>
  );
}
