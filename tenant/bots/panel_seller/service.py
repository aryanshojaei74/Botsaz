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
from models import BotUser, PanelSellerAccount, PanelSellerPackage, PanelSellerSecret, VpnPanel, WalletTxn

from tenant.bots.panel_seller import adapter as AD
from tenant.shared.panel_clients.vpn_panel import PanelError

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

GB = 1024 ** 3
WARN_DAYS = 3
WARN_TRAFFIC = 0.8


def gen_username() -> str:
    return "ps" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))


_PW_SPECIAL = "!@#$%^&*-_=+"          # بدونِ " و بدونِ نمادهایِ مشکل‌ساز؛ همهٔ پنل‌ها می‌پذیرند


def gen_password() -> str:
    """PATCH_PSEL_STRONG_PASSWORD_V1 — پاسارگاردِ جدید: ≥۱۲ نویسه، ≥۲ رقم، ≥۲ بزرگ، ≥۲ کوچک، ≥۱ نمادِ خاص (۴۲۲ می‌داد)."""
    chars = ([secrets.choice(string.ascii_uppercase) for _ in range(3)]
             + [secrets.choice(string.ascii_lowercase) for _ in range(5)]
             + [secrets.choice(string.digits) for _ in range(4)]
             + [secrets.choice(_PW_SPECIAL) for _ in range(2)])
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


async def role_for(api, panel) -> int:
    """نقشِ ادمینِ فروخته‌شده در پاسارگاردِ جدید: انتخابِ مالک (psel_role_<panel_id>)، وگرنه اگر فقط یک نقشِ غیرِمالک هست همان.
    چند نقش ⇒ حدس نمی‌زنیم (ممکن است نقشِ پرسطح‌دسترسی باشد) و از مالک می‌خواهیم انتخاب کند."""
    from tenant import get_setting
    try:
        saved = int((await get_setting(panel.bot_id, f"psel_role_{panel.id}", "")) or 0)
    except (TypeError, ValueError):
        saved = 0
    roles = await api.list_roles()
    if saved and any(int(r["id"]) == saved for r in roles):
        return saved
    cand = [r for r in roles if not r.get("is_owner")]
    if len(cand) == 1:
        return int(cand[0]["id"])
    names = "، ".join(f"{r.get('name')}#{r['id']}" for r in cand) or "هیچ"
    raise PanelError("نقشِ ادمین در این پنل مشخص نیست؛ از «🛡 نقشِ ادمینِ پنل» در تنظیماتِ پنل‌فروشی یکی را انتخاب کن. "
                     f"نقش‌هایِ موجود: {names}")


GB_BYTES = 1024 ** 3


# === PATCH_PSEL_PASSWORD_VAULT_V1 === رمزِ پنلِ مشتری رمزنگاری‌شده نگه داشته می‌شود تا «🔑 نمایش رمز» داشته باشیم.
# بدونِ کلید (BOTSAZ_SECRET_KEY / PLATFORM_BOT_TOKEN) هرگز متنِ ساده ذخیره نمی‌شود.
async def save_secret(account_id: int, password: str | None) -> bool:
    """password=None ⇒ حذف (رمز تصادفی/ناشناخته شد)."""
    try:
        async with Session() as s:
            row = await s.scalar(select(PanelSellerSecret).where(PanelSellerSecret.account_id == account_id))
            if password is None:
                if row is not None:
                    await s.delete(row)
                    await s.commit()
                return True
            import secret_box
            if secret_box._fernet() is None:
                return False
            enc = secret_box.encrypt(password)
            if row is None:
                s.add(PanelSellerSecret(account_id=account_id, pw=enc))
            else:
                row.pw, row.updated_at = enc, datetime.utcnow()
            await s.commit()
            return True
    except Exception:      # noqa: BLE001
        logger.exception("panel_seller: ذخیرهٔ رمزِ رمزنگاری‌شده ناموفق بود (acc=%s)", account_id)
        return False


