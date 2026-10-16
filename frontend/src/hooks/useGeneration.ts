import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../services/api";
import type { FormValues, Job, Project } from "../types";

export function useGeneration() {
  const [project, setProject] = useState<Project | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const timer = useRef<number>();

  const stop = () => window.clearTimeout(timer.current);
  useEffect(() => stop, []);

  const poll = useCallback((jobId: string, projectId: string) => {
    const tick = async () => {
      try {
        const j = await api.job(jobId);
        setJob(j);
        if (j.status === "completed") { setProject(await api.project(projectId)); return; }
        if (j.status === "queued" || j.status === "running") timer.current = window.setTimeout(tick, 1500);
      } catch (e) {
        setError(e instanceof ApiError ? e : new ApiError("Lost connection to the backend."));
      }
    };
    tick();
  }, []);

  const start = useCallback(async (values: FormValues) => {
    stop(); setError(null); setSubmitting(true); setJob(null); setProject(null);
    try {
      const p = await api.createProject(values);
      setProject(p);
      const j = await api.generate(p.id);
      setJob(j);
      poll(j.id, p.id);
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError("Something went wrong."));
    } finally { setSubmitting(false); }
  }, [poll]);

  const cancel = useCallback(async () => {
    if (!job) return;
    try { setJob(await api.cancel(job.id)); } catch (e) { setError(e instanceof ApiError ? e : null); }
  }, [job]);

  const busy = submitting || job?.status === "queued" || job?.status === "running";
  return { project, job, error, busy, start, cancel };
}
