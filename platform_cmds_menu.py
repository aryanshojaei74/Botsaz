"""platform_cmds_menu.py — «🧰 دستورهایِ مدیر»: همهٔ دستورهایِ / رباتِ اصلی به‌صورتِ دکمه + توضیحِ هر کدام."""
from __future__ import annotations

import datetime as _dt

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, MessageEntity, Update

from config import PLATFORM_ADMIN_ID
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

cmds_router = Router()

GROUPS = [
    ("🛠", "مدیریتِ ربات‌ها و سرور"),
    ("🔑", "دسترسیِ ساختِ ربات"),
    ("📨", "پیام‌رسان‌هایِ دیگر"),
    ("🎁", "گیفت و ایموجی"),
    ("🛡", "محافظِ گروه و کانال"),
]

# (گروه، دستور، عنوان، توضیح، نمونهٔ استفاده، [(برچسبِ دکمه, متنِ کاملِ دستور)])
COMMANDS: list[tuple[int, str, str, str, str, list[tuple[str, str]]]] = [
    (0, "addays", "افزودنِ روز به همهٔ ربات‌ها",
     "برایِ جبرانِ قطعی: به همهٔ رباتِ فعالِ دارایِ سررسید چند روز اضافه می‌کند. اول پیش‌نمایش (چند ربات، چند بایگانی‌شده) را نشان می‌دهد و با تأییدِ تو اعمال می‌شود؛ جلوی اجرایِ دوباره در ۱۰ دقیقه را می‌گیرد.",
     "/addays  ← بعد تعدادِ روز را بفرست (مثلاً 2)",
     [("▶️ اجرا", "/addays")]),
    (0, "miniapp_all", "مینی‌اپ برایِ همهٔ ربات‌ها",
     "مینی‌اپ را برایِ همهٔ ربات‌هایِ مشتری یک‌جا روشن یا خاموش می‌کند.",
     "/miniapp_all on   یا   /miniapp_all off",
     [("▶️ روشن‌کردنِ همه", "/miniapp_all on"), ("⏹ خاموش‌کردنِ همه", "/miniapp_all off")]),
    (0, "offsite", "بک‌آپِ رمزگذاری‌شدهٔ بیرونِ سرور",
     "هر چند ساعت یک بار آخرین dump (و فایلِ .env) را با رمزِ تو رمزگذاری می‌کند و به تلگرامِ مدیر می‌فرستد تا اگر سرور از دسترس رفت، بک‌آپ سالم باشد. بدونِ رمز چیزی فرستاده نمی‌شود. بازیابی: scripts/decrypt_backup.py.",
     "/offsite  ← وضعیت و تنظیمات",
     [("▶️ اجرا", "/offsite")]),
    (0, "fakejoins", "جوین‌هایِ اخیرِ فیک",
     "جوین‌هایِ N دقیقهٔ اخیرِ چت‌هایِ محافظت‌شده را برایِ حذفِ یک‌جا آماده می‌کند.",
     "/fakejoins [دقیقه=10]",
     [("▶️ اجرا (۱۰ دقیقه)", "/fakejoins")]),
    (1, "bookgrant", "دسترسیِ ساختِ رباتِ رزرو",
     "به یک کاربر برایِ ساختِ رباتِ رزرو و نوبت‌دهی دسترسیِ تستی می‌دهد (چند روز = «پلنِ حرفه‌ای» رایگان به همان مدت). کار با آیدیِ عددیِ کاربر.",
     "/bookgrant <آیدی> [روز]",
     []),
    (1, "bookrevoke", "لغوِ دسترسیِ رزرو", "دسترسیِ ساختِ رباتِ رزروِ یک کاربر را می‌گیرد.", "/bookrevoke <آیدی>", []),
    (1, "booklist", "فهرستِ دسترسی‌هایِ رزرو", "فهرستِ کاربرانی که دسترسیِ ساختِ رباتِ رزرو دارند.", "/booklist",
     [("▶️ نمایش فهرست", "/booklist")]),
    (1, "pselgrant", "دسترسیِ ساختِ ربات پنل‌فروشی",
     "به یک کاربر برایِ ساختِ رباتِ پنل‌فروشی دسترسیِ تستی (چند روز یا دائمی) می‌دهد.", "/pselgrant <آیدی> [روز]", []),
    (1, "pselrevoke", "لغوِ دسترسیِ پنل‌فروشی", "دسترسیِ ساختِ رباتِ پنل‌فروشیِ یک کاربر را می‌گیرد.", "/pselrevoke <آیدی>", []),
    (1, "pselist", "فهرستِ دسترسی‌هایِ پنل‌فروشی", "فهرستِ کاربرانِ دارایِ دسترسیِ پنل‌فروشی.", "/pselist",
     [("▶️ نمایش فهرست", "/pselist")]),
    (2, "rubika", "پلِ روبیکا → پی‌وی مدیر",
     "توکنِ رباتی که در روبیکا ساخته‌ای ثبت می‌شود؛ پیام‌هایِ کاربران به پی‌ویِ تو می‌آید و ریپلایِ تو برایِ آن کاربر فرستاده می‌شود.",
     "/rubika  ← بعد توکن را بفرست", [("▶️ اجرا", "/rubika")]),
    (2, "splus", "پلِ سروش پلاس → پی‌وی مدیر", "همان پلِ روبیکا برایِ پیام‌رسانِ سروش پلاس.", "/splus  ← بعد توکن را بفرست",
     [("▶️ اجرا", "/splus")]),
    (2, "sadmin", "پنلِ مدیریتِ بات‌ساز در سروش پلاس",
     "توکنِ رباتِ سروش را ثبت می‌کنی تا پنلِ مدیریت (آمار، ربات، کیف پول، همگانی) با کدِ دسترسی در سروش باز شود. کارهایِ خطرناک دوباره کد می‌خواهند.",
     "/sadmin", [("▶️ اجرا", "/sadmin")]),
    (2, "tickets", "تیکت‌هایِ روبیکا و سروش پلاس", "فهرستِ تیکت‌هایِ پشتیبانیِ رباتِ روبیکا/سروش پلاس (باز، پاسخ‌داده‌شده، بسته).", "/tickets",
     [("▶️ نمایش تیکت‌ها", "/tickets")]),
    (2, "tkset", "تنظیماتِ تیکتِ روبیکا/سروش", "تنظیماتِ سیستمِ تیکتِ این دو پیام‌رسان (دسته‌ها و رفتارها).", "/tkset",
     [("▶️ اجرا", "/tkset")]),
    (3, "giftmap", "نگاشتِ خودکارِ گیفت → ایموجیِ پرمیوم",
     "گیفت‌هایِ تلگرام را با پکِ ایموجیِ گیفت‌ها مقایسه می‌کند و برایِ هر کدام ایموجیِ پرمیومِ مخصوص را ذخیره می‌کند (برایِ همهٔ ربات‌ها).",
     "/giftmap", [("▶️ اجرا", "/giftmap")]),
    (3, "giftmaplist", "فهرستِ نگاشتِ گیفت‌ها", "نگاشت‌هایِ ذخیره‌شدهٔ گیفت ← ایموجی را نشان می‌دهد.", "/giftmaplist",
     [("▶️ نمایش فهرست", "/giftmaplist")]),
    (3, "gifte", "نگاشتِ دستیِ یک گیفت",
     "برایِ گیفتی که خودکار پیدا نشد: شناسهٔ گیفت و یک ایموجیِ پرمیوم را در همان پیام بفرست (ایموجی باید پرمیوم باشد).",
     "/gifte <gift_id> 🧸", []),
    (3, "giftepack", "ثبتِ پکِ ایموجیِ گیفت‌ها", "پکِ ایموجیِ گیفت‌ها را برایِ نگاشت تعیین می‌کند.", "/giftepack", [("▶️ اجرا", "/giftepack")]),
    (3, "giftdump", "فایلِ تشخیصیِ گیفت‌ها",
     "مشخصاتِ استیکرِ گیفت‌ها و ایموجی‌هایِ پک را کنارِ هم در یک فایل می‌دهد تا کلیدِ تطبیق پیدا شود.", "/giftdump",
     [("▶️ اجرا", "/giftdump")]),
    (4, "fjgchat", "چت‌هایِ محافظ‌شده (جوینِ فیک)",
     "کانال/گروه را به فهرستِ محافظ‌شده اضافه می‌کند (ریپلای روی پستِ فورواردشده یا با آیدی). «حذف» برایِ برداشتن؛ بدونِ آرگومان فهرست را نشان می‌دهد.",
     "/fjgchat  یا  /fjgchat -100…  یا  /fjgchat حذف -100…", [("▶️ نمایش فهرست", "/fjgchat")]),
    (4, "fjgset", "آستانهٔ هشدارِ جوینِ انبوه",
     "تعدادِ جوین و فاصلهٔ ثانیه‌ایِ هشدار را تنظیم می‌کند؛ بدونِ آرگومان مقدارِ فعلی را نشان می‌دهد.", "/fjgset [تعداد] [فاصلهٔ ثانیه]",
     [("▶️ نمایشِ مقدارِ فعلی", "/fjgset")]),
]

