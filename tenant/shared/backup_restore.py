"""
backup_restore.py — بک‌آپِ کاملِ ربات، فرمتِ .botsaz (PATCH_BOT_BACKUP_V1).

خروجی یه فایلِ JSON با پسوندِ .botsaz است که شاملِ:
  - تنظیماتِ ربات (جدولِ Setting)
  - دکمه‌هایِ صفحه‌یِ اصلی (MainButton) — با حفظِ ساختارِ زیرمنوها
  - (برایِ ربات‌هایِ خدماتِ مجازی) پنل‌هایِ API، دسته‌بندی‌ها، سرویس‌ها

ایمپورت یعنی: همه‌ی این جدول‌ها برایِ رباتِ مقصد پاک و از رویِ فایل
بازسازی می‌شن — برایِ همین یه تأییدِ صریح و قوی قبلش لازمه. آیدی‌هایِ
داخلیِ رکوردها (که ارجاعِ بینِ جدول‌ها دارن، مثلِ parent_id دکمه‌ها یا
panel_pk/category_pk سرویس‌ها) دوباره‌نگاشت می‌شن، چون تویِ رباتِ مقصد
آیدی‌هایِ جدید می‌گیرن.

=== هشدارِ امنیتی ===
کلیدهایِ API پنل‌ها (SmmPanel.api_key) هم داخلِ بک‌آپ می‌مونه، چون بدونشون
پنل‌ها بی‌فایده‌ن. یعنی این فایل یه رازِ حساسه — باید بهِ ادمین صراحتاً
گفته بشه که با کسی به اشتراک نذاره.
"""
from __future__ import annotations

import json
from datetime import datetime, date
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, Document,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)
from sqlalchemy import delete, select

from config import Session
from models import Bot as BotRow, MainButton, Setting, SmmPanel, SmmCategory, SmmService, VpnPanel, VpnPlan

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

backup_router = Router()

_FORMAT_NAME = "botsaz-backup"
_FORMAT_VERSION = 1

_MAIN_BUTTON_FIELDS = ["row", "col", "label", "action", "message", "rules", "config",
                       "color", "icon_emoji_id", "support_btn", "support_contact"]
_SMM_PANEL_FIELDS = ["name", "api_url", "api_key", "is_active", "bulk_status",
                     "price_mode", "margin_pct", "rate_mode", "toman_per_unit",
                     "order_mode", "fail_policy", "price_round"]
_SMM_CATEGORY_FIELDS = ["platform", "name", "margin_pct", "is_active"]
_SMM_SERVICE_FIELDS = ["service_id", "name", "category", "panel_rate", "sell_rate",
                       "min_qty", "max_qty", "is_active", "is_manual", "description"]
# === PATCH_BACKUP_VPN_V1 === پنل‌ها و پلن‌هایِ VPN هم در بک‌آپ (برایِ انتقال به رباتِ جدید)
_VPN_PANEL_FIELDS = ["panel_key", "base_url", "username", "password", "api_key", "sub_base",
                     "inbound_ids", "inbound_mode", "enabled", "auto_sync", "default_limit_ip"]
_VPN_PLAN_FIELDS = ["panel_key", "audience", "title", "volume_gb", "days", "price",
                    "limit_ip", "active", "color", "category"]


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def _row_to_dict(obj, fields: list[str]) -> dict:
    out = {}
    for f in fields:
        if hasattr(obj, f):
            out[f] = getattr(obj, f)
    return out


class BackupFSM(StatesGroup):
    waiting_file = State()


