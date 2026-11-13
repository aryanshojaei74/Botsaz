import pytest
from types import SimpleNamespace as NS
import premium_emoji_dynamic as P


@pytest.mark.asyncio
async def test_marker_skips_premium_and_is_stripped(monkeypatch):
    called = []

    async def _m(token):
        called.append(token)
        return {"🐟": ("1", "🐟")}
    monkeypatch.setattr(P, "_map_for_token", _m)
    from aiogram.methods import SendMessage
    m = SendMessage(chat_id=1, text="🐟🍚" + P.NO_PREMIUM, parse_mode="HTML")
    await P._PremiumEmojiMiddleware()._rewrite_text(NS(token="t", default=NS(parse_mode="HTML")), m)
    assert m.text == "🐟🍚" and not called and not m.entities
