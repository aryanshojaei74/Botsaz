"""
fake_join_guard.py — شناساییِ حمله‌ی جوینِ انبوه (burst join) در کانال/گروهِ
محافظت‌شده و کیکِ دسته‌جمعی با یک دکمه.

منطق:
    هر بار عضوِ جدیدی می‌آد، timestamp‌اش ثبت می‌شه. اگه یک زنجیره از
    جوین‌ها پیدا شه که فاصله‌ی هرکدوم تا بعدی حداکثر BURST_GAP_MAX_SEC
    ثانیه باشه (پشتِ سرِ هم، بدونِ وقفه‌ی طولانی) و تعدادِ کلِ زنجیره از
    BURST_MIN_COUNT بیشتر بشه → همه‌ی اعضایِ اون زنجیره «مشکوک به فیک»
    علامت می‌خورن و در دیتابیس ذخیره می‌شن.

    وقتی زنجیره برایِ اولین بار به آستانه رسید، یک پیام به ادمین
    (PLATFORM_ADMIN_ID) با دکمه‌ی inline فرستاده می‌شه: «🚨 حذف N عضو فیک»
    که با یک تپ همه‌شونو بن+آنبن (= کیک) می‌کنه.

نکته‌ی مهم دربارهٔ کانال‌ها:
    برایِ کانال (channel)، آپدیتِ جوین به‌صورتِ پیامِ «new_chat_members»
    نمی‌آد؛ باید هندلرِ chat_member رو داشته باشیم و در setWebhook مقدارِ
    allowed_updates حتماً شاملِ "chat_member" باشه (پیش‌فرضِ تلگرام این
    آپدیت رو نمی‌فرسته). این فایل هردو حالت (گروه + کانال) رو پوشش می‌ده.

نصب (این نسخه از قبل کاملاً سیم‌کشی شده — فقط برایِ مستندسازی):
    1) مدلِ FakeJoinCandidate در models.py اضافه شده (کنارِ GroupGuardCaptcha).
    2) روتر در platform_bot.py include شده:
           from fake_join_guard import fake_join_router
           router.include_router(fake_join_router)
    3) در main.py و main_platform.py، set_webhook پلتفرم حالا با
       allowed_updates=PLATFORM_ALLOWED_UPDATES (تعریف‌شده در
       webhook_utils.py) صدا زده می‌شه تا آپدیتِ "chat_member" (لازم برایِ
       شناساییِ جوینِ کانال) هم برسه.
    4) این ماژول از همون pset هایِ group_guard استفاده می‌کنه، یعنی فقط
       رویِ چتی که با /فعال_محافظ وصل شده فعاله (group_guard_chat_id).
"""
from __future__ import annotations

import asyncio
import datetime
import html as _html
import json
import time
from collections import deque

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select

from config import PLATFORM_ADMIN_ID, Session
from models import FakeJoinCandidate

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

fake_join_router = Router()

# ── تنظیماتِ آستانه (خواستِ شما: ۱ تا ۱۰ ثانیه، بالایِ ۲۰۰ نفر) ──
BURST_GAP_MAX_SEC = 10       # حداکثر فاصله‌ی مجاز بینِ دو جوینِ پیاپی
BURST_MIN_COUNT = 200        # حداقلِ تعداد برایِ اینکه «قطعاً فیک» باشه
BURST_ALERT_COOLDOWN_SEC = 120  # بعدِ هر هشدار، حداقل این‌قدر صبر کن قبلِ هشدارِ بعدی

# chat_id -> deque[(ts, user_id, full_name, username)]
_join_times: dict[int, deque] = {}
_last_alert_at: dict[int, float] = {}


async def _get_pset(key: str, default: str = "") -> str:
    from platform_bot import get_pset as _fn
    return await _fn(key, default)


async def _set_pset(key: str, value: str) -> None:
    from platform_bot import set_pset as _fn
    await _fn(key, value)


# === PATCH_FJG_PERSIST_V1 === فهرستِ جوین‌هایِ ۱۵ دقیقهٔ اخیر هر چت به‌صورتِ دوره‌ای در تنظیمات ذخیره می‌شود تا با
# ری‌استارت/نصبِ نسخهٔ تازه پاک نشود (قبلاً فقط در حافظه بود و /fakejoins بعد از ری‌استارت چیزی نداشت).
_WINDOW_SEC = 900
_loaded: set[int] = set()
_flush_pending: set[int] = set()
_FLUSH_DELAY = 3.0
_MAX_KEEP = 3000