async def get_secret(account_id: int) -> str | None:
    try:
        async with Session() as s:
            row = await s.scalar(select(PanelSellerSecret).where(PanelSellerSecret.account_id == account_id))
        if row is None:
            return None
        import secret_box
        val = secret_box.decrypt(row.pw)
        return None if (val or "").startswith(secret_box.PREFIX) else val          # کلید عوض شده ⇒ خوانده نشد
    except Exception:      # noqa: BLE001
        logger.exception("panel_seller: خواندنِ رمزِ ذخیره‌شده ناموفق بود (acc=%s)", account_id)
        return None


async def keep_login_on_traffic(bot_id: int) -> bool:
    """پایانِ حجم: فقط کاربرانِ پنل خاموش شوند و ورودِ خودِ مشتری (رمز) سرِ جایش بماند تا بتواند مصرفِ هر کاربر را ببیند."""
    from tenant import get_setting
    return (await get_setting(bot_id, "psel_keep_login", "1")) == "1"


async def create_admin_on(api, panel, username: str, password: str, data_gb: int = 0) -> None:
    """ساختِ ادمین؛ اگر پنل «role_id» خواست (پاسارگاردِ جدید) نقش را پیدا و یک‌بار دوباره (همراهِ سقفِ حجمِ بومی) امتحان می‌کند."""
    try:
        await api.create_admin(username, password)
    except PanelError as e:
        if "role_id" not in str(e):
            raise
        await api.create_admin(username, password, role_id=await role_for(api, panel),
                               data_limit_bytes=(int(data_gb) * GB_BYTES if data_gb and int(data_gb) > 0 else None))


async def apply_native_limit(api, username: str, gb: int) -> None:
    """بهترین‌تلاش: سقفِ حجم را روی خودِ پاسارگاردِ جدید هم بگذار (کنترلِ بومی؛ کنترلِ سمتِ ربات هم می‌ماند). خطا خرید را نمی‌شکند."""
    try:
        await api.apply_data_limit(username, int(gb or 0))
    except Exception as e:      # noqa: BLE001
        logger.warning("panel_seller: اعمالِ سقفِ حجمِ بومی ناموفق بود (%s): %s", username, e)


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


USERNAME_RE = r"^[A-Za-z][A-Za-z0-9_]{2,31}$"


def valid_username(u: str) -> bool:
    import re
    return bool(re.match(USERNAME_RE, u or ""))


async def username_taken(bot_id: int, username: str) -> bool:
    """نامِ ادمین در هیچ پنلِ همین ربات (به‌جز حذف‌شده‌ها) نباید تکراری باشد (مقایسه بدونِ حساسیت به حروف)."""
    from sqlalchemy import func
    async with Session() as s:
        n = await s.scalar(select(func.count(PanelSellerAccount.id)).where(
            PanelSellerAccount.bot_id == bot_id, PanelSellerAccount.status != "deleted",
            func.lower(PanelSellerAccount.admin_username) == (username or "").lower()))
    return bool(n)


async def pick_username_enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, "psel_pick_username", "0")) == "1"


async def quote_discount(bot_id: int, buyer_tg: int, code: str, *, package_id: int | None = None, gb: int | None = None) -> dict:
    """PATCH_PSEL_DISCOUNT_V1 — پیش‌نمایشِ کد تخفیف (بدونِ ثبتِ مصرف). خروجی: {ok, reason, price, final, discount, label}."""
    from tenant.gateways.discount_engine import validate_discount
    panel_key = None
    async with Session() as s:
        if gb is not None:
            cfg = await custom_settings(bot_id)
            if cfg["price"] <= 0:
                return {"ok": False, "reason": "این بخش فعال نیست.", "price": 0, "final": 0, "discount": 0, "label": ""}
            price = int(gb) * cfg["price"]
            panels = (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == bot_id, VpnPanel.enabled == True)   # noqa: E712
                                      .order_by(VpnPanel.id))).all()
            pn = next((p for p in panels if AD.supports(p.panel_key)), None)
        else:
            pk = await s.get(PanelSellerPackage, int(package_id or 0))
            if not pk or pk.bot_id != bot_id or not pk.active:
                return {"ok": False, "reason": "این بسته دیگر موجود نیست.", "price": 0, "final": 0, "discount": 0, "label": ""}
            price = int(pk.price_toman or 0)
            pn = await s.get(VpnPanel, pk.panel_id)
        panel_key = pn.panel_key if pn else None
    d = await validate_discount(bot_id=bot_id, code=code, telegram_id=buyer_tg, price=price, panel_key=panel_key)
    return {"ok": d.ok, "reason": d.reason, "price": price, "final": d.final_price if d.ok else price,
            "discount": d.discount if d.ok else 0, "label": d.label}


