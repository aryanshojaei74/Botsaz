"""PATCH_USER_VS_ORDERS_V1 (SINA) — مدیریتِ کاربران: لیستِ سفارش‌هایِ خدماتِ مجازیِ کاربر (۱۰تایی) + جستجو با کدِ پیگیری/نامِ محصول."""
import datetime as dt

import pytest

import config
import models
from tests.test_paused_text_e2e import cb_update, msg_update, env, OWNER, USER  # noqa: F401

TG = 810001


async def _seed(env, n_smm=12):
    async with config.Session() as s:
        u = models.BotUser(bot_id=env["bid"], telegram_id=TG, full_name="Ali", balance=0)
        s.add(u)
        await s.flush()
        base = dt.datetime(2026, 11, 1, 10, 0)
        for i in range(n_smm):
            s.add(models.SmmOrder(bot_id=env["bid"], user_telegram_id=TG, service_id="1", service_name="فالوور اینستاگرام",
                                  link="x", quantity=100 + i, price_toman=1000 + i, status="completed",
                                  tracking_code=f"S{100 + i}", created_at=base + dt.timedelta(minutes=i)))
        s.add(models.GiftOrder(bot_id=env["bid"], buyer_user_id=u.id, gift_id="starsell_100", gift_emoji="⭐", star_count=100,
                               price_toman=20000, dest_telegram_id=TG, dest_label="خودم", status="sent",
                               tracking_code="G777", quantity=1, created_at=base + dt.timedelta(hours=5)))
        await s.commit()


def _vs(env):
    if env["field"] != "virtual_service":
        pytest.skip("فقط ربات خدمات مجازی")


def _texts(env):
    return " ".join(t or "" for t, _b in env["cap"].shown())


@pytest.mark.asyncio
async def test_card_has_button_and_list_paginates_by_ten_newest_first(env):
    _vs(env)
    await _seed(env)
    await env["dp"].feed_update(env["bot"], cb_update(f"usr:card:{TG}"))
    assert any(f"usr:vso:{TG}:0" in btns for _t, btns in env["cap"].shown())
    env["cap"].calls.clear()
    await env["dp"].feed_update(env["bot"], cb_update(f"usr:vso:{TG}:0"))
    t, btns = env["cap"].shown()[-1]
    assert "مجموع: 13" in t and "صفحهٔ 1 از 2" in t
    assert t.count("🔖") == 10
    assert t.index("G777") < t.index("S111")                      # جدیدترین (گیفتِ ساعتِ ۱۵) اول
    assert f"usr:vso:{TG}:1" in btns and f"usr:vsos:{TG}" in btns and f"usr:card:{TG}" in btns
    env["cap"].calls.clear()
    await env["dp"].feed_update(env["bot"], cb_update(f"usr:vso:{TG}:1"))
    t2, btns2 = env["cap"].shown()[-1]
    assert t2.count("🔖") == 3 and f"usr:vso:{TG}:0" in btns2 and not any(b.endswith(":2") for b in btns2 if b.startswith("usr:vso"))


@pytest.mark.asyncio
async def test_search_by_tracking_code_and_by_product_name(env):
    _vs(env)
    await _seed(env)
    dp, bot, cap = env["dp"], env["bot"], env["cap"]
    await dp.feed_update(bot, cb_update(f"usr:vsos:{TG}"))
    assert "کدِ پیگیری" in _texts(env)
    cap.calls.clear()
    await dp.feed_update(bot, msg_update("s105"))                          # بدونِ حساسیت به حروف
    t, _b = cap.shown()[-1]
    assert "S105" in t and t.count("🔖") == 1 and "مجموع: 1" in t
    await dp.feed_update(bot, cb_update(f"usr:vsos:{TG}"))
    cap.calls.clear()
    await dp.feed_update(bot, msg_update("استارز"))                        # نامِ محصول
    t2, _b2 = cap.shown()[-1]
    assert "G777" in t2 and t2.count("🔖") == 1
    await dp.feed_update(bot, cb_update(f"usr:vsos:{TG}"))
    cap.calls.clear()
    await dp.feed_update(bot, msg_update("چیزی که نیست"))
    assert "سفارشی پیدا نشد" in cap.shown()[-1][0]


@pytest.mark.asyncio
async def test_non_admin_cannot_open_and_other_kinds_have_no_button(env):
    await _seed(env)
    await env["dp"].feed_update(env["bot"], cb_update(f"usr:vso:{TG}:0", uid=USER))
    assert "G777" not in _texts(env)
    if env["field"] != "virtual_service":
        await env["dp"].feed_update(env["bot"], cb_update(f"usr:card:{TG}"))
        assert not any(f"usr:vso:{TG}:0" in btns for _t, btns in env["cap"].shown())