async def _ensure_loaded(chat_id: int) -> None:
    if chat_id in _loaded:
        return
    _loaded.add(chat_id)
    try:
        items = json.loads(await _get_pset(f"fjg_recent_{chat_id}", "") or "[]")
    except (ValueError, TypeError):
        items = []
    now_e, now_m = time.time(), time.monotonic()
    dq = _join_times.setdefault(chat_id, deque())
    have = {e[1] for e in dq}
    restored = []
    for it in items:
        try:
            epoch, uid, name, uname = float(it[0]), int(it[1]), it[2], it[3]
        except (ValueError, TypeError, IndexError):
            continue
        age = now_e - epoch
        if 0 <= age <= _WINDOW_SEC and uid not in have:
            restored.append((now_m - age, uid, name, uname))
    if restored:
        _join_times[chat_id] = deque(sorted(list(dq) + restored, key=lambda e: e[0]))


async def _flush(chat_id: int) -> None:
    await asyncio.sleep(_FLUSH_DELAY)
    _flush_pending.discard(chat_id)
    try:
        now_e, now_m = time.time(), time.monotonic()
        dq = list(_join_times.get(chat_id, ()))[-_MAX_KEEP:]
        await _set_pset(f"fjg_recent_{chat_id}",
                        json.dumps([[round(now_e - (now_m - e[0]), 1), e[1], e[2], e[3]] for e in dq], ensure_ascii=False))
    except Exception:
        logger.exception("fake_join_guard: ذخیرهٔ فهرستِ جوین‌هایِ اخیر ناموفق بود (نادیده گرفته شد)")


def _schedule_flush(chat_id: int) -> None:
    if chat_id not in _flush_pending:
        _flush_pending.add(chat_id)
        asyncio.ensure_future(_flush(chat_id))


# === PATCH_FJG_CONFIGURABLE_V1 === آستانه‌ها قابل‌تنظیم‌اند (/fjgset)؛ پیش‌فرضِ تازه ۳۰ نفر (قبلاً ۲۰۰ و ۱۰۰ نفر هشدار نمی‌داد)
DEFAULT_MIN_COUNT = 30


def _to_int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(v).strip())))
    except (TypeError, ValueError):
        return default


async def _thresholds() -> tuple[int, int]:
    """(حداقلِ تعدادِ زنجیره, حداکثرِ فاصلهٔ ثانیه‌ایِ دو جوین)."""
    n = _to_int(await _get_pset("fjg_min_count", ""), DEFAULT_MIN_COUNT, 3, 5000)
    g = _to_int(await _get_pset("fjg_gap_sec", ""), BURST_GAP_MAX_SEC, 1, 120)
    return n, g


def _guard_kb(chat_id: int, count: int) -> InlineKeyboardMarkup:
    from premium_emoji import hx_button
    return InlineKeyboardMarkup(inline_keyboard=[
        [hx_button(f"🚨 حذف {count} عضو فیک", callback_data=f"fjg:kick:{chat_id}")],
        [hx_button("نادیده بگیر", callback_data=f"fjg:dismiss:{chat_id}")],
    ])


# === PATCH_FJG_CHANNELS_V1 === «/فعال_محافظ» گروهِ گفتگویِ کانال را وصل می‌کند، نه خودِ کانال؛ پس جوینِ مستقیم به کانال
# هیچ‌وقت دیده نمی‌شد. حالا کانال‌ها جدا با /fjgchat ثبت می‌شوند (pset: fjg_chats = فهرستِ آیدی‌ها با کاما).
async def _extra_chats() -> list[int]:
    out = []
    for x in (await _get_pset("fjg_chats", "")).split(","):
        x = x.strip()
        if x.lstrip("-").isdigit():
            out.append(int(x))
    return out


async def _tracked_chats() -> list[int]:
    out = await _extra_chats()
    g = (await _get_pset("group_guard_chat_id", "")).strip()
    if g.lstrip("-").isdigit() and (await _get_pset("group_guard_enabled", "off")) == "on" and int(g) not in out:
        out.append(int(g))
    return out


async def _is_guarded_chat(chat_id: int) -> bool:
    return chat_id in await _tracked_chats()


