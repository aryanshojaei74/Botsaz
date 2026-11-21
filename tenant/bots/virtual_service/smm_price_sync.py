"""
smm_price_sync.py — PATCH_SMM_PRICE_AUTOSYNC_V1

بروزرسانیِ خودکارِ قیمتِ سرویس‌هایِ SMM از ارائه‌دهنده + گزارش به مالک.

قبلاً panel_rate فقط موقعِ «افزودنِ محصول» نوشته می‌شد و هیچ‌وقت تازه نمی‌شد؛ اگر پنل قیمت را
بالا می‌برد ربات با قیمتِ قدیمی (گاهی زیرِ قیمتِ خرید) می‌فروخت. هر ۳۰ دقیقه برایِ هر پنل یک‌بار
لیستِ سرویس‌ها گرفته می‌شود و قیمت/حداقل/حداکثرِ سرویس‌هایِ متصل به‌روز می‌شود.
"""
from __future__ import annotations

import time

from sqlalchemy import select, update

from config import Session
from logging_setup import get_logger as _get_logger
from models import Bot as BotRow, SmmService

logger = _get_logger(__name__)

INTERVAL_MIN = 30
NOTIFY_MODES = ("all", "changes", "off")
NOTIFY_LABELS = {"all": "همیشه (حتی بدونِ تغییر)", "changes": "فقط وقتی تغییر داشت", "off": "خاموش"}
_SKIP_CATEGORIES = ("شمارهٔ مجازیِ تلگرام",)     # اوزوینو حلقهٔ قیمتِ جدای خودش را دارد
_MAX_LINES = 40
_MASS_GONE_MIN = 5          # حداقلِ تعدادِ ناپدیدشده برایِ فعال‌شدنِ محافظِ «لیستِ مشکوک»


async def _gs(bot_id: int, key: str, default: str = "") -> str:
    from tenant import get_setting
    return await get_setting(bot_id, key, default)


async def _ss(bot_id: int, key: str, value: str) -> None:
    from tenant import set_setting
    await set_setting(bot_id, key, value)


async def notify_mode(bot_id: int) -> str:
    m = await _gs(bot_id, "smm_price_sync_notify", "changes")
    return m if m in NOTIFY_MODES else "changes"


def _panel_key(panel_pk) -> str:
    return f"smm_price_autosync_p{panel_pk if panel_pk is not None else 0}"


async def panel_auto_on(bot_id: int, panel_pk) -> bool:
    """کلیدِ استعلامِ خودکارِ قیمت برایِ هر ارائه‌دهنده (پیش‌فرض روشن)."""
    return (await _gs(bot_id, _panel_key(panel_pk), "1")) == "1"


async def set_panel_auto(bot_id: int, panel_pk, on: bool) -> None:
    await _ss(bot_id, _panel_key(panel_pk), "1" if on else "0")


def _num(v) -> float:
    try:
        return round(float(v or 0), 4)
    except (TypeError, ValueError):
        return 0.0


def _fmt_rate(v: float) -> str:
    return f"{v:,.4f}".rstrip("0").rstrip(".")