async def buy(bot_id: int, buyer_tg: int, package_id: int, api_factory=None, bot=None, row=None,
              username: str | None = None, pk_override=None, discount_code: str | None = None) -> dict:
    """status: ok | no_package | no_panel | unsupported | insufficient | panel_failed | bad_username | username_taken | bad_discount
    (+ account/username/password/…). username: نامِ دلخواهِ مشتری (اختیاری)؛ pk_override: بستهٔ موقت (فروشِ حجمیِ دلخواه)؛
    discount_code: کد تخفیف (اعتبارسنجیِ دوباره + ثبتِ مصرف در همان تراکنشِ کسرِ کیف‌پول؛ با خطای پنل مصرف برمی‌گردد)."""
    api_factory = api_factory or _api
    if username is not None:
        if not valid_username(username):
            return {"status": "bad_username"}
        if await username_taken(bot_id, username):
            return {"status": "username_taken"}
    async with Session() as s:
        if pk_override is not None:
            pk = pk_override
        else:
            pk = await s.get(PanelSellerPackage, package_id)
            if not pk or pk.bot_id != bot_id or not pk.active:
                return {"status": "no_package"}
        panel = await s.get(VpnPanel, pk.panel_id)
    if not panel or panel.bot_id != bot_id or not panel.enabled:
        return {"status": "no_panel"}
    if not AD.supports(panel.panel_key):
        return {"status": "unsupported"}
    price = int(pk.price_toman or 0)
    disc = None
    async with Session() as s:
        if discount_code:                                           # PATCH_PSEL_DISCOUNT_V1
            from tenant.gateways.discount_engine import record_usage, validate_discount
            disc = await validate_discount(bot_id=bot_id, code=discount_code, telegram_id=buyer_tg, price=price,
                                           panel_key=panel.panel_key, session=s)
            if not disc.ok:
                return {"status": "bad_discount", "reason": disc.reason}
            price = int(disc.final_price)
        if price > 0:
            u, bal = await _wallet_debit(s, bot_id, buyer_tg, price, f"خریدِ پنل: {pk.title}")
            if u is None:
                return {"status": "insufficient", "need": price, "balance": bal}
        if disc is not None:
            await record_usage(s, code_id=disc.code_id, bot_id=bot_id, telegram_id=buyer_tg, saved_amount=int(disc.discount))
        await s.commit()
    username, password = (username or gen_username()), gen_password()
    try:
        api = api_factory(panel)
        try:
            await create_admin_on(api, panel, username, password, data_gb=int(pk.data_gb or 0))
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("panel_seller: ساختِ ادمین ناموفق (bot=%s): %s", bot_id, e)
        if price > 0:
            await _refund(bot_id, buyer_tg, price, "بازگشتِ وجهِ خریدِ پنل (خطایِ پنل)")
        if disc is not None:
            from tenant.gateways.discount_engine import revert_usage
            async with Session() as s:
                await revert_usage(s, code_id=disc.code_id, telegram_id=buyer_tg)
                await s.commit()
        return {"status": "panel_failed", "error": str(e)[:300]}
    from models import alloc_tracking_in
    async with Session() as s:
        trk = await alloc_tracking_in(s, bot_id)
        acc = PanelSellerAccount(
            bot_id=bot_id, buyer_tg=buyer_tg, panel_id=panel.id, package_id=(getattr(pk, "id", None) if pk_override is None else None),
            title=pk.title, admin_username=username, data_gb=pk.data_gb, max_users=pk.max_users,
            expires_at=(datetime.utcnow() + timedelta(days=int(pk.days)) if int(pk.days or 0) > 0 else None), status="active",   # ۰ = نامحدود
            price_toman=price, tracking_code=trk)
        s.add(acc)
        await s.commit()
        acc_id = acc.id
    await save_secret(acc_id, password)               # PATCH_PSEL_PASSWORD_VAULT_V1
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
            "panel_url": panel.base_url, "tracking": trk, "discount": int(disc.discount) if disc is not None else 0, "paid": price}


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
                if acc.suspend_reason == "traffic" and await get_secret(acc.id) is not None:
                    await api.activate_users(acc.admin_username)        # رمز دست‌نخورده بود (ورودِ مشتری باز مانده بود)
                else:
                    new_pw = gen_password()
                    await api.set_password(acc.admin_username, new_pw)
                    await api.activate_users(acc.admin_username)
            try:
                await api.reset_usage(acc.admin_username)
            except Exception:       # noqa: BLE001 — ریستِ مصرف اختیاری است
                logger.info("panel_seller: ریستِ مصرفِ ادمین ممکن نشد (بی‌اهمیت)")
            await apply_native_limit(api, acc.admin_username, int(pk.data_gb or 0))
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("panel_seller: تمدید ناموفق (acc=%s): %s", account_id, e)
        if price > 0:
            await _refund(bot_id, buyer_tg, price, "بازگشتِ وجهِ تمدیدِ پنل (خطایِ پنل)")
        return {"status": "panel_failed", "error": str(e)[:300]}
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id, with_for_update=True)
        if int(pk.days or 0) > 0:
            base = max(acc.expires_at or datetime.utcnow(), datetime.utcnow())
            acc.expires_at = base + timedelta(days=int(pk.days))
        else:
            acc.expires_at = None                        # بستهٔ نامحدود: بدونِ انقضا
        acc.data_gb, acc.max_users = pk.data_gb, pk.max_users
        acc.status, acc.suspend_reason, acc.flags = "active", None, ("nt0" if "nt0" in _flags(acc) else None)
        acc.price_toman = int(acc.price_toman or 0) + price
        await s.commit()
        exp = acc.expires_at
    if new_pw:
        await save_secret(account_id, new_pw)
    return {"status": "ok", "expires_at": exp, "new_password": new_pw, "username": acc.admin_username}


