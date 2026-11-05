"""PATCH_BOOKING_V25_CHAN — کانالِ تأییدِ نوبت‌ها: کپیِ درخواست با دکمه، تأیید/ردِ ادمین از داخلِ کانال."""
from types import SimpleNamespace

import pytest

import tenant
from tenant.bots.booking import bk_admin as ADM
from tests.test_booking_flow import FCb, FMsg, OWNER, CUST, _book_flow, env, fsm  # noqa: F401

CH = -1001234567890


@pytest.mark.asyncio
async def test_pending_request_goes_to_channel_and_admin_can_approve_there(env):
    await tenant.set_setting(21, "bk_appt_channel", str(CH))
    await _book_flow(env)
    posts = [(c, t) for c, t in env.bot.sent if c == CH]
    assert posts and "درخواستِ نوبتِ جدید" in posts[0][1]
    async with env.mk() as s:
        from sqlalchemy import select
        import models
        ap = (await s.scalars(select(models.BkAppt))).first()
    cb = FCb(f"bka:ok:{ap.id}", OWNER)
    cb.message.chat = SimpleNamespace(id=CH)
    await ADM.bka_act(cb, env.bot)
    assert "توسط" in cb.texts() and not cb.buttons()                # پیامِ کانال بسته شد (بدونِ دکمه)
    assert any(c == CUST and "تأیید" in t for c, t in env.bot.sent)  # مشتری خبردار شد
    # غیرِ ادمین (مثلاً عضوِ کانال) اثری ندارد
    bad = FCb(f"bka:no:{ap.id}", 9999)
    bad.message.chat = SimpleNamespace(id=CH)
    await ADM.bka_act(bad, env.bot)
    assert not bad.message.out


@pytest.mark.asyncio
async def test_no_channel_means_no_post_and_chan_screen_works(env):
    await _book_flow(env)
    assert not [1 for c, _t in env.bot.sent if c == CH]
    cb = FCb("bka:chan", OWNER)
    await ADM.bka_chan(cb, env.bot, fsm(OWNER))
    assert "bka:chan:set" in cb.buttons() and "bka:chan:off" not in cb.buttons()


@pytest.mark.asyncio
async def test_back_from_generic_pages_returns_to_the_same_tools_category(env):
    """PATCH_BOOKING_TOOLS_BACK_V1 — «بازگشت» در صفحه‌هایِ عمومی به همان دسته برمی‌گردد، نه منویِ قدیمی."""
    cb = FCb("bka:gc:bot", OWNER)
    await ADM.bka_generic_cat(cb, env.bot)
    assert "tadm:backup:menu" in cb.buttons()
    # صفحهٔ عمومی دکمهٔ «بازگشت» = tadm:sec:settings را می‌زند
    for fn, data in ((tenant.tadm_sec_settings, "tadm:sec:settings"), (tenant.tadm_sec_finance, "tadm:sec:finance"),
                     (tenant.tadm_sec_stats, "tadm:sec:stats"), (tenant.tadm_sec_admins, "tadm:sec:admins"),
                     (tenant.tadm_menu, "tadm:menu")):
        back = FCb(data, OWNER)
        await fn(back, env.bot)
        assert "tadm:backup:menu" in back.buttons(), data          # همان دسته (مدیریتِ ربات)
        assert "tadm:sec:finance" not in back.buttons()            # منویِ قدیمیِ عمومی نیست
    # از صفحهٔ اصلیِ ابزارها که بیایی، بازگشت = خودِ صفحهٔ ابزارها / پنلِ رزرو
    await ADM.bka_generic_tools(FCb("bka:gen", OWNER), env.bot, fsm(OWNER))
    top = FCb("tadm:sec:settings", OWNER)
    await tenant.tadm_sec_settings(top, env.bot)
    assert "bka:gc:bot" in top.buttons() and "tadm:sec:finance" not in top.buttons()
    home = FCb("tadm:menu", OWNER)
    await tenant.tadm_menu(home, env.bot)
    assert "bka:ap" in home.buttons()                               # پنلِ رزرو
