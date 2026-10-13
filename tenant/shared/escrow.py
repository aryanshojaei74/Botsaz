"""
escrow.py — PATCH_ESCROW_V1

«واسطه‌گری» (معامله با واسطه) برایِ رباتِ خدماتِ مجازی.

جریان:
  ۱) یکی از دو نفر (خریدار یا فروشنده) در ربات «درخواستِ معامله» می‌دهد: نقشِ خودش، نامِ محصول،
     مبلغ، و طرفِ مقابل (@یوزرنیم یا آیدیِ عددی؛ هر دو باید ربات را استارت کرده باشند).
  ۲) ربات نرخِ واسطه‌گری (درصدی که مالک تنظیم کرده) را رویِ مبلغ می‌گذارد و خلاصه را نشان می‌دهد.
  ۳) درخواست برایِ طرفِ مقابل می‌رود؛ اگر «پذیرفت»، یک گروه ساخته می‌شود.
  ۴) گروه با «اکانتِ واسط» (یک اکانتِ تلگرامیِ معمولی که مالک به ربات می‌دهد — رباتِ تلگرام نمی‌تواند
     گروه بسازد) ساخته می‌شود؛ نامِ گروه = نامِ محصول + شمارهٔ معامله. ربات و واسط‌ها در گروه ادمین
     می‌شوند، مبلغ و جزئیات در گروه ارسال و پین می‌شود.
  ۵) به خریدار و فروشنده هرکدام یک لینکِ ورودِ «یک‌بارمصرف» داده می‌شود.

اکانتِ واسط: Telethon StringSession (ابزارِ tools/make_telethon_session.py). اگر کتابخانه یا اکانت
آماده نباشد، معامله «ناموفق» ثبت و به مالک خبر داده می‌شود (قابلِ تلاشِ دوباره از پنلِ مالک).
"""
from __future__ import annotations

import html as _html
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from logging_setup import get_logger as _get_logger
from models import BotUser, EscrowDeal

logger = _get_logger(__name__)

escrow_router = Router(name="escrow")

FEE_PAYERS = {"buyer": "خریدار (روی مبلغ اضافه می‌شود)", "seller": "فروشنده (از مبلغ کم می‌شود)",
              "split": "نصف‌نصف"}
OPEN_STATUSES = ("pending", "accepted", "group_created")
STATUS_LABELS = {
    "pending": "⏳ منتظرِ پذیرشِ طرفِ مقابل", "accepted": "🔧 در حالِ ساختِ گروه",
    "group_created": "🟢 گروه ساخته شد — در جریان", "completed": "✅ تکمیل‌شده",
    "cancelled": "🚫 لغوشده", "rejected": "❌ ردشده", "failed": "⚠️ خطا در ساختِ گروه",
}


class EscNew(StatesGroup):
    title = State()
    amount = State()
    party = State()


class EscAdm(StatesGroup):
    fee = State()
    min_amount = State()
    max_amount = State()
    api_id = State()
    api_hash = State()
    session = State()
    mediators = State()


# ═══════════════════ منطقِ خالص (قابلِ تست) ═══════════════════
def _fa(t: str) -> str:
    return (t or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).strip()


def fmt(n) -> str:
    return f"{int(round(float(n))):,}"


def calc_fee(amount: int, percent: float, payer: str) -> dict:
    """نرخِ واسطه‌گری. buyer: خریدار = مبلغ + کارمزد | seller: فروشنده = مبلغ − کارمزد | split: نصف‌نصف."""
    amount = int(amount)
    fee = int(round(amount * float(percent) / 100.0))
    if payer == "seller":
        return {"fee": fee, "buyer_pays": amount, "seller_gets": amount - fee}
    if payer == "split":
        half = fee // 2
        return {"fee": fee, "buyer_pays": amount + half, "seller_gets": amount - (fee - half)}
    return {"fee": fee, "buyer_pays": amount + fee, "seller_gets": amount}


def parse_percent(txt: str) -> float | None:
    t = _fa(txt).replace("%", "").replace("٪", "").replace("٫", ".").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return round(v, 2) if 0 < v <= 50 else None


def parse_amount(txt: str) -> int | None:
    t = _fa(txt).replace(",", "").replace("،", "").replace(".", "")
    return int(t) if t.isdigit() and int(t) > 0 else None


def roles(deal) -> tuple[int, int]:
    """(buyer_id, seller_id)"""
    if deal.initiator_role == "buyer":
        return deal.initiator_tg, deal.counterparty_tg
    return deal.counterparty_tg, deal.initiator_tg


def group_title(deal) -> str:
    return f"{deal.title[:70]} | معامله #{deal.number}"


def deal_summary(deal, buyer_label: str = "", seller_label: str = "") -> str:
    c = calc_fee(deal.amount_toman, deal.fee_percent, deal.fee_payer)
    lines = [f"🤝 <b>معامله با واسطه{f' #{deal.number}' if deal.number else ''}</b>", "",
             f"📦 محصول: <b>{_html.escape(deal.title)}</b>",
             f"💰 مبلغِ معامله: <b>{fmt(deal.amount_toman)}</b> تومان",
             f"🧾 کارمزدِ واسطه ({deal.fee_percent:g}٪): <b>{fmt(c['fee'])}</b> تومان — {FEE_PAYERS.get(deal.fee_payer, '')}",
             f"💳 خریدار باید واریز کند: <b>{fmt(c['buyer_pays'])}</b> تومان",
             f"💵 فروشنده دریافت می‌کند: <b>{fmt(c['seller_gets'])}</b> تومان"]
    if buyer_label or seller_label:
        lines += ["", f"🛒 خریدار: {buyer_label or '—'}", f"🏷 فروشنده: {seller_label or '—'}"]
    return "\n".join(lines)


