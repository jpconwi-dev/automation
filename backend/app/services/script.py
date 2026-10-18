import json
import re

import httpx

from ..core.config import settings
from ..core.errors import PipelineError
from ..schemas.schemas import Script  # noqa: E402

SCENE_COUNT = {30: 7, 60: 12, 90: 18, 120: 22, 300: 55}  # a new shot every ~4-5 seconds keeps short videos moving
LANG_NAMES = {"en-us": "American English", "en-gb": "British English", "es": "Spanish", "fr": "French",
              "hi": "Hindi", "it": "Italian", "ja": "Japanese", "pt-br": "Brazilian Portuguese", "zh": "Mandarin Chinese"}
STYLE_HINTS = {
    "cinematic": "dramatic, atmospheric shots with strong lighting",
    "whiteboard": "simple hand-drawn diagrams on a whiteboard",
    "minimal": "clean flat shapes with lots of empty space",
    "illustrated": "friendly colorful storybook illustrations",
}
WORDS_PER_SECOND = 2.0  # relaxed, easy-to-follow pace; faster scripts forced the voice to be sped up
TONE_GUIDE = {
    "friendly": "warm and encouraging, like a kind teacher; simple everyday words",
    "professional": "clear, precise and polished; neutral wording, no slang",
    "energetic": "upbeat and fast-paced; short punchy sentences and enthusiasm",
    "calm": "slow, gentle and soothing; relaxed, reassuring sentences",
    "humorous": "light and playful with a few gentle jokes; never mocking",
    "dramatic": "cinematic trailer voice; vivid, tense and awe-inspiring",
}


def word_target(p: dict) -> int:
    """Total narration words that fit the chosen duration."""
    return round(p["duration"] * WORDS_PER_SECOND)


def _messages(p: dict, feedback: str | None) -> list[dict]:
    n = SCENE_COUNT[p["duration"]]
    words = round(word_target(p) / n)
    system = (
        "You write scripts for short educational explainer videos. Reply with ONE JSON object and nothing else. "
        "Keys: title, description, hook, scenes, closing_summary. Each item of scenes has: narration, "
        "visual_description, on_screen_text, subtitle_text, duration_seconds, search_keywords. "
        "search_keywords is 1 to 3 simple, common ENGLISH nouns a stock-video site would have, e.g. 'power plant', 'laboratory scientist', 'city night'. Never abstract words. "
        "visual_description describes only what to draw (no text in the picture). "
        "on_screen_text is a BIG caption of at most 4 words: put the key number, name or striking phrase of that scene in it "
        "(for example '0.01°C', '3 hearts', '1,500 km/h'); no full sentences. subtitle_text equals the narration. "
        "The hook is ONE spoken sentence of at most 12 words that makes people stop scrolling: open with a concrete, "
        "surprising detail or a specific number, not a generic question. NEVER open with 'Welcome', 'Today', 'In this video', "
        "'Did you know', 'Have you ever wondered', 'Imagine', 'What if' or 'Fun fact'. "
        "Write like you are talking to a curious 16 year old: plain words, short vivid sentences, one idea per scene, "
        "no emojis, no filler. Include at least one specific number or named detail. "
        "ACCURACY IS CRITICAL: use only well-established, widely documented facts. Do not invent statistics, names or dates. "
        "Round numbers honestly, avoid absolute claims like 'the only', 'the first' or 'the hottest' unless certain, "
        "and leave out any claim you are not sure about. "
        "End the last scene with one short question that invites a comment."
    )
    if p.get("mode") == "prompt":
        brief = f"Video brief from the user: {p['prompt']}\n" + (f"Audience: {p['audience']}\n" if p.get("audience") else "")
    else:
        brief = f"Topic: {p['topic']}\nAudience: {p['audience']}\n"
        if p.get("subject"):
            brief += (f"Main subject: {p['subject']}. Every scene must be about and show the {p['subject']} "
                      f"(never a different animal, object or place), and every search_keywords value must begin with "
                      f"\"{p['subject']}\".\n")
    user = (
        f"{brief}Narration tone: {p.get('tone', 'friendly')} ({TONE_GUIDE[p.get('tone', 'friendly')]}).\n"
        f"Write the script (narration, titles and captions) in {LANG_NAMES[p['language']]}.\n"
        f"Visual style: {p['style']} ({STYLE_HINTS[p['style']]}).\n"
        f"Total length: about {p['duration']} seconds. Use exactly {n} scenes of about {words} narration words each "
        f"(about {round(p['duration'] / n)} seconds). The first scene's narration must start with the hook. "
        "The last scene must end with the closing summary. Explain in simple steps."
    )
    if feedback:
        user += "\n\n" + feedback
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _hf_error(r: httpx.Response, what: str) -> PipelineError:
    if r.status_code == 402:
        return PipelineError("Hugging Face inference credits are used up. Since 2026 free accounts get none included: "
                             "buy credits, upgrade to PRO, or switch this backend to a free provider (see DEPLOY.md).")
    if r.status_code == 404:
        return PipelineError(f"Hugging Face doesn't serve this {what} model through its API. Pick another model.")
    return _api_error("Hugging Face", r)


