"""
partner_promo.py — «🎁 جشنوارهٔ خرید عمده» برایِ همکاران (PATCH_PARTNER_PROMO_V1).

مدیرِ بات‌ساز یک بازهٔ زمانی (شروع/پایان به وقتِ تهران) و قاعدهٔ «به‌ازای هر N ربات M ربات هدیه» را برایِ حرفه‌ای و رشد
تعریف می‌کند. هر خریدِ عمدهٔ همکار که در این بازه ثبت شده باشد (کیف پول، کارت‌به‌کارت، آپی) به‌طورِ تجمعی حساب می‌شود:

    هدیه = floor((خریدهایِ قبلی + این خرید) / N) × M − floor(خریدهایِ قبلی / N) × M

یعنی دو خریدِ تکی هم جمع می‌شوند و هیچ هدیه‌ای دو بار حساب نمی‌شود. موجودیِ هدیه جدا از «bought_*» (آمارِ خرید) به انبار
اضافه و در دفترچهٔ انبار با دلیلِ promo ثبت می‌شود.

تنظیمات (Setting با bot_id=0؛ سرویسِ مستأجر هم می‌خواند): ppromo_on، ppromo_start، ppromo_end (ISO بدونِ منطقه، وقتِ سرور = تهران)،
ppromo_pro_buy/ppromo_pro_free، ppromo_growth_buy/ppromo_growth_free (۰ = خاموش).

فقط به config/models وابسته است (نه platform_bot)، تا از سرویسِ مستأجر هم import شود.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import func, select

from config import Session
from models import PartnerStockOrder, Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

KEYS = ("ppromo_on", "ppromo_start", "ppromo_end", "ppromo_pro_buy", "ppromo_pro_free",
        "ppromo_growth_buy", "ppromo_growth_free",
        "ppromo_channel", "ppromo_notify", "ppromo_sent_start", "ppromo_sent_soon", "ppromo_sent_end")
MAX_BUY = 100
MAX_FREE = 20


async def _read_all(session=None) -> dict[str, str]:
    q = select(Setting).where(Setting.bot_id == 0, Setting.key.in_(KEYS))
    if session is not None:                 # داخلِ تراکنشِ خرید: نشستِ دوم باز نکن (کانکشنِ اضافه + خطرِ تداخل)
        rows = (await session.scalars(q)).all()
    else:
        async with Session() as s:
            rows = (await s.scalars(q)).all()
    return {r.key: (r.value or "") for r in rows}


async def set_value(key: str, value: str) -> None:
    if key not in KEYS:
        raise KeyError(key)
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
        if r:
            r.value = value
        else:
            s.add(Setting(bot_id=0, key=key, value=value))
        await s.commit()


def now_local() -> _dt.datetime:
    """اکنون به وقتِ تهران (بدونِ منطقه)؛ هم‌شکل با تاریخ‌هایِ ذخیره‌شده در دیتابیس و با ورودیِ مدیر."""
    from partner_rules import now_tehran
    return now_tehran()


def parse_jalali_datetime(raw: str) -> _dt.datetime | None:
    """«۱۴۰۵/۰۸/۲۵ 18:30» (یا با - و ارقامِ فارسی) ⇒ datetimeِ میلادیِ بدونِ منطقه به وقتِ تهران؛ نامعتبر ⇒ None."""
    import re
    t = (raw or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[ T]+(\d{1,2})(?::(\d{2}))?)?$", t)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    h, mi = int(m.group(4) or 0), int(m.group(5) or 0)
    try:
        import jdatetime
        g = jdatetime.datetime(y, mo, d, h, mi).togregorian()
        return _dt.datetime(g.year, g.month, g.day, g.hour, g.minute)
    except Exception:
        return None


def _dt_or_none(v: str):
    try:
        return _dt.datetime.fromisoformat(v) if v else None
    except ValueError:
        logger.warning("ppromo: تاریخِ نامعتبر در تنظیمات: %r", v)
        return None


def _int(v: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(v)))
    except (TypeError, ValueError):
        return default


async def load(session=None) -> dict:
    raw = await _read_all(session)
    return {
        "on": raw.get("ppromo_on", "0") == "1",
        "start": _dt_or_none(raw.get("ppromo_start", "")),
        "end": _dt_or_none(raw.get("ppromo_end", "")),
        "pro_buy": _int(raw.get("ppromo_pro_buy", "2"), 2, 0, MAX_BUY),
        "pro_free": _int(raw.get("ppromo_pro_free", "1"), 1, 0, MAX_FREE),
        "growth_buy": _int(raw.get("ppromo_growth_buy", "0"), 0, 0, MAX_BUY),
        "growth_free": _int(raw.get("ppromo_growth_free", "0"), 0, 0, MAX_FREE),
        "channel": (raw.get("ppromo_channel", "") or "").strip(),
        "notify": raw.get("ppromo_notify", "1") != "0",
        "sent": {k: raw.get(f"ppromo_sent_{k}", "") for k in ("start", "soon", "end")},
    }


def round_id(cfg: dict) -> str:
    """شناسهٔ این دورهٔ جشنواره؛ با عوض‌شدنِ شروع/پایان، اعلام‌هایِ قبلی دوباره مجاز می‌شوند."""
    return f"{cfg['start']:%Y%m%d%H%M}-{cfg['end']:%Y%m%d%H%M}" if cfg.get("start") and cfg.get("end") else ""


SOON_HOURS = 2


def due_kinds(cfg: dict, now: _dt.datetime) -> list[str]:
    """کدام اعلام‌ها الان باید برود (هر کدام برایِ هر دوره فقط یک‌بار). نوبتِ از دست‌رفته دیرتر هم جبران می‌شود، ولی بعد از پایان فقط «پایان»."""
    if not cfg.get("on") or not cfg.get("start") or not cfg.get("end"):
        return []
    rid = round_id(cfg)
    sent = cfg.get("sent") or {}
    out = []
    if now >= cfg["end"]:
        # فقط اگر در همین دوره چیزی اعلام شده بود (یعنی واقعاً جشنواره‌ای در جریان بود) پایان را بگو
        if sent.get("end") != rid and (sent.get("start") == rid or sent.get("soon") == rid):
            out.append("end")
        return out
    if now >= cfg["start"]:
        if sent.get("start") != rid:
            out.append("start")
        soon_at = cfg["end"] - _dt.timedelta(hours=SOON_HOURS)
        if now >= soon_at and sent.get("soon") != rid and (cfg["end"] - cfg["start"]) > _dt.timedelta(hours=SOON_HOURS):
            out.append("soon")
    return out


def is_active(cfg: dict, at: _dt.datetime | None = None) -> bool:
    at = at or now_local()
    if not cfg.get("on") or not cfg.get("start") or not cfg.get("end"):
        return False
    return cfg["start"] <= at < cfg["end"]


def bonus_units(before: int, add: int, buy: int, free: int) -> int:
    if buy <= 0 or free <= 0 or add <= 0:
        return 0
    return ((before + add) // buy - before // buy) * free


async def bonus_for_order(s, partner_bot_id: int, g: int, p: int, at: _dt.datetime | None = None,
                          exclude_order_id: int | None = None, cfg: dict | None = None) -> tuple[int, int]:
    """(هدیهٔ رشد، هدیهٔ حرفه‌ای) برایِ خریدی با g رشد و p حرفه‌ای که در لحظهٔ ``at`` ثبت شده؛ ``s`` همان نشستِ تراکنشِ فعلی است."""
    cfg = cfg or await load(s)
    at = at or now_local()
    if not is_active(cfg, at):
        return 0, 0
    q = select(func.coalesce(func.sum(PartnerStockOrder.qty_growth), 0),
               func.coalesce(func.sum(PartnerStockOrder.qty_pro), 0)).where(
        PartnerStockOrder.partner_bot_id == partner_bot_id, PartnerStockOrder.status == "approved",
        PartnerStockOrder.created_at >= cfg["start"], PartnerStockOrder.created_at < cfg["end"])
    if exclude_order_id is not None:
        q = q.where(PartnerStockOrder.id != exclude_order_id)
    prev_g, prev_p = (await s.execute(q)).one()
    return (bonus_units(int(prev_g or 0), int(g or 0), cfg["growth_buy"], cfg["growth_free"]),
            bonus_units(int(prev_p or 0), int(p or 0), cfg["pro_buy"], cfg["pro_free"]))


def bonus_text(bg: int, bp: int) -> str:
    if not (bg or bp):
        return ""
    parts = []
    if bp:
        parts.append(f"💎 حرفه‌ای: +{bp}")
    if bg:
        parts.append(f"🚀 رشد: +{bg}")
    return "\n🎁 <b>هدیهٔ جشنوارهٔ خرید:</b> " + " | ".join(parts)


def rule_text(cfg: dict) -> str:
    lines = []
    if cfg["pro_buy"] and cfg["pro_free"]:
        lines.append(f"💎 به‌ازای هر {cfg['pro_buy']} ربات حرفه‌ای، {cfg['pro_free']} ربات حرفه‌ای هدیه")
    if cfg["growth_buy"] and cfg["growth_free"]:
        lines.append(f"🚀 به‌ازای هر {cfg['growth_buy']} ربات رشد، {cfg['growth_free']} ربات رشد هدیه")
    return "\n".join(lines) or "هیچ قاعده‌ای تنظیم نشده"


def _j(dt) -> str:
    from partner_rules import jalali
    return jalali(dt, with_time=True) if dt else "—"


def announcement_text(kind: str, cfg: dict) -> str:
    rules = rule_text(cfg)
    if kind == "start":
        return ("🎁 <b>جشنوارهٔ خرید عمده شروع شد!</b>\n\n"
                f"از <b>{_j(cfg['start'])}</b> تا <b>{_j(cfg['end'])}</b> فرصت داری:\n\n{rules}\n\n"
                "هدیه خودکار و بلافاصله بعد از تأییدِ خرید به انبارت اضافه می‌شود و خریدهایِ این بازه با هم جمع می‌شوند.\n"
                "از پنل ربات همکارت دکمهٔ «🛒 خرید ربات جهت فروش» را بزن.")
    if kind == "soon":
        return (f"⏰ <b>فقط {SOON_HOURS} ساعت تا پایانِ جشنوارهٔ خرید عمده!</b>\n\n"
                "فرصت را از دست نده؛ هنوز می‌توانی با خریدِ عمده هدیه بگیری:\n\n"
                f"{rules}\n\n⏳ پایان: <b>{_j(cfg['end'])}</b>\n"
                "از پنل ربات همکارت دکمهٔ «🛒 خرید ربات جهت فروش» را بزن.")
    return ("🏁 <b>جشنوارهٔ خرید عمده به پایان رسید.</b>\n\n"
            "از همراهی‌ات ممنونیم. هدیه‌هایِ خریدهایِ این بازه به انبارِ همکارانی که خرید کردند اضافه شده است.\n"
            "جشنوارهٔ بعدی را در همین‌جا اعلام می‌کنیم.")
