"""SINA 8 Nov: دستیارِ گروهِ مشتری با ریپلای، پاک‌شدنِ پیامِ درخواستِ ممو، دامنهٔ تاگلِ «تغییر کارت»."""
import inspect
from types import SimpleNamespace as NS

import pytest

from tenant.bots.virtual_service import smm_ai as AI
import tenant.shared.kyc_user as KU


def test_card_change_toggle_scope():
    assert "card_change_btn_enabled" in inspect.getsource(KU.user_content)          # صفحهٔ احرازِ هویتِ کیف‌پول
    assert "card_change_btn_enabled" not in inspect.getsource(KU.card_select_gate)  # انتخابِ کارتِ پرداخت: همیشه
    import tenant
    assert "_cc_btn" in inspect.getsource(tenant)                                   # میان‌بُرِ کیف‌پول


class FakeMsg:
    def __init__(self, text="", reply_text=None, reply_from=None, chat=-100, uid=7):
        self.text, self.chat, self.from_user = text, NS(id=chat), NS(id=uid)
        self.replies = []
        self.reply_to_message = (NS(text=reply_text, caption=None, message_id=5, from_user=NS(id=reply_from or 9))
                                 if reply_text is not None else None)
        self.bot = NS()

    async def reply(self, text, **k):
        self.replies.append(text)
        return NS(message_id=100 + len(self.replies))


@pytest.fixture
def ai(monkeypatch):
    seen = {}

    async def gs(bot_id, key, default=""):
        return {"smm_ai_api_key": "K"}.get(key, default)

    async def prov(bot_id): return "grok"

    async def ctx(*a, **k): return ""

    async def ask(provider, key, question, context, system, bot_id=None):
        seen["q"] = question
        return "جواب"
    monkeypatch.setattr(AI, "_get_setting", gs)
    monkeypatch.setattr(AI, "_get_provider", prov)
    for n in ("_order_lookup_context", "_recent_orders_context", "_user_facts_context", "_live_bot_context"):
        monkeypatch.setattr(AI, n, ctx)
    monkeypatch.setattr(AI, "ask_ai", ask)
    monkeypatch.setattr(AI, "_bot_system_prompt", lambda row: "sys")
    AI._answer_ids.clear()
    return seen


ROW = NS(id=1)


@pytest.mark.asyncio
async def test_name_alone_as_reply_answers_the_replied_message(ai):
    m = FakeMsg("اکسا", reply_text="چطوری سفارشمو لغو کنم؟")
    await AI._smm_ai_answer_in_group(m, "", ROW)
    assert ai["q"] == "چطوری سفارشمو لغو کنم؟"
    assert m.replies and "سؤالت رو بپرس" not in m.replies[0]


@pytest.mark.asyncio
async def test_name_alone_without_reply_still_asks(ai):
    m = FakeMsg("اکسا")
    await AI._smm_ai_answer_in_group(m, "", ROW)
    assert m.replies == ["جانم، سؤالت رو بپرس."] and "q" not in ai


@pytest.mark.asyncio
async def test_name_plus_words_with_reply_adds_context(ai):
    m = FakeMsg("اکسا جواب بده", reply_text="قیمت استارز چنده؟")
    await AI._smm_ai_answer_in_group(m, "جواب بده", ROW)
    assert "جواب بده" in ai["q"] and "قیمت استارز چنده؟" in ai["q"]


@pytest.mark.asyncio
async def test_followup_reply_to_ai_answer_without_name(ai, monkeypatch):
    async def gs(bot_id, key, default=""):
        return {"smm_ai_group_chat_id": "-100", "smm_ai_enabled": "1", "smm_ai_group_name": "اکسا"}.get(key, default)

    async def grow(bot): return ROW
    monkeypatch.setattr(AI, "_get_setting", gs)
    monkeypatch.setattr(AI, "_group_ai_allowed", lambda row: True)
    import tenant
    monkeypatch.setattr(tenant, "get_bot_row", grow)
    bot = NS(id=42)
    m = FakeMsg("پس چطور پولم برگرده؟", reply_text="🤖 جواب قبلی", reply_from=42)
    m.chat.type = "supergroup"
    assert await AI._smm_ai_group_match(m, bot) is False                     # هنوز جوابِ ربات ثبت نشده
    AI._remember_answer(-100, 5)
    r = await AI._smm_ai_group_match(m, bot)
    assert r and r["smm_ai_question"] == "پس چطور پولم برگرده؟"
    m2 = FakeMsg("سلام همه", reply_text="x", reply_from=99)                   # ریپلای رویِ پیامِ آدمِ دیگر ⇒ دخالت نمی‌کند
    m2.chat.type = "supergroup"
    assert await AI._smm_ai_group_match(m2, bot) is False


@pytest.mark.asyncio
async def test_answer_is_remembered_for_followups(ai):
    m = FakeMsg("اکسا سلام")
    await AI._smm_ai_answer_in_group(m, "سلام", ROW)
    assert (-100, 101) in AI._answer_ids


@pytest.mark.asyncio
async def test_memo_prompt_is_deleted_after_answer():
    from tenant.bots.virtual_service import smm as S
    deleted, edited = [], []

    class B:
        async def delete_message(self, chat, mid): deleted.append((chat, mid))
    await S._ton_drop_memo_prompt(B(), 5, {"ton_memo_prompt_id": 77})
    assert deleted == [(5, 77)]

    class M:
        async def delete(self): deleted.append("fb")
    await S._ton_drop_memo_prompt(B(), 5, {}, M())                           # بدونِ شناسه: همان پیامِ دکمه
    assert deleted[-1] == "fb"

    class Bad:
        async def delete_message(self, *a): raise RuntimeError("gone")

    class M2:
        async def delete(self): raise RuntimeError("no")

        async def edit_reply_markup(self, **k): edited.append(1)
    await S._ton_drop_memo_prompt(Bad(), 5, {"ton_memo_prompt_id": 1}, M2())  # خطا هیچ‌وقت بالا نمی‌آید؛ دکمه‌ها برداشته می‌شوند
    assert edited == [1]
