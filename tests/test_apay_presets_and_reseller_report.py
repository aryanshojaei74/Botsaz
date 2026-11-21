import pytest
from types import SimpleNamespace as NS
from tenant.gateways import apay_admin as AA, apay as AP
from tenant.shared import trial_report as TR, web_panel as WP


def test_apay_presets_respect_range():
    assert AA.apu_presets(50_000, 0) == [50_000, 100_000, 200_000, 500_000, 1_000_000]
    assert AA.apu_presets(100_000, 500_000) == [100_000, 200_000, 500_000]
    assert AA.apu_presets(2_000_000, 0) == []
    assert all(p <= AP.API_MAX_TOMAN for p in AA.apu_presets(1, 10 ** 12))


def _data(b): return b.callback_data


def test_apay_keyboard_shape():
    kb = AA._apu_amount_kb([50_000, 100_000, 200_000])
    flat = [b for r in kb.inline_keyboard for b in r]
    datas = [_data(b) for b in flat]
    assert "apu:amt:50000" in datas and "apu:custom" in datas and "w:back" in datas
    assert flat[0].text == "50,000 تومان" and len(kb.inline_keyboard[0]) == 2


def test_report_text_marks_reseller():
    t = TR.build_report(user_id=5, username="rep", full_name="N", link="", volume="1 گیگ × 3",
                        duration="1 روز", panel="P", source="reseller")
    assert "🤝 ساخته‌شده توسط نماینده" in t and "× 3" in t and "#تست_جدید" in t
    assert "نماینده" not in TR.build_report(user_id=5, username="x", full_name="N", link="", volume="1", duration="1",
                                            panel="P", source="panel")


@pytest.mark.asyncio
async def test_reseller_trial_report_calls_shared_poster(monkeypatch):
    import tenant, aiogram
    got = {}

    class S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, model, pk): return NS(token="1:T")
        async def scalar(self, *a, **k): return NS(telegram_id=5, username="rep", full_name="N")

    class FakeBot:
        def __init__(self, token): self.session = NS(close=self._close)
        async def _close(self): got["closed"] = True

    async def poster(bot, bot_id, u, tg_username, link, volume, duration, panel_key, source):
        got.update(bot_id=bot_id, volume=volume, duration=duration, panel=panel_key, source=source, user=u.telegram_id)
    monkeypatch.setattr(WP, "Session", lambda: S())
    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setattr(tenant, "_trial_report_post", poster)
    await WP._rs_trial_report(7, 5, "rep", "pk1", 0.5, 1, 0, 3)
    assert got["source"] == "reseller" and got["panel"] == "pk1" and got["bot_id"] == 7
    assert got["volume"].endswith("× 3") and "مگابایت" in got["volume"] and got["duration"] == "1 روز" and got["closed"]


@pytest.mark.asyncio
async def test_reseller_report_errors_never_raise(monkeypatch):
    class S:
        async def __aenter__(self): raise RuntimeError("db down")
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(WP, "Session", lambda: S())
    await WP._rs_trial_report(1, 2, "x", "p", 1, 1, 0, 1)      # نباید exception بدهد


@pytest.mark.asyncio
async def test_admin_answer_dup_shows_force_button_without_crediting(monkeypatch):
    import tenant
    calls = []

    async def owner_ok(cb, bot): return NS(id=5)

    async def settle(bot_id, cid, admin, force=False):
        calls.append(force)
        return (False, "DUP:احتمالِ تکراری") if not force else (True, "ok")
    monkeypatch.setattr(tenant, "_owner_ok", owner_ok)
    monkeypatch.setattr(AP, "manual_settle", settle)
    edits, answers = [], []

    async def edit_text(text, **k): edits.append((text, k.get("reply_markup")))

    async def answer(t=None, **k): answers.append((t, k))
    cb = NS(data="apadm:ok:3", from_user=NS(id=9), answer=answer,
            message=NS(html_text="سوال", edit_text=edit_text))
    await AA.ap_admin_answer(cb, None)
    assert calls == [False] and "احتمالِ شارژِ تکراری" in edits[-1][0]
    datas = [b.callback_data for r in edits[-1][1].inline_keyboard for b in r]
    assert "apadm:okf:3" in datas and "apadm:no:3" in datas and answers[-1][1].get("show_alert") is True
