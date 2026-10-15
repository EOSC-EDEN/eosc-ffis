"""
FFIS API routes.

Implements FFIS-REQ-4-01 — two input modes:
  POST /identify          — by value (file upload / byte stream)
  POST /identify/path     — by reference (filesystem path)

Additional endpoints:
  GET  /health            — liveness check
  GET  /tools             — list engines and availability
"""

from typing import Annotated, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..models.identification import IdentificationResult, IdentifyByPathRequest

router = APIRouter()


@router.post(
    "/identify",
    response_model=IdentificationResult,
    summary="Identify by value (file upload)",
    description=(
        "Upload a file as a byte stream. The service identifies its format "
        "without relying on the filename or any user-supplied MIME type. "
        "Implements FFIS-REQ-4-01 (by-value input mode)."
    ),
)
async def identify_by_value(
    request: Request,
    file: Annotated[UploadFile, File(description="File to identify")],
    claimed_mimetype: Annotated[
        Optional[str], Form(description="Optional depositor-supplied MIME type for verification")
    ] = None,
    claimed_puid: Annotated[
        Optional[str], Form(description="Optional depositor-supplied PRONOM PUID for verification")
    ] = None,
) -> IdentificationResult:
    cache = request.app.state.cache
    orchestrator = request.app.state.orchestrator

    max_bytes = request.app.state.settings.max_upload_bytes
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {max_bytes // (1024*1024)} MiB.")
    sha256 = _sha256(content)

    if cache:
        hit = await cache.get(sha256)
        if hit:
            return hit

    result = await orchestrator.identify_bytes(
        content,
        filename=file.filename,
        claimed_mimetype=claimed_mimetype,
        claimed_puid=claimed_puid,
    )

    if cache:
        await cache.set(sha256, result)

    return result


@router.post(
    "/identify/path",
    response_model=IdentificationResult,
    summary="Identify by reference (filesystem path)",
    description=(
        "Accept a filesystem path pointer. The service reads the file directly "
        "from the shared volume — no data transfer needed. Intended for "
        "containerised deployments where the service and storage share a volume. "
        "Implements FFIS-REQ-4-01 (by-reference input mode)."
    ),
)
async def identify_by_path(
    request: Request,
    body: IdentifyByPathRequest,
) -> IdentificationResult:
    import hashlib
    from pathlib import Path

    path = body.path
    allowed_prefixes = request.app.state.settings.allowed_path_prefixes

    # Path traversal protection — resolve to absolute path then check whitelist
    try:
        resolved = Path(path).resolve()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid path.") from exc

    if not allowed_prefixes:
        raise HTTPException(status_code=403, detail="By-reference mode is not enabled on this server.")

    if not any(resolved.is_relative_to(Path(p)) for p in allowed_prefixes):
        raise HTTPException(status_code=403, detail="Path is outside the allowed directories.")

    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="Path does not point to a regular file.")

    cache = request.app.state.cache
    orchestrator = request.app.state.orchestrator

    resolved_str = str(resolved)

    # Compute checksum for cache lookup without reading twice
    if cache:
        try:
            with open(resolved_str, "rb") as fh:
                sha256 = hashlib.sha256(fh.read()).hexdigest()
            hit = await cache.get(sha256)
            if hit:
                return hit
        except OSError:
            pass  # let the orchestrator surface the error properly

    result = await orchestrator.identify_path(
        resolved_str,
        claimed_mimetype=body.claimed_mimetype,
        claimed_puid=body.claimed_puid,
    )

    if cache and result.checksum_sha256:
        await cache.set(result.checksum_sha256, result)

    return result


@router.get(
    "/health",
    summary="Liveness check",
)
async def health() -> dict:
    return {"status": "ok"}


@router.get(
    "/tools",
    summary="List configured engines and their availability",
)
async def tools(request: Request) -> dict:
    engines = request.app.state.engines
    statuses = []
    for engine in engines:
        available = await engine.is_available()
        version = await engine.get_version() if available else None
        statuses.append({"name": engine.name, "available": available, "version": version})
    return {"engines": statuses}


def _sha256(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()
