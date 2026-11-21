"""ایلیا: {name} در متنِ بازی نباید نام را دو بار بیاورد (پیشوندِ خودکارِ «👤 نام» وقتی متن خودش {name} دارد حذف می‌شود)."""
import re
from pathlib import Path

import pytest

import tenant
from tenant.shared import dice_texts

pytestmark = pytest.mark.asyncio


async def test_template_has_name(monkeypatch):
    st = {"dice_txt_win_cash": "سلام {name} بردی", "dice_txt_lose": "باختی", "dice_txt_quota": ""}

    async def gs(bid, key, default=""):
        return st.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    assert await dice_texts.template_has_name(1, "win_cash") is True
    assert await dice_texts.template_has_name(1, "lose") is False
    assert await dice_texts.template_has_name(1, "quota") is False      # پیش‌فرض {name} ندارد


async def test_custom_text_renders_name_once(monkeypatch):
    st = {"dice_txt_win_cash": "🎉 {name} عزیز، {prize}"}

    async def gs(bid, key, default=""):
        return st.get(key, default)
    monkeypatch.setattr(tenant, "get_setting", gs)
    text = await dice_texts.get_text(1, "win_cash", name="علی", prize="۱۰۰")
    if not await dice_texts.template_has_name(1, "win_cash"):
        text = "👤 علی\n" + text
    assert text.count("علی") == 1


def test_game_code_skips_auto_prefix_when_template_has_name():
    src = Path("tenant/shared/dice_game.py").read_text(encoding="utf-8")
    assert re.search(r"if player_name and not await _has_name\(bot_id, _key\):", src)