async def _build_backup(bot_id: int) -> dict:
    async with Session() as s:
        row = await s.get(BotRow, bot_id)
        settings = (await s.scalars(select(Setting).where(Setting.bot_id == bot_id))).all()
        buttons = (await s.scalars(select(MainButton).where(MainButton.bot_id == bot_id))).all()
        panels = (await s.scalars(select(SmmPanel).where(SmmPanel.bot_id == bot_id))).all()
        cats = (await s.scalars(select(SmmCategory).where(SmmCategory.bot_id == bot_id))).all()
        svcs = (await s.scalars(select(SmmService).where(SmmService.bot_id == bot_id))).all()
        vpanels = (await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == bot_id))).all()
        vplans = (await s.scalars(select(VpnPlan).where(VpnPlan.bot_id == bot_id))).all()

    data = {
        "format": _FORMAT_NAME,
        "version": _FORMAT_VERSION,
        "bot_field": row.field if row else None,
        "exported_at": datetime.utcnow().isoformat(),
        "tables": {
            "settings": [{"key": st.key, "value": st.value} for st in settings],
            "main_buttons": [
                dict(_tmp_id=b.id, _tmp_parent_id=b.parent_id, **_row_to_dict(b, _MAIN_BUTTON_FIELDS))
                for b in buttons
            ],
            "smm_panels": [
                dict(_tmp_id=p.id, **_row_to_dict(p, _SMM_PANEL_FIELDS)) for p in panels
            ],
            "smm_categories": [
                dict(_tmp_id=c.id, **_row_to_dict(c, _SMM_CATEGORY_FIELDS)) for c in cats
            ],
            "smm_services": [
                dict(_tmp_id=v.id, _tmp_panel_pk=v.panel_pk, _tmp_category_pk=v.category_pk,
                    **_row_to_dict(v, _SMM_SERVICE_FIELDS))
                for v in svcs
            ],
            "vpn_panels": [_row_to_dict(p, _VPN_PANEL_FIELDS) for p in vpanels],
            "vpn_plans": [_row_to_dict(p, _VPN_PLAN_FIELDS) for p in vplans],
        },
    }
    return data


async def _summary_line(data: dict) -> str:
    t = data.get("tables", {})
    return (
        f"⚙️ تنظیمات: {len(t.get('settings', []))}\n"
        f"🎛 دکمه‌ها: {len(t.get('main_buttons', []))}\n"
        f"🌐 پنل‌هایِ API: {len(t.get('smm_panels', []))}\n"
        f"📦 دسته‌ها: {len(t.get('smm_categories', []))}\n"
        f"🛰 سرویس‌ها: {len(t.get('smm_services', []))}"
        + (f"\n🖥 پنل‌هایِ VPN: {len(t.get('vpn_panels', []))}"
           f"\n🏷 پلن‌هایِ فروشِ VPN: {len(t.get('vpn_plans', []))}"
           if (t.get('vpn_panels') or t.get('vpn_plans')) else "")
    )


# ═══════════════════ خروجی گرفتن (Export) ═══════════════════

