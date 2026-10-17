export type Style = "cinematic" | "whiteboard" | "minimal" | "illustrated";
export type Aspect = "16:9" | "9:16" | "1:1";
export type Duration = 30 | 60 | 90 | 120 | 300;
export type Mode = "explainer" | "prompt";
export type Tone = "friendly" | "professional" | "energetic" | "calm" | "humorous" | "dramatic";
export type MusicStyle = "auto" | "calm" | "upbeat" | "cinematic" | "playful" | "epic";

export interface FormValues {
  mode: Mode;
  topic: string;
  audience: string;
  prompt: string;
  style: Style;
  duration: Duration;
  language: string;
  aspect_ratio: Aspect;
  tone: Tone;
  voice: string;          // "auto" or a voice id from /api/voices
  voice_speed: number;    // 0.8 to 1.3
  background_music: boolean;
  music_style: MusicStyle;
  sound_effects: boolean;
}

export interface Voice { id: string; label: string; gender: string }
export interface VoiceList { backend: string; voices: Voice[] }

export interface Scene {
  narration: string;
  visual_description: string;
  on_screen_text: string;
  subtitle_text: string;
  duration_seconds: number;
}
export interface Script { title: string; description: string; hook: string; scenes: Scene[]; closing_summary: string }

export interface Project extends FormValues {
  id: string; status: string; script: Script | null; video_ready: boolean; silent: boolean; latest_job_id: string | null;
  video_seconds: number | null;
}
export type Stage = "queued" | "script" | "images" | "narration" | "audio" | "rendering" | "completed";
export interface Job {
  id: string; project_id: string; status: "queued" | "running" | "completed" | "failed" | "cancelled";
  stage: Stage; progress: number; message: string; error: string | null;
  scenes_total: number; images_done: number; cancel_requested: boolean;
}
export interface Asset { name: string; url: string; ai_generated?: boolean }
