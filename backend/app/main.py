import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api.routes import router
from .core import db, storage
from .core.config import settings
from .core.errors import ApiError

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    db.fail_stale_jobs()
    storage.enforce()  # start inside the disk budget
    yield


app = FastAPI(title="AI Explainer Video Generator", version="1.0.0", lifespan=lifespan)


def err(status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


@app.middleware("http")
async def limit_body(request: Request, call_next):
    if int(request.headers.get("content-length") or 0) > settings.max_body_bytes:
        return err(413, "request_too_large", "Request body is too large.")
    return await call_next(request)


@app.middleware("http")
async def require_access_key(request: Request, call_next):
    """Optional shared password (ACCESS_KEY). /api/health stays open so the page can tell it needs a key.
    <video> and <audio> tags can't send headers, so the key is also accepted as ?key=..."""
    path = request.url.path
    if settings.access_key and path.startswith("/api/") and path != "/api/health" and request.method != "OPTIONS":
        supplied = request.headers.get("x-access-key") or request.query_params.get("key") or ""
        if not hmac.compare_digest(supplied.encode(), settings.access_key.encode()):
            return err(401, "unauthorized", "This server needs an access key.")
    return await call_next(request)


@app.exception_handler(ApiError)
async def _api(_, e: ApiError):
    return err(e.status, e.code, e.message)


@app.exception_handler(StarletteHTTPException)
async def _http(_, e: StarletteHTTPException):
    return err(e.status_code, "http_error", str(e.detail))


@app.exception_handler(RequestValidationError)
async def _val(_, e: RequestValidationError):
    details = [{"field": ".".join(str(x) for x in d["loc"][1:]), "message": d["msg"]} for d in e.errors()]
    return err(422, "validation_error", "Some fields are invalid.", details)


@app.exception_handler(Exception)
async def _any(_, e: Exception):
    logging.getLogger("app").exception("unhandled")
    return err(500, "internal_error", "Something went wrong on the server.")


app.include_router(router)

# Added last so it is the outermost layer: CORS headers then appear on every response, including 401 and 413.
app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "X-Access-Key"])