async def _register_join(bot: Bot, chat_id: int, user_id: int,
                          full_name: str, username: str | None) -> None:
    await _ensure_loaded(chat_id)
    now = time.monotonic()
    dq = _join_times.setdefault(chat_id, deque())
    dq.append((now, user_id, full_name, username))
    _schedule_flush(chat_id)

    # حذفِ ورودی‌هایی که خیلی قدیمی‌ان (بیشتر از یک پنجره‌ی معقول، مثلاً ۱۵ دقیقه)
    while dq and now - dq[0][0] > _WINDOW_SEC:
        dq.popleft()

    # زنجیره رو از انتها به اول می‌گردیم: تا وقتی فاصله‌ی پیاپی‌ها <= فاصلهٔ مجاز
    min_count, gap_max = await _thresholds()
    chain = [dq[-1]]
    for i in range(len(dq) - 2, -1, -1):
        if chain[-1][0] - dq[i][0] <= gap_max:
            chain.append(dq[i])
        else:
            break

    if len(chain) < min_count:
        return

    last = _last_alert_at.get(chat_id, 0)
    if now - last < BURST_ALERT_COOLDOWN_SEC:
        # قبلاً برایِ همین بحران هشدار دادیم؛ فقط کاندیدهایِ جدید رو به دیتابیس اضافه کن
        await _save_candidates(chat_id, chain)
        return
    _last_alert_at[chat_id] = now

    await _save_candidates(chat_id, chain)
    n = len(chain)
    try:
        await bot.send_message(
            PLATFORM_ADMIN_ID,
            f"⚠️ <b>حمله‌ی جوینِ انبوه شناسایی شد</b>\n"
            f"چت: <code>{chat_id}</code>\n"
            f"تعدادِ اعضایِ مشکوک (جوینِ پشتِ‌سرِهم با فاصله‌ی ≤{gap_max} ثانیه): "
            f"<b>{n}</b>\n\n"
            f"می‌تونی با دکمه‌ی زیر همه‌شونو یک‌جا حذف کنی:",
            reply_markup=_guard_kb(chat_id, n),
        )
    except Exception:
        logger.exception("خطا در ارسالِ هشدارِ فیک‌جوین به ادمین")


async def _save_candidates(chat_id: int, chain: list[tuple]) -> None:
    async with Session() as s:
        for _, uid, full_name, username in chain:
            existing = await s.scalar(select(FakeJoinCandidate).where(
                FakeJoinCandidate.chat_id == chat_id, FakeJoinCandidate.user_id == uid))
            if existing:
                continue
            s.add(FakeJoinCandidate(
                chat_id=chat_id, user_id=uid,
                full_name=(full_name or "")[:255],
                username=(username or None),
            ))
        await s.commit()


# ── هندلرِ عضویت در گروه (پیامِ سرویسیِ new_chat_members) ──
@fake_join_router.message(F.chat.type.in_({"group", "supergroup"}), F.new_chat_members)
async def fjg_group_join(msg: Message, bot: Bot):
    if not await _is_guarded_chat(msg.chat.id):
        return
    for member in msg.new_chat_members:
        if member.is_bot:
            continue
        await _register_join(bot, msg.chat.id, member.id, member.full_name, member.username)


# ── هندلرِ عضویت در کانال/سوپرگروه (آپدیتِ chat_member) ──
@fake_join_router.chat_member()
async def fjg_chat_member_join(event: ChatMemberUpdated, bot: Bot):
    if not await _is_guarded_chat(event.chat.id):
        return
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    became_member = old_status in ("left", "kicked", "restricted") and new_status in ("member", "restricted")
    if not became_member:
        return
    u = event.new_chat_member.user
    if u.is_bot:
        return
    await _register_join(bot, event.chat.id, u.id, u.full_name, u.username)



# === PATCH_FJG_RECENT_CMD_V1 === (مدیر: «الان یهو ۱۰۰ نفر فیک اومد، چطور حذف کنم؟»)
def _recent(chat_id: int, minutes: int) -> list[tuple]:
    now = time.monotonic()
    return [e for e in _join_times.get(chat_id, ()) if now - e[0] <= minutes * 60]


