"""PATCH_BTN_NORMAL_EMOJI_V1 — «افزودن ایموجی معمولی روی دکمه» (بدونِ نیاز به پرمیوم)."""
import pytest
from sqlalchemy import select

import config
import models
from tests.test_paused_text_e2e import cb_update, msg_update, env, OWNER, USER  # noqa: F401


def test_split_and_replace_logic():
    import tenant
    S, L = tenant._split_leading_emoji, tenant._label_with_emoji
    assert S("🛒 خرید") == ("🛒", "خرید") and S("خرید") == ("", "خرید")
    assert S("❤️ پشتیبانی") == ("❤️", "پشتیبانی") and S("🇮🇷 ایران")[0] == "🇮🇷"
    assert S("خرید 🛒") == ("", "خرید 🛒")                     # ایموجیِ آخرِ نام ابتدایی حساب نمی‌شود
    assert L("🛒 خرید", "🎁") == "🎁 خرید" and L("🛒🔥 خرید", "🎁") == "🎁 خرید"    # جایگزین، نه تکرار
    assert L("خرید", "🎁") == "🎁 خرید" and L("🎁 خرید", "") == "خرید"
    assert len(L("ا" * 100, "🎁")) <= 80


async def _mk_button(env, label="خرید سرویس"):
    async with config.Session() as s:
        mb = models.MainButton(bot_id=env["bid"], row=0, col=0, label=label, action="custom")
        s.add(mb)
        await s.commit()
        return mb.id


async def _label(env, mb_id):
    async with config.Session() as s:
        return (await s.get(models.MainButton, mb_id)).label


@pytest.mark.asyncio
async def test_owner_picks_emoji_sends_own_emoji_and_removes_it(env):
    if env["field"] != "vpn_sales":
        pytest.skip("یک نوع کافی است")
    dp, bot, cap = env["dp"], env["bot"], env["cap"]
    mb = await _mk_button(env)
    # صفحهٔ «نام، متن و ظاهر» دکمهٔ جدید را دارد
    await dp.feed_update(bot, cb_update(f"thb:cat:look:{mb}"))
    assert any(f"thb:nemoji:{mb}" in btns for _t, btns in cap.shown())
    # ۱) انتخاب از فهرست
    await dp.feed_update(bot, cb_update(f"thb:nemoji:{mb}"))
    await dp.feed_update(bot, cb_update(f"thb:nepick:{mb}:0"))
    assert await _label(env, mb) == "🛒 خرید سرویس"
    # ۲) فرستادنِ ایموجیِ دلخواه، جایگزینِ قبلی می‌شود
    await dp.feed_update(bot, cb_update(f"thb:nemoji:{mb}"))
    await dp.feed_update(bot, msg_update("🚀"))
    assert await _label(env, mb) == "🚀 خرید سرویس"
    # ۳) متنِ بدونِ ایموجی ذخیره نمی‌شود و حالت باقی می‌ماند
    await dp.feed_update(bot, cb_update(f"thb:nemoji:{mb}"))
    await dp.feed_update(bot, msg_update("فقط متن"))
    assert await _label(env, mb) == "🚀 خرید سرویس"
    await dp.feed_update(bot, msg_update("🔥"))
    assert await _label(env, mb) == "🔥 خرید سرویس"
    # ۴) حذف
    await dp.feed_update(bot, cb_update(f"thb:nedel:{mb}"))
    assert await _label(env, mb) == "خرید سرویس"


@pytest.mark.asyncio
async def test_non_owner_cannot_change_button_emoji(env):
    if env["field"] != "vpn_sales":
        pytest.skip("یک نوع کافی است")
    mb = await _mk_button(env)
    await env["dp"].feed_update(env["bot"], cb_update(f"thb:nepick:{mb}:0", uid=USER))
    await env["dp"].feed_update(env["bot"], cb_update(f"thb:nedel:{mb}", uid=USER))
    assert await _label(env, mb) == "خرید سرویس"