# === PATCH_PSEL_CUSTOM_VOLUME_V1 === (Whale Support) «فروشِ حجمیِ دلخواه»: مشتری گیگ را خودش می‌نویسد (با حداقل)، زمانِ نامحدود
async def custom_settings(bot_id: int) -> dict:
    from tenant import get_setting

    async def _i(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(float((await get_setting(bot_id, key, str(default))) or default))))
        except (TypeError, ValueError):
            return default
    return {"price": await _i("psel_custom_price_gb", 0, 0, 100_000_000),      # ۰ = خاموش
            "min_gb": await _i("psel_custom_min_gb", 10, 1, 100000),
            "max_gb": await _i("psel_custom_max_gb", 1000, 1, 100000),
            "users": await _i("psel_custom_users", 0, 0, 100000),               # ۰ = نامحدود
            "days": await _i("psel_custom_days", 0, 0, 3650)}                   # ۰ = نامحدود (بدونِ انقضا)


async def buy_custom(bot_id: int, buyer_tg: int, gb: int, api_factory=None, bot=None, row=None,
                     username: str | None = None, discount_code: str | None = None) -> dict:
    """status مثلِ buy + disabled | bad_amount."""
    cfg = await custom_settings(bot_id)
    if cfg["price"] <= 0:
        return {"status": "disabled"}
    if not (cfg["min_gb"] <= int(gb) <= max(cfg["min_gb"], cfg["max_gb"])):
        return {"status": "bad_amount", "min_gb": cfg["min_gb"], "max_gb": cfg["max_gb"]}
    async with Session() as s:
        panels = (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == bot_id, VpnPanel.enabled == True)   # noqa: E712
                                  .order_by(VpnPanel.id))).all()
    panel = next((p for p in panels if AD.supports(p.panel_key)), None)
    if panel is None:
        return {"status": "no_panel" if not panels else "unsupported"}
    from types import SimpleNamespace
    gb = int(gb)
    pk = SimpleNamespace(id=None, panel_id=panel.id, title=f"📦 حجمِ دلخواه {gb} گیگ", data_gb=gb, max_users=cfg["users"],
                         days=cfg["days"], price_toman=gb * cfg["price"], active=True)
    return await buy(bot_id, buyer_tg, 0, api_factory=api_factory, bot=bot, row=row, username=username, pk_override=pk,
                     discount_code=discount_code)