@fake_join_router.message(Command("fakejoins"), F.chat.type == "private")
async def fjg_recent_cmd(msg: Message, bot: Bot, command: CommandObject):
    """/fakejoins [دقیقه=10] — جوین‌هایِ N دقیقهٔ اخیرِ چت‌هایِ محافظت‌شده (گروه + کانال‌هایِ /fjgchat) را بدونِ انتظار برایِ آستانه
    برایِ حذفِ یک‌جا آماده می‌کند."""
    if not msg.from_user or msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    chats = await _tracked_chats()
    if not chats:
        await msg.answer("⚠️ هیچ کانال/گروهِ محافظت‌شده‌ای ثبت نشده. برایِ کانال: یک پستش را برایِ من فوروارد کن و رویش ریپلای بزن "
                         "<code>/fjgchat</code> (یا <code>/fjgchat -100…</code>). برایِ گروه: <code>/فعال_محافظ</code>.", parse_mode="HTML")
        return
    minutes = _to_int((command.args or "").strip() or 10, 10, 1, 15)      # فقط ۱۵ دقیقهٔ اخیر نگه داشته می‌شود
    found = False
    for chat_id in chats:
        await _ensure_loaded(chat_id)
        chain = _recent(chat_id, minutes)
        if not chain:
            continue
        found = True
        await _save_candidates(chat_id, chain)
        await msg.answer(
            f"🔎 چتِ <code>{chat_id}</code>: <b>{len(chain)}</b> جوین در {minutes} دقیقهٔ اخیر پیدا شد و برایِ حذف آماده است.\n"
            "⚠️ همهٔ این‌ها (حتی اگر یکی‌دو نفرشان واقعی باشند) با دکمهٔ زیر حذف می‌شوند؛ "
            "اگر مطمئن نیستی، «نادیده بگیر» را بزن و بازهٔ کوتاه‌تری بده (مثلاً <code>/fakejoins 3</code>).",
            reply_markup=_guard_kb(chat_id, len(chain)), parse_mode="HTML")
    if not found:
        await msg.answer(
            f"هیچ جوینی در {minutes} دقیقهٔ اخیر ثبت نشده (چت‌ها: {', '.join(str(c) for c in chats)}).\n\n"
            "علت‌هایِ ممکن: ربات در آن کانال ادمین نیست، تلگرام آپدیتِ chat_member را نفرستاده، یا آن جوین‌ها "
            "قبل از نصبِ این نسخه (که فهرست را ذخیره می‌کند) انجام شده‌اند.", parse_mode="HTML")


_CHAT_ARG_RE = __import__("re").compile(r"(-100\d{5,}|@[A-Za-z0-9_]{4,}|t\.me/([A-Za-z0-9_]{4,}))")


@fake_join_router.message(Command("fjgchat"), F.chat.type == "private")
async def fjg_chat_cmd(msg: Message, bot: Bot, command: CommandObject):
    """/fjgchat — کانال/گروه را به فهرستِ محافظ‌شده اضافه می‌کند: روی یک پستِ فورواردشده از کانال ریپلای بزن، یا
    /fjgchat -100… | @username بده. «/fjgchat حذف -100…» برایِ برداشتن؛ بدونِ آرگومان و بدونِ ریپلای فهرست را نشان می‌دهد."""
    if not msg.from_user or msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    args = (command.args or "").strip()
    cur = await _extra_chats()
    target = None
    rep = getattr(msg, "reply_to_message", None)
    if rep is not None:
        fo = getattr(rep, "forward_origin", None)
        if fo is not None and getattr(fo, "chat", None) is not None:
            target = fo.chat.id
        elif getattr(rep, "forward_from_chat", None) is not None:
            target = rep.forward_from_chat.id
    remove = args.startswith("حذف") or args.startswith("remove")
    if target is None and args:
        m = _CHAT_ARG_RE.search(args)
        if m:
            raw = m.group(1)
            if raw.startswith("t.me/"):
                raw = "@" + m.group(2)
            target = int(raw) if raw.lstrip("-").isdigit() else raw
    if target is None:
        listing = "\n".join(f"• <code>{c}</code>" for c in cur) or "— (هنوز کانالی ثبت نشده)"
        await msg.answer("📺 کانال‌هایِ تحتِ نظرِ ضدِ جوینِ انبوه:\n" + listing +
                         "\n\nافزودن: یک پستِ کانال را برایِ من فوروارد کن و رویش ریپلای بزن <code>/fjgchat</code>\n"
                         "(ربات باید در کانال ادمین باشد.) حذف: <code>/fjgchat حذف -100…</code>", parse_mode="HTML")
        return
    try:
        chat = await bot.get_chat(target)
    except Exception:
        await msg.answer("❌ به این چت دسترسی ندارم؛ مطمئن شو ربات آنجا ادمین است.")
        return
    cid = int(chat.id)
    if remove:
        cur = [c for c in cur if c != cid]
        await _set_pset("fjg_chats", ",".join(str(c) for c in cur))
        await msg.answer(f"🗑 «{_html.escape(chat.title or str(cid))}» از فهرست برداشته شد.")
        return
    if cid not in cur:
        cur.append(cid)
        await _set_pset("fjg_chats", ",".join(str(c) for c in cur))
    n, g = await _thresholds()
    await msg.answer(f"✅ «{_html.escape(chat.title or str(cid))}» (<code>{cid}</code>) تحتِ نظر است.\n"
                     f"هشدار: {n} نفر پشتِ‌سرِهم با فاصلهٔ ≤ {g} ثانیه. تغییر: <code>/fjgset</code>\n"
                     "⚠️ ربات باید در این چت ادمین باشد تا آپدیتِ عضویت را بگیرد.", parse_mode="HTML")


