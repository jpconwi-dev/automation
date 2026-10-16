"""Cloudflare Workers AI over plain REST (free allowance: 10,000 Neurons per day, no card).

Used for images (FLUX.1 schnell) and narration (MeloTTS). One account ID + one API token covers both.
Create the token at dash.cloudflare.com > Manage Account > API Tokens > "Workers AI" template.
"""
import httpx

from ..core.config import settings
from ..core.errors import PipelineError


def configured() -> bool:
    return bool(settings.cloudflare_account_id and settings.cloudflare_api_token)


def _explain(r: httpx.Response) -> PipelineError:
    try:
        errs = r.json().get("errors") or []
        detail = "; ".join(str(e.get("message", e)) for e in errs)[:240]
    except ValueError:
        detail = r.text[:240]
    if r.status_code in (401, 403):
        return PipelineError("Cloudflare rejected the API token. Check CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN "
                             f"(the token needs Workers AI permission). {detail}")
    if r.status_code == 429 or "neuron" in detail.lower() or "allocation" in detail.lower():
        return PipelineError("The free Cloudflare Workers AI allowance for today is used up. It resets at 00:00 UTC. "
                             f"{detail}")
    return PipelineError(f"Cloudflare Workers AI returned {r.status_code}: {detail}")


def run(model: str, payload: dict) -> dict:
    """Run a model and return its `result` object. Raises PipelineError with a fix-it message on failure."""
    if not configured():
        raise PipelineError("This needs CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN (free; see DEPLOY.md).")
    url = f"{settings.cloudflare_base_url.rstrip('/')}/accounts/{settings.cloudflare_account_id}/ai/run/{model}"
    try:
        r = httpx.post(url, json=payload, timeout=settings.cloudflare_timeout,
                       headers={"Authorization": f"Bearer {settings.cloudflare_api_token}"})
    except httpx.TimeoutException:
        raise PipelineError("Cloudflare Workers AI took too long to answer. Try again.")
    except httpx.HTTPError as e:
        raise PipelineError(f"Couldn't reach Cloudflare Workers AI: {e}")
    if r.status_code >= 400:
        raise _explain(r)
    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("application/json"):  # some models answer with the raw file
        return {"_bytes": r.content}
    try:
        body = r.json()
    except ValueError:
        raise PipelineError("Cloudflare sent a reply this app couldn't read.")
    if body.get("success") is False:
        raise _explain(r)
    return body.get("result") or {}
