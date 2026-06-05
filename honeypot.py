import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

# ============================================================================
# Config
# ============================================================================

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
LOG_DIR = Path(__file__).resolve().parent / "prompt_get"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_providers(raw: dict) -> dict:
    """Normalize config into ``{"openai": {...}, "anthropic": {...}}``.

    Supports the legacy flat format (openai-only) for backward compatibility.
    """
    if "openai" in raw or "anthropic" in raw:
        return {
            "openai": raw.get("openai") or {},
            "anthropic": raw.get("anthropic") or {},
        }

    # Legacy flat format — treat everything as the OpenAI provider
    return {
        "openai": {
            "real_api_url": raw.get("real_api_url", ""),
            "real_api_key": raw.get("real_api_key", ""),
            "target_model": raw.get("target_model", ""),
        },
        "anthropic": {},
    }


cfg_raw = load_config()
cfg = resolve_providers(cfg_raw)
FAKE_API_KEY = cfg_raw["fake_api_key"]

# ============================================================================
# FastAPI app
# ============================================================================

app = FastAPI(title="Multi-Protocol API Honeypot")

# ============================================================================
# Auth
# ============================================================================


async def verify_openai_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.split("Bearer ")[1] != FAKE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


async def verify_anthropic_auth(request: Request):
    # 1) x-api-key header
    key = request.headers.get("x-api-key", "")
    if key == FAKE_API_KEY:
        return

    # 2) Authorization: Bearer <token> (used by Claude Code CLI and similar tools)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth.split("Bearer ")[1] == FAKE_API_KEY:
        return

    raise HTTPException(status_code=401, detail="Invalid API key")

# ============================================================================
# Logging
# ============================================================================


def log_request(protocol: str, payload: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:8]
    filename = f"{protocol}_prompt_{ts}_{uid}.json"
    filepath = LOG_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return filepath

# ============================================================================
# Error helpers
# ============================================================================


def openai_error(message: str, code: str = "internal_error", status: int = 500):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code, "code": code}},
    )


def anthropic_error(message: str, code: str = "internal_error", status: int = 500):
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": code, "message": message}},
    )

# ============================================================================
# URL resolution
# ============================================================================


def resolve_openai_url(base_url: str) -> str:
    """Build the /chat/completions endpoint for an OpenAI-compatible provider."""
    base = base_url.rstrip("/")

    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    if "api.deepseek.com" in base:
        return f"{base}/chat/completions"

    return f"{base}/v1/chat/completions"


def resolve_anthropic_url(base_url: str) -> str:
    """Build the /messages endpoint for an Anthropic-compatible provider."""
    base = base_url.rstrip("/")

    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    if "api.anthropic.com" in base:
        return f"{base}/v1/messages"

    return f"{base}/v1/messages"

# ============================================================================
# Streaming relay (shared)
# ============================================================================


async def _relay_stream_and_close(client: httpx.AsyncClient, upstream: httpx.Response):
    """Yield SSE chunks to the client, then close upstream + httpx client."""
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        await upstream.aclose()
        await client.aclose()

# ============================================================================
# Generic forwarding (protocol-agnostic plumbing)
# ============================================================================


async def _forward_stream(
    upstream_url: str,
    payload: dict,
    fwd_headers: dict,
    error_fn,
):
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    try:
        req = client.build_request("POST", upstream_url, json=payload, headers=fwd_headers)
        upstream = await client.send(req, stream=True)
        upstream.raise_for_status()
    except httpx.HTTPStatusError as e:
        await client.aclose()
        try:
            body = e.response.json()
        except Exception:
            body = _fallback_error_body(e, error_fn)
        return JSONResponse(status_code=e.response.status_code, content=body)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as e:
        await client.aclose()
        return error_fn(f"Honeypot could not reach upstream API: {e}", code="upstream_unreachable")

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() != "content-length"}

    return StreamingResponse(
        _relay_stream_and_close(client, upstream),
        media_type="text/event-stream",
        headers=resp_headers,
    )


async def _forward_normal(
    upstream_url: str,
    payload: dict,
    fwd_headers: dict,
    error_fn,
):
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    try:
        upstream = await client.post(upstream_url, json=payload, headers=fwd_headers)
        upstream.raise_for_status()
        return JSONResponse(status_code=upstream.status_code, content=upstream.json())
    except httpx.HTTPStatusError as e:
        try:
            body = e.response.json()
        except Exception:
            body = _fallback_error_body(e, error_fn)
        return JSONResponse(status_code=e.response.status_code, content=body)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as e:
        return error_fn(f"Honeypot could not reach upstream API: {e}", code="upstream_unreachable")
    finally:
        await client.aclose()


def _fallback_error_body(exc: httpx.HTTPStatusError, error_fn) -> dict:
    """Return a minimal error body matching the provider's shape when upstream JSON is unparseable."""
    if error_fn is anthropic_error:
        return {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}}
    return {"error": {"message": str(exc), "type": "upstream_error", "code": "upstream_error"}}

# ============================================================================
# Routes — OpenAI
# ============================================================================

OPENAI_CONF = cfg["openai"]


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    await verify_openai_auth(request)

    payload = await request.json()

    target = (OPENAI_CONF.get("target_model") or "").strip()
    if target:
        payload["model"] = target.lower()

    log_request("openai", payload)

    api_key = OPENAI_CONF.get("real_api_key", "")
    upstream_url = resolve_openai_url(OPENAI_CONF.get("real_api_url", ""))

    fwd_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if payload.get("stream"):
        return await _forward_stream(upstream_url, payload, fwd_headers, openai_error)
    else:
        return await _forward_normal(upstream_url, payload, fwd_headers, openai_error)

# ============================================================================
# Routes — Anthropic
# ============================================================================

ANTHROPIC_CONF = cfg["anthropic"]
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    await verify_anthropic_auth(request)

    payload = await request.json()

    target = (ANTHROPIC_CONF.get("target_model") or "").strip()
    if target:
        payload["model"] = target.lower()

    log_request("anthropic", payload)

    api_key = ANTHROPIC_CONF.get("real_api_key", "")
    upstream_url = resolve_anthropic_url(ANTHROPIC_CONF.get("real_api_url", ""))

    # Preserve query parameters from the incoming request (e.g. ?beta=true)
    qs = request.url.query
    if qs:
        upstream_url = f"{upstream_url}&{qs}" if "?" in upstream_url else f"{upstream_url}?{qs}"

    version = request.headers.get("anthropic-version", DEFAULT_ANTHROPIC_VERSION)

    fwd_headers = {
        "x-api-key": api_key,
        "anthropic-version": version,
        "Content-Type": "application/json",
    }

    if payload.get("stream"):
        return await _forward_stream(upstream_url, payload, fwd_headers, anthropic_error)
    else:
        return await _forward_normal(upstream_url, payload, fwd_headers, anthropic_error)

# ============================================================================
# Entrypoint
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("honeypot:app", host="127.0.0.1", port=int(cfg_raw["port"]), reload=True)