async def sync_bot(bot_id: int, *, auto: bool = False, only_pk="all") -> list[dict]:
    """یک دورِ بروزرسانی برایِ یک ربات. خروجی: گزارشِ هر پنل
    [{"panel": نام, "ok": bool, "checked": n, "up": [...], "down": [...], "missing": [...]}]."""
    from tenant.bots.virtual_service import smm as SM
    async with Session() as s:
        pks = (await s.scalars(select(SmmService.panel_pk).where(
            SmmService.bot_id == bot_id, SmmService.is_manual == False,  # noqa: E712
            SmmService.category.notin_(_SKIP_CATEGORIES) | SmmService.category.is_(None)))).all()
    reports = []
    for panel_pk in sorted(set(pks), key=lambda x: (x is not None, x)):
        if only_pk != "all" and panel_pk != only_pk:
            continue
        if auto and not await panel_auto_on(bot_id, panel_pk):
            continue
        _u, _k, pname = await SM._panel_creds(bot_id, panel_pk)
        rep = {"panel": pname, "ok": False, "checked": 0, "up": [], "down": [], "missing": [], "held": []}
        try:
            services = await SM._fetch_services_cached(bot_id, panel_pk, force=True)
        except Exception as e:
            logger.info("smm price sync: لیستِ سرویس‌هایِ پنلِ %s (bot=%s) گرفته نشد: %s", pname, bot_id, e)
            services = []
        if not services:
            reports.append(rep)
            continue
        rep["ok"] = True
        by_id = {str(x.get("service")): x for x in services}
        # === PATCH_PRICE_SYNC_LIGHT_V1 ===
        # لاگِ سرور: وقتی رباتی هزاران سرویس دارد، `select(SmmService)` همه را به‌صورتِ شیِ ORM می‌ساخت و حلقهٔ worker را
        # بارها بیش از ۱ثانیه می‌بست (stack: sqlalchemy loading.instances ← smm_price_sync.sync_bot). حالا فقط ستون‌هایِ لازم
        # خوانده می‌شوند و فقط ردیف‌هایِ *تغییرکرده* شیِ ORM می‌گیرند؛ معنا/خروجی همان قبل است.
        import asyncio as _aio
        async with Session() as s:
            pk_cond = SmmService.panel_pk.is_(None) if panel_pk is None else SmmService.panel_pk == panel_pk
            light = (await s.execute(select(
                SmmService.id, SmmService.service_id, SmmService.panel_rate, SmmService.min_qty,
                SmmService.max_qty, SmmService.category, SmmService.is_active, SmmService.name,
            ).where(SmmService.bot_id == bot_id, pk_cond, SmmService.is_manual == False))).all()  # noqa: E712
            gone: list = []          # (id, name)
            rate_ids: list[int] = []
            qty_updates: list[tuple[int, dict]] = []
            active_n = 0
            for i, r in enumerate(light):
                if i and i % 400 == 0:
                    await _aio.sleep(0)          # به لوپ نفس بده
                if (r.category or "") in _SKIP_CATEGORIES:
                    continue
                rep["checked"] += 1
                if r.is_active:
                    active_n += 1
                it = by_id.get(str(r.service_id))
                if not it:
                    if r.is_active:
                        gone.append((r.id, r.name))
                    continue
                upd = {}
                try:
                    if it.get("min") is not None and int(it["min"]) != r.min_qty:
                        upd["min_qty"] = int(it["min"])
                    if it.get("max") is not None and int(it["max"]) != r.max_qty:
                        upd["max_qty"] = int(it["max"])
                except (TypeError, ValueError):
                    pass
                if upd:
                    qty_updates.append((r.id, upd))
                new_rate, old_rate = _num(it.get("rate")), _num(r.panel_rate)
                if new_rate > 0 and new_rate != old_rate:
                    rate_ids.append(r.id)
            for sid_, upd in qty_updates:
                await s.execute(update(SmmService).where(SmmService.id == sid_).values(**upd))
            if rate_ids:
                objs = (await s.scalars(select(SmmService).where(SmmService.id.in_(rate_ids)))).all()
                for v in sorted(objs, key=lambda x: x.id):
                    it = by_id.get(str(v.service_id))
                    new_rate, old_rate = _num(it.get("rate")), _num(v.panel_rate)
                    try:
                        old_sell = await SM.compute_sell_price(bot_id, v, 1000)
                    except Exception:
                        old_sell = None
                    v.panel_rate = new_rate
                    try:
                        new_sell = await SM.compute_sell_price(bot_id, v, 1000)
                    except Exception:
                        new_sell = None
                    ch = {"name": v.name, "sid": v.service_id, "old": old_rate, "new": new_rate,
                          "old_sell": old_sell, "new_sell": new_sell, "manual": float(v.sell_rate or 0) > 0}
                    (rep["up"] if new_rate > old_rate else rep["down"]).append(ch)
            # سرویسی که از ارائه‌دهنده حذف شده به مشتری فروخته نشود: خودکار غیرفعال می‌شود و فقط یک بار
            # گزارش می‌رود. محافظ: اگر ناگهان تعدادِ زیادی غیب شدند (خطا/لیستِ ناقصِ پنل) دست نمی‌زنیم.
            if len(gone) >= _MASS_GONE_MIN and len(gone) * 2 > active_n:
                rep["held"] = [n for _i, n in gone]
            elif gone:
                await s.execute(update(SmmService).where(SmmService.id.in_([g[0] for g in gone]))
                                .values(is_active=False))
                rep["missing"] = [n for _i, n in gone]
            await s.commit()
        reports.append(rep)
    await _ss(bot_id, "smm_price_sync_last", str(int(time.time())))
    return reports