@fake_join_router.message(Command("fjgset"), F.chat.type == "private")
async def fjg_set_cmd(msg: Message, command: CommandObject):
    """/fjgset [تعداد] [فاصلهٔ ثانیه] — آستانهٔ هشدارِ جوینِ انبوه؛ بدونِ آرگومان مقدارِ فعلی را نشان می‌دهد."""
    if not msg.from_user or msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    parts = (command.args or "").split()
    if parts:
        n = _to_int(parts[0], 0, 3, 5000)
        if n <= 0:
            await msg.answer("فرمت: <code>/fjgset 30 10</code> (تعداد، فاصلهٔ ثانیه‌ایِ مجازِ بینِ دو جوین).", parse_mode="HTML")
            return
        await _set_pset("fjg_min_count", str(n))
        if len(parts) > 1:
            await _set_pset("fjg_gap_sec", str(_to_int(parts[1], BURST_GAP_MAX_SEC, 1, 120)))
    n, g = await _thresholds()
    await msg.answer(f"⚙️ هشدارِ جوینِ انبوه: وقتی <b>{n}</b> نفر پشتِ‌سرِهم با فاصلهٔ ≤ <b>{g}</b> ثانیه بیایند.\n"
                     "تغییر: <code>/fjgset 30 10</code> — جوین‌هایِ اخیر: <code>/fakejoins 10</code>", parse_mode="HTML")


# ── دکمه‌ی کیکِ دسته‌جمعی ──
@fake_join_router.callback_query(F.data.startswith("fjg:kick:"))
async def fjg_kick(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("این دکمه فقط برایِ مدیرِ اصلیه.", show_alert=True)
        return
    chat_id = int(cb.data.split(":")[2])
    async with Session() as s:
        rows = (await s.scalars(select(FakeJoinCandidate).where(
            FakeJoinCandidate.chat_id == chat_id))).all()
        ids = [r.user_id for r in rows]
        await s.execute(delete(FakeJoinCandidate).where(FakeJoinCandidate.chat_id == chat_id))
        await s.commit()

    if not ids:
        await cb.answer("چیزی برای حذف نمونده.", show_alert=True)
        return

    await cb.answer(f"در حالِ حذفِ {len(ids)} نفر... (ممکنه چند دقیقه طول بکشه)")
    ok, fail = 0, 0
    for uid in ids:
        try:
            await bot.ban_chat_member(chat_id, uid)
            await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # رعایتِ ریت‌لیمیتِ تلگرام

    try:
        await cb.message.edit_text(
            f"✅ <b>حذف انجام شد</b>\nموفق: {ok}\nناموفق: {fail}"
        )
    except Exception:
        logger.exception("خطا در ادیتِ پیامِ نتیجه")


@fake_join_router.callback_query(F.data.startswith("fjg:dismiss:"))
async def fjg_dismiss(cb: CallbackQuery):
    if cb.from_user.id != PLATFORM_ADMIN_ID:
        await cb.answer("این دکمه فقط برایِ مدیرِ اصلیه.", show_alert=True)
        return
    chat_id = int(cb.data.split(":")[2])
    async with Session() as s:
        await s.execute(delete(FakeJoinCandidate).where(FakeJoinCandidate.chat_id == chat_id))
        await s.commit()
    await cb.answer("لیست پاک شد.")
    try:
        await cb.message.edit_text("لغو شد؛ لیستِ مشکوک‌ها پاک شد.")
    except Exception:
        logger.debug("تمیزکاریِ ظاهریِ پیام ناموفق بود (بی‌اهمیت)")

