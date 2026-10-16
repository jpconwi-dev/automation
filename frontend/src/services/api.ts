import type { Asset, FormValues, Job, Project, VoiceList } from "../types";

// Empty in dev (Vite proxies /api). On Vercel set VITE_API_URL to the Render backend, e.g. https://explainer-api.onrender.com
const BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");
const KEY_STORE = "explainer_access_key";

/** Optional shared password. The backend only asks for it when ACCESS_KEY is set there. */
export const accessKey = {
  get: (): string => { try { return localStorage.getItem(KEY_STORE) ?? ""; } catch { return ""; } },
  set: (k: string) => { try { if (k) localStorage.setItem(KEY_STORE, k); else localStorage.removeItem(KEY_STORE); } catch { /* storage blocked */ } },
};
const authHeaders = (): Record<string, string> => (accessKey.get() ? { "X-Access-Key": accessKey.get() } : {});
/** <video>, <audio> and <img> tags can't send headers, so media URLs carry the key as a query parameter. */
const withKey = (url: string) => (accessKey.get() ? `${url}${url.includes("?") ? "&" : "?"}key=${encodeURIComponent(accessKey.get())}` : url);

export class ApiError extends Error {
  constructor(message: string, public code = "error", public details?: { field: string; message: string }[]) { super(message); }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { ...init, headers: { "Content-Type": "application/json", ...authHeaders() } });
  } catch {
    throw new ApiError("Can't reach the backend. A free Render server needs about a minute to wake up: wait a moment and try again.", "network");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(body?.error?.message ?? `Request failed (${res.status})`, body?.error?.code, body?.error?.details);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => req<{ status: string; auth_required?: boolean; dependencies: Record<string, { ok: boolean; detail: string }> }>("/api/health"),
  createProject: (v: FormValues) => req<Project>("/api/projects", { method: "POST", body: JSON.stringify(v) }),
  generate: (id: string) => req<Job>(`/api/projects/${id}/generate`, { method: "POST" }),
  job: (id: string) => req<Job>(`/api/jobs/${id}`),
  cancel: (id: string) => req<Job>(`/api/jobs/${id}/cancel`, { method: "POST" }),
  project: (id: string) => req<Project>(`/api/projects/${id}`),
  voices: (language: string) => req<VoiceList>(`/api/voices?language=${encodeURIComponent(language)}`),
  /** A few seconds of sample narration as a Blob (so errors from the server can be shown as text). */
  voicePreview: async (v: Pick<FormValues, "language" | "voice" | "tone" | "voice_speed">): Promise<Blob> => {
    const q = new URLSearchParams({ language: v.language, voice: v.voice, tone: v.tone, speed: String(v.voice_speed) });
    let res: Response;
    try { res = await fetch(`${BASE}/api/voices/preview?${q}`, { headers: authHeaders() }); } catch { throw new ApiError("Can't reach the backend.", "network"); }
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw new ApiError(body?.error?.message ?? `Preview failed (${res.status})`, body?.error?.code);
    }
    return res.blob();
  },
  assets: (id: string) => req<{ assets: Asset[] }>(`/api/projects/${id}/assets`),
  videoUrl: (id: string, download = false) => withKey(`${BASE}/api/projects/${id}/video${download ? "?download=true" : ""}`),
  assetUrl: (id: string, name: string) => withKey(`${BASE}/api/projects/${id}/assets/${name}`),
};