def _line(ch: dict, arrow: str) -> str:
    if ch["manual"] or ch["old_sell"] is None or ch["new_sell"] is None or ch["old_sell"] == ch["new_sell"]:
        tail = " (قیمتِ فروشِ دستیِ شما ثابت ماند)" if ch["manual"] else ""
        return (f"{arrow} {ch['name']} — قیمتِ پنل از {_fmt_rate(ch['old'])} به "
                f"{_fmt_rate(ch['new'])}{tail}")
    return (f"{arrow} {ch['name']} — قیمتِ هر ۱۰۰۰ از {int(ch['old_sell']):,} به "
            f"{int(ch['new_sell']):,} تومان")


def report_text(rep: dict) -> str | None:
    """متنِ گزارشِ یک پنل؛ None یعنی چیزی برایِ گزارش نیست."""
    head = f"🔄 <b>بررسیِ خودکارِ سرویس‌هایِ پنلِ {rep['panel']} انجام شد</b>\n"
    if not rep["ok"]:
        return head + "\n⚠️ ارائه‌دهنده جواب نداد؛ قیمت‌ها دست نخوردند و دورِ بعد دوباره امتحان می‌شود."
    if not (rep["up"] or rep["down"] or rep["missing"] or rep.get("held")):
        return head + f"\n✅ تغییری در قیمت‌ها نبود ({rep['checked']} سرویس بررسی شد)."
    lines = [head, "<b>تغییرات:</b>"]
    for ch in rep["up"]:
        lines.append(_line(ch, "🔺") + " (افزایش)")
    for ch in rep["down"]:
        lines.append(_line(ch, "🔻") + " (کاهش)")
    if rep["missing"]:
        lines.append("\n🗑 این سرویس‌ها از ارائه‌دهنده حذف شدند و از ربات شما هم برداشته شدند "
                     "(از دسترسِ مشتریان خارج شدند):")
        lines += [f"• {n}" for n in rep["missing"]]
    if rep.get("held"):
        lines.append(f"\n⚠️ {len(rep['held'])} سرویس یکجا از لیستِ ارائه‌دهنده ناپدید شد؛ احتمالاً لیستِ پنل ناقص "
                     "برگشته است، پس برایِ ایمنی چیزی از ربات برداشته نشد و دورِ بعد دوباره بررسی می‌شود:")
        lines += [f"• {n}" for n in rep["held"][:10]]
    if len(lines) > _MAX_LINES:
        extra = len(lines) - _MAX_LINES
        lines = lines[:_MAX_LINES] + [f"… و {extra} مورد دیگر"]
    return "\n".join(lines)


async def send_reports(bot_id: int, reports: list[dict], mode: str) -> None:
    if mode == "off":
        return
    msgs = []
    for rep in reports:
        changed = rep["ok"] and (rep["up"] or rep["down"] or rep["missing"] or rep.get("held"))
        if mode == "changes" and not changed:
            continue
        t = report_text(rep)
        if t:
            msgs.append(t)
    if not msgs:
        return
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow:
        return
    tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        for t in msgs:
            try:
                await tbot.send_message(brow.owner_id, t)
            except Exception as e:
                logger.info("smm price sync: گزارش به مالکِ ربات %s نرسید: %s", bot_id, e)
                break
    finally:
        await tbot.session.close()


async def run_due_once() -> int:
    """ربات‌هایی که وقتِ بروزرسانی‌شان رسیده؛ خروجی: تعدادِ ربات‌هایِ بروزشده."""
    now = time.time()
    async with Session() as s:
        bots = (await s.execute(select(BotRow.id).where(
            BotRow.status == "active", BotRow.field == "virtual_service",
            BotRow.id.in_(select(SmmService.bot_id).where(SmmService.is_manual == False)  # noqa: E712
                          .distinct())))).scalars().all()
    n = 0
    for bid in bots:
        try:
            if (await _gs(bid, "smm_price_autosync", "1")) != "1":
                continue
            last = float(await _gs(bid, "smm_price_sync_last", "0") or 0)
            if now - last < INTERVAL_MIN * 60:
                continue
            reports = await sync_bot(bid, auto=True)
            await send_reports(bid, reports, await notify_mode(bid))
            n += 1
        except Exception:
            logger.exception("smm price sync: ربات %s خطا داد", bid)
    return n


async def smm_price_sync_loop():
    import asyncio
    await asyncio.sleep(90)
    while True:
        try:
            await run_due_once()
        except Exception:
            logger.exception("smm_price_sync_loop error")
        await asyncio.sleep(60)
