"""
autorenew.py — «♻️ تمدیدِ خودکار از کیف‌پول» (PATCH_VPN_AUTORENEW_V1).

• مالک از «تنظیمات» کلِ قابلیت را روشن می‌کند (پیش‌فرض خاموش).
• مشتری در صفحهٔ هر سرویس، برایِ همان سرویس روشن/خاموش می‌کند؛ پلنِ تمدید همان پلنِ هم‌حجم/هم‌مدتِ سرویس است
  (اگر نبود، نزدیک‌ترین پلنِ هم‌نوع).
• نزدیکِ پایانِ مهلت (≤ ۱۲ ساعت) یا بعد از پایان، اگر موجودی کافی بود تمدید می‌شود؛ وگرنه فقط یک‌بار در هر چرخه
  به مشتری خبر می‌رود و تلاشِ دوباره نمی‌شود. در خطایِ پنل، پول برمی‌گردد.
مسیرِ تمدید همان قاعدهٔ تمدیدِ دستی است (تخفیفِ همکار، ریستِ هشدارها، اطلاعِ خرید).
"""
from __future__ import annotations

import datetime as _dt
import html as _html

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import Session
from models import Bot as BotRow, BotUser, PanelAccount, VpnAutoRenew, VpnPlan, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

autorenew_router = Router(name="vpn_autorenew")

SETTING = "autorenew_enabled"
LEAD = _dt.timedelta(hours=12)
LAG = _dt.timedelta(days=3)          # سرویسی که بیش از ۳ روز پیش تمام شده خودکار تمدید نمی‌شود (غافلگیریِ مشتری)
INTERVAL = 600


def _rial(n) -> str:
    return f"{int(round(float(n))):,} تومان"


async def enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, SETTING, "0")) == "1"


async def pick_plan(s, bot_id: int, account, panel_key: str, tg: int):
    """پلنِ تمدیدِ این سرویس: هم‌حجم/هم‌مدت، وگرنه نزدیک‌ترین هم‌نوع (حجمی/نامحدود). None اگر نباشد."""
    from tenant.bots.vpn.vpn_user import _plan_allowed_for
    plans = (await s.scalars(select(VpnPlan).where(
        VpnPlan.bot_id == bot_id, VpnPlan.active == True, VpnPlan.panel_key == panel_key)   # noqa: E712
        .order_by(VpnPlan.sort_order, VpnPlan.id))).all()
    plans = [p for p in plans if await _plan_allowed_for(p, bot_id, tg)]
    unl = (account.volume_gb or 0) == 0
    same = [p for p in plans if ((p.volume_gb or 0) == 0) == unl] or plans
    if not same:
        return None
    exact = [p for p in same if (p.volume_gb or 0) == (account.volume_gb or 0) and (p.days or 0) == (account.days or 0)]
    if exact:
        return exact[0]
    return min(same, key=lambda p: (abs((p.volume_gb or 0) - (account.volume_gb or 0)), abs((p.days or 0) - (account.days or 0))))


# ───────────────────────── مشتری ─────────────────────────

async def _own(cb: CallbackQuery, bot: Bot, aid: int):
    async with Session() as s:
        row = await s.scalar(select(BotRow).where(BotRow.token == bot.token))
        a = await s.get(PanelAccount, aid)
    if not row or not a or a.bot_id != row.id or a.user_telegram_id != cb.from_user.id:
        await cb.answer("یافت نشد.", show_alert=True)
        return None, None
    return row, a


async def button_for(bot_id: int, aid: int) -> InlineKeyboardButton | None:
    """دکمهٔ صفحهٔ سرویس (None اگر مالک قابلیت را روشن نکرده)."""
    if not await enabled(bot_id):
        return None
    async with Session() as s:
        r = await s.scalar(select(VpnAutoRenew).where(VpnAutoRenew.bot_id == bot_id, VpnAutoRenew.account_id == aid))
    on = bool(r and r.enabled)
    return InlineKeyboardButton(text="♻️ تمدیدِ خودکار: " + ("✅ روشن" if on else "⬜️ خاموش"),
                                callback_data=f"vauto:{aid}",
                                style=ButtonStyle.SUCCESS if on else ButtonStyle.PRIMARY)


