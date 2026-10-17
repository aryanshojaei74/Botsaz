"""
backup_full.py — بک‌آپِ *کاملِ* ربات برای مالک (PATCH_BACKUP_FULL_V1).

فرقش با «💾 بک‌آپِ کاملِ ربات» (.botsaz): آن فقط تنظیمات/دکمه‌ها/پنل‌ها/پلن‌ها را نگه می‌دارد؛ این یکی همهٔ دادهٔ ربات را
نگه می‌دارد: کاربران، موجودیِ کیف‌پول و تراکنش‌ها، سفارش‌ها، سرویس‌ها، تیکت‌ها، زیرمجموعه‌ها و ... (همهٔ جدول‌هایِ
TENANT_OWNED_MODELS + فرزندانشان). اگر سرور از بین رفت، مالک فایل را به یک رباتِ تازه می‌دهد و همه‌چیز برمی‌گردد.

قالب: JSONِ فشرده‌شده با gzip (پسوند .botsazfull). ایمپورت = پاک‌کردنِ دادهٔ فعلیِ همین ربات + درج از رویِ فایل، با
بازنگاشتِ شناسه‌ها (id ها در رباتِ مقصد جدید می‌شوند؛ ستون‌هایِ ForeignKey از رویِ اسکیما به‌روز می‌شوند).

امنیت: ستون‌هایِ bot_id همیشه به رباتِ جاری نگاشت می‌شوند (فایلِ دست‌ساز نمی‌تواند به رباتِ دیگر بنویسد)؛ ارجاع به
جدول‌هایی که در فایل/لیستِ مجاز نیستند NULL می‌شود یا ردیف رد می‌شود. کلیدِ کیف‌پولِ حضانتیِ TON (fragment_wallets)
و فایل‌هایِ دیسکی (file_links) در بک‌آپ نمی‌آیند.
"""
from __future__ import annotations

import base64
import datetime as _dt
import gzip
import json
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import delete, select

from config import Session

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

backup_full_router = Router()

FORMAT_NAME = "botsaz-full-backup"
FORMAT_VERSION = 1
EXCLUDED_TABLES = {"fragment_wallets", "file_links"}
MAX_DOWNLOAD = 19 * 1024 * 1024          # سقفِ دانلودِ فایل توسطِ Bot API ≈ ۲۰MB
MAX_SEND = 45 * 1024 * 1024


class FullBackupFSM(StatesGroup):
    waiting_file = State()


# ───────────────────────── هستهٔ بک‌آپ/بازیابی ─────────────────────────

def _scoped_models():
    """[(Model, parent_spec|None)] برایِ همهٔ جدول‌هایِ متعلق به ربات. parent_spec = (fk_attr, ParentModel, parent_bot_attr)."""
    from models import TENANT_OWNED_MODELS, TENANT_CHILD_VIA_MODELS
    out = []
    for M in TENANT_OWNED_MODELS:
        if M.__table__.name not in EXCLUDED_TABLES:
            out.append((M, None))
    for C, fk_attr, P, p_bot in TENANT_CHILD_VIA_MODELS:
        if C.__table__.name not in EXCLUDED_TABLES:
            out.append((C, (fk_attr, P, p_bot)))
    return out


def _scope_where(M, spec, bot_id: int):
    if spec is None:
        return M.bot_id == bot_id
    fk_attr, P, p_bot = spec
    return getattr(M, fk_attr).in_(select(P.id).where(getattr(P, p_bot) == bot_id))


def _ser(v):
    if isinstance(v, _dt.datetime):
        return {"__t": "dt", "v": v.isoformat()}
    if isinstance(v, _dt.date):
        return {"__t": "d", "v": v.isoformat()}
    if isinstance(v, Decimal):
        return {"__t": "dec", "v": str(v)}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__t": "b64", "v": base64.b64encode(bytes(v)).decode()}
    return v