# ═══════════════════ ارائه‌دهندهٔ ساختِ گروه (Telethon) ═══════════════════
class EscrowGroupError(Exception):
    pass


class TelethonProvider:
    """ساختِ گروه با یک اکانتِ تلگرامیِ واقعی (StringSession). فقط وقتی لازم شد import می‌شود."""

    def __init__(self, api_id: int, api_hash: str, session: str):
        self.api_id, self.api_hash, self.session = int(api_id), api_hash, session

    def _client(self):
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as e:      # pragma: no cover
            raise EscrowGroupError("کتابخانهٔ telethon نصب نیست (pip install telethon)") from e
        return TelegramClient(StringSession(self.session), self.api_id, self.api_hash)

    async def whoami(self) -> str:
        c = self._client()
        await c.connect()
        try:
            if not await c.is_user_authorized():
                raise EscrowGroupError("نشستِ اکانت معتبر نیست؛ دوباره بساز.")
            me = await c.get_me()
            return f"{(me.first_name or '')} {(me.last_name or '')}".strip() + (f" (@{me.username})" if me.username else "")
        finally:
            await c.disconnect()

    async def create_group(self, title: str, about: str, bot_username: str, admins: list[str]) -> int:
        """گروهِ فوق‌گروه می‌سازد، ربات و واسط‌ها را ادمین می‌کند؛ شناسهٔ Bot API (‎-100…‎) را برمی‌گرداند."""
        from telethon.tl.functions.channels import CreateChannelRequest, EditAdminRequest, InviteToChannelRequest
        from telethon.tl.types import ChatAdminRights
        c = self._client()
        await c.connect()
        try:
            if not await c.is_user_authorized():
                raise EscrowGroupError("نشستِ اکانتِ واسط معتبر نیست.")
            res = await c(CreateChannelRequest(title=title[:120], about=about[:200], megagroup=True))
            ch = res.chats[0]
            bot_ent = await c.get_entity(bot_username.lstrip("@"))
            await c(InviteToChannelRequest(ch, [bot_ent]))
            rights = ChatAdminRights(change_info=True, delete_messages=True, ban_users=True, invite_users=True,
                                     pin_messages=True)
            await c(EditAdminRequest(ch, bot_ent, rights, rank="ربات"))
            for a in admins:
                try:
                    ent = await c.get_entity(int(a) if str(a).lstrip("-").isdigit() else str(a).lstrip("@"))
                    await c(InviteToChannelRequest(ch, [ent]))
                    await c(EditAdminRequest(ch, ent, ChatAdminRights(
                        change_info=True, delete_messages=True, ban_users=True, invite_users=True,
                        pin_messages=True), rank="واسط"))
                except Exception as e:
                    logger.warning("escrow: افزودنِ واسطِ %s به گروه ممکن نشد: %s", a, e)
            return int(f"-100{ch.id}")
        finally:
            await c.disconnect()


async def get_provider(bot_id: int):
    """ارائه‌دهندهٔ فعلیِ این ربات یا None (تست‌ها این تابع را جایگزین می‌کنند)."""
    from tenant import get_setting
    api_id = (await get_setting(bot_id, "escrow_api_id", "") or "").strip()
    api_hash = (await get_setting(bot_id, "escrow_api_hash", "") or "").strip()
    session = (await get_setting(bot_id, "escrow_session", "") or "").strip()
    if not (api_id.isdigit() and api_hash and session):
        return None
    return TelethonProvider(int(api_id), api_hash, session)


async def _cfg(bot_id: int) -> dict:
    from tenant import get_setting
    def _f(v, d):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d
    payer = await get_setting(bot_id, "escrow_fee_payer", "buyer")
    return {
        "enabled": (await get_setting(bot_id, "escrow_enabled", "0")) == "1",
        "percent": _f(await get_setting(bot_id, "escrow_fee_percent", "10"), 10.0),
        "payer": payer if payer in FEE_PAYERS else "buyer",
        "min": int(_f(await get_setting(bot_id, "escrow_min", "10000"), 10000)),
        "max": int(_f(await get_setting(bot_id, "escrow_max", "0"), 0)),
        "mediators": [x.strip() for x in (await get_setting(bot_id, "escrow_mediators", "") or "").split(",") if x.strip()],
    }


# ═══════════════════ جریانِ کاربر ═══════════════════
def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, cb, style=None):
    return InlineKeyboardButton(text=text, callback_data=cb, style=style)


async def _user_label(s, bot_id: int, tg: int) -> str:
    u = await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == tg))
    if not u:
        return str(tg)
    return f"{(u.full_name or '').strip() or tg}" + (f" (@{u.username})" if u.username else "")


async def resolve_party(bot_id: int, text: str):
    """@یوزرنیم یا آیدیِ عددی → BotUser همین ربات (باید ربات را استارت کرده باشد)."""
    t = _fa(text)
    async with Session() as s:
        if t.isdigit():
            return await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == int(t)))
        name = t.lstrip("@").strip()
        if not name:
            return None
        return await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id,
                                                    func.lower(BotUser.username) == name.lower()))


