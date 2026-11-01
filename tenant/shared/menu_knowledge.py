"""
menu_knowledge.py — PATCH_AI_MENU_KNOWLEDGE_V1

دانشِ دستیارِ هوش‌مصنوعیِ ربات‌هایِ مستأجر از «همهٔ دکمه‌هایِ ربات» (SINA): هر دکمه با مسیرِ کاملش (زیرمنوها) و
توضیحِ اینکه چه کاری می‌کند. هر بار که کاربر سؤال می‌پرسد از رویِ منویِ همان لحظهٔ دیتابیس ساخته می‌شود؛ پس با هر
افزودن/حذف/تغییرِ دکمه خودبه‌خود به‌روز است و هرگز کهنه نمی‌ماند (نیازی به ذخیرهٔ جداگانه نیست).

  • فقط دکمه‌هایِ همین ربات (bot_id) — هیچ دادهٔ رباتِ دیگر نمی‌آید.
  • هر دکمه یک خطِ مستقل («- …») است تا fit_context بتواند در بودجهٔ توکن، مرتبط‌ترین‌ها را با سؤال نگه دارد.
  • توضیحِ عملکرد: ۱) متنِ ثبت‌شدهٔ اکشن در منویِ مالک (_SIMPLE_ACTIONS)، ۲) فهرستِ پایهٔ اکشن‌هایِ رایج، ۳) پیامِ ثابتِ دکمه.
"""
from __future__ import annotations

from sqlalchemy import select

from config import Session
from models import MainButton

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# اکشن‌هایِ رایجی که در _SIMPLE_ACTIONS نیستند (یا توضیحشان کوتاه است): اکشن → کاری که برایِ کاربر انجام می‌دهد
BASE_HELP: dict[str, str] = {
    "wallet": "کیف پول کاربر را نشان می‌دهد: موجودی، شارژ حساب، انتقال موجودی (اگر روشن باشد)، کد شارژ و تاریخچهٔ تراکنش‌ها",
    "profile": "مشخصات کاربری را نشان می‌دهد: آیدی، موجودی، تعداد سفارش‌ها، وضعیت احراز هویت و زیرمجموعه‌ها",
    "ticket": "کاربر می‌تواند تیکت پشتیبانی بسازد و پیام/عکس برای پشتیبانی بفرستد",
    "referral": "لینک دعوت اختصاصی کاربر و آمار زیرمجموعه‌ها و پاداش دعوت را نشان می‌دهد",
    "top_customers": "فهرست نفرات برتر (بیشترین خرید) و جوایز/تخفیف آن‌ها را نشان می‌دهد",
    "web_app": "یک مینی‌اپ/صفحهٔ وب را داخل تلگرام باز می‌کند",
    "submenu": "یک زیرمنو با دکمه‌های بیشتر باز می‌کند",
    "skip": "فقط یک پیام/متن ثابت را نشان می‌دهد",
    "free_trial": "یک سرویس تست رایگان می‌سازد (هر کاربر معمولاً یک بار)",
    "buy_config": "خرید کانفیگ آماده با انتخاب حجم/مدت/تعداد",
    "show_shop": "فروشگاه محصولات را نشان می‌دهد",
    "slideshow": "یک اسلایدشوی چندرسانه‌ای نشان می‌دهد",
}


def _strip_prefix(s: str) -> str:
    s = (s or "").strip()
    for p in ("این دکمه حالا ", "این دکمه "):
        if s.startswith(p):
            return s[len(p):].strip()
    return s


def describe_action(action: str | None, simple_actions: dict | None = None) -> str:
    """توضیحِ فارسیِ یک اکشن؛ '' اگر ناشناخته است."""
    act = (action or "").strip()
    if not act:
        return ""
    if act in BASE_HELP:                      # توضیحِ کامل‌ترِ ما اول؛ متنِ منویِ مالک گاهی فقط «X را نمایش می‌دهد» است
        return BASE_HELP[act]
    if simple_actions and act in simple_actions:
        try:
            return _strip_prefix(simple_actions[act][1])
        except Exception:      # noqa: BLE001
            logger.debug("menu_knowledge: توضیحِ اکشن %s خوانده نشد", act)
    if act.startswith("buy_vpn"):
        return "خرید VPN خودکار از پنل مربوط؛ بعد از پرداخت سرویس ساخته و لینک اتصال داده می‌شود"
    return ""


