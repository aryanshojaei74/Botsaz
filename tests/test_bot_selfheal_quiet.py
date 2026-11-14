"""PATCH_SELFHEAL_QUIET_V1 — اعلانِ بازیابیِ خودکار: ربات‌هایِ تکراری هر ۲۴ ساعت حداکثر یک اعلان (+ یک اعلانِ «تکراری»)."""
import pytest

import bot_selfheal as SH


@pytest.fixture(autouse=True)
def _clean():
    SH._HEAL_LOG.clear()
    SH._NOTIFIED.clear()


def test_first_heal_notifies_then_quiet_until_repeat_threshold():
    H = 3600.0
    assert SH.decide_notice(1, "webhook", 0) == ("first", 1)
    assert SH.decide_notice(1, "webhook", H) == (None, 2)
    assert SH.decide_notice(1, "webhook", 2 * H) == ("repeat", 3)         # سومین بار ⇒ یک اعلانِ تکراری
    assert SH.decide_notice(1, "webhook", 3 * H)[0] is None               # بعدش ساکت
    assert SH.decide_notice(1, "webhook", 10 * H)[0] is None


def test_notice_comes_back_after_window_and_bots_are_independent():
    assert SH.decide_notice(1, "webhook", 0)[0] == "first"
    assert SH.decide_notice(2, "webhook", 10)[0] == "first"               # رباتِ دیگر مستقل
    assert SH.decide_notice(1, "token", 20)[0] == "first"                 # نوعِ دیگر مستقل
    assert SH.decide_notice(1, "webhook", SH.NOTIFY_WINDOW_SEC + 5)[0] == "first"


@pytest.mark.asyncio
async def test_selfheal_once_sends_single_message_for_repeating_bot(monkeypatch):
    class Rows:
        def all(self):
            return [type("R", (), {"id": 7, "token": "t", "status": "active"})()]

    class S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, q):
            return Rows()
    monkeypatch.setattr(SH, "Session", lambda: S())
    monkeypatch.setattr(SH, "PLATFORM_ADMIN_ID", 1)

    async def chk(b_id, token, status):
        return ("webhook", "aifree", b_id)
    monkeypatch.setattr(SH, "_check_one", chk)
    sent = []

    class B:
        async def send_message(self, chat, text, **k):
            sent.append(text)
    for _ in range(2):
        assert await SH.selfheal_once(B()) == 1
    assert len(sent) == 1 and "aifree" in sent[0]                          # دورِ دوم پیامی نرفت
