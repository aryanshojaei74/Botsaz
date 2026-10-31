"""
service.py — منطقِ فروشِ ادمینِ پنل (PATCH_PANEL_SELLER_V1): خرید، تمدید، تعلیق/فعال‌سازی، اعمالِ سقف‌ها.

هیچ‌چیزِ تلگرامی اینجا نیست (تست‌پذیر)؛ پیام‌هایِ لازم به‌صورتِ لیستِ (telegram_id, text) برمی‌گردند.
خریدِ کیف‌پولی: اول کسرِ اتمیکِ موجودی، بعد ساختِ ادمین روی پنل؛ اگر پنل خطا داد، پول برمی‌گردد.
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta

from sqlalchemy import select

from config import Session
from models import BotUser, PanelSellerAccount, PanelSellerPackage, VpnPanel, WalletTxn

from tenant.bots.panel_seller import adapter as AD

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

GB = 1024 ** 3
WARN_DAYS = 3
WARN_TRAFFIC = 0.8


def gen_username() -> str:
    return "ps" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))


def gen_password() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(14))


def _api(panel):
    return AD.PanelAdminAPI.for_panel(panel)


def _flags(acc) -> set[str]:
    return {f for f in (acc.flags or "").split(",") if f}


def _set_flags(acc, fl: set[str]) -> None:
    acc.flags = ",".join(sorted(fl))[:120] or None


async def _wallet_debit(s, bot_id: int, tg: int, price: int, note: str):
    """بدهکارکردنِ اتمیکِ کیف‌پول. خروجی: (user, balance_before) یا (None, balance) اگر کافی نبود."""
    u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == tg).with_for_update())
    if u is None:
        return None, 0
    bal = int(u.balance or 0)
    if bal < price:
        return None, bal
    u.balance = float(u.balance) - price
    s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=-price, kind="purchase", note=note))
    return u, bal


async def _refund(bot_id: int, tg: int, price: int, note: str) -> None:
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == tg).with_for_update())
        if u is not None:
            u.balance = float(u.balance) + price
            s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=price, kind="charge", note=note))
            await s.commit()


async def buy(bot_id: int, buyer_tg: int, package_id: int, api_factory=None, bot=None, row=None) -> dict:
    """status: ok | no_package | no_panel | unsupported | insufficient | panel_failed (+ account/username/password/…)."""
    api_factory = api_factory or _api
    async with Session() as s:
        pk = await s.get(PanelSellerPackage, package_id)
        if not pk or pk.bot_id != bot_id or not pk.active:
            return {"status": "no_package"}
        panel = await s.get(VpnPanel, pk.panel_id)
    if not panel or panel.bot_id != bot_id or not panel.enabled:
        return {"status": "no_panel"}
    if not AD.supports(panel.panel_key):
        return {"status": "unsupported"}
    price = int(pk.price_toman or 0)
    async with Session() as s:
        if price > 0:
            u, bal = await _wallet_debit(s, bot_id, buyer_tg, price, f"خریدِ پنل: {pk.title}")
            if u is None:
                return {"status": "insufficient", "need": price, "balance": bal}
        await s.commit()
    username, password = gen_username(), gen_password()
    try:
        api = api_factory(panel)
        try:
            await api.create_admin(username, password)
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("panel_seller: ساختِ ادمین ناموفق (bot=%s): %s", bot_id, e)
        if price > 0:
            await _refund(bot_id, buyer_tg, price, "بازگشتِ وجهِ خریدِ پنل (خطایِ پنل)")
        return {"status": "panel_failed", "error": str(e)[:300]}
    from models import alloc_tracking_in
    async with Session() as s:
        trk = await alloc_tracking_in(s, bot_id)
        acc = PanelSellerAccount(
            bot_id=bot_id, buyer_tg=buyer_tg, panel_id=panel.id, package_id=pk.id, title=pk.title,
            admin_username=username, data_gb=pk.data_gb, max_users=pk.max_users,
            expires_at=datetime.utcnow() + timedelta(days=int(pk.days or 30)), status="active",
            price_toman=price, tracking_code=trk)
        s.add(acc)
        await s.commit()
        acc_id = acc.id
    if bot is not None and row is not None:         # اطلاعِ خرید به مالک/کانالِ اعتمادسازی/کانالِ لاگ
        try:
            from tenant import _purchase_alert
            async with Session() as s:
                buyer = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == buyer_tg))
            await _purchase_alert(bot, row, buyer=buyer, section="پنل‌فروشی", detail=pk.title, amount=price,
                                  tracking_code=trk)
        except Exception:      # noqa: BLE001
            logger.exception("panel_seller: اعلانِ خرید ناموفق بود")
    return {"status": "ok", "account_id": acc_id, "username": username, "password": password,
            "panel_url": panel.base_url, "tracking": trk}


async def renew(bot_id: int, account_id: int, buyer_tg: int, api_factory=None) -> dict:
    """تمدید با کیف‌پول (همان بسته). اگر تعلیق‌شده بود فعال می‌شود و رمزِ تازه می‌گیرد."""
    api_factory = api_factory or _api
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id)
        if not acc or acc.bot_id != bot_id or acc.buyer_tg != buyer_tg or acc.status == "deleted":
            return {"status": "not_found"}
        pk = await s.get(PanelSellerPackage, acc.package_id) if acc.package_id else None
        panel = await s.get(VpnPanel, acc.panel_id)
    if not pk or not pk.active:
        return {"status": "no_package"}
    if not panel:
        return {"status": "no_panel"}
    price = int(pk.price_toman or 0)
    async with Session() as s:
        if price > 0:
            u, bal = await _wallet_debit(s, bot_id, buyer_tg, price, f"تمدیدِ پنل: {pk.title}")
            if u is None:
                return {"status": "insufficient", "need": price, "balance": bal}
        await s.commit()
    new_pw = None
    try:
        api = api_factory(panel)
        try:
            if acc.status == "suspended":
                new_pw = gen_password()
                await api.set_password(acc.admin_username, new_pw)
                await api.activate_users(acc.admin_username)
            try:
                await api.c._request("POST", f"/api/admin/usage/reset/{acc.admin_username}")
            except Exception:       # noqa: BLE001 — ریستِ مصرف اختیاری است
                logger.info("panel_seller: ریستِ مصرفِ ادمین ممکن نشد (بی‌اهمیت)")
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("panel_seller: تمدید ناموفق (acc=%s): %s", account_id, e)
        if price > 0:
            await _refund(bot_id, buyer_tg, price, "بازگشتِ وجهِ تمدیدِ پنل (خطایِ پنل)")
        return {"status": "panel_failed", "error": str(e)[:300]}
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id, with_for_update=True)
        base = max(acc.expires_at or datetime.utcnow(), datetime.utcnow())
        acc.expires_at = base + timedelta(days=int(pk.days or 30))
        acc.data_gb, acc.max_users = pk.data_gb, pk.max_users
        acc.status, acc.suspend_reason, acc.flags = "active", None, None
        acc.price_toman = int(acc.price_toman or 0) + price
        await s.commit()
        exp = acc.expires_at
    return {"status": "ok", "expires_at": exp, "new_password": new_pw, "username": acc.admin_username}


# === PATCH_PSEL_EXTRA_VOLUME_V1 === خریدِ حجمِ اضافه (درخواستِ Whale Support): سقفِ حجمِ همین دوره بالا می‌رود؛ با تمدید به حجمِ بسته برمی‌گردد.
EXTRA_GB_MAX = 1000


async def extra_gb_price(bot_id: int) -> int:
    """قیمتِ هر گیگ حجمِ اضافه (تومان)؛ ۰ = خاموش."""
    from tenant import get_setting
    try:
        return max(0, int(float((await get_setting(bot_id, "psel_extra_gb_price", "0")) or 0)))
    except (TypeError, ValueError):
        return 0


async def add_volume(bot_id: int, account_id: int, buyer_tg: int, gb: int, api_factory=None, bot=None, row=None) -> dict:
    """status: ok | disabled | bad_amount | not_found | unlimited | insufficient | panel_failed (+ new_gb / new_password)."""
    api_factory = api_factory or _api
    unit = await extra_gb_price(bot_id)
    if unit <= 0:
        return {"status": "disabled"}
    if not (1 <= int(gb) <= EXTRA_GB_MAX):
        return {"status": "bad_amount"}
    gb = int(gb)
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id)
        if not acc or acc.bot_id != bot_id or acc.buyer_tg != buyer_tg or acc.status not in ("active", "suspended"):
            return {"status": "not_found"}
        if not acc.data_gb:
            return {"status": "unlimited"}               # حجمِ نامحدود نیازی به افزودن ندارد
        panel = await s.get(VpnPanel, acc.panel_id)
        was_traffic_suspended = acc.status == "suspended" and acc.suspend_reason == "traffic"
        if acc.status == "suspended" and not was_traffic_suspended:
            return {"status": "not_found"}                # تعلیقِ انقضا/مدیر با حجم باز نمی‌شود
    price = unit * gb
    async with Session() as s:
        u, bal = await _wallet_debit(s, bot_id, buyer_tg, price, f"حجمِ اضافه {gb}G — پنل {acc.admin_username}")
        if u is None:
            return {"status": "insufficient", "need": price, "balance": bal}
        await s.commit()
    new_pw = None
    if was_traffic_suspended:
        if not panel:
            await _refund(bot_id, buyer_tg, price, "بازگشتِ وجهِ حجمِ اضافه (پنل در دسترس نیست)")
            return {"status": "panel_failed", "error": "panel missing"}
        try:
            api = api_factory(panel)
            try:
                new_pw = gen_password()
                await api.set_password(acc.admin_username, new_pw)
                await api.activate_users(acc.admin_username)
            finally:
                await api.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("panel_seller: بازکردنِ تعلیقِ حجم ناموفق (acc=%s): %s", account_id, e)
            await _refund(bot_id, buyer_tg, price, "بازگشتِ وجهِ حجمِ اضافه (خطایِ پنل)")
            return {"status": "panel_failed", "error": str(e)[:300]}
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id, with_for_update=True)
        acc.data_gb = int(acc.data_gb or 0) + gb
        acc.price_toman = int(acc.price_toman or 0) + price
        fl = _flags(acc)
        fl.discard("tr80")                               # هشدارِ ۸۰٪ دوباره قابلِ ارسال می‌شود
        _set_flags(acc, fl)
        if was_traffic_suspended:
            acc.status, acc.suspend_reason = "active", None
        await s.commit()
        new_gb, uname = acc.data_gb, acc.admin_username
    if bot is not None and row is not None:
        try:
            from tenant import _purchase_alert
            async with Session() as s:
                buyer = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == buyer_tg))
            await _purchase_alert(bot, row, buyer=buyer, section="پنل‌فروشی", detail=f"حجمِ اضافه {gb} گیگ — {uname}",
                                  amount=price, tracking_code=None)
        except Exception:      # noqa: BLE001
            logger.exception("panel_seller: اعلانِ خریدِ حجمِ اضافه ناموفق بود")
    return {"status": "ok", "new_gb": new_gb, "price": price, "new_password": new_pw, "username": uname}


async def change_password(bot_id: int, account_id: int, buyer_tg: int, api_factory=None) -> dict:
    api_factory = api_factory or _api
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id)
        if not acc or acc.bot_id != bot_id or acc.buyer_tg != buyer_tg or acc.status != "active":
            return {"status": "not_found"}
        panel = await s.get(VpnPanel, acc.panel_id)
    if not panel:
        return {"status": "no_panel"}
    pw = gen_password()
    try:
        api = api_factory(panel)
        try:
            await api.set_password(acc.admin_username, pw)
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        return {"status": "panel_failed", "error": str(e)[:300]}
    return {"status": "ok", "password": pw, "username": acc.admin_username}


async def set_users_enabled(bot_id: int, account_id: int, buyer_tg: int | None, enable: bool, api_factory=None) -> dict:
    """خاموش/روشن‌کردنِ همهٔ اشتراک‌هایِ ادمین. buyer_tg=None ⇒ مالکِ ربات."""
    api_factory = api_factory or _api
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id)
        if not acc or acc.bot_id != bot_id or (buyer_tg is not None and acc.buyer_tg != buyer_tg):
            return {"status": "not_found"}
        if acc.status != "active":
            return {"status": "inactive"}
        panel = await s.get(VpnPanel, acc.panel_id)
    if not panel:
        return {"status": "no_panel"}
    try:
        api = api_factory(panel)
        try:
            await (api.activate_users if enable else api.disable_users)(acc.admin_username)
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        return {"status": "panel_failed", "error": str(e)[:300]}
    return {"status": "ok"}


async def suspend(account_id: int, reason: str, api_factory=None) -> bool:
    """تعلیق: اشتراک‌هایِ ادمین خاموش و رمزِ ادمین تصادفی می‌شود (ورودِ مشتری بسته)."""
    api_factory = api_factory or _api
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id)
        if not acc or acc.status != "active":
            return False
        panel = await s.get(VpnPanel, acc.panel_id)
        uname = acc.admin_username
    if not panel:
        return False
    try:
        api = api_factory(panel)
        try:
            await api.disable_users(uname)
            await api.set_password(uname, gen_password())
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("panel_seller: تعلیق ناموفق (acc=%s): %s", account_id, e)
        return False
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id, with_for_update=True)
        acc.status, acc.suspend_reason = "suspended", reason
        await s.commit()
    return True


async def resume(bot_id: int, account_id: int, api_factory=None) -> dict:
    """فعال‌سازیِ دستیِ ادمینِ تعلیق‌شده (مالک): رمزِ تازه + روشن‌کردنِ اشتراک‌ها."""
    api_factory = api_factory or _api
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id)
        if not acc or acc.bot_id != bot_id or acc.status != "suspended":
            return {"status": "not_found"}
        panel = await s.get(VpnPanel, acc.panel_id)
        uname = acc.admin_username
    if not panel:
        return {"status": "no_panel"}
    pw = gen_password()
    try:
        api = api_factory(panel)
        try:
            await api.set_password(uname, pw)
            await api.activate_users(uname)
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        return {"status": "panel_failed", "error": str(e)[:300]}
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id, with_for_update=True)
        acc.status, acc.suspend_reason, acc.flags = "active", None, None
        await s.commit()
    return {"status": "ok", "password": pw, "username": uname, "buyer_tg": acc.buyer_tg}


async def delete_account(bot_id: int, account_id: int, api_factory=None) -> bool:
    api_factory = api_factory or _api
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id)
        if not acc or acc.bot_id != bot_id:
            return False
        panel = await s.get(VpnPanel, acc.panel_id)
        uname = acc.admin_username
    if panel:
        try:
            api = api_factory(panel)
            try:
                await api.delete_admin(uname)
            finally:
                await api.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("panel_seller: حذفِ ادمین از پنل ناموفق (acc=%s): %s", account_id, e)
            return False
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id, with_for_update=True)
        acc.status = "deleted"
        await s.commit()
    return True


def _fa_days(left: timedelta) -> int:
    return max(0, int(left.total_seconds() // 86400))


async def enforce_once(bot_id: int, api_factory=None, now: datetime | None = None) -> list[tuple[int, str]]:
    """یک دورِ بررسیِ ادمین‌هایِ فعال: انقضا/حجم ⇒ تعلیق، هشدارِ نزدیکِ پایان. خروجی: [(telegram_id, متن)]؛
    telegram_id=0 یعنی پیام برایِ مالکِ ربات."""
    api_factory = api_factory or _api
    now = now or datetime.utcnow()
    out: list[tuple[int, str]] = []
    async with Session() as s:
        accs = (await s.scalars(select(PanelSellerAccount).where(
            PanelSellerAccount.bot_id == bot_id, PanelSellerAccount.status == "active"))).all()
        panels = {p.id: p for p in (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == bot_id))).all()}
    for a in accs:
        panel = panels.get(a.panel_id)
        if not panel:
            continue
        label = f"«{a.title}» ({a.admin_username})"
        if a.expires_at and a.expires_at <= now:
            if await suspend(a.id, "expired", api_factory):
                out.append((a.buyer_tg, f"⏰ مهلتِ پنلِ {label} تمام شد و اشتراک‌ها خاموش شد. برای ادامه از «🗂 پنل‌های من» تمدید کن."))
                out.append((0, f"⏰ پنلِ {label} (خریدار {a.buyer_tg}) منقضی و خاموش شد."))
            continue
        fl = _flags(a)
        used = users = None
        if a.data_gb or a.max_users:
            try:
                api = api_factory(panel)
                try:
                    if a.data_gb:
                        used = await api.usage_bytes(a.admin_username)
                    if a.max_users:
                        users = await api.user_count(a.admin_username)
                finally:
                    await api.close()
            except Exception as e:  # noqa: BLE001
                logger.info("panel_seller: خواندنِ مصرف ناموفق (acc=%s): %s", a.id, e)
        if used is not None and a.data_gb:
            limit = a.data_gb * GB
            if used >= limit:
                if await suspend(a.id, "traffic", api_factory):
                    out.append((a.buyer_tg, f"📊 حجمِ پنلِ {label} تمام شد و اشتراک‌ها خاموش شد. برای ادامه تمدید کن."))
                    out.append((0, f"📊 حجمِ پنلِ {label} (خریدار {a.buyer_tg}) تمام شد و خاموش شد."))
                continue
            if used >= limit * WARN_TRAFFIC and "tr80" not in fl:
                fl.add("tr80")
                out.append((a.buyer_tg, f"⚠️ بیش از ۸۰٪ حجمِ پنلِ {label} مصرف شده."))
        if users is not None and a.max_users and users > a.max_users and "usr" not in fl:
            fl.add("usr")
            out.append((a.buyer_tg, f"⚠️ تعدادِ کاربرانِ پنلِ {label} ({users}) از سقفِ بسته ({a.max_users}) بیشتر است."))
            out.append((0, f"⚠️ پنلِ {label} (خریدار {a.buyer_tg}) از سقفِ کاربر گذشت: {users}/{a.max_users}."))
        if a.expires_at and (a.expires_at - now) <= timedelta(days=WARN_DAYS) and "exp3" not in fl:
            fl.add("exp3")
            out.append((a.buyer_tg, f"⏳ فقط {_fa_days(a.expires_at - now)} روز تا پایانِ پنلِ {label} مانده؛ تمدید کن."))
        if fl != _flags(a):
            async with Session() as s:
                row = await s.get(PanelSellerAccount, a.id, with_for_update=True)
                if row:
                    _set_flags(row, fl)
                    await s.commit()
    return out
