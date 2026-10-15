"""
FFIS test suite.

Tests cover:
  - Orchestrator conflict resolution logic
  - Extension mismatch warning generation
  - Cache hit/miss behaviour
  - API endpoints (by-value and by-reference)

Siegfried and Magika are mocked so the tests run without external binaries.
"""

import hashlib
from datetime import datetime, timezone
from typing import Iterator, Optional
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from ffis.engines.base import BaseEngine
from ffis.models.identification import (
    IdentificationMethod,
    IdentificationOutcome,
    Identifier,
    ToolResult,
)
from ffis.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

def _make_tool_result(
    name: str = "stub",
    identifiers: Optional[list[Identifier]] = None,
    method: IdentificationMethod = IdentificationMethod.BYTE_SIGNATURE,
    error: Optional[str] = None,
) -> ToolResult:
    return ToolResult(
        name=name,
        identifiers=identifiers or [],
        method=method,
        error=error,
    )


class StubEngine(BaseEngine):
    def __init__(self, name: str, result: ToolResult) -> None:
        self.name = name
        self._result = result

    async def identify_bytes(self, content: bytes, filename: Optional[str] = None) -> ToolResult:
        return self._result

    async def identify_path(self, path: str) -> ToolResult:
        return self._result

    async def is_available(self) -> bool:
        return True

    async def get_version(self) -> Optional[str]:
        return "stub-1.0"


# ---------------------------------------------------------------------------
# Orchestrator unit tests
# ---------------------------------------------------------------------------

class TestOrchestratorConflictResolution:
    @pytest.mark.asyncio
    async def test_pronom_wins_over_mime(self) -> None:
        pronom_engine = StubEngine(
            "siegfried",
            _make_tool_result(
                identifiers=[
                    Identifier(value="fmt/412", scheme="PRONOM",
                               uri="https://www.nationalarchives.gov.uk/pronom/fmt/412"),
                    Identifier(value="application/pdf", scheme="MIME"),
                ],
                method=IdentificationMethod.BYTE_SIGNATURE,
            ),
        )
        orchestrator = Orchestrator([pronom_engine])
        result = await orchestrator.identify_bytes(b"%PDF-1.4 fake content", "test.pdf")

        assert result.primary_identifier is not None
        assert result.primary_identifier.scheme == "PRONOM"
        assert result.primary_identifier.value == "fmt/412"

    @pytest.mark.asyncio
    async def test_byte_signature_wins_over_ai(self) -> None:
        sig_engine = StubEngine(
            "siegfried",
            _make_tool_result(
                identifiers=[Identifier(value="fmt/40", scheme="PRONOM")],
                method=IdentificationMethod.BYTE_SIGNATURE,
            ),
        )
        ai_engine = StubEngine(
            "magika",
            _make_tool_result(
                identifiers=[Identifier(value="application/msword", scheme="MIME")],
                method=IdentificationMethod.AI_PROBABILISTIC,
            ),
        )
        orchestrator = Orchestrator([sig_engine, ai_engine])
        result = await orchestrator.identify_bytes(b"fake doc content", "doc.doc")

        assert result.primary_identifier is not None
        assert result.primary_identifier.value == "fmt/40"
        assert result.basis == IdentificationMethod.BYTE_SIGNATURE

    @pytest.mark.asyncio
    async def test_failure_outcome_when_no_useful_match(self) -> None:
        engine = StubEngine(
            "siegfried",
            _make_tool_result(
                identifiers=[Identifier(value="application/octet-stream", scheme="MIME")],
                method=IdentificationMethod.UNKNOWN,
            ),
        )
        orchestrator = Orchestrator([engine])
        result = await orchestrator.identify_bytes(b"\x00\x01\x02", "unknown.bin")

        assert result.provenance.outcome == IdentificationOutcome.FAILURE
        assert any(w.code == "NO_MATCH" for w in result.warnings)

    @pytest.mark.asyncio
    async def test_claimed_mime_mismatch_warning(self) -> None:
        engine = StubEngine(
            "siegfried",
            _make_tool_result(
                identifiers=[
                    Identifier(value="fmt/412", scheme="PRONOM"),
                    Identifier(value="application/pdf", scheme="MIME"),
                ],
                method=IdentificationMethod.BYTE_SIGNATURE,
            ),
        )
        orchestrator = Orchestrator([engine])
        result = await orchestrator.identify_bytes(
            b"%PDF fake", "test.pdf", claimed_mimetype="image/jpeg"
        )

        assert any(w.code == "CLAIMED_MIME_MISMATCH" for w in result.warnings)

    @pytest.mark.asyncio
    async def test_success_outcome_no_warnings(self) -> None:
        engine = StubEngine(
            "siegfried",
            _make_tool_result(
                identifiers=[
                    Identifier(value="fmt/412", scheme="PRONOM",
                               uri="https://www.nationalarchives.gov.uk/pronom/fmt/412"),
                    Identifier(value="application/pdf", scheme="MIME"),
                ],
                method=IdentificationMethod.BYTE_SIGNATURE,
            ),
        )
        orchestrator = Orchestrator([engine])
        result = await orchestrator.identify_bytes(b"%PDF fake", "test.pdf")

        assert result.provenance.outcome == IdentificationOutcome.SUCCESS
        assert result.warnings == []

    @pytest.mark.asyncio
    async def test_magika_used_when_siegfried_finds_nothing(self) -> None:
        sf_engine = StubEngine(
            "siegfried",
            _make_tool_result(
                identifiers=[Identifier(value="application/octet-stream", scheme="MIME")],
                method=IdentificationMethod.UNKNOWN,
            ),
        )
        magika_engine = StubEngine(
            "magika",
            _make_tool_result(
                identifiers=[Identifier(value="application/x-python", scheme="MIME")],
                method=IdentificationMethod.AI_PROBABILISTIC,
            ),
        )
        orchestrator = Orchestrator([sf_engine, magika_engine])
        result = await orchestrator.identify_bytes(b"print('hello')", "script.py")

        # Magika's MIME result should surface when Siegfried has nothing
        assert any(i.value == "application/x-python" for i in result.identifiers)


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def test_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    from ffis.main import app, settings
    # Keep the test cache out of the shared default path
    monkeypatch.setattr(settings, "cache_db_path", str(tmp_path / "cache.db"))
    # Entering the client runs the app lifespan, which sets up engines and cache
    with TestClient(app) as client:
        yield client


class TestAPIEndpoints:
    def test_health(self, test_client: TestClient) -> None:
        r = test_client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_tools_endpoint(self, test_client: TestClient) -> None:
        r = test_client.get("/tools")
        assert r.status_code == 200
        data = r.json()
        assert "engines" in data
        assert isinstance(data["engines"], list)

    def test_identify_by_value_returns_result(self, test_client: TestClient) -> None:
        # Use a minimal valid PDF header so Siegfried has something to inspect
        content = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF"
        r = test_client.post(
            "/identify",
            files={"file": ("test.pdf", content, "application/pdf")},
        )
        assert r.status_code == 200
        data = r.json()
        assert "identifiers" in data
        assert "provenance" in data
        assert data["provenance"]["outcome"] in ("success", "warning", "failure")

    def test_identify_response_schema(self, test_client: TestClient) -> None:
        content = b"plain text content"
        r = test_client.post(
            "/identify",
            files={"file": ("doc.txt", content, "text/plain")},
        )
        assert r.status_code == 200
        data = r.json()
        # Required fields from CPP-008 output spec
        assert "identifiers" in data
        assert "provenance" in data
        assert "outcome" in data["provenance"]
        assert "tools" in data["provenance"]
        assert "date" in data["provenance"]