@autorenew_router.callback_query(F.data.regexp(r"^vauto:\d+$"))
async def auto_page(cb: CallbackQuery, bot: Bot):
    from tenant.bots.vpn.vpn_user import _panel_for_account, _renew_partner_price
    row, a = await _own(cb, bot, int(cb.data.split(":")[1]))
    if not a:
        return
    if not await enabled(row.id):
        return await cb.answer("تمدیدِ خودکار فعلاً فعال نیست.", show_alert=True)
    async with Session() as s:
        panel = await _panel_for_account(s, row.id, a)
        plan = await pick_plan(s, row.id, a, panel.panel_key, cb.from_user.id) if panel else None
        cur = await s.scalar(select(VpnAutoRenew).where(VpnAutoRenew.bot_id == row.id, VpnAutoRenew.account_id == a.id))
    if not plan:
        return await cb.answer("برایِ این سرویس پلنِ تمدید پیدا نشد؛ با پشتیبانی هماهنگ کن.", show_alert=True)
    price = await _renew_partner_price(row.id, cb.from_user.id, float(plan.price), plan.panel_key, "plans", plan.volume_gb)
    on = bool(cur and cur.enabled)
    vol = "نامحدود" if not plan.volume_gb else f"{plan.volume_gb} گیگ"
    dur = "بی‌انقضا" if not plan.days else f"{plan.days} روز"
    text = ("♻️ <b>تمدیدِ خودکار از کیف‌پول</b>\n\n"
            f"پلنِ تمدید: <b>{_html.escape(plan.title)}</b> — +{vol} / +{dur}\n💰 مبلغ: <b>{_rial(price)}</b>\n\n"
            "نزدیکِ پایانِ سرویس، اگر موجودیِ کیف‌پولت کافی باشد خودکار تمدید می‌شود و بهت خبر می‌دهیم. "
            "اگر موجودی کم باشد فقط هشدار می‌گیری. هر وقت خواستی خاموشش کن.\n\n"
            f"وضعیتِ فعلی: {'✅ روشن' if on else '⬜️ خاموش'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬜️ خاموش کن" if on else "✅ روشن کن", callback_data=f"vautoset:{a.id}:{0 if on else 1}",
                              style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"vsvc:{a.id}", style=ButtonStyle.DANGER)]])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@autorenew_router.callback_query(F.data.regexp(r"^vautoset:\d+:[01]$"))
async def auto_set(cb: CallbackQuery, bot: Bot):
    from tenant.bots.vpn.vpn_user import _panel_for_account
    _, aid_s, on_s = cb.data.split(":")
    row, a = await _own(cb, bot, int(aid_s))
    if not a:
        return
    if not await enabled(row.id):
        return await cb.answer("تمدیدِ خودکار فعلاً فعال نیست.", show_alert=True)
    on = on_s == "1"
    async with Session() as s:
        panel = await _panel_for_account(s, row.id, a)
        plan = await pick_plan(s, row.id, a, panel.panel_key, cb.from_user.id) if panel else None
        r = await s.scalar(select(VpnAutoRenew).where(VpnAutoRenew.bot_id == row.id, VpnAutoRenew.account_id == a.id))
        if on and not plan:
            return await cb.answer("برایِ این سرویس پلنِ تمدید پیدا نشد.", show_alert=True)
        if r is None:
            r = VpnAutoRenew(bot_id=row.id, account_id=a.id, user_tg=cb.from_user.id)
            s.add(r)
        r.enabled, r.plan_id, r.last_cycle, r.last_result = on, (plan.id if plan else None), None, None
        await s.commit()
    await cb.answer("✅ تمدیدِ خودکار روشن شد." if on else "⬜️ خاموش شد.", show_alert=True)
    await auto_page(cb, bot)


# ───────────────────────── مالک ─────────────────────────