@backup_router.callback_query(F.data == "tadm:backup:export")
async def backup_export(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or cb.from_user.id != row.owner_id:  # فایل شاملِ رمز/کلیدِ پنل‌هاست — فقط مالک
        await cb.answer()
        return
    await cb.answer("⏳ در حالِ ساختنِ بک‌آپ...")
    data = await _build_backup(row.id)
    raw = json.dumps(data, ensure_ascii=False, indent=2, default=_json_default).encode("utf-8")
    fname = f"backup_{row.id}_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.botsaz"
    summary = await _summary_line(data)
    await cb.message.answer_document(
        BufferedInputFile(raw, filename=fname),
        caption=(
            "✅ <b>بک‌آپِ رباتت آماده شد.</b>\n\n" + summary +
            "\n\n⚠️ این فایل شاملِ کلیدهایِ API پنل‌هاته — با کسی به اشتراک نذار و "
            "یه‌جایِ امن نگهش دار."
        ),
        parse_mode="HTML")


# ═══════════════════ بازگردانی (Import) ═══════════════════

@backup_router.callback_query(F.data == "tadm:backup:import")
async def backup_import_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or cb.from_user.id != row.owner_id:  # فایل شاملِ رمز/کلیدِ پنل‌هاست — فقط مالک
        await cb.answer()
        return
    await state.set_state(BackupFSM.waiting_file)
    await cb.message.answer(
        "📥 فایلِ <code>.botsaz</code> رو (همون فایلی که قبلاً از «📤 خروجی‌گرفتن» گرفتی) "
        "به‌صورتِ داکیومنت بفرست.\n\n"
        "⚠️ توجه: این کار همه‌ی دکمه‌ها، پنل‌ها، دسته‌ها و سرویس‌هایِ فعلیِ این ربات رو "
        "پاک می‌کنه و با محتوایِ فایل جایگزین می‌کنه — قابلِ‌برگشت نیست (مگه اینکه خودِ "
        "وضعیتِ فعلی رو هم قبلش بک‌آپ گرفته باشی).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data="tadm:backup:menu", style=ButtonStyle.DANGER)]]))
    await cb.answer()


@backup_router.message(BackupFSM.waiting_file, F.document)
async def backup_import_file(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or msg.from_user.id != row.owner_id:  # فقط مالک
        await state.clear()
        return
    doc: Document = msg.document
    if not (doc.file_name or "").lower().endswith(".botsaz"):
        await msg.answer("❌ فایل باید پسوندِ .botsaz داشته باشه. دوباره بفرست یا /cancel بزن.")
        return
    if doc.file_size and doc.file_size > 8 * 1024 * 1024:
        await msg.answer("❌ فایل خیلی بزرگه (بیشتر از ۸ مگابایت) — این یه بک‌آپِ معتبرِ botsaz نیست.")
        return
    file = await bot.get_file(doc.file_id)
    buf = await bot.download_file(file.file_path)
    try:
        data = json.loads(buf.read().decode("utf-8"))
    except Exception:
        await msg.answer("❌ فایل معتبر نیست (JSONِ سالم نبود). دوباره بفرست یا /cancel بزن.")
        return
    if data.get("format") != _FORMAT_NAME:
        await msg.answer("❌ این فایل یه بک‌آپِ botsaz نیست.")
        return
    if int(data.get("version") or 0) > _FORMAT_VERSION:
        await msg.answer("❌ این بک‌آپ با نسخه‌ی جدیدتری گرفته شده که این رباتِ فعلی پشتیبانی نمی‌کنه.")
        return
    backup_field = data.get("bot_field")
    if backup_field and row.field and backup_field != row.field:
        await msg.answer(
            f"❌ این بک‌آپ مالِ یه رباتِ «{backup_field}»ه، ولی این ربات از نوعِ «{row.field}»ه — "
            "این دو با هم سازگار نیستن.")
        return
    await state.update_data(backup_payload=data)
    summary = await _summary_line(data)
    await msg.answer(
        "⚠️ <b>مطمئنی؟</b>\n\nاین کار همه‌ی دکمه‌ها/پنل‌ها/دسته‌ها/سرویس‌هایِ فعلیِ این ربات رو "
        "کامل پاک و با این جایگزین می‌کنه:\n\n" + summary +
        "\n\n⚠️ این کار قابلِ‌برگشت نیست.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ بله، جایگزین کن", callback_data="tadm:backup:confirm", style=ButtonStyle.DANGER)],
            [InlineKeyboardButton(text="❌ نه، لغو کن", callback_data="tadm:backup:menu", style=ButtonStyle.DANGER)],
        ]))


@backup_router.message(BackupFSM.waiting_file, F.text)
async def backup_import_cancel(msg: Message, bot: Bot, state: FSMContext):
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "/cancel", "لغو"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    await msg.answer("❌ این یه فایل نبود. فایلِ .botsaz رو به‌صورتِ داکیومنت بفرست، یا /cancel بزن.")


@backup_router.callback_query(F.data == "tadm:backup:confirm")
async def backup_import_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    data = (await state.get_data()).get("backup_payload")
    await state.clear()
    if not row or cb.from_user.id != row.owner_id or not data:  # فقط مالک
        await cb.answer()
        return
    await cb.answer("⏳ در حالِ بازگردانی...")
    try:
        await _restore_backup(row.id, data)
    except Exception:
        logger.exception("خطا در بازگردانیِ بک‌آپ")
        await cb.message.answer("❌ بازگردانی با خطا مواجه شد. هیچ تغییری اعمال نشد (یا نیمه‌کاره موند — لطفاً وضعیتِ ربات رو چک کن).")
        return
    await cb.message.answer("✅ بازگردانی با موفقیت انجام شد.")