def _deser(v):
    if isinstance(v, dict) and "__t" in v and "v" in v:
        t = v["__t"]
        try:
            if t == "dt":
                return _dt.datetime.fromisoformat(v["v"])
            if t == "d":
                return _dt.date.fromisoformat(v["v"])
            if t == "dec":
                return Decimal(v["v"])
            if t == "b64":
                return base64.b64decode(v["v"])
        except Exception:
            logger.info("بک‌آپ کامل: مقدارِ نامعتبر نادیده گرفته شد (%s)", t)
            return None
    return v


async def build_full_backup(bot_id: int) -> dict:
    from models import Bot as BotRow
    tables: dict[str, list[dict]] = {}
    async with Session() as s:
        b = await s.get(BotRow, bot_id)
        for M, spec in _scoped_models():
            try:
                objs = (await s.scalars(select(M).where(_scope_where(M, spec, bot_id)))).all()
            except Exception:
                logger.exception("بک‌آپ کامل: خواندنِ جدولِ %s ناموفق بود", M.__table__.name)
                continue
            rows = []
            for o in objs:
                rows.append({c.key: _ser(getattr(o, c.key)) for c in M.__table__.columns
                             if hasattr(o, c.key)})
            tables[M.__table__.name] = rows
    return {
        "format": FORMAT_NAME, "version": FORMAT_VERSION,
        "bot_field": getattr(b, "field", None), "bot_username": getattr(b, "username", None),
        "created_at": _dt.datetime.utcnow().isoformat(),
        "tables": tables,
    }


def dump_bytes(data: dict) -> bytes:
    return gzip.compress(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), compresslevel=6)


def load_bytes(raw: bytes) -> dict:
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass          # فایلِ JSONِ خام هم پذیرفته می‌شود
    return json.loads(raw.decode("utf-8"))


def summarize(data: dict) -> str:
    t = data.get("tables", {})
    users = len(t.get("bot_users", []))
    orders = sum(len(t.get(k, [])) for k in ("orders", "gift_orders", "smm_orders", "product_orders"))
    txns = len(t.get("wallet_txns", []))
    total = sum(len(v) for v in t.values())
    return (f"👥 کاربران: {users:,}\n🧾 سفارش‌ها: {orders:,}\n💳 تراکنش‌هایِ کیف‌پول: {txns:,}\n"
            f"📦 جمعِ ردیف‌ها: {total:,} در {len(t)} جدول")


async def restore_full_backup(bot_id: int, data: dict) -> dict:
    """دادهٔ فعلیِ ربات را پاک و از رویِ بک‌آپ بازسازی می‌کند (تراکنشِ واحد؛ خطا = هیچ تغییری)."""
    from sqlalchemy import inspect as _inspect
    from models import Base
    tables = data.get("tables") or {}
    scoped = {M.__table__.name: (M, spec) for M, spec in _scoped_models()}
    order = [t for t in Base.metadata.sorted_tables if t.name in scoped]
    stats = {"inserted": 0, "skipped": 0}
    async with Session() as s:
        try:
            # ۱) پاک‌سازی: فرزندان قبل از والدان
            for t in reversed(order):
                M, spec = scoped[t.name]
                await s.execute(delete(M).where(_scope_where(M, spec, bot_id)))
            # ۲) درج: والدان قبل از فرزندان، با نگاشتِ شناسه‌ها
            id_map: dict[tuple[str, int], int] = {}
            done: set[str] = set()
            deferred: list = []     # (obj, attr_key, target_table, old_value)
            for t in order:
                M, spec = scoped[t.name]
                mapper = _inspect(M)
                rows = tables.get(t.name) or []
                batch = []
                for row in rows:
                    kwargs, pend, skip = {}, [], False
                    old_id = None
                    for col in t.columns:
                        if col.name not in row:
                            continue
                        val = _deser(row[col.name])
                        key = mapper.get_property_by_column(col).key
                        if col.primary_key and col.name == "id":
                            old_id = val
                            continue
                        fks = list(col.foreign_keys)
                        if fks and val is not None:
                            tgt = fks[0].column.table.name
                            if tgt == "bots":
                                val = bot_id
                            elif tgt in scoped and tgt != t.name and tgt in done:
                                new = id_map.get((tgt, val))
                                if new is None:
                                    if col.nullable:
                                        val = None
                                    else:
                                        skip = True
                                        break
                                else:
                                    val = new
                            elif tgt in scoped:       # خودارجاع یا جدولِ بعدی → پس از درج
                                pend.append((key, tgt, val, col.nullable))
                                val = None
                            else:                      # جدولِ ناشناس/بیرونی: ایمن نیست
                                if col.nullable:
                                    val = None
                                else:
                                    skip = True
                                    break
                        kwargs[key] = val
                    if skip:
                        stats["skipped"] += 1
                        continue
                    batch.append((old_id, M(**kwargs), pend))
                if batch:
                    s.add_all([b[1] for b in batch])
                    await s.flush()
                for old_id, obj, pend in batch:
                    if old_id is not None:
                        id_map[(t.name, old_id)] = obj.id
                    for key, tgt, old_val, _nullable in pend:
                        deferred.append((obj, key, tgt, old_val))
                    stats["inserted"] += 1
                done.add(t.name)
            for obj, key, tgt, old_val in deferred:
                setattr(obj, key, id_map.get((tgt, old_val)))
            await s.flush()
            await s.commit()
        except Exception:
            await s.rollback()
            raise
    try:
        import tenant
        tenant._SETTING_CACHE.clear()
        tenant.invalidate_bot_row_cache()
    except Exception:
        logger.exception("بک‌آپ کامل: پاک‌کردنِ کشِ تنظیمات ناموفق بود")
    return stats