@autorenew_router.callback_query(F.data == "set:autorenew")
async def owner_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting, tadm_sec_settings
    row = await _owner_ok(cb, bot)
    if not row:
        return
    new = "0" if (await get_setting(row.id, SETTING, "0")) == "1" else "1"
    await set_setting(row.id, SETTING, new)
    await cb.answer("♻️ تمدیدِ خودکار برایِ مشتری‌ها روشن شد (هر مشتری برایِ سرویسِ خودش فعالش می‌کند)." if new == "1"
                    else "⬜️ تمدیدِ خودکار خاموش شد؛ تمدیدهایِ خودکارِ قبلی اجرا نمی‌شوند.", show_alert=True)
    await tadm_sec_settings(cb, bot)


# ───────────────────────── اجرا ─────────────────────────

async def _renew_one(bot: Bot | None, bot_id: int, ar: VpnAutoRenew, acc: PanelAccount, now: _dt.datetime,
                     client_factory=None) -> tuple[str, str]:
    """(نتیجه, متن برایِ مشتری): ok | nofunds | failed | noplan."""
    from tenant.bots.vpn.vpn_user import _panel_for_account, _renew_partner_price
    async with Session() as s:
        panel = await _panel_for_account(s, bot_id, acc)
        plan = await s.get(VpnPlan, ar.plan_id) if ar.plan_id else None
        if not plan or not plan.active:
            plan = await pick_plan(s, bot_id, acc, panel.panel_key, ar.user_tg) if panel else None
    if not panel or not panel.enabled or not plan:
        return "noplan", "⚠️ تمدیدِ خودکارِ سرویست انجام نشد (پلنِ تمدید در دسترس نیست). دستی تمدید کن."
    price = await _renew_partner_price(bot_id, ar.user_tg, float(plan.price), plan.panel_key, "plans", plan.volume_gb)
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == ar.user_tg).with_for_update())
        if not u or float(u.balance) < price:
            bal = int(float(u.balance)) if u else 0
            return "nofunds", (f"⚠️ سرویسِ «{_html.escape(acc.plan_title or acc.email)}» رو به پایان است و موجودیِ کیف‌پولت "
                               f"({_rial(bal)}) برایِ تمدیدِ خودکار ({_rial(price)}) کافی نیست. کیف‌پول را شارژ یا دستی تمدید کن.")
        u.balance = float(u.balance) - price
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=-price, kind="renew", note=f"تمدیدِ خودکار VPN: {plan.title}"))
        await s.commit()
        user_db_id = u.id
        pdata = (panel.base_url, panel.username, panel.password, panel.sub_base or "", panel.inbound_ids, panel.panel_key, panel.api_key)
    if client_factory is None:
        from tenant.shared.panel_clients import panel_pool as _pool

        def client_factory():
            return _pool.get_sync(pdata[5], pdata[0], pdata[1], pdata[2], sub_base=pdata[3], inbound_ids_str=pdata[4], api_key=pdata[6])
    client = client_factory()
    try:
        if plan.days:
            await client.renew(acc.email, plan.days)
        if plan.volume_gb:
            await client.add_traffic(acc.email, plan.volume_gb)
    except Exception as e:  # noqa: BLE001
        logger.warning("autorenew: خطایِ پنل (bot=%s acc=%s): %s", bot_id, acc.id, e)
        async with Session() as s:
            u = await s.get(BotUser, user_db_id)
            if u:
                u.balance = float(u.balance) + price
                s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=price, kind="refund", note="عودت: خطایِ تمدیدِ خودکار"))
                await s.commit()
        return "failed", "❌ تمدیدِ خودکار با خطایِ پنل روبه‌رو شد و مبلغ برگشت. دستی تمدید کن یا به پشتیبانی بگو."
    finally:
        try:
            await client.close()
        except Exception:      # noqa: BLE001
            pass
    async with Session() as s:
        a2 = await s.get(PanelAccount, acc.id)
        if a2:
            a2.expiry_notice, a2.expiry_warn_count, a2.status = None, 0, "active"
            if plan.days:
                base = a2.expires_at if a2.expires_at and a2.expires_at > now else now
                a2.expires_at = base + _dt.timedelta(days=plan.days)
                a2.days = (a2.days or 0) + plan.days
            if plan.volume_gb:
                a2.volume_gb = (a2.volume_gb or 0) + plan.volume_gb
                a2.traffic_out_at = None
            await s.commit()
    if bot is not None:
        try:
            from tenant import _purchase_alert
            async with Session() as s:
                row = await s.get(BotRow, bot_id)
                buyer = await s.get(BotUser, user_db_id)
            await _purchase_alert(bot, row, buyer=buyer, section="تمدیدِ خودکار VPN", detail=_html.escape(plan.title),
                                  amount=price, product_id=acc.id)
        except Exception:      # noqa: BLE001
            logger.exception("autorenew: اطلاعِ خرید ناموفق بود")
    vol = "نامحدود" if not plan.volume_gb else f"{plan.volume_gb} گیگ"
    dur = "بی‌انقضا" if not plan.days else f"{plan.days} روز"
    return "ok", f"♻️ سرویسِ «{_html.escape(acc.plan_title or acc.email)}» خودکار تمدید شد (+{vol} / +{dur}) — {_rial(price)} از کیف‌پول کم شد."


