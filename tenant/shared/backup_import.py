"""
backup_import.py — بک‌آپ‌گیری و ایمپورتِ کانفیگ‌های انبار (کانفیگ‌های دستیِ
«آماده» و «نامحدود») به‌صورت فایلِ <code>.botsaz</code> (JSON).

مثلِ tenant/token_swap.py، این هم از یک استیتِ اختصاصیِ FSM
(BackupImportFSM.import_backup) به‌جایِ AdminFSM.import_backupِ قدیمی
استفاده می‌کند — بررسی شد که آن استیت فقط همین‌جا استفاده می‌شد.
"""
from __future__ import annotations

import io
import json

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select

from config import CONFIG_CODE_START, Session
from models import Config

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

backup_import_router = Router()

BOTSAZ_MAGIC = "BOTSAZ_CFG_BACKUP_V1"


class BackupImportFSM(StatesGroup):
    import_backup = State()


def _make_backup_bytes(cfgs: list) -> bytes:
    """ساخت محتوای فایل .botsaz برای لیستی از کانفیگ‌ها."""
    data = {
        "magic": BOTSAZ_MAGIC,
        "configs": [
            {
                "volume_gb": c.volume_gb,
                "days": c.days,
                "payload": c.payload,
                "status": c.status,
                "price": c.price,
                "price_per_day": c.price_per_day,
                "user_count": c.user_count,
                "display_mode": c.display_mode,
                "category": c.category,
            }
            for c in cfgs
        ],
    }
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


@backup_import_router.callback_query(F.data == "tadm:cfg:backup_normal")
async def tadm_cfg_backup_normal(cb: CallbackQuery, bot: Bot):
    """بک‌آپ کانفیگ‌های آماده (volume_gb > 0)."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    async with Session() as s:
        cfgs = (await s.scalars(
            select(Config).where(
                Config.bot_id == row.id,
                Config.volume_gb > 0,
                Config.status == "in_stock",
            ).order_by(Config.code)
        )).all()
    if not cfgs:
        await cb.answer("انبار کانفیگ آماده خالی است.", show_alert=True)
        return
    file_bytes = _make_backup_bytes(list(cfgs))
    doc = BufferedInputFile(file_bytes, filename="backup_normal.botsaz")
    await cb.message.answer_document(
        doc,
        caption=(
            f"📤 <b>بک‌آپ کانفیگ‌های آماده</b>\n\n"
            f"تعداد: <b>{len(cfgs)}</b> کانفیگ\n"
            "برای بازگردانی از دکمه «📥 ایمپورت بک‌آپ» استفاده کن."
        ),
    )
    await cb.answer()


@backup_import_router.callback_query(F.data == "tadm:cfg:backup_unlimited")
async def tadm_cfg_backup_unlimited(cb: CallbackQuery, bot: Bot):
    """بک‌آپ کانفیگ‌های نامحدود (volume_gb == 0)."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    async with Session() as s:
        cfgs = (await s.scalars(
            select(Config).where(
                Config.bot_id == row.id,
                Config.volume_gb == 0,
                Config.status == "in_stock",
            ).order_by(Config.code)
        )).all()
    if not cfgs:
        await cb.answer("انبار کانفیگ نامحدود خالی است.", show_alert=True)
        return
    file_bytes = _make_backup_bytes(list(cfgs))
    doc = BufferedInputFile(file_bytes, filename="backup_unlimited.botsaz")
    await cb.message.answer_document(
        doc,
        caption=(
            f"📤 <b>بک‌آپ کانفیگ‌های نامحدود</b>\n\n"
            f"تعداد: <b>{len(cfgs)}</b> کانفیگ\n"
            "برای بازگردانی از دکمه «📥 ایمپورت بک‌آپ» استفاده کن."
        ),
    )
    await cb.answer()


@backup_import_router.callback_query(F.data == "tadm:cfg:import")
async def tadm_cfg_import_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """شروع فرآیند ایمپورت — منتظر ارسال فایل .botsaz."""
    from tenant import _owner_ok, smart_cancel_kb
    if not await _owner_ok(cb, bot):
        return
    await state.set_state(BackupImportFSM.import_backup)
    await cb.message.answer(
        "📥 <b>ایمپورت بک‌آپ</b>\n\n"
        "فایل بک‌آپ با پسوند <code>.botsaz</code> را بفرست.\n"
        "⚠️ فقط فایل‌هایی که از همین ربات بک‌آپ گرفته شده قابل قبول است.",
        reply_markup=await smart_cancel_kb(bot),
    )
    await cb.answer()