async def _restore_backup(bot_id: int, data: dict) -> None:
    t = data.get("tables", {})
    async with Session() as s:
        # === PATCH_BOT_BACKUP_V1 ===
        # پاک‌سازیِ کاملِ جدول‌هایِ مقصد، قبلِ درج — دقیقاً همون چیزی که تویِ
        # هشدارِ تأیید هم گفته شده بود.
        await s.execute(delete(SmmService).where(SmmService.bot_id == bot_id))
        await s.execute(delete(SmmCategory).where(SmmCategory.bot_id == bot_id))
        await s.execute(delete(SmmPanel).where(SmmPanel.bot_id == bot_id))
        await s.execute(delete(MainButton).where(MainButton.bot_id == bot_id))
        await s.execute(delete(Setting).where(Setting.bot_id == bot_id))
        # پنل/پلنِ VPN فقط وقتی جایگزین می‌شوند که خودِ بک‌آپ آن‌ها را داشته باشد
        # (بک‌آپ‌هایِ قدیمی این کلیدها را ندارند و نباید پنل‌هایِ فعلی را پاک کنند)
        if "vpn_plans" in t:
            await s.execute(delete(VpnPlan).where(VpnPlan.bot_id == bot_id))
        if "vpn_panels" in t:
            await s.execute(delete(VpnPanel).where(VpnPanel.bot_id == bot_id))
        await s.flush()

        for p in t.get("vpn_panels", []):
            if p.get("panel_key") and p.get("base_url"):
                s.add(VpnPanel(bot_id=bot_id, **{k: v for k, v in p.items() if k in _VPN_PANEL_FIELDS}))
        for p in t.get("vpn_plans", []):
            if p.get("title") is not None:
                s.add(VpnPlan(bot_id=bot_id, **{k: v for k, v in p.items() if k in _VPN_PLAN_FIELDS}))

        # تنظیمات — بدونِ ارجاعِ بین‌جدولی، ساده درج می‌شه
        for st in t.get("settings", []):
            if st.get("key"):
                s.add(Setting(bot_id=bot_id, key=st["key"], value=st.get("value")))

        # پنل‌ها — بدونِ ارجاع به جدولِ دیگه، اول می‌سازیمشون تا نگاشتِ
        # آیدیِ قدیم→جدید برایِ سرویس‌ها آماده باشه
        panel_map: dict[int, int] = {}
        for p in t.get("smm_panels", []):
            old_id = p.pop("_tmp_id", None)
            row_obj = SmmPanel(bot_id=bot_id, **{k: v for k, v in p.items() if k in _SMM_PANEL_FIELDS})
            s.add(row_obj)
            await s.flush()
            if old_id is not None:
                panel_map[old_id] = row_obj.id

        # دسته‌ها — همین‌طور
        cat_map: dict[int, int] = {}
        for c in t.get("smm_categories", []):
            old_id = c.pop("_tmp_id", None)
            row_obj = SmmCategory(bot_id=bot_id, **{k: v for k, v in c.items() if k in _SMM_CATEGORY_FIELDS})
            s.add(row_obj)
            await s.flush()
            if old_id is not None:
                cat_map[old_id] = row_obj.id

        # سرویس‌ها — panel_pk/category_pk رو با نگاشتِ بالا درست می‌کنیم
        for v in t.get("smm_services", []):
            old_panel_pk = v.pop("_tmp_panel_pk", None)
            old_cat_pk = v.pop("_tmp_category_pk", None)
            v.pop("_tmp_id", None)
            row_obj = SmmService(
                bot_id=bot_id,
                panel_pk=panel_map.get(old_panel_pk) if old_panel_pk is not None else None,
                category_pk=cat_map.get(old_cat_pk) if old_cat_pk is not None else None,
                **{k: v2 for k, v2 in v.items() if k in _SMM_SERVICE_FIELDS})
            s.add(row_obj)

        # دکمه‌ها — دوپاس، چون parent_id به یه دکمه‌یِ دیگه (که ممکنه هنوز
        # درج نشده باشه) اشاره می‌کنه
        btn_map: dict[int, int] = {}
        btn_parent_pending: list[tuple[int, int | None]] = []
        for b in t.get("main_buttons", []):
            old_id = b.pop("_tmp_id", None)
            old_parent = b.pop("_tmp_parent_id", None)
            row_obj = MainButton(bot_id=bot_id, parent_id=None,
                                 **{k: v for k, v in b.items() if k in _MAIN_BUTTON_FIELDS})
            s.add(row_obj)
            await s.flush()
            if old_id is not None:
                btn_map[old_id] = row_obj.id
            btn_parent_pending.append((row_obj.id, old_parent))
        for new_id, old_parent in btn_parent_pending:
            if old_parent is not None and old_parent in btn_map:
                obj = await s.get(MainButton, new_id)
                obj.parent_id = btn_map[old_parent]

        await s.commit()


