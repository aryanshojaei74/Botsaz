import pytest
from types import SimpleNamespace
import tenant

pytestmark = pytest.mark.asyncio


def _patch(monkeypatch, settings):
    async def gs(bid, key, default=""):
        return settings.get(key, default)

    async def gbr(bot):
        return SimpleNamespace(id=1, owner_id=5, plan="pro")
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)


def _msg(text, chat="private"):
    return SimpleNamespace(text=text, chat=SimpleNamespace(type=chat))


async def test_enabled_only_in_inline_mode_and_on(monkeypatch):
    _patch(monkeypatch, {"menu_mode": "inline", "home_kb_btn": "1"})
    assert await tenant.home_kb_enabled(1) is True
    assert (await tenant.home_kb_markup(1)).keyboard[0][0].text == "🏠 منوی اصلی"
    _patch(monkeypatch, {"menu_mode": "reply", "home_kb_btn": "1"})
    assert await tenant.home_kb_enabled(1) is False and await tenant.home_kb_markup(1) is None
    _patch(monkeypatch, {"menu_mode": "inline"})
    assert await tenant.home_kb_enabled(1) is False


async def test_press_filter_custom_label_and_old_label(monkeypatch):
    _patch(monkeypatch, {"menu_mode": "inline", "home_kb_btn": "1", "home_kb_label": "خانه 🏠",
                         "home_kb_label_old": "🏠 منوی اصلی"})
    f = tenant._is_home_kb_press
    assert await f(_msg("خانه 🏠"), object()) is True
    assert await f(_msg("🏠 منوی اصلی"), object()) is True          # کیبوردِ قدیمیِ کاربران
    assert await f(_msg("چیز دیگر"), object()) is False
    assert await f(_msg("/start"), object()) is False
    assert await f(_msg("خانه 🏠", chat="group"), object()) is False


async def test_press_filter_off_by_default(monkeypatch):
    _patch(monkeypatch, {"menu_mode": "inline"})
    assert await tenant._is_home_kb_press(_msg("🏠 منوی اصلی"), object()) is False