# === PATCH_PSEL_FREE_TEST_V1 === (Whale Support) «تستِ رایگان»: هر مشتری یک بار، با مشخصاتِ کوچکِ تعیین‌شده توسطِ مالک
TEST_TITLE = "🧪 تستِ رایگان"


async def test_settings(bot_id: int) -> dict:
    from tenant import get_setting

    async def _i(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(float((await get_setting(bot_id, key, str(default))) or default))))
        except (TypeError, ValueError):
            return default
    return {"enabled": (await get_setting(bot_id, "psel_test_enabled", "0")) == "1",
            "hours": await _i("psel_test_hours", 24, 1, 24 * 30),
            "gb": await _i("psel_test_gb", 1, 1, 1000),
            "users": await _i("psel_test_users", 3, 0, 10000)}


async def test_available(bot_id: int, buyer_tg: int) -> bool:
    """تست روشن است و این مشتری هنوز تست نگرفته (حتی اگر پنلِ تستش حذف شده باشد)."""
    if not (await test_settings(bot_id))["enabled"]:
        return False
    async with Session() as s:
        used = await s.scalar(select(PanelSellerAccount.id).where(
            PanelSellerAccount.bot_id == bot_id, PanelSellerAccount.buyer_tg == buyer_tg,
            PanelSellerAccount.title == TEST_TITLE).limit(1))
    return used is None


# === PATCH_PSEL_TEST_NOTIFY_V1 === (Whale) «برایِ نفراتی که تست می‌گیرن: پیام به ادمین یا کانالِ لاگ که چه کسی چه حجمی گرفته»
async def test_notify_settings(bot_id: int) -> dict:
    from tenant import get_setting
    return {"admins": (await get_setting(bot_id, "psel_test_notify", "0")) == "1",
            "channel": ((await get_setting(bot_id, "psel_test_log_channel", "")) or "").strip()}


def parse_channel(raw: str) -> str | None:
    """«@name» / «t.me/name» / «https://t.me/name» / «-100…» ⇒ مقدارِ قابلِ‌استفاده برایِ send_message؛ نامعتبر ⇒ None."""
    import re
    v = (raw or "").strip()
    v = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", v).strip("/ ")
    if re.fullmatch(r"-100\d{5,15}", v):
        return v
    v = v.lstrip("@")
    return f"@{v}" if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", v) else None


def test_notice_text(buyer, username: str, hours: int, gb: int, users: int, tracking, ident_mode: str = "both") -> str:
    """ident_mode (همان «نمایشِ مشتری» رباتِ VPN): both | id | username — رمزِ پنل هرگز در اعلان نمی‌آید."""
    import html as _h
    from tenant.shared.buyer_ident import normalize
    mode = normalize(ident_mode)
    name = _h.escape(getattr(buyer, "full_name", "") or "—")
    uname = getattr(buyer, "username", None)
    tg = getattr(buyer, "telegram_id", "—")
    if mode == "id":
        who = f"🆔 <code>{tg}</code>"
    elif mode == "username":
        who = f"👤 {name} (@{_h.escape(uname)})" if uname else f"🆔 <code>{tg}</code>"      # یوزرنیم نداشت ⇒ آیدی
    else:
        who = f"👤 {name}" + (f" (@{_h.escape(uname)})" if uname else "") + f"\n🆔 <code>{tg}</code>"
    return ("🧪 <b>تستِ رایگانِ جدید</b>\n\n" + who + "\n\n"
            f"📊 حجم: <b>{gb} گیگ</b>\n⏳ مدت: <b>{hours} ساعت</b>\n👥 حداکثرِ کاربر: <b>{users if users else 'نامحدود'}</b>\n"
            f"🔑 نامِ کاربریِ پنل: <code>{_h.escape(username)}</code>\n🔖 کدِ پیگیری: <code>{_h.escape(str(tracking or '—'))}</code>")