def _api_error(provider: str, r: httpx.Response) -> PipelineError:
    hints = {401: f"The {provider} API key was rejected. Check the key in your environment variables.",
             403: f"The {provider} key isn't allowed to use this model, or the provider blocked the request.",
             402: f"{provider} says the free credits are used up.",
             404: f"{provider} doesn't serve this model. Pick another model in your environment variables.",
             429: f"{provider} rate limit or free daily quota reached. Wait for the reset and try again."}
    return PipelineError(hints.get(r.status_code, f"{provider} returned {r.status_code}: {r.text[:200]}"))


def _chat_openai(messages: list[dict], base_url: str, api_key: str | None, models: list[str], provider: str,
                 timeout: int, max_tokens: int) -> str:
    """OpenAI-compatible chat completions (Groq, OpenRouter, Cerebras, Hugging Face router, ...).
    Walks the model list when one is rate-limited or missing, so a busy free model doesn't fail the whole job."""
    if not api_key:
        raise PipelineError(f"The language model needs an API key for {provider}. Set LLM_API_KEY (see DEPLOY.md).")
    last: PipelineError | None = None
    for model in models:
        for json_mode in (True, False):  # some models/providers reject response_format; retry without it
            body = {"model": model, "messages": messages, "temperature": 0.6, "max_tokens": max_tokens}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                r = httpx.post(f"{base_url.rstrip('/')}/chat/completions", timeout=timeout,
                               headers={"Authorization": f"Bearer {api_key}"}, json=body)
            except httpx.TimeoutException:
                last = PipelineError(f"{provider} took too long to answer ({model}).")
                break
            except httpx.HTTPError as e:
                raise PipelineError(f"Couldn't reach {provider}: {e}")
            if r.status_code == 400 and json_mode:
                continue
            if r.status_code in (401, 403):
                raise _api_error(provider, r)
            if r.status_code >= 400:
                last = _api_error(provider, r)
                break  # rate limit, unknown model, overloaded: try the next model
            try:
                return r.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError):
                last = PipelineError(f"{provider} sent a reply this app couldn't read ({model}).")
                break
    raise last or PipelineError(f"{provider} did not return a script.")


def _model_list(primary: str, fallbacks: str) -> list[str]:
    return [m for m in dict.fromkeys([primary, *[x.strip() for x in fallbacks.split(",")]]) if m]


def _chat_hf(messages: list[dict]) -> str:
    return _chat_openai(messages, f"{settings.hf_base_url.rstrip('/')}/v1", settings.hf_token,
                        [settings.hf_llm_model], "Hugging Face", settings.hf_timeout, settings.llm_max_tokens)


def _token_budget(p: dict) -> int:
    """Longer videos need a longer JSON reply; reasoning models also spend part of this budget thinking, so a small
    fixed limit cuts a 90 second script off mid-JSON. Capped to stay inside free-tier per-minute limits."""
    return max(settings.llm_max_tokens, min(6000, 2000 + int(p.get("duration", 30)) * 45))