# ───────────────────────── رابطِ کاربری (مالک) ─────────────────────────

async def _owner_row(cb_or_msg, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or cb_or_msg.from_user.id != row.owner_id:
        return None
    return row


@backup_full_router.callback_query(F.data == "tadm:bkfull:export")
async def full_export(cb: CallbackQuery, bot: Bot):
    row = await _owner_row(cb, bot)
    if not row:
        await cb.answer()
        return
    await cb.answer("⏳ در حالِ ساختنِ بک‌آپِ کامل...")
    data = await build_full_backup(row.id)
    raw = dump_bytes(data)
    if len(raw) > MAX_SEND:
        await cb.message.answer("❌ حجمِ بک‌آپ از سقفِ ارسالِ تلگرام بیشتر شد؛ با پشتیبانی تماس بگیر.")
        return
    fname = f"full_{row.username or row.id}_{_dt.datetime.utcnow().strftime('%Y%m%d_%H%M')}.botsazfull"
    note = ""
    if len(raw) > MAX_DOWNLOAD:
        note = ("\n⚠️ حجمِ فایل از ۲۰ مگابایت بیشتر است؛ تلگرام اجازهٔ دانلودِ آن را به ربات نمی‌دهد و "
                "بازگردانی از داخلِ ربات ممکن نیست — برایِ بازیابی با پشتیبانیِ بات‌ساز هماهنگ کن.")
    await cb.message.answer_document(
        BufferedInputFile(raw, filename=fname),
        caption=("✅ <b>بک‌آپِ کاملِ ربات آماده شد.</b>\n\n" + summarize(data) +
                 "\n\n🔐 این فایل شاملِ اطلاعاتِ مشتری‌ها و کلیدهایِ پنل‌هاست — با کسی به اشتراک نذار." + note),
        parse_mode="HTML")


@backup_full_router.callback_query(F.data == "tadm:bkfull:import")
async def full_import_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_row(cb, bot)
    if not row:
        await cb.answer()
        return
    await state.set_state(FullBackupFSM.waiting_file)
    await cb.message.answer(
        "📥 فایلِ <code>.botsazfull</code> (بک‌آپِ کامل) را به‌صورتِ داکیومنت بفرست.\n\n"
        "⚠️ همهٔ دادهٔ فعلیِ این ربات (کاربران، کیف‌پول‌ها، سفارش‌ها، دکمه‌ها، تنظیمات و ...) پاک و با محتوایِ فایل "
        "جایگزین می‌شود و قابلِ‌برگشت نیست — اگر وضعیتِ فعلی مهم است اول از آن بک‌آپِ کامل بگیر.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data="tadm:backup:menu", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@backup_full_router.message(FullBackupFSM.waiting_file, F.document)
async def full_import_file(msg: Message, bot: Bot, state: FSMContext):
    row = await _owner_row(msg, bot)
    if not row:
        await state.clear()
        return
    doc = msg.document
    if not (doc.file_name or "").lower().endswith((".botsazfull", ".botsazfull.gz")):
        await msg.answer("❌ فایل باید پسوندِ .botsazfull داشته باشد. دوباره بفرست یا /cancel بزن.")
        return
    if doc.file_size and doc.file_size > MAX_DOWNLOAD:
        await msg.answer("❌ حجمِ فایل بیشتر از ۲۰ مگابایت است و تلگرام اجازهٔ دانلودش را به ربات نمی‌دهد.")
        return
    f = await bot.get_file(doc.file_id)
    buf = await bot.download_file(f.file_path)
    try:
        data = load_bytes(buf.read())
    except Exception:
        await msg.answer("❌ فایل معتبر نیست (سالم نبود). دوباره بفرست یا /cancel بزن.")
        return
    if data.get("format") != FORMAT_NAME:
        await msg.answer("❌ این فایل یک بک‌آپِ کاملِ بات‌ساز نیست.")
        return
    if int(data.get("version") or 0) > FORMAT_VERSION:
        await msg.answer("❌ این بک‌آپ با نسخهٔ جدیدتری گرفته شده و این ربات پشتیبانی‌اش نمی‌کند.")
        return
    bf = data.get("bot_field")
    if bf and row.field and bf != row.field:
        await msg.answer(f"❌ این بک‌آپ مالِ رباتِ «{bf}» است ولی این ربات از نوعِ «{row.field}» است.")
        return
    import os
    os.makedirs("backups/pending_restore", exist_ok=True)
    _path = f"backups/pending_restore/{row.id}_{msg.from_user.id}.botsazfull"
    with open(_path, "wb") as fh:
        fh.write(dump_bytes(data))
    await state.update_data(full_backup_path=_path)
    await msg.answer(
        "⚠️ <b>مطمئنی؟</b>\n\nهمهٔ دادهٔ فعلیِ این ربات پاک و با این جایگزین می‌شود:\n\n" + summarize(data) +
        "\n\n⚠️ این کار قابلِ‌برگشت نیست.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، جایگزین کن", callback_data="tadm:bkfull:confirm", style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="❌ نه، لغو کن", callback_data="tadm:backup:menu", style=ButtonStyle.DANGER)],
        ]))