@backup_import_router.message(BackupImportFSM.import_backup, F.document)
async def tadm_cfg_import_file(msg: Message, bot: Bot, state: FSMContext):
    """دریافت فایل .botsaz و ایمپورت کانفیگ‌ها به انبار."""
    from tenant import get_bot_row, smart_cancel_kb, cfg_manage_kb, _sanitize_category
    row = await get_bot_row(bot)
    if not row or msg.from_user.id != row.owner_id:
        return

    doc = msg.document
    # اعتبارسنجی پسوند
    fname = doc.file_name or ""
    if not fname.lower().endswith(".botsaz"):
        await msg.answer(
            "❌ فایل باید پسوند <code>.botsaz</code> داشته باشد.\n"
            "فایل درستی بفرست یا /cancel بزن.",
            reply_markup=await smart_cancel_kb(bot),
        )
        return

    # دانلود و پارس
    try:
        tg_file = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, destination=buf)
        raw = buf.getvalue().decode("utf-8")
        data = json.loads(raw)
    except Exception as e:
        await msg.answer(f"❌ خطا در خواندن فایل:\n<code>{e}</code>", reply_markup=await smart_cancel_kb(bot))
        return

    # بررسی magic
    if data.get("magic") != BOTSAZ_MAGIC:
        await msg.answer(
            "❌ این فایل معتبر نیست یا از منبع دیگری است.\n"
            "فقط فایل‌های <code>.botsaz</code> که از همین سیستم ساخته شده‌اند قابل ایمپورت‌اند.",
            reply_markup=await smart_cancel_kb(bot),
        )
        return

    cfgs_data = data.get("configs", [])
    if not cfgs_data:
        await msg.answer("⚠️ فایل بک‌آپ خالی است.", reply_markup=cfg_manage_kb())
        await state.clear()
        return

    # ایمپورت به انبار
    added = 0
    skipped = 0
    async with Session() as s:
        # کد بعدی موجود
        mx = await s.scalar(select(func.max(Config.code)).where(Config.bot_id == row.id))
        next_code = (mx + 1) if mx else CONFIG_CODE_START
        # کانفیگ‌های موجود برای تشخیص تکراری (payload یکتاست)
        existing_payloads = set(
            (await s.scalars(
                select(Config.payload).where(Config.bot_id == row.id)
            )).all()
        )
        for item in cfgs_data:
            payload = (item.get("payload") or "").strip()
            if not payload:
                continue
            if payload in existing_payloads:
                skipped += 1
                continue
            s.add(Config(
                bot_id=row.id,
                code=next_code,
                volume_gb=item.get("volume_gb", 0),
                days=item.get("days", 30),
                payload=payload,
                status="in_stock",
                price=item.get("price", 0),
                price_per_day=item.get("price_per_day", 0),
                user_count=item.get("user_count", 0),
                display_mode=item.get("display_mode", "glass"),
                category=_sanitize_category(item.get("category") or ""),
            ))
            existing_payloads.add(payload)
            next_code += 1
            added += 1
        await s.commit()

    await state.clear()
    lines = ["✅ <b>ایمپورت بک‌آپ تمام شد</b>\n"]
    lines.append(f"➕ اضافه شد: <b>{added}</b> کانفیگ")
    if skipped:
        lines.append(f"♻️ از قبل موجود بود (تکراری): <b>{skipped}</b> کانفیگ")
    await msg.answer("\n".join(lines), reply_markup=cfg_manage_kb())


@backup_import_router.message(BackupImportFSM.import_backup)
async def tadm_cfg_import_wrong(msg: Message, bot: Bot):
    """اگر چیزی غیر از فایل فرستاد."""
    from tenant import smart_cancel_kb
    await msg.answer(
        "❌ لطفاً فایل <code>.botsaz</code> بفرست (نه متن یا عکس).",
        reply_markup=await smart_cancel_kb(bot),
    )
