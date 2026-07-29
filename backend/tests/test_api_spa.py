from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.main import SPAStaticFiles


def test_spa_static_files_returns_index_only_for_client_routes(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (tmp_path / "index.html").write_text("<main>导演台</main>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("window.app = true", encoding="utf-8")
    (media_dir / "frame.txt").write_text("media", encoding="utf-8")
    test_app = FastAPI()

    @test_app.get("/api/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    test_app.mount("/media", StaticFiles(directory=media_dir))
    test_app.mount("/", SPAStaticFiles(directory=tmp_path, html=True))

    with TestClient(test_app) as client:
        api_response = client.get("/api/ping")
        unknown_api = client.get("/api/unknown")
        media_response = client.get("/media/frame.txt")
        client_route = client.get("/writing")
        assets_client_route = client.get("/assets")
        existing_asset = client.get("/assets/app.js")
        missing_asset = client.get("/assets/missing.js")

    assert api_response.json() == {"status": "ok"}
    assert unknown_api.status_code == 404
    assert unknown_api.text != "<main>导演台</main>"
    assert media_response.text == "media"
    assert client_route.status_code == 200
    assert client_route.text == "<main>导演台</main>"
    assert assets_client_route.status_code == 200
    assert assets_client_route.text == "<main>导演台</main>"
    assert existing_asset.status_code == 200
    assert existing_asset.text == "window.app = true"
    assert missing_asset.status_code == 404


def test_spa_static_files_does_not_fallback_media_paths(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<main>导演台</main>", encoding="utf-8")
    test_app = FastAPI()
    test_app.mount("/", SPAStaticFiles(directory=tmp_path, html=True))

    with TestClient(test_app) as client:
        response = client.get("/media/missing.mp4")

    assert response.status_code == 404
    assert response.text != "<main>导演台</main>"