async def notify_test_taken(bot, row, buyer, username: str, hours: int, gb: int, users: int, tracking) -> bool:
    """اگر مالک روشن کرده باشد: پیام به ادمین‌هایِ ربات و/یا کانالِ لاگ. خروجی: True اگر اعلانِ اختصاصی فعال بود (⇒ اعلانِ عمومیِ خرید تکراری نشود)."""
    cfg = await test_notify_settings(row.id)
    if not cfg["admins"] and not cfg["channel"]:
        return False
    from tenant.shared.buyer_ident import get_mode
    text = test_notice_text(buyer, username, hours, gb, users, tracking, ident_mode=await get_mode(row.id))
    if cfg["admins"]:
        try:
            from tenant import _notify_admins
            await _notify_admins(bot, row, text)
        except Exception:      # noqa: BLE001
            logger.exception("panel_seller: اعلانِ تست به ادمین‌ها ناموفق بود")
    if cfg["channel"]:
        try:
            await bot.send_message(parse_channel(cfg["channel"]) or cfg["channel"], text, parse_mode="HTML")
        except Exception as e:      # noqa: BLE001
            logger.warning("panel_seller: ارسال به کانالِ لاگِ تست ناموفق بود (%s): %s", cfg["channel"], e)
    return True


async def buy_test(bot_id: int, buyer_tg: int, api_factory=None, bot=None, row=None) -> dict:
    """status: ok | disabled | test_used | no_panel | unsupported | panel_failed."""
    api_factory = api_factory or _api
    cfg = await test_settings(bot_id)
    if not cfg["enabled"]:
        return {"status": "disabled"}
    if not await test_available(bot_id, buyer_tg):
        return {"status": "test_used"}
    async with Session() as s:
        panels = (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == bot_id, VpnPanel.enabled == True)   # noqa: E712
                                  .order_by(VpnPanel.id))).all()
    panel = next((p for p in panels if AD.supports(p.panel_key)), None)
    if panel is None:
        return {"status": "no_panel" if not panels else "unsupported"}
    username, password = gen_username(), gen_password()
    try:
        api = api_factory(panel)
        try:
            await create_admin_on(api, panel, username, password, data_gb=int(cfg["gb"]))
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("panel_seller: ساختِ ادمینِ تست ناموفق (bot=%s): %s", bot_id, e)
        return {"status": "panel_failed", "error": str(e)[:300]}
    from models import alloc_tracking_in
    async with Session() as s:
        trk = await alloc_tracking_in(s, bot_id)
        acc = PanelSellerAccount(
            bot_id=bot_id, buyer_tg=buyer_tg, panel_id=panel.id, package_id=None, title=TEST_TITLE,
            admin_username=username, data_gb=cfg["gb"], max_users=cfg["users"],
            expires_at=datetime.utcnow() + timedelta(hours=cfg["hours"]), status="active", price_toman=0, tracking_code=trk)
        s.add(acc)
        await s.commit()
        acc_id = acc.id
    await save_secret(acc_id, password)               # PATCH_PSEL_PASSWORD_VAULT_V1
    if bot is not None and row is not None:
        try:
            async with Session() as s:
                buyer = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == buyer_tg))
            custom = await notify_test_taken(bot, row, buyer, username, cfg["hours"], cfg["gb"], cfg["users"], trk)
            if not custom:                                  # اعلانِ اختصاصیِ تست خاموش است ⇒ همان اعلانِ عمومیِ خرید
                from tenant import _purchase_alert
                await _purchase_alert(bot, row, buyer=buyer, section="پنل‌فروشی", detail="تستِ رایگان", amount=0, tracking_code=trk)
        except Exception:      # noqa: BLE001
            logger.exception("panel_seller: اعلانِ تستِ رایگان ناموفق بود")
    return {"status": "ok", "account_id": acc_id, "username": username, "password": password,
            "panel_url": panel.base_url, "tracking": trk, "hours": cfg["hours"], "gb": cfg["gb"], "users": cfg["users"]}


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
                if await get_secret(acc.id) is not None:                 # ورودِ مشتری باز مانده بود ⇒ فقط کاربران روشن شوند
                    await api.activate_users(acc.admin_username)
                else:
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
    if panel is not None:                                   # PATCH_PSEL_NATIVE_LIMIT_V1 — سقفِ بومیِ پنلِ جدید هم بالا برود
        try:
            api = api_factory(panel)
            try:
                await apply_native_limit(api, uname, int(new_gb))
            finally:
                await api.close()
        except Exception as e:      # noqa: BLE001
            logger.warning("panel_seller: به‌روزرسانیِ سقفِ بومیِ حجمِ اضافه ناموفق (acc=%s): %s", account_id, e)
    if bot is not None and row is not None:
        try:
            from tenant import _purchase_alert
            async with Session() as s:
                buyer = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == buyer_tg))
            await _purchase_alert(bot, row, buyer=buyer, section="پنل‌فروشی", detail=f"حجمِ اضافه {gb} گیگ — {uname}",
                                  amount=price, tracking_code=None)
        except Exception:      # noqa: BLE001
            logger.exception("panel_seller: اعلانِ خریدِ حجمِ اضافه ناموفق بود")
    if new_pw:
        await save_secret(account_id, new_pw)
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
    await save_secret(account_id, pw)
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