async def run_bot_once(bot_id: int, bot: Bot | None = None, now: _dt.datetime | None = None, client_factory=None) -> list[tuple[int, str]]:
    """یک دور برایِ یک ربات. خروجی: [(telegram_id, متن)]."""
    now = now or _dt.datetime.utcnow()
    if not await enabled(bot_id):
        return []
    out: list[tuple[int, str]] = []
    async with Session() as s:
        items = (await s.scalars(select(VpnAutoRenew).where(
            VpnAutoRenew.bot_id == bot_id, VpnAutoRenew.enabled == True))).all()   # noqa: E712
        accs = {a.id: a for a in (await s.scalars(select(PanelAccount).where(
            PanelAccount.bot_id == bot_id, PanelAccount.id.in_([i.account_id for i in items] or [0])))).all()}
    for ar in items:
        acc = accs.get(ar.account_id)
        if not acc or acc.user_telegram_id != ar.user_tg or acc.status in ("deleted",) or not acc.expires_at:
            continue
        if acc.expires_at - now > LEAD or now - acc.expires_at > LAG or acc.status == "disabled":
            continue
        cycle = acc.expires_at.isoformat()
        if ar.last_cycle == cycle:                        # برایِ همین چرخه قبلاً تلاش شده (موفق یا هشدار)
            continue
        async with Session() as s:                        # ادعایِ اتمیکِ چرخه؛ اجرایِ هم‌زمانِ دو حلقه دوبار تمدید نکند
            row = await s.get(VpnAutoRenew, ar.id, with_for_update=True)
            if not row or not row.enabled or row.last_cycle == cycle:
                continue
            row.last_cycle, row.last_result = cycle, "claimed"
            await s.commit()
        result, text = await _renew_one(bot, bot_id, ar, acc, now, client_factory)
        async with Session() as s:
            row = await s.get(VpnAutoRenew, ar.id, with_for_update=True)
            if row:
                row.last_result = result
                if result == "failed":
                    row.last_cycle = None                 # خطایِ گذرایِ پنل: دورِ بعد دوباره تلاش
                await s.commit()
        out.append((ar.user_tg, text))
    return out


async def run_once() -> int:
    sent = 0
    async with Session() as s:
        rows = (await s.scalars(select(BotRow).where(BotRow.field == "vpn_sales", BotRow.status == "active"))).all()
    for row in rows:
        try:
            if not await enabled(row.id):
                continue
            bot = Bot(token=row.token)
            try:
                for tg, text in await run_bot_once(row.id, bot):
                    try:
                        await bot.send_message(tg, text, parse_mode="HTML")
                        sent += 1
                    except Exception:      # noqa: BLE001
                        logger.info("autorenew: پیام به %s نرسید", tg)
            finally:
                await bot.session.close()
        except Exception:      # noqa: BLE001
            logger.exception("autorenew loop: خطا (bot=%s)", row.id)
    return sent


async def autorenew_loop() -> None:
    import asyncio
    await asyncio.sleep(45)
    while True:
        try:
            await run_once()
        except Exception:      # noqa: BLE001
            logger.exception("autorenew loop crashed; retrying")
        await asyncio.sleep(INTERVAL)
