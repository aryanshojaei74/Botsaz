"""تیکت #892 — تستِ رایگانِ VPN: مشتری از میانِ چند پنل یکی را انتخاب کند؛ نام/حجم/مدتِ هر پنل قابلِ تنظیم است."""
import asyncio
import json
from types import SimpleNamespace as NS

import tenant

CFG = {"trial_mode": "auto", "trial_pick_panel": "1", "trial_panel_keys": "marzban,pasargad", "trial_gb": "2",
       "trial_days": "3",
       "trial_panel_cfg": {"marzban": {"name": "🇩🇪 آلمان", "gb": 5, "days": 7}, "pasargad": {"name": "🇹🇷 ترکیه"}}}


def _patch(monkeypatch, delivered):
    async def back(_b): return None
    async def allowed(*a, **k): return True, ""
    async def fill(_b, cfg): return dict(cfg)
    async def live(_b): return ["marzban", "pasargad", "sanai"]

    async def deliver(bot, bot_id, u, mode, gb, days, panel_key, back_kb, **kw):
        delivered.append((panel_key, gb, days))
    monkeypatch.setattr(tenant, "inline_back_kb", back)
    monkeypatch.setattr(tenant, "_check_button_trial_allowance", allowed)
    monkeypatch.setattr(tenant, "_trial_fill_global", fill)
    monkeypatch.setattr(tenant, "_trial_enabled_panel_keys", live)
    monkeypatch.setattr(tenant, "_trial_deliver", deliver)
    monkeypatch.setattr(tenant, "_trial_user_name", lambda *a, **k: "u")


class _Msg:
    def __init__(self): self.out = []
    async def answer(self, text, **kw): self.out.append((text, kw))


def test_picker_lists_panels_with_custom_names_and_values(monkeypatch):
    got = []
    _patch(monkeypatch, got)
    m = _Msg()
    asyncio.run(tenant.do_free_trial(m, object(), 1, NS(telegram_id=5, id=1), btn_cfg=dict(CFG), mb_id=9))
    assert got == []                                           # هنوز چیزی ساخته نشده؛ منتظرِ انتخابِ مشتری
    kb = m.out[-1][1]["reply_markup"]
    btns = [(b.text, b.callback_data) for r in kb.inline_keyboard for b in r]
    assert ("🇩🇪 آلمان — 5 گیگ / 7 روز", "trpick:9:marzban") in [(t.replace("٫", "."), c) for t, c in btns] or \
        any(c == "trpick:9:marzban" and "آلمان" in t and "7 روز" in t for t, c in btns)
    assert any(c == "trpick:9:pasargad" and "ترکیه" in t for t, c in btns)


def test_forced_panel_uses_panel_specific_gb_and_days(monkeypatch):
    got = []
    _patch(monkeypatch, got)
    asyncio.run(tenant.do_free_trial(_Msg(), object(), 1, NS(telegram_id=5, id=1), btn_cfg=dict(CFG), mb_id=9,
                                     forced_panel_key="marzban"))
    assert got == [("marzban", 5.0, 7)]
    got.clear()
    asyncio.run(tenant.do_free_trial(_Msg(), object(), 1, NS(telegram_id=5, id=1), btn_cfg=dict(CFG), mb_id=9,
                                     forced_panel_key="pasargad"))
    assert got == [("pasargad", 2.0, 3)]                       # بدونِ مقدارِ اختصاصی ⇒ حجم/مدتِ دکمه


def test_without_pick_flag_keeps_random_behaviour(monkeypatch):
    got = []
    _patch(monkeypatch, got)
    cfg = dict(CFG)
    cfg.pop("trial_pick_panel")
    asyncio.run(tenant.do_free_trial(_Msg(), object(), 1, NS(telegram_id=5, id=1), btn_cfg=cfg, mb_id=9))
    assert len(got) == 1 and got[0][0] in ("marzban", "pasargad")
    assert got[0][1:] in ((5.0, 7), (2.0, 3))                  # حجم/مدتِ اختصاصی در حالتِ رندوم هم اعمال می‌شود


def test_panel_cfg_helper_accepts_json_string():
    assert tenant._trial_panel_cfg({"trial_panel_cfg": json.dumps({"a": {"gb": 1}})}, "a") == {"gb": 1}
    assert tenant._trial_panel_cfg({}, "a") == {} and tenant._trial_panel_cfg(CFG, None) == {}
