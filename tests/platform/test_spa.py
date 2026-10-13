"""The dashboard is served from SPA_DIR with a Content-Security-Policy and friends; API paths never get the HTML."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.config import settings


@pytest.fixture
def spa_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    monkeypatch.setattr(settings, "SPA_DIR", str(tmp_path))
    return AsyncClient(transport=ASGITransport(app=main.create_app()), base_url="http://t")


@pytest.mark.parametrize("path", ["/", "/chat/123", "/favicon.svg", "/assets/app.js"])
async def test_spa_responses_carry_security_headers(spa_client: AsyncClient, path: str) -> None:
    async with spa_client as c:
        r = await c.get(path)
    assert r.status_code == 200
    for name, value in main.SPA_SECURITY_HEADERS.items():
        assert r.headers[name] == value
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_api_paths_never_get_the_dashboard(spa_client: AsyncClient) -> None:
    async with spa_client as c:
        r = await c.get("/api/v1/nope")
        docs = await c.get("/docs")                       # Swagger UI loads from a CDN: no SPA CSP there
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert "content-security-policy" not in docs.headers


def test_spa_dir_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "SPA_DIR", None)
    monkeypatch.setattr(main, "IMAGE_SPA_DIR", tmp_path / "missing")
    assert main.spa_dir() == main.REPO_SPA_DIR                # local checkout without the image's /srv/spa
    monkeypatch.setattr(main, "IMAGE_SPA_DIR", tmp_path)
    assert main.spa_dir() == tmp_path                         # the image: outside the dev bind mount of ./app
    monkeypatch.setattr(settings, "SPA_DIR", "/elsewhere")
    assert main.spa_dir() == Path("/elsewhere")