async def build_menu_knowledge(bot_id: int, limit: int = 500) -> list[str]:
    """خط‌هایِ «- «مسیر دکمه» ← توضیح» برایِ همهٔ دکمه‌هایِ منویِ این ربات (با زیرمنوها)."""
    try:
        from tenant import _SIMPLE_ACTIONS
    except Exception:      # noqa: BLE001
        _SIMPLE_ACTIONS = {}
    async with Session() as s:
        rows = (await s.scalars(select(MainButton).where(MainButton.bot_id == bot_id)
                                .order_by(MainButton.row, MainButton.col, MainButton.id).limit(limit))).all()
    ids = {b.id for b in rows}
    kids: dict[int | None, list] = {}
    for b in rows:
        kids.setdefault(b.parent_id if b.parent_id in ids else None, []).append(b)
    lines: list[str] = []

    def walk(parent, trail, depth):
        if depth > 6:
            return
        for b in kids.get(parent, []):
            label = (b.label or "").strip()
            if not label:
                continue
            path = trail + [label]
            what = describe_action(b.action, _SIMPLE_ACTIONS)
            if kids.get(b.id):
                what = "یک زیرمنو باز می‌کند: " + "، ".join((k.label or "").strip() for k in kids[b.id][:8] if (k.label or "").strip())
            msg = " ".join((b.message or "").split())
            if msg:
                what = (what + "؛ " if what else "") + f"پیام ثابت: «{msg[:140]}{'…' if len(msg) > 140 else ''}»"
            lines.append(f"- مسیر «{' ← '.join(path)}»" + (f": {what}" if what else ""))
            walk(b.id, path, depth + 1)
    walk(None, [], 0)
    return lines


async def kyc_facts(bot_id: int) -> list[str]:
    """واقعیت‌هایِ احرازِ هویتِ همین ربات (سطح‌ها، سقف‌ها، روش) تا دستیار به «چند سطح احراز داریم؟» جواب بدهد."""
    try:
        from tenant import get_setting
        from tenant.shared import kyc as K
        if not await K.enabled(bot_id):
            return ["احرازِ هویت: در این ربات فعال نیست (نیازی به احراز نیست)."]
        c0, c1 = await K.cap_for(bot_id, 0), await K.cap_for(bot_id, 1)
        media = K._MEDIA_LABELS.get((await get_setting(bot_id, "kyc_l2_media", "any")) or "any", "مدرک")
        return [
            "احرازِ هویت: فعال است و ۳ سطح دارد (سطح ۰ تا ۲):",
            f"- سطح ۰ (بدون احراز): سقفِ شارژِ روزانه {c0:,} تومان." if c0 else "- سطح ۰ (بدون احراز): بدون سقفِ شارژ.",
            f"- سطح ۱ (کارتِ بانکیِ تأییدشده): با ثبتِ کارتِ بانکی؛ سقفِ شارژِ روزانه {c1:,} تومان. "
            "پرداخت با کارتِ غیر از کارتِ ثبت‌شده یا عبور از سقف باعث محدود شدنِ حساب تا انجامِ سطح ۲ می‌شود." if c1 else
            "- سطح ۱ (کارتِ بانکیِ تأییدشده): با ثبتِ کارتِ بانکی؛ بدون سقفِ شارژ.",
            f"- سطح ۲ (احرازِ کامل): ارسالِ مدرک ({media}) و تأییدِ مدیر؛ بدون محدودیتِ شارژ.",
            "برای احراز کاربر از دکمهٔ «🛡 احرازِ هویت» (در کیف پول/منو) اقدام می‌کند.",
        ]
    except Exception:      # noqa: BLE001
        logger.exception("menu_knowledge: خواندنِ واقعیت‌هایِ احرازِ هویت ناموفق بود")
        return []