async def show_menu(msg: Message, bot: Bot, bot_id: int, header: str = "") -> None:
    cfg = await _cfg(bot_id)
    if not cfg["enabled"]:
        await msg.answer("⛔️ بخشِ واسطه‌گری فعلاً فعال نیست.")
        return
    txt = ("🤝 <b>معامله با واسطه</b>\n\n"
           "برایِ خرید و فروشِ امن بینِ دو نفر: ربات یک گروهِ اختصاصی با حضورِ واسط می‌سازد، مبلغ و "
           "جزئیات را همان‌جا ثبت می‌کند و به هر دو نفر لینکِ ورودِ یک‌بارمصرف می‌دهد.\n\n"
           f"🧾 کارمزدِ واسطه‌گری: <b>{cfg['percent']:g}٪</b> ({FEE_PAYERS[cfg['payer']]})\n"
           "هر دو طرف باید ربات را استارت کرده باشند و طرفِ مقابل درخواست را بپذیرد.")
    await msg.answer(txt, reply_markup=_kb([
        [_btn("➕ درخواستِ معاملهٔ جدید", "esc:new", ButtonStyle.SUCCESS)],
        [_btn("📂 معاملاتِ من", "esc:mine", ButtonStyle.PRIMARY)]]))


@escrow_router.message(F.text == "/escrow")
async def cmd_escrow(msg: Message, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if row:
        await show_menu(msg, bot, row.id)


@escrow_router.callback_query(F.data == "esc:new")
async def esc_new(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row or not (await _cfg(row.id))["enabled"]:
        return await cb.answer("واسطه‌گری فعال نیست.", show_alert=True)
    await state.clear()
    await cb.message.answer("👤 نقشِ <b>تو</b> در این معامله چیست؟", reply_markup=_kb([
        [_btn("🏷 من فروشنده‌ام", "esc:role:seller", ButtonStyle.PRIMARY),
         _btn("🛒 من خریدارم", "esc:role:buyer", ButtonStyle.SUCCESS)],
        [_btn("❌ لغو", "esc:cancel", ButtonStyle.DANGER)]]))
    await cb.answer()


@escrow_router.callback_query(F.data == "esc:cancel")
async def esc_cancel_flow(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("لغو شد.")


@escrow_router.callback_query(F.data.startswith("esc:role:"))
async def esc_role(cb: CallbackQuery, state: FSMContext):
    role = cb.data.split(":")[2]
    if role not in ("seller", "buyer"):
        return await cb.answer()
    await state.update_data(esc_role=role)
    await state.set_state(EscNew.title)
    await cb.message.answer("📦 <b>نامِ محصول/خدمت</b> را بنویس (مثلاً: «یوزرنیمِ @name» یا «۱۰۰۰ استارز»):")
    await cb.answer()


@escrow_router.message(EscNew.title, F.text)
async def esc_title(msg: Message, state: FSMContext):
    t = (msg.text or "").strip()
    if len(t) < 2 or t.startswith("/"):
        return await msg.answer("❌ یک نامِ معتبر بنویس.")
    await state.update_data(esc_title=t[:80])
    await state.set_state(EscNew.amount)
    await msg.answer("💰 <b>مبلغِ معامله</b> را به تومان بفرست (فقط عدد):")


@escrow_router.message(EscNew.amount, F.text)
async def esc_amount(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    cfg = await _cfg(row.id)
    amt = parse_amount(msg.text)
    if not amt:
        return await msg.answer("❌ فقط عدد (به تومان) بفرست.")
    if amt < cfg["min"]:
        return await msg.answer(f"❌ حداقلِ مبلغِ معامله {fmt(cfg['min'])} تومان است.")
    if cfg["max"] and amt > cfg["max"]:
        return await msg.answer(f"❌ حداکثرِ مبلغِ معامله {fmt(cfg['max'])} تومان است.")
    await state.update_data(esc_amount=amt)
    await state.set_state(EscNew.party)
    await msg.answer("👥 <b>طرفِ مقابل</b> را بفرست: @یوزرنیم یا آیدیِ عددی.\n"
                     "⚠️ او باید همین ربات را استارت کرده باشد.")


@escrow_router.message(EscNew.party, F.text)
async def esc_party(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    other = await resolve_party(row.id, msg.text)
    if not other:
        me = await bot.get_me()
        return await msg.answer(
            "❌ این کاربر پیدا نشد. او باید اول ربات را استارت کند:\n"
            f"https://t.me/{me.username}\nبعد دوباره @یوزرنیم یا آیدیِ عددی‌اش را بفرست.")
    if other.telegram_id == msg.from_user.id:
        return await msg.answer("❌ نمی‌توانی با خودت معامله کنی.")
    d = await state.get_data()
    cfg = await _cfg(row.id)
    deal_preview = EscrowDeal(bot_id=row.id, number=0, initiator_tg=msg.from_user.id,
                              counterparty_tg=other.telegram_id, initiator_role=d["esc_role"],
                              title=d["esc_title"], amount_toman=d["esc_amount"],
                              fee_percent=cfg["percent"], fee_toman=0, fee_payer=cfg["payer"])
    await state.update_data(esc_other=other.telegram_id)
    await state.set_state(None)
    other_label = f"{other.full_name or other.telegram_id}" + (f" (@{other.username})" if other.username else "")
    await msg.answer(deal_summary(deal_preview) + f"\n\n👥 طرفِ مقابل: {_html.escape(other_label)}\n\n"
                     "درخواست برایِ او ارسال شود؟", reply_markup=_kb([
        [_btn("✅ ارسالِ درخواست", "esc:send", ButtonStyle.SUCCESS)],
        [_btn("❌ لغو", "esc:cancel", ButtonStyle.DANGER)]]))


@escrow_router.callback_query(F.data == "esc:send")
async def esc_send(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    d = await state.get_data()
    if not row or not d.get("esc_other") or not d.get("esc_amount"):
        return await cb.answer("درخواست منقضی شده؛ دوباره شروع کن.", show_alert=True)
    cfg = await _cfg(row.id)
    await state.clear()
    async with Session() as s:
        n = (await s.scalar(select(func.max(EscrowDeal.number)).where(EscrowDeal.bot_id == row.id))) or 0
        deal = EscrowDeal(bot_id=row.id, number=int(n) + 1, initiator_tg=cb.from_user.id,
                          counterparty_tg=int(d["esc_other"]), initiator_role=d["esc_role"],
                          title=d["esc_title"], amount_toman=int(d["esc_amount"]),
                          fee_percent=cfg["percent"], fee_toman=calc_fee(d["esc_amount"], cfg["percent"], cfg["payer"])["fee"],
                          fee_payer=cfg["payer"], status="pending")
        s.add(deal)
        await s.commit()
        did, num = deal.id, deal.number
        buyer, seller = roles(deal)
        bl, sl = await _user_label(s, row.id, buyer), await _user_label(s, row.id, seller)
        init_label = await _user_label(s, row.id, deal.initiator_tg)
    role_fa = "فروشنده" if d["esc_role"] == "seller" else "خریدار"
    other_role = "خریدار" if d["esc_role"] == "seller" else "فروشنده"
    try:
        await bot.send_message(
            int(d["esc_other"]),
            f"🤝 <b>درخواستِ معامله با واسطه</b>\n\n{_html.escape(init_label)} از تو درخواستِ معامله دارد "
            f"(او <b>{role_fa}</b> و تو <b>{other_role}</b>).\n\n"
            + deal_summary(deal, bl, sl) + "\n\nاگر می‌پذیری، گروهِ معامله ساخته می‌شود.",
            reply_markup=_kb([[_btn("✅ می‌پذیرم", f"esc:acc:{did}", ButtonStyle.SUCCESS),
                               _btn("❌ رد", f"esc:rej:{did}", ButtonStyle.DANGER)]]))
        await cb.message.answer(f"✅ درخواستِ معامله #{num} برایِ طرفِ مقابل فرستاده شد؛ منتظرِ پذیرشِ او باش.")
    except Exception as e:
        logger.warning("escrow: ارسال به طرفِ مقابل ناموفق بود: %s", e)
        async with Session() as s:
            dd = await s.get(EscrowDeal, did)
            dd.status, dd.note = "failed", "طرفِ مقابل پیام را دریافت نکرد"
            await s.commit()
        await cb.message.answer("❌ پیام به طرفِ مقابل نرسید (شاید ربات را مسدود کرده). معامله لغو شد.")
    await cb.answer()


async def _load(did: int):
    async with Session() as s:
        return await s.get(EscrowDeal, did)


@escrow_router.callback_query(F.data.startswith("esc:rej:"))
async def esc_reject(cb: CallbackQuery, bot: Bot):
    did = int(cb.data.split(":")[2])
    async with Session() as s:
        d = await s.get(EscrowDeal, did)
        if not d or d.counterparty_tg != cb.from_user.id or d.status != "pending":
            return await cb.answer("این درخواست دیگر قابلِ رسیدگی نیست.", show_alert=True)
        d.status, d.closed_at = "rejected", datetime.utcnow()
        init, num = d.initiator_tg, d.number
        await s.commit()
    try:
        await cb.message.edit_text(f"❌ درخواستِ معامله #{num} را رد کردی.")
    except Exception:
        logger.info("escrow: یک ارسال/ویرایشِ پیام ممکن نشد (بی‌اهمیت)")
    try:
        await bot.send_message(init, f"❌ طرفِ مقابل درخواستِ معامله #{num} را رد کرد.")
    except Exception:
        logger.info("escrow: اطلاعِ ردِ معامله به درخواست‌دهنده ممکن نشد")
    await cb.answer()


@escrow_router.callback_query(F.data.startswith("esc:acc:"))
async def esc_accept(cb: CallbackQuery, bot: Bot):
    did = int(cb.data.split(":")[2])
    async with Session() as s:
        d = await s.get(EscrowDeal, did, with_for_update=True)
        if not d or d.counterparty_tg != cb.from_user.id or d.status != "pending":
            return await cb.answer("این درخواست دیگر قابلِ رسیدگی نیست.", show_alert=True)
        d.status, d.accepted_at = "accepted", datetime.utcnow()
        await s.commit()
    try:
        await cb.message.edit_text("✅ پذیرفتی. در حالِ ساختِ گروهِ معامله… (چند ثانیه)")
    except Exception:
        logger.info("escrow: یک ارسال/ویرایشِ پیام ممکن نشد (بی‌اهمیت)")
    await cb.answer()
    await open_deal_group(bot, did)


async def open_deal_group(bot: Bot, did: int) -> bool:
    """گروه را می‌سازد، لینک‌هایِ یک‌بارمصرف می‌دهد و پیام‌ها را می‌فرستد. True اگر موفق."""
    from tenant import get_setting
    async with Session() as s:
        d = await s.get(EscrowDeal, did)
        if not d or d.status not in ("accepted", "failed"):
            return False
        bot_id = d.bot_id
    cfg = await _cfg(bot_id)
    buyer, seller = roles(d)
    err = None
    chat_id = None
    try:
        provider = await get_provider(bot_id)
        if provider is None:
            raise EscrowGroupError("اکانتِ واسط هنوز تنظیم نشده")
        me = await bot.get_me()
        title = group_title(d)
        chat_id = await provider.create_group(
            title=title, about=f"معاملهٔ #{d.number} — {d.title}", bot_username=me.username or "",
            admins=cfg["mediators"])
        link_b = await bot.create_chat_invite_link(chat_id, member_limit=1, name=f"buyer #{d.number}")
        link_s = await bot.create_chat_invite_link(chat_id, member_limit=1, name=f"seller #{d.number}")
        async with Session() as s:
            bl, sl = await _user_label(s, bot_id, buyer), await _user_label(s, bot_id, seller)
        summary = deal_summary(d, bl, sl)
        m = await bot.send_message(
            chat_id,
            summary + "\n\n📌 قوانین: مبلغ را فقط پس از تأییدِ واسط واریز کنید و تحویلِ کالا/خدمت را همین‌جا اطلاع دهید.",
            reply_markup=_kb([[_btn("✅ پایانِ موفقِ معامله (فقط واسط)", f"esc:done:{d.id}", ButtonStyle.SUCCESS)],
                              [_btn("🚫 لغوِ معامله (فقط واسط)", f"esc:stop:{d.id}", ButtonStyle.DANGER)]]))
        try:
            await bot.pin_chat_message(chat_id, m.message_id, disable_notification=True)
        except Exception:
            logger.info("escrow: پینِ پیامِ گروه ممکن نشد")
        async with Session() as s:
            dd = await s.get(EscrowDeal, did)
            dd.status, dd.group_chat_id, dd.group_title = "group_created", chat_id, title
            dd.invite_buyer, dd.invite_seller, dd.note = link_b.invite_link, link_s.invite_link, None
            await s.commit()
        for uid, link, role in ((buyer, link_b.invite_link, "خریدار"), (seller, link_s.invite_link, "فروشنده")):
            try:
                await bot.send_message(
                    uid, f"🟢 <b>گروهِ معاملهٔ #{d.number} آماده است</b>\n\nنقشِ تو: <b>{role}</b>\n"
                         f"📦 {_html.escape(d.title)} — {fmt(d.amount_toman)} تومان\n\n"
                         "🔗 لینکِ ورودِ <b>اختصاصی و یک‌بارمصرف</b> (فقط برایِ خودت؛ به کسی نده):",
                    reply_markup=_kb([[InlineKeyboardButton(text="🚪 ورود به گروهِ معامله", url=link)]]))
            except Exception:
                logger.warning("escrow: ارسالِ لینک به %s ممکن نشد", uid)
        await _notify_owner(bot, bot_id, f"🤝 معاملهٔ #{d.number} ({d.title} — {fmt(d.amount_toman)} تومان) آغاز شد؛ گروه ساخته شد.")
        return True
    except Exception as e:
        err = str(e)[:300]
        logger.warning("escrow: ساختِ گروهِ معامله #%s ناموفق بود: %s", did, e)
    async with Session() as s:
        dd = await s.get(EscrowDeal, did)
        dd.status, dd.note = "failed", err
        await s.commit()
    for uid in (buyer, seller):
        try:
            await bot.send_message(uid, f"⚠️ ساختِ گروهِ معاملهٔ #{d.number} با مشکل روبه‌رو شد. "
                                        "مدیر خبردار شد و به‌زودی پیگیری می‌کند.")
        except Exception:
            logger.info("escrow: یک ارسال/ویرایشِ پیام ممکن نشد (بی‌اهمیت)")
    await _notify_owner(bot, bot_id, f"⚠️ ساختِ گروهِ معاملهٔ #{d.number} ناموفق بود:\n<code>{_html.escape(err or '')}</code>\n"
                                     "بعد از رفعِ مشکل، از «🤝 واسطه‌گری ← معاملات» دوباره تلاش کن.")
    return False


async def _notify_owner(bot: Bot, bot_id: int, text: str) -> None:
    from tenant import get_bot_row_by_id
    try:
        row = await get_bot_row_by_id(bot_id)
        if row and row.owner_id:
            await bot.send_message(int(row.owner_id), text)
    except Exception:
        logger.info("escrow: اطلاع به مالک ممکن نشد")


async def _can_close(cb: CallbackQuery, d) -> bool:
    from tenant import get_bot_row_by_id, is_owner_or_admin
    row = await get_bot_row_by_id(d.bot_id)
    if row and await is_owner_or_admin(d.bot_id, cb.from_user.id, row.owner_id):
        return True
    cfg = await _cfg(d.bot_id)
    uname = (getattr(cb.from_user, "username", None) or "").lower()
    return str(cb.from_user.id) in cfg["mediators"] or (uname and f"@{uname}" in [m.lower() for m in cfg["mediators"]])


async def _close_deal(cb: CallbackQuery, bot: Bot, status: str, label: str):
    did = int(cb.data.split(":")[2])
    async with Session() as s:
        d = await s.get(EscrowDeal, did, with_for_update=True)
        if not d:
            return await cb.answer("پیدا نشد.", show_alert=True)
        if not await _can_close(cb, d):
            return await cb.answer("فقط واسط/مدیر می‌تواند این کار را بکند.", show_alert=True)
        if d.status not in ("group_created", "accepted", "failed", "pending"):
            return await cb.answer("این معامله قبلاً بسته شده.", show_alert=True)
        d.status, d.closed_at = status, datetime.utcnow()
        num = d.number
        buyer, seller = roles(d)
        await s.commit()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(f"{label} (معاملهٔ #{num})")
    except Exception:
        logger.info("escrow: یک ارسال/ویرایشِ پیام ممکن نشد (بی‌اهمیت)")
    for uid in (buyer, seller):
        try:
            await bot.send_message(uid, f"{label} — معاملهٔ #{num}")
        except Exception:
            logger.info("escrow: یک ارسال/ویرایشِ پیام ممکن نشد (بی‌اهمیت)")
    await cb.answer("انجام شد")


@escrow_router.callback_query(F.data.startswith("esc:done:"))
async def esc_done(cb: CallbackQuery, bot: Bot):
    await _close_deal(cb, bot, "completed", "✅ معامله با موفقیت پایان یافت")


@escrow_router.callback_query(F.data.startswith("esc:stop:"))
async def esc_stop(cb: CallbackQuery, bot: Bot):
    await _close_deal(cb, bot, "cancelled", "🚫 معامله لغو شد")


@escrow_router.callback_query(F.data == "esc:mine")
async def esc_mine(cb: CallbackQuery, bot: Bot):
    from tenant import get_bot_row
    row = await get_bot_row(bot)
    if not row:
        return await cb.answer()
    async with Session() as s:
        deals = (await s.scalars(select(EscrowDeal).where(
            EscrowDeal.bot_id == row.id,
            (EscrowDeal.initiator_tg == cb.from_user.id) | (EscrowDeal.counterparty_tg == cb.from_user.id)
        ).order_by(EscrowDeal.id.desc()).limit(10))).all()
    if not deals:
        await cb.message.answer("📭 هنوز معامله‌ای نداری.")
        return await cb.answer()
    lines = ["📂 <b>معاملاتِ من</b>\n"]
    rows = []
    for d in deals:
        lines.append(f"#{d.number} — {_html.escape(d.title)} — {fmt(d.amount_toman)} ت — {STATUS_LABELS.get(d.status, d.status)}")
        if d.status == "group_created" and d.group_chat_id:
            link = d.invite_buyer if roles(d)[0] == cb.from_user.id else d.invite_seller
            if link:
                rows.append([InlineKeyboardButton(text=f"🚪 گروهِ #{d.number}", url=link)])
    await cb.message.answer("\n".join(lines), reply_markup=_kb(rows) if rows else None)
    await cb.answer()


# ═══════════════════ پنلِ مدیریتِ مالک ═══════════════════
async def _gate(cb_or_msg, bot: Bot):
    from tenant import _owner_ok, _owner_ok_msg
    from plans import Feature, has_feature
    is_msg = isinstance(cb_or_msg, Message)
    row = await (_owner_ok_msg(cb_or_msg, bot) if is_msg else _owner_ok(cb_or_msg, bot))
    if not row:
        return None
    if getattr(row, "field", None) != "virtual_service" or not has_feature(row.plan, Feature.ESCROW):
        txt = "این قابلیت فقط برایِ رباتِ خدماتِ مجازی (پلنِ رشد به بالا) فعال است."
        await (cb_or_msg.answer(txt) if is_msg else cb_or_msg.answer(txt, show_alert=True))
        return None
    return row


async def _admin_view(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import get_setting
    cfg = await _cfg(bot_id)
    has_acc = bool((await get_setting(bot_id, "escrow_session", "")).strip())
    async with Session() as s:
        open_n = await s.scalar(select(func.count()).select_from(EscrowDeal).where(
            EscrowDeal.bot_id == bot_id, EscrowDeal.status.in_(OPEN_STATUSES + ("failed",)))) or 0
    txt = ("🤝 <b>واسطه‌گری (معامله با واسطه)</b>\n\n"
           f"وضعیت: {'✅ فعال' if cfg['enabled'] else '❌ غیرفعال'}\n"
           f"🧾 نرخِ واسطه: <b>{cfg['percent']:g}٪</b> — کارمزد را می‌پردازد: <b>{FEE_PAYERS[cfg['payer']]}</b>\n"
           f"🔽 حداقلِ مبلغ: <b>{fmt(cfg['min'])}</b> | 🔼 حداکثر: <b>{fmt(cfg['max']) if cfg['max'] else 'بدونِ سقف'}</b>\n"
           f"👤 اکانتِ واسط (سازندهٔ گروه): {'✅ متصل' if has_acc else '❌ تنظیم نشده'}\n"
           f"🛡 واسط‌هایِ ادمینِ گروه: {', '.join(cfg['mediators']) or '— (فقط اکانتِ واسط و ربات)'}\n"
           f"📂 معاملاتِ باز/ناموفق: <b>{open_n}</b>")
    rows = [
        [_btn("⛔️ غیرفعال‌کردن" if cfg["enabled"] else "✅ فعال‌کردن", "esc:adm:toggle",
              ButtonStyle.DANGER if cfg["enabled"] else ButtonStyle.SUCCESS)],
        [_btn(f"🧾 نرخ: {cfg['percent']:g}٪", "esc:adm:fee"), _btn("💳 پرداخت‌کنندهٔ کارمزد", "esc:adm:payer")],
        [_btn("🔽 حداقل", "esc:adm:min"), _btn("🔼 حداکثر", "esc:adm:max")],
        [_btn("👤 اتصالِ اکانتِ واسط", "esc:adm:acc", ButtonStyle.PRIMARY),
         _btn("🧪 تستِ اتصال", "esc:adm:test")],
        [_btn("🛡 واسط‌هایِ گروه", "esc:adm:med")],
        [_btn("📂 معاملات", "esc:adm:deals", ButtonStyle.PRIMARY)],
        [_btn("⬅️ بازگشت", "tadm:sec:settings", ButtonStyle.DANGER)],
    ]
    return txt, _kb(rows)


async def admin_menu(cb, bot: Bot, state: FSMContext | None = None):
    """نقطهٔ ورودِ منویِ تنظیمات (ثبت‌شده در settings registry؛ در حالتِ کیبوردی هم صدا زده می‌شود)."""
    row = await _gate(cb, bot)
    if not row:
        return
    if state is not None:
        await state.clear()
    txt, kb = await _admin_view(row.id)
    m = cb.message
    try:
        await m.edit_text(txt, reply_markup=kb)
    except Exception:
        await m.answer(txt, reply_markup=kb)
    await cb.answer()


@escrow_router.callback_query(F.data == "set:escrow")
async def _adm_entry(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await admin_menu(cb, bot, state)


async def _refresh(cb: CallbackQuery, bot: Bot):
    row = await _gate(cb, bot)
    if row:
        txt, kb = await _admin_view(row.id)
        try:
            await cb.message.edit_text(txt, reply_markup=kb)
        except Exception:
            await cb.message.answer(txt, reply_markup=kb)


@escrow_router.callback_query(F.data == "esc:adm:toggle")
async def adm_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, set_setting
    row = await _gate(cb, bot)
    if not row:
        return
    cur = (await get_setting(row.id, "escrow_enabled", "0")) == "1"
    if not cur and not (await get_setting(row.id, "escrow_session", "")).strip():
        return await cb.answer("اول اکانتِ واسط را وصل کن.", show_alert=True)
    await set_setting(row.id, "escrow_enabled", "0" if cur else "1")
    await _refresh(cb, bot)
    await cb.answer("خاموش شد" if cur else "فعال شد")


@escrow_router.callback_query(F.data == "esc:adm:payer")
async def adm_payer(cb: CallbackQuery, bot: Bot):
    from tenant import get_setting, set_setting
    row = await _gate(cb, bot)
    if not row:
        return
    order = list(FEE_PAYERS)
    cur = await get_setting(row.id, "escrow_fee_payer", "buyer")
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "buyer"
    await set_setting(row.id, "escrow_fee_payer", nxt)
    await _refresh(cb, bot)
    await cb.answer(FEE_PAYERS[nxt], show_alert=True)


_ASK = {
    "fee": (EscAdm.fee, "🧾 نرخِ واسطه‌گری را به درصد بفرست (مثلاً <code>10</code> یا <code>2.5</code>؛ حداکثر ۵۰):"),
    "min": (EscAdm.min_amount, "🔽 حداقلِ مبلغِ معامله را به تومان بفرست:"),
    "max": (EscAdm.max_amount, "🔼 حداکثرِ مبلغِ معامله را به تومان بفرست (<code>0</code> = بدونِ سقف):"),
    "acc": (EscAdm.api_id, "👤 <b>اتصالِ اکانتِ واسط</b>\n\nاین اکانت گروه‌ها را می‌سازد. اول از my.telegram.org بخشِ "
                           "API development tools، <b>api_id</b> (عدد) را بفرست:"),
    "med": (EscAdm.mediators, "🛡 آیدیِ عددی یا @یوزرنیمِ واسط‌هایی که باید در هر گروه ادمین شوند را با کاما بفرست "
                              "(یا <code>-</code> برایِ هیچ‌کس):"),
}


@escrow_router.callback_query(F.data.regexp(r"^esc:adm:(fee|min|max|acc|med)$"))
async def adm_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _gate(cb, bot)
    if not row:
        return
    st, text = _ASK[cb.data.split(":")[2]]
    await state.set_state(st)
    await cb.message.answer(text, reply_markup=_kb([[_btn("❌ لغو", "set:escrow", ButtonStyle.DANGER)]]))
    await cb.answer()


async def _adm_done(msg: Message, bot: Bot, state: FSMContext, ok_text: str):
    await state.clear()
    await msg.answer(ok_text)
    row = await _gate(msg, bot)
    if row:
        txt, kb = await _admin_view(row.id)
        await msg.answer(txt, reply_markup=kb)


@escrow_router.message(EscAdm.fee, F.text)
async def adm_fee(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _gate(msg, bot)
    if not row:
        return
    v = parse_percent(msg.text)
    if v is None:
        return await msg.answer("❌ عددی بینِ بیشتر از ۰ تا ۵۰ بفرست.")
    await set_setting(row.id, "escrow_fee_percent", f"{v:g}")
    await _adm_done(msg, bot, state, f"✅ نرخِ واسطه‌گری {v:g}٪ شد.")


@escrow_router.message(EscAdm.min_amount, F.text)
async def adm_min(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _gate(msg, bot)
    if not row:
        return
    v = parse_amount(msg.text)
    if not v:
        return await msg.answer("❌ فقط عدد بفرست.")
    await set_setting(row.id, "escrow_min", str(v))
    await _adm_done(msg, bot, state, f"✅ حداقلِ مبلغ {fmt(v)} تومان شد.")


@escrow_router.message(EscAdm.max_amount, F.text)
async def adm_max(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _gate(msg, bot)
    if not row:
        return
    t = _fa(msg.text).replace(",", "")
    if not t.isdigit():
        return await msg.answer("❌ فقط عدد بفرست (۰ = بدونِ سقف).")
    await set_setting(row.id, "escrow_max", str(int(t)))
    await _adm_done(msg, bot, state, "✅ ذخیره شد.")


@escrow_router.message(EscAdm.mediators, F.text)
async def adm_mediators(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _gate(msg, bot)
    if not row:
        return
    t = (msg.text or "").strip()
    items = [] if t == "-" else [x.strip() for x in _fa(t).replace("،", ",").split(",") if x.strip()]
    bad = [x for x in items if not (x.lstrip("@").replace("_", "").isalnum())]
    if bad:
        return await msg.answer("❌ فقط آیدیِ عددی یا @یوزرنیم، با کاما جدا شده.")
    await set_setting(row.id, "escrow_mediators", ",".join(items))
    await _adm_done(msg, bot, state, "✅ واسط‌هایِ گروه ذخیره شد.")


@escrow_router.message(EscAdm.api_id, F.text)
async def adm_api_id(msg: Message, bot: Bot, state: FSMContext):
    row = await _gate(msg, bot)
    if not row:
        return
    t = _fa(msg.text)
    if not t.isdigit():
        return await msg.answer("❌ api_id فقط عدد است.")
    await state.update_data(esc_api_id=t)
    await state.set_state(EscAdm.api_hash)
    await msg.answer("🔑 حالا <b>api_hash</b> را بفرست:")


@escrow_router.message(EscAdm.api_hash, F.text)
async def adm_api_hash(msg: Message, bot: Bot, state: FSMContext):
    row = await _gate(msg, bot)
    if not row:
        return
    h = (msg.text or "").strip()
    if len(h) < 16 or " " in h:
        return await msg.answer("❌ api_hash معتبر نیست.")
    await state.update_data(esc_api_hash=h)
    await state.set_state(EscAdm.session)
    await msg.answer(
        "🧩 <b>StringSession</b> اکانتِ واسط را بفرست.\n\nبا ابزارِ <code>tools/make_telethon_session.py</code> "
        "(روی کامپیوتر/سرورِ خودت) ساخته می‌شود؛ پس از فرستادن، پیامت پاک می‌شود.")
    try:
        await msg.delete()
    except Exception:
        logger.info("escrow: یک ارسال/ویرایشِ پیام ممکن نشد (بی‌اهمیت)")


@escrow_router.message(EscAdm.session, F.text)
async def adm_session(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _gate(msg, bot)
    if not row:
        return
    sess = (msg.text or "").strip()
    try:
        await msg.delete()           # رشتهٔ نشست حساس است
    except Exception:
        logger.info("escrow: یک ارسال/ویرایشِ پیام ممکن نشد (بی‌اهمیت)")
    if len(sess) < 40 or " " in sess:
        return await msg.answer("❌ StringSession معتبر نیست.")
    d = await state.get_data()
    prov = TelethonProvider(int(d["esc_api_id"]), d["esc_api_hash"], sess)
    try:
        who = await prov.whoami()
    except Exception as e:
        return await msg.answer(f"❌ اتصال ناموفق بود:\n<code>{_html.escape(str(e)[:300])}</code>\nدوباره StringSession را بفرست.")
    await set_setting(row.id, "escrow_api_id", d["esc_api_id"])
    await set_setting(row.id, "escrow_api_hash", d["esc_api_hash"])
    await set_setting(row.id, "escrow_session", sess)
    await _adm_done(msg, bot, state, f"✅ اکانتِ واسط وصل شد: {who}")


@escrow_router.callback_query(F.data == "esc:adm:test")
async def adm_test(cb: CallbackQuery, bot: Bot):
    row = await _gate(cb, bot)
    if not row:
        return
    prov = await get_provider(row.id)
    if prov is None:
        return await cb.answer("اکانتِ واسط تنظیم نشده.", show_alert=True)
    await cb.answer("در حالِ تست…")
    try:
        who = await prov.whoami()
        await cb.message.answer(f"✅ اتصال برقرار است: {who}")
    except Exception as e:
        await cb.message.answer(f"❌ اتصال ناموفق:\n<code>{_html.escape(str(e)[:300])}</code>")


@escrow_router.callback_query(F.data == "esc:adm:deals")
async def adm_deals(cb: CallbackQuery, bot: Bot):
    row = await _gate(cb, bot)
    if not row:
        return
    async with Session() as s:
        deals = (await s.scalars(select(EscrowDeal).where(EscrowDeal.bot_id == row.id)
                                 .order_by(EscrowDeal.id.desc()).limit(15))).all()
    if not deals:
        await cb.message.answer("📭 هنوز معامله‌ای ثبت نشده.")
        return await cb.answer()
    rows = [[_btn(f"#{d.number} {d.title[:20]} — {STATUS_LABELS.get(d.status, d.status)}", f"esc:adm:v:{d.id}")]
            for d in deals]
    rows.append([_btn("⬅️ بازگشت", "set:escrow", ButtonStyle.DANGER)])
    await cb.message.answer("📂 <b>معاملات (۱۵ موردِ آخر)</b>", reply_markup=_kb(rows))
    await cb.answer()


@escrow_router.callback_query(F.data.startswith("esc:adm:v:"))
async def adm_deal_view(cb: CallbackQuery, bot: Bot):
    row = await _gate(cb, bot)
    if not row:
        return
    did = int(cb.data.split(":")[3])
    async with Session() as s:
        d = await s.get(EscrowDeal, did)
        if not d or d.bot_id != row.id:
            return await cb.answer("پیدا نشد.", show_alert=True)
        buyer, seller = roles(d)
        bl, sl = await _user_label(s, row.id, buyer), await _user_label(s, row.id, seller)
    txt = deal_summary(d, bl, sl) + f"\n\n📌 وضعیت: {STATUS_LABELS.get(d.status, d.status)}"
    if d.note:
        txt += f"\n⚠️ {_html.escape(d.note)}"
    rows = []
    if d.status in ("failed", "accepted"):
        rows.append([_btn("🔁 تلاشِ دوباره برایِ ساختِ گروه", f"esc:adm:retry:{d.id}", ButtonStyle.PRIMARY)])
    if d.status in OPEN_STATUSES + ("failed",):
        rows.append([_btn("✅ تکمیل", f"esc:done:{d.id}", ButtonStyle.SUCCESS),
                     _btn("🚫 لغو", f"esc:stop:{d.id}", ButtonStyle.DANGER)])
    rows.append([_btn("⬅️ بازگشت", "esc:adm:deals", ButtonStyle.DANGER)])
    await cb.message.answer(txt, reply_markup=_kb(rows))
    await cb.answer()


@escrow_router.callback_query(F.data.startswith("esc:adm:retry:"))
async def adm_retry(cb: CallbackQuery, bot: Bot):
    row = await _gate(cb, bot)
    if not row:
        return
    did = int(cb.data.split(":")[3])
    await cb.answer("در حالِ تلاش…")
    ok = await open_deal_group(bot, did)
    await cb.message.answer("✅ گروه ساخته شد." if ok else "❌ باز هم ناموفق بود؛ جزئیاتِ خطا در صفحهٔ معامله است.")