@backup_full_router.message(FullBackupFSM.waiting_file, F.text)
async def full_import_cancel(msg: Message, state: FSMContext):
    if (msg.text or "").strip() in ("❌ لغو", "/cancel", "لغو"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    await msg.answer("❌ این یک فایل نبود. فایلِ .botsazfull را به‌صورتِ داکیومنت بفرست، یا /cancel بزن.")


@backup_full_router.callback_query(F.data == "tadm:bkfull:confirm")
async def full_import_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _owner_row(cb, bot)
    _path = (await state.get_data()).get("full_backup_path")
    await state.clear()
    if not row or not _path or not _path.startswith(f"backups/pending_restore/{row.id}_"):
        await cb.answer("منقضی شد؛ دوباره فایل را بفرست.", show_alert=True)
        return
    await cb.answer("⏳ در حالِ بازگردانی...")
    try:
        import os
        with open(_path, "rb") as fh:
            _data = load_bytes(fh.read())
        stats = await restore_full_backup(row.id, _data)
        os.remove(_path)
    except Exception:
        logger.exception("خطا در بازگردانیِ بک‌آپِ کامل (bot=%s)", row.id)
        await cb.message.answer("❌ بازگردانی با خطا مواجه شد و هیچ تغییری اعمال نشد (تراکنش برگشت خورد). "
                                "فایل را به پشتیبانی بده تا بررسی کنند.")
        return
    await cb.message.answer(
        f"✅ بازگردانی انجام شد. {stats['inserted']:,} ردیف برگشت"
        + (f" ({stats['skipped']:,} ردیفِ ناسازگار رد شد)" if stats["skipped"] else "") + ".\n"
        "اگر منو یا تنظیماتی کهنه دیده شد، یک بار /start بزن.")


_AUTO_CYCLE = ["0", "7", "1"]
_AUTO_LABEL = {"0": "⛔️ خاموش", "7": "🗓 هفتگی", "1": "📆 روزانه"}


@backup_full_router.callback_query(F.data == "tadm:bkfull:auto")
async def full_auto_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, set_setting
    row = await _owner_row(cb, bot)
    if not row:
        await cb.answer()
        return
    cur = str(await get_setting(row.id, "auto_full_backup_days", "0"))
    nxt = _AUTO_CYCLE[(_AUTO_CYCLE.index(cur) + 1) % len(_AUTO_CYCLE)] if cur in _AUTO_CYCLE else "7"
    await set_setting(row.id, "auto_full_backup_days", nxt)
    await cb.answer("بک‌آپِ خودکار: " + _AUTO_LABEL[nxt])
    from tenant.shared.backup_restore import backup_menu
    await backup_menu(cb, bot)


@backup_full_router.callback_query(F.data == "tadm:bkfull:auto7")
async def full_auto_weekly(cb: CallbackQuery, bot: Bot):
    """دکمهٔ پیامِ اعلانِ ادمین: بک‌آپِ خودکارِ هفتگی را (بدونِ چرخش) روشن می‌کند."""
    from tenant import set_setting
    row = await _owner_row(cb, bot)
    if not row:
        await cb.answer()
        return
    await set_setting(row.id, "auto_full_backup_days", "7")
    await cb.answer("✅ بک‌آپِ خودکارِ هفتگی روشن شد؛ فایل در همین چت می‌آید.", show_alert=True)


async def full_backup_auto_once() -> int:
    """برایِ رباتی که بک‌آپِ خودکار را روشن کرده، در فاصلهٔ تنظیم‌شده فایل را به پیوی مالک می‌فرستد."""
    from models import Bot as BotRow
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from tenant.shared.extras import _eget, _eset
    sent = 0
    async with Session() as s:
        bots = (await s.scalars(select(BotRow).where(BotRow.status == "active"))).all()
    for b in bots:
        try:
            days = int(float(await _eget(b.id, "auto_full_backup_days", "0") or 0))
            if days <= 0:
                continue
            last = await _eget(b.id, "auto_full_backup_last", "")
            if last and (_dt.datetime.utcnow() - _dt.datetime.fromisoformat(last)) < _dt.timedelta(days=days) - _dt.timedelta(hours=1):
                continue
            data = await build_full_backup(b.id)
            raw = dump_bytes(data)
            await _eset(b.id, "auto_full_backup_last", _dt.datetime.utcnow().isoformat())
            if len(raw) > MAX_SEND:
                continue
            tb = Bot(token=b.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            try:
                await tb.send_document(
                    b.owner_id,
                    BufferedInputFile(raw, filename=f"full_{b.username or b.id}_{_dt.datetime.utcnow():%Y%m%d}.botsazfull"),
                    caption="🗄 <b>بک‌آپِ خودکارِ کاملِ ربات</b>\n\n" + summarize(data) +
                            "\n\n🔐 با کسی به اشتراک نذار. بازگردانی: پنل ← تنظیمات ← 💾 بک‌آپِ کامل.")
                sent += 1
            finally:
                await tb.session.close()
        except Exception:
            logger.exception("auto full backup failed (bot=%s)", b.id)
    return sent


async def full_backup_auto_loop() -> None:
    import asyncio
    while True:
        try:
            await full_backup_auto_once()
        except Exception:
            logger.exception("auto full backup loop failed")
        await asyncio.sleep(1800)