INFO_ONLY = [
    ("/funstat", "جست‌وجویِ Fun Stat (آیدیِ عددی ↔ یوزرنیم و تاریخچه)؛ دکمه‌اش در «🔎 Fun Stat (تست)» هست."),
    ("/code <کد>", "ویژهٔ کاربر: کدِ معرفِ فروش را وارد می‌کند تا روی خریدِ اول تخفیف بگیرد."),
    ("/dice", "ویژهٔ گروه: بازیِ «بازی کن پول بگیر» را به همان گروه وصل می‌کند."),
    ("/hwreward، /gameson، /gamesoff", "دستورهایِ بازیِ گروهی؛ فقط داخلِ گروه/پی‌وی خودِ بازی کار می‌کنند."),
]


def _is_super(uid) -> bool:
    return bool(PLATFORM_ADMIN_ID) and uid is not None and int(uid) == int(PLATFORM_ADMIN_ID)


def _menu() -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🧰 <b>دستورهایِ مدیر</b>\n\nهر دکمه توضیحِ دستور را نشان می‌دهد و با «▶️ اجرا» همان دستور را برایِ تو اجرا می‌کند.\n"]
    rows = []
    for gi, (icon, gname) in enumerate(GROUPS):
        items = [(i, c) for i, c in enumerate(COMMANDS) if c[0] == gi]
        if not items:
            continue
        lines.append(f"\n{icon} <b>{gname}</b>")
        for i, c in items:
            lines.append(f"• <code>/{c[1]}</code> — {c[2]}")
        row = []
        for i, c in items:
            row.append(hx_button(f"/{c[1]}", callback_data=f"cmds:i:{i}", style=ButtonStyle.PRIMARY))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    lines.append("\n📌 <b>دستورهایِ دیگر (ویژهٔ کاربر/گروه)</b>")
    for cmd, desc in INFO_ONLY:
        lines.append(f"• <code>{cmd}</code> — {desc}")
    rows.append([hx_button("⬅️ بازگشت", callback_data="adm:cat:settings", style=ButtonStyle.DANGER)])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _detail(i: int) -> tuple[str, InlineKeyboardMarkup]:
    _g, cmd, title, desc, usage, runs = COMMANDS[i]
    txt = (f"🧰 <b>{title}</b>\n<code>/{cmd}</code>\n\n{desc}\n\n<b>نمونهٔ استفاده:</b>\n<code>{usage}</code>")
    rows = [[hx_button(lbl, callback_data=f"cmds:r:{i}:{k}", style=ButtonStyle.SUCCESS)] for k, (lbl, _t) in enumerate(runs)]
    if not runs:
        txt += "\n\nاین دستور آرگومان لازم دارد؛ متنش را با همین شکل در همین چت بنویس."
    rows.append([hx_button("⬅️ بازگشت به فهرست", callback_data="cmds:menu", style=ButtonStyle.DANGER)])
    return txt, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb: CallbackQuery, txt: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(txt, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        if "not modified" not in str(e).lower():
            await cb.message.answer(txt, reply_markup=kb, parse_mode="HTML")


def _root(r: Router):
    while getattr(r, "parent_router", None) is not None:
        r = r.parent_router
    return r


@cmds_router.callback_query(F.data == "cmds:menu")
async def cmds_menu(cb: CallbackQuery):
    if not _is_super(cb.from_user.id):
        await cb.answer("فقط مدیرِ اصلی", show_alert=True)
        return
    txt, kb = _menu()
    await _show(cb, txt, kb)
    await cb.answer()


@cmds_router.callback_query(F.data.regexp(r"^cmds:i:\d+$"))
async def cmds_info(cb: CallbackQuery):
    if not _is_super(cb.from_user.id):
        await cb.answer("فقط مدیرِ اصلی", show_alert=True)
        return
    i = int(cb.data.split(":")[2])
    if not 0 <= i < len(COMMANDS):
        await cb.answer("یافت نشد", show_alert=True)
        return
    txt, kb = _detail(i)
    await _show(cb, txt, kb)
    await cb.answer()


@cmds_router.callback_query(F.data.regexp(r"^cmds:r:\d+:\d+$"))
async def cmds_run(cb: CallbackQuery, bot: Bot):
    if not _is_super(cb.from_user.id):
        await cb.answer("فقط مدیرِ اصلی", show_alert=True)
        return
    _, _, i_s, k_s = cb.data.split(":")
    i, k = int(i_s), int(k_s)
    try:
        text = COMMANDS[i][5][k][1]
    except IndexError:
        await cb.answer("یافت نشد", show_alert=True)
        return
    await cb.answer("در حال اجرا…")
    cmd_len = len(text.split()[0])
    msg = Message(message_id=int(cb.message.message_id) + 1, date=_dt.datetime.now(), chat=cb.message.chat,
                  from_user=cb.from_user, text=text,
                  entities=[MessageEntity(type="bot_command", offset=0, length=cmd_len)])
    upd = Update(update_id=int(_dt.datetime.now().timestamp() * 1000), message=msg)
    try:
        await _root(cmds_router).feed_update(bot, upd.as_(bot))
    except Exception:
        logger.exception("cmds_menu: اجرایِ %s ناموفق بود", text)
        await cb.message.answer("❌ اجرا نشد؛ دستور را دستی بفرست: " + text)
