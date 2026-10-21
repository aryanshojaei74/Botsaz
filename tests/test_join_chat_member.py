"""PATCH_JOIN_CHAT_MEMBER_V1 — خوش‌آمد برای اعضایی که از لینک دعوت می‌آیند (آپدیت chat_member) + عدم تکرار."""
from types import SimpleNamespace

import pytest

from tenant.shared import join_dedupe as JD
import webhook_utils


def test_is_join_matrix():
    assert JD.is_join("left", "member") and JD.is_join("kicked", "member") and JD.is_join("left", "administrator")
    assert JD.is_join("left", "restricted", True) and not JD.is_join("left", "restricted", False)
    assert not JD.is_join("member", "administrator") and not JD.is_join("member", "left") and not JD.is_join("restricted", "member")


def test_claim_dedupes_per_user_within_ttl():
    JD._SEEN.clear()
    assert JD.claim(1, -100, 5) is True and JD.claim(1, -100, 5) is False
    assert JD.claim(1, -100, 6) is True and JD.claim(2, -100, 5) is True and JD.claim(1, -101, 5) is True
    assert JD.claim(1, -100, 5, ttl=0) is True                      # ttl صفر ⇒ همیشه تازه


def test_tenant_webhooks_request_chat_member():
    assert "chat_member" in webhook_utils.TENANT_ALLOWED_UPDATES
    for must in ("message", "callback_query", "pre_checkout_query", "my_chat_member", "chat_join_request"):
        assert must in webhook_utils.TENANT_ALLOWED_UPDATES


@pytest.mark.asyncio
async def test_guard_chat_member_path_welcomes_once_and_message_path_skips(monkeypatch):
    import tenant_group_guard as G
    from aiogram.dispatcher.event.bases import SkipHandler
    JD._SEEN.clear()
    sent = []

    class Bot_:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))
    row = SimpleNamespace(id=3)
    settings = {"group_guard_welcome_enabled": "1", "group_guard_welcome_text": "سلام {name}"}

    async def fake_get(bot_id, key, default=""):
        return settings.get(key, default)

    async def fake_row(bot):
        return row

    async def fake_allowed(r, chat_id):
        return True
    monkeypatch.setattr(G, "_get", fake_get)
    monkeypatch.setattr(G, "_get_row", fake_row)
    monkeypatch.setattr(G, "_allowed", fake_allowed)
    user = SimpleNamespace(id=77, is_bot=False, full_name="Ali")
    ev = SimpleNamespace(chat=SimpleNamespace(id=-100),
                         old_chat_member=SimpleNamespace(status="left"),
                         new_chat_member=SimpleNamespace(status="member", user=user, is_member=True))
    with pytest.raises(SkipHandler):
        await G.tenant_guard_chat_member(ev, Bot_())
    assert sent == [(-100, "سلام Ali")]
    # همان ورود از مسیرِ پیامِ سرویس: دوباره خوش‌آمد نمی‌گوید
    msg = SimpleNamespace(chat=SimpleNamespace(id=-100), new_chat_members=[user])
    with pytest.raises(SkipHandler):
        await G.tenant_guard_new_members(msg, Bot_())
    assert len(sent) == 1
    # آپدیتِ غیرِ ورود (ارتقا) نادیده گرفته می‌شود
    ev2 = SimpleNamespace(chat=SimpleNamespace(id=-100), old_chat_member=SimpleNamespace(status="member"),
                          new_chat_member=SimpleNamespace(status="administrator", user=user, is_member=True))
    with pytest.raises(SkipHandler):
        await G.tenant_guard_chat_member(ev2, Bot_())
    assert len(sent) == 1