# ═══════════════════ صفحه‌ی اصلیِ بخش ═══════════════════

async def _backup_menu_content(bot_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "💾 <b>بک‌آپِ کاملِ ربات</b>\n\n"
        "همه‌ی دکمه‌ها، پنل‌هایِ API، دسته‌بندی‌ها و سرویس‌هایِ رباتت رو تویِ یه فایلِ "
        "<code>.botsaz</code> ذخیره کن. اگه بعداً ربات رو عوض کردی (توکنِ جدید، ربات‌سازِ جدید)، "
        "همین فایل رو ایمپورت کن تا همه‌چی دقیقاً به همون حالتِ قبل برگرده.\n\n"
        "⚠️ فایلِ خروجی شاملِ کلیدهایِ API پنل‌هاته — نگهش دار ولی با کسی به اشتراک نذار.")
    _auto_lbl = "⛔️ خاموش"
    _dest_lbl = "پیویِ خودم"
    if bot_id:
        from tenant import get_setting as _gs
        from tenant.shared.backup_full import DEST_KEY, DEST_TITLE_KEY, dest_label
        _dest_lbl = dest_label(await _gs(bot_id, DEST_KEY, ""), await _gs(bot_id, DEST_TITLE_KEY, ""))
        _auto_lbl = {"7": "🗓 هفتگی", "1": "📆 روزانه"}.get(str(await _gs(bot_id, "auto_full_backup_days", "0")), "⛔️ خاموش")
    text += ("\n\n🗄 <b>بک‌آپِ کامل</b> علاوه بر این‌ها، کاربران، موجودیِ کیف‌پول‌ها، تراکنش‌ها، سفارش‌ها و تیکت‌ها را هم "
             "نگه می‌دارد؛ اگر سرور از بین رفت، فایل را به رباتِ جدید بده تا همه‌چیز برگردد.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 خروجی‌گرفتن (Export)", callback_data="tadm:backup:export", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="📥 بازگردانی (Import)", callback_data="tadm:backup:import", style=ButtonStyle.DANGER)],
        # === PATCH_BACKUP_FULL_V1 === بک‌آپِ کامل: کاربران/کیف‌پول/سفارش‌ها و ...
        [InlineKeyboardButton(text="🗄 بک‌آپِ کامل (با کاربران و کیف‌پول)", callback_data="tadm:bkfull:export",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="📥 بازگردانیِ کامل", callback_data="tadm:bkfull:import", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton(text=f"⏰ بک‌آپِ خودکار: {_auto_lbl}", callback_data="tadm:bkfull:auto",
                              style=ButtonStyle.PRIMARY)],
        # === PATCH_BACKUP_DEST_V1 === ارسال به گروه/کانال/پیویِ شخصِ دیگر
        [InlineKeyboardButton(text=f"📍 مقصدِ بک‌آپ: {_dest_lbl}", callback_data="tadm:bkfull:dest",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)],
    ])
    return text, kb


@backup_router.callback_query(F.data == "tadm:backup:menu")
async def backup_menu(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or cb.from_user.id != row.owner_id:  # فایل شاملِ رمز/کلیدِ پنل‌هاست — فقط مالک
        await cb.answer()
        return
    text, kb = await _backup_menu_content(row.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()