# === PATCH_PSEL_ADMIN_LOOKUP_V1 === (Whale Support) مدیر با نام‌کاربری/کدِ پیگیری/آیدیِ خریدار پنل را پیدا و مدیریت می‌کند
async def find_accounts(bot_id: int, query: str, limit: int = 10) -> list:
    """جستجو در پنل‌هایِ فروخته‌شدهٔ همین ربات (به‌جز حذف‌شده): نام‌کاربریِ ادمین (بدونِ حساسیت به حروف)،
    کدِ پیگیری، یا آیدیِ عددیِ خریدار. تازه‌ترین اول."""
    from sqlalchemy import func, or_
    q = (query or "").strip().lstrip("@")
    if not q:
        return []
    conds = [func.lower(PanelSellerAccount.admin_username) == q.lower(),
             func.lower(PanelSellerAccount.tracking_code) == q.lower()]
    if q.lstrip("-").isdigit():
        conds.append(PanelSellerAccount.buyer_tg == int(q))
    async with Session() as s:
        return list((await s.scalars(select(PanelSellerAccount).where(
            PanelSellerAccount.bot_id == bot_id, PanelSellerAccount.status != "deleted", or_(*conds))
            .order_by(PanelSellerAccount.id.desc()).limit(limit))).all())


async def admin_adjust_volume(bot_id: int, account_id: int, delta_gb: int, api_factory=None) -> dict:
    """افزایش/کاهشِ سقفِ حجمِ یک پنل توسطِ مدیر (بدونِ کسرِ کیف‌پول). status: ok | not_found | unlimited | bad_amount | below_min
    | panel_failed (+ new_gb, reopened). حجمِ نامحدود با این ابزار تغییر نمی‌کند. کمتر از ۱ گیگ نمی‌شود."""
    api_factory = api_factory or _api
    delta_gb = int(delta_gb)
    if delta_gb == 0 or abs(delta_gb) > 100000:
        return {"status": "bad_amount"}
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id)
        if not acc or acc.bot_id != bot_id or acc.status not in ("active", "suspended"):
            return {"status": "not_found"}
        if not acc.data_gb:
            return {"status": "unlimited"}
        new_gb = int(acc.data_gb) + delta_gb
        if new_gb < 1:
            return {"status": "below_min", "current": int(acc.data_gb)}
        panel = await s.get(VpnPanel, acc.panel_id)
        reopen = delta_gb > 0 and acc.status == "suspended" and acc.suspend_reason == "traffic"
        uname = acc.admin_username
    if not panel:
        return {"status": "no_panel"}
    new_pw = None
    try:
        api = api_factory(panel)
        try:
            if reopen:
                if await get_secret(account_id) is not None:
                    await api.activate_users(uname)
                else:
                    new_pw = gen_password()
                    await api.set_password(uname, new_pw)
                    await api.activate_users(uname)
            await apply_native_limit(api, uname, new_gb)
        finally:
            await api.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("panel_seller: تغییرِ حجم توسطِ مدیر ناموفق (acc=%s): %s", account_id, e)
        return {"status": "panel_failed", "error": str(e)[:300]}
    async with Session() as s:
        acc = await s.get(PanelSellerAccount, account_id, with_for_update=True)
        acc.data_gb = new_gb
        fl = _flags(acc)
        fl.discard("tr80")
        _set_flags(acc, fl)
        if reopen:
            acc.status, acc.suspend_reason = "active", None
        buyer = acc.buyer_tg
        await s.commit()
    if new_pw:
        await save_secret(account_id, new_pw)
    return {"status": "ok", "new_gb": new_gb, "reopened": reopen, "new_password": new_pw, "buyer_tg": buyer, "username": uname}


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
    # PATCH_PSEL_KEEP_LOGIN_V1 (Whale): پایانِ حجم ⇒ کاربرانِ پنل خاموش می‌شوند ولی ورودِ خودِ مشتری (رمز) باز می‌ماند تا بتواند ببیند
    # هر کاربر چقدر مصرف کرده؛ پنل هرگز حذف نمی‌شود. (پایانِ مهلت/تعلیقِ مدیر مثلِ قبل: رمز تصادفی ⇒ ورود بسته.)
    keep = reason == "traffic" and await keep_login_on_traffic(acc.bot_id)
    try:
        api = api_factory(panel)
        try:
            await api.disable_users(uname)
            if not keep:
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
    if not keep:
        await save_secret(account_id, None)            # رمز تصادفی و ناشناخته شد
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
        acc.status, acc.suspend_reason, acc.flags = "active", None, ("nt0" if "nt0" in _flags(acc) else None)
        await s.commit()
    await save_secret(account_id, pw)
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
    await save_secret(account_id, None)               # PATCH_PSEL_PASSWORD_VAULT_V1: رمزِ ذخیره‌شده پاک شود
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
                if "nt0" not in fl:                    # PATCH_PSEL_SPEC_PAGE_V1: مشتری اعلانِ حجم را خاموش کرده؛ (تعلیقِ ۱۰۰٪ همیشه اطلاع می‌دهد)
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
    # === PATCH_PSEL_KEEP_LOGIN_V1 === ورودِ مشتریِ «حجم‌تمام‌شده» باز است؛ مطمئن شو کاربرانش دوباره روشن نشده‌اند (دورزدنِ سقف).
    try:
        async with Session() as s:
            ended = (await s.scalars(select(PanelSellerAccount).where(
                PanelSellerAccount.bot_id == bot_id, PanelSellerAccount.status == "suspended",
                PanelSellerAccount.suspend_reason == "traffic").limit(100))).all()
        for a in ended:
            panel = panels.get(a.panel_id)
            if panel is None or await get_secret(a.id) is None:      # رمزِ تصادفی شده بود ⇒ خودش نمی‌تواند وارد شود
                continue
            try:
                api = api_factory(panel)
                try:
                    await api.disable_users(a.admin_username)
                finally:
                    await api.close()
            except Exception as e:      # noqa: BLE001
                logger.info("panel_seller: خاموش‌نگه‌داشتنِ کاربرانِ حجم‌تمام‌شده ناموفق (acc=%s): %s", a.id, e)
    except Exception:      # noqa: BLE001
        logger.exception("panel_seller: بازبینیِ حساب‌هایِ حجم‌تمام‌شده خطا داد")
    return out
