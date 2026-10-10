from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main


def client() -> TestClient:
    return TestClient(main.app)


def test_home_page_links_both_bots_and_the_web_calendar(monkeypatch):
    monkeypatch.setenv("MAX_BOT_USERNAME", "@id_planwithai_bot")
    with patch("dashboard_router.get_bot_username", new_callable=AsyncMock, return_value="planwithai_bot"):
        response = client().get("/")
    assert response.status_code == 200
    page = response.text
    assert "https://t.me/planwithai_bot" in page
    assert "https://max.ru/id_planwithai_bot" in page
    assert 'href="/login"' in page
    assert page.count("<h1") == 1
    assert "/static/brand/hero.webp" in page


def test_home_page_without_max_username_hides_the_max_button(monkeypatch):
    monkeypatch.delenv("MAX_BOT_USERNAME", raising=False)
    with patch("dashboard_router.get_bot_username", new_callable=AsyncMock, return_value="planwithai_bot"):
        page = client().get("/").text
    assert "max.ru/" not in page.replace("max.ru/channel_planwithai", "")


def test_brand_images_are_served():
    for path in ("/static/brand/hero.webp", "/static/brand/emoji/check.webp", "/static/brand/emoji/wave.webp"):
        response = client().get(path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/webp"


def test_login_page_uses_the_new_design():
    with patch("dashboard_router.get_bot_username", new_callable=AsyncMock, return_value="planwithai_bot"):
        page = client().get("/login").text
    assert "/static/brand/emoji/wave.webp" in page and 'href="/"' in page
    assert "bootstrap" not in page.lower()


def test_home_page_shows_the_whole_emoji_set():
    with patch("dashboard_router.get_bot_username", new_callable=AsyncMock, return_value="planwithai_bot"):
        page = client().get("/").text
    assert "https://t.me/addemoji/planwithai" in page
    assert page.count("/static/brand/pack/") == len(main.BRAND_PACK) == 30
