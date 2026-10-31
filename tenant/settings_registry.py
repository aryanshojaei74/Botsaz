"""
tenant/settings_registry.py — ثبتِ آیتم‌هایِ منویِ «🎛 دکمه‌ها و تنظیمات» به‌ازایِ نوعِ ربات.

=== PATCH_SETTINGS_REGISTRY_V1 ===
مشکلِ قبلی: هر گزینهٔ تنظیمات در سه جا باید نوشته می‌شد
  ۱) ساختِ دکمه در _sec_settings_kb (با if _is_virtual / if _is_vpn)،
  ۲) دیسپچرِ حالتِ کیبوردی (_settings_handlers)،
  ۳) خودِ هندلر.
و بارها یکی از اینها فراموش شد («همان باگِ همیشگی»: دکمه هست ولی زدنش
بی‌واکنش است). حالا هر نوعِ ربات آیتم‌هایِ خودش را *یک‌بار* و با هم ثبت
می‌کند (برچسب + callback + هندلرِ حالتِ کیبوردی + شرطِ نمایش) و منویِ
مرکزی فقط از اینجا می‌خواند.

این فایل عمداً هیچ import سنگینی ندارد (نه aiogram، نه دیتابیس) تا با
تست‌هایِ خالص قابلِ بررسی باشد.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingsItem:
    label: str                       # متنِ دکمه (در حالتِ کیبوردی باید ثابت باشد)
    callback: str                    # callback_data — همان مقدارِ موجود؛ تغییرش ممنوع
    handler: Optional[Callable[..., Awaitable[None]]] = None
    #   handler(cb, bot) یا handler(cb, bot, state) — فقط برایِ حالتِ کیبوردی
    #   لازم است؛ در حالتِ شیشه‌ای خودِ callback به روترِ مربوطه می‌رسد.
    needs_state: bool = False        # هندلر FSMContext هم می‌گیرد
    after: tuple[str, ...] = ()      # بعد از اولین callbackِ موجود در این فهرست بنشیند
    visible: Optional[Callable[[str], bool]] = None   # (plan) -> bool ؛ None = همیشه
    owner_only: bool = False         # در حالتِ کیبوردی فقط مالک (نه ادمینِ ربات)
    # برچسبِ وضعیت‌دار (مثلاً «...: روشن/خاموش»): label_fn(bot_id) متنِ لحظه‌ای را
    # می‌سازد و label_prefix ثابتِ ابتدایِ آن است که حالتِ کیبوردی با آن تطبیق می‌دهد.
    label_fn: Optional[Callable[[int], Awaitable[str]]] = None
    label_prefix: str = ""
    # رنگِ نسخهٔ اینلاینِ دکمه ("primary" | "success" | "danger" | ""). فقط بخش‌هایی که
    # کیبوردشان را دستی می‌سازند (home، hub) استفاده می‌کنند؛ منویِ تنظیمات/مالی/ادمین‌ها
    # رنگ را از جدولِ خودشان می‌گیرند.
    style: str = ""
    deny_text: str = "🔒 این بخش فقط برای مالک ربات در دسترس است."


_REGISTRY: dict[str, list[SettingsItem]] = {}


def register_settings_items(kind: str, items: list[SettingsItem]) -> None:
    """آیتم‌هایِ یک نوعِ ربات را ثبت می‌کند. callback یا برچسبِ تکراری در یک نوع
    = باگِ برنامه‌نویسی و همان لحظهٔ import خطا می‌دهد (نه وقتیِ کاربر می‌زند)."""
    bucket = _REGISTRY.setdefault(kind, [])
    seen_cb = {i.callback for i in bucket}
    seen_lbl = {i.label for i in bucket}
    for it in items:
        if it.callback in seen_cb:
            raise ValueError(f"[{kind}] callback تکراری: {it.callback}")
        if it.label in seen_lbl:
            raise ValueError(f"[{kind}] برچسبِ تکراری: {it.label}")
        if it.needs_state and it.handler is None:
            raise ValueError(f"[{kind}] needs_state بدونِ handler: {it.callback}")
        if it.style not in ("", "primary", "success", "danger"):
            raise ValueError(f"[{kind}] رنگِ نامعتبر: {it.style!r} ({it.callback})")
        if it.label_fn is not None and not it.label_prefix:
            raise ValueError(f"[{kind}] label_fn بدونِ label_prefix: {it.callback}")
        if it.label_prefix and not it.label.startswith(it.label_prefix):
            raise ValueError(f"[{kind}] label باید با label_prefix شروع شود: {it.callback}")
        bucket.append(it)
        seen_cb.add(it.callback)
        seen_lbl.add(it.label)


def registered_items(kind: str) -> list[SettingsItem]:
    return list(_REGISTRY.get(kind, ()))


def visible_items(kind: str, plan: str) -> list[SettingsItem]:
    """آیتم‌هایِ قابلِ‌نمایش برایِ این پلن. شرطی که خطا بدهد = آیتم پنهان
    (و در لاگ می‌آید) — منویِ بقیهٔ ربات‌ها هرگز به‌خاطرِ یک آیتم نمی‌خوابد."""
    out: list[SettingsItem] = []
    for it in _REGISTRY.get(kind, ()):
        try:
            if it.visible is None or it.visible(plan):
                out.append(it)
        except Exception:
            logger.exception("settings_registry: شرطِ نمایشِ %s خطا داد", it.callback)
    return out


async def resolve_labels(items: list[SettingsItem], bot_id: int) -> list[SettingsItem]:
    """برچسبِ آیتم‌هایِ وضعیت‌دار را می‌سازد. اگر label_fn خطا بدهد، برچسبِ
    پیش‌فرض می‌ماند (آیتم پنهان نمی‌شود، منو هم نمی‌خوابد)."""
    out: list[SettingsItem] = []
    for it in items:
        if it.label_fn is not None:
            try:
                it = replace(it, label=await it.label_fn(bot_id))
            except Exception:
                logger.exception("settings_registry: label_fn %s خطا داد", it.callback)
        out.append(it)
    return out


def merge_into(opts: list[tuple[str, str]], items: list[SettingsItem]) -> list[tuple[str, str]]:
    """آیتم‌ها را در لیستِ (برچسب، callback) درج می‌کند؛ هر آیتم بعد از اولین
    anchorِ موجود، و اگر هیچ anchor نبود، انتهایِ لیست. ورودی را تغییر نمی‌دهد."""
    result = list(opts)
    for it in items:
        if any(cb == it.callback for _, cb in result):
            continue  # قبلاً (مثلاً از منویِ مرکزی) هست؛ دوبار نشان داده نشود
        pos = len(result)
        for anchor in it.after:
            idx = next((i for i, (_, cb) in enumerate(result) if cb == anchor), None)
            if idx is not None:
                pos = idx + 1
                break
        result.insert(pos, (it.label, it.callback))
    return result


def keyboard_item(kind: str, label: str) -> Optional[SettingsItem]:
    """در حالتِ منویِ کیبوردی: آیتمِ ثبت‌شدهٔ این برچسب (فقط اگر handler دارد)."""
    for it in _REGISTRY.get(kind, ()):
        if it.handler is None:
            continue
        if it.label == label or (it.label_prefix and label.startswith(it.label_prefix)):
            return it
    return None


# ═══════════════ منویِ «📦 سرویس‌ها و محصولات» به‌ازایِ نوعِ ربات ═══════════════
# === PATCH_SETTINGS_REGISTRY_V3_PRODUCTS ===
# قبلاً _sec_products_kb یک تابعِ بلند با شش `if bot_field == ...` بود. حالا هر
# نوعِ ربات فهرستِ دکمه‌ها، جفت‌شدنِ ردیف‌ها، رنگ‌ها و متنِ توضیحِ خودش را ثبت می‌کند.
# رنگ‌ها به‌صورتِ نام ("primary" / "success" / "danger") ثبت می‌شوند تا این
# ماژول به aiogram وابسته نشود.

@dataclass(frozen=True)
class ProductsMenu:
    opts: Callable[[int, str], Awaitable[list[tuple[str, str]]]]   # (bot_id, plan) -> [(label, callback)]
    description: str = ""                                            # متنِ زیرِ عنوانِ منو
    row_pairs: dict = field(default_factory=dict)                    # callback -> callbackِ هم‌ردیف
    styles: dict = field(default_factory=dict)                       # callback -> "primary"|"success"|"danger"
    # === PATCH_SETTINGS_REGISTRY_V4_PRODUCTS_KB ===
    # حالتِ منویِ کیبوردی: برچسب -> handler(msg, row, bot, state). اگر handler مقدارِ
    # False برگرداند یعنی «من رسیدگی نکردم» و زنجیرهٔ مرکزی ادامه پیدا می‌کند.
    keyboard: dict = field(default_factory=dict)
    # برچسب‌هایی از همین منو که عمداً هنوز در زنجیرهٔ مرکزی هندل می‌شوند (چون همان
    # برچسب در منویِ نوعِ دیگری هم هست). تست می‌گوید هر دکمه یا این‌جا یا در
    # keyboard باشد تا «دکمهٔ مرده» جا نماند.
    keyboard_shared: tuple = ()


_PRODUCTS: dict[str, ProductsMenu] = {}
_VALID_STYLES = {"primary", "success", "danger"}


def register_products_menu(kind: str, menu: ProductsMenu) -> None:
    if kind in _PRODUCTS:
        raise ValueError(f"[{kind}] منویِ محصولات دوبار ثبت شد")
    bad = {cb: st for cb, st in menu.styles.items() if st not in _VALID_STYLES}
    if bad:
        raise ValueError(f"[{kind}] رنگِ نامعتبر: {bad}")
    _PRODUCTS[kind] = menu


def products_menu_for(kind) -> Optional[ProductsMenu]:
    return _PRODUCTS.get(kind) if kind else None


def products_keyboard_handler(kind, label: str):
    """handlerِ حالتِ کیبوردیِ این برچسب در منویِ محصولاتِ این نوعِ ربات (یا None)."""
    pm = _PRODUCTS.get(kind) if kind else None
    return pm.keyboard.get(label) if pm is not None else None


# ═══════════════ بخش‌هایِ دیگرِ پنل (مالی، ادمین‌ها) ═══════════════
# === PATCH_SETTINGS_REGISTRY_V5_SECTIONS ===
# همان API آیتم‌هایِ تنظیمات، فقط با کلیدِ «<بخش>:<نوع>»؛ مثلاً section_key("finance",
# "virtual_service"). بخشِ «تنظیمات» بدونِ پیشوند (کلید = خودِ نوعِ ربات) می‌ماند.
SECTIONS = ("finance", "admins", "home", "hub")


def section_key(section: str, kind: str) -> str:
    return f"{section}:{kind}"


def inline_style_name(item: SettingsItem):
    """نامِ رنگِ اینلاینِ آیتم (یا None اگر رنگی تعیین نشده)."""
    return item.style or None