def _chat_one(backend: str, messages: list[dict], max_tokens: int | None = None) -> str:
    if backend == "api":
        return _chat_openai(messages, settings.llm_base_url, settings.llm_api_key,
                            _model_list(settings.llm_model, settings.llm_fallback_models), "the language model API",
                            settings.llm_timeout, max_tokens or settings.llm_max_tokens)
    if backend == "hf_api":
        if not settings.hf_token:
            raise PipelineError("Hugging Face fallback skipped: HF_TOKEN is not set.")
        return _chat_openai(messages, f"{settings.hf_base_url.rstrip('/')}/v1", settings.hf_token,
                            [settings.hf_llm_model], "Hugging Face", settings.hf_timeout,
                            max_tokens or settings.llm_max_tokens)
    if backend != "ollama":
        raise PipelineError(f"Unknown LLM_BACKEND '{backend}'. Use 'api', 'ollama' or 'hf_api'.")
    url, model = settings.ollama_url.rstrip("/"), settings.ollama_model
    for fmt in (Script.model_json_schema(), "json"):
        try:
            r = httpx.post(f"{url}/api/chat", timeout=settings.ollama_timeout, json={
                "model": model, "messages": messages, "stream": False, "format": fmt,
                "options": {"temperature": 0.6, "num_ctx": 8192}})
        except httpx.ConnectError:
            raise PipelineError(f"Can't reach Ollama at {url}. Install it from https://ollama.com and start it "
                                "with `ollama serve`, or set OLLAMA_URL in backend/.env.")
        except httpx.TimeoutException:
            raise PipelineError("Ollama took too long to answer. Use a smaller model or raise OLLAMA_TIMEOUT.")
        if r.status_code == 404:
            raise PipelineError(f"Model '{model}' isn't installed in Ollama. Run: ollama pull {model}")
        if r.status_code < 400:
            return r.json()["message"]["content"]
        # Older Ollama versions reject schema formats; retry with plain JSON mode.
    raise PipelineError(f"Ollama returned an error ({r.status_code}): {r.text[:200]}")


def _chat(messages: list[dict], max_tokens: int | None = None) -> str:
    """Main backend first. If it fails (rate limit, quota, outage), try each backend in LLM_FALLBACK_BACKENDS
    (e.g. "hf_api,ollama") that is actually available, instead of failing the whole video."""
    order = [settings.llm_backend, *[b.strip() for b in settings.llm_fallback_backends.split(",")]]
    errors: list[str] = []
    for backend in dict.fromkeys(b for b in order if b):
        try:
            return _chat_one(backend, messages, max_tokens)
        except PipelineError as e:
            errors.append(f"{backend}: {e}")
            print(f"language model backend '{backend}' failed, trying next: {e}", flush=True)
    raise PipelineError(" | ".join(errors))


def _extract_json(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # reasoning models (e.g. Qwen3) emit this first
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        raise ValueError("no JSON object found")
    return text[a:b + 1]


def _check_fit(script: Script, p: dict) -> None:
    n = SCENE_COUNT[p["duration"]]
    if not (max(2, n // 2) <= len(script.scenes) <= n * 2):
        raise ValueError(f"expected about {n} scenes, got {len(script.scenes)}")
    words = sum(len(s.narration.split()) for s in script.scenes)
    if words > p["duration"] * WORDS_PER_SECOND * 1.35:
        raise ValueError(f"narration has {words} words; it must be shorter to fit {p['duration']} seconds")


def generate_script(p: dict, check) -> Script:
    feedback, last = None, ""
    budget = _token_budget(p)
    for attempt in range(1, 5):  # 4 tries: a long script sometimes needs one more
        check()
        raw = _chat(_messages(p, feedback), budget)
        try:
            script = Script.model_validate_json(_extract_json(raw))
            _check_fit(script, p)
            return script
        except ValueError as e:  # includes pydantic.ValidationError and json errors
            last = str(e)[:300].replace("\n", " ")
            print(f"script attempt {attempt} rejected: {last} (reply was {len(raw)} chars)", flush=True)
            feedback = f"Your previous reply was invalid ({str(e)[:500]}). Return corrected JSON only."
    raise PipelineError("The language model didn't produce a valid script after 4 attempts. "
                        f"Last problem: {last}. Try a larger model (LLM_MODEL / OLLAMA_MODEL) or a simpler topic.")
