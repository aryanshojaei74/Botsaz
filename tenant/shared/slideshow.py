"""
tenant/slideshow.py
-------------------
قابلیت اسلایدشو (چندرسانه‌ای چند صفحه‌ای) برای دکمه‌های اصلی.

ویژگی‌ها:
  • کاربر: صفحه‌بندی با دکمه‌های «بعدی ▶️» / «◀️ قبلی» + «🏠 بازگشت»
  • دکمه شیشه‌ای لینک‌دار برای هر اسلاید (قابل تنظیم از ادمین)
  • دکمه خرید محصول برای هر اسلاید (قابل تنظیم از ادمین)
  • ادمین: افزودن / ویرایش / حذف اسلایدها مرحله‌به‌مرحله
  • پشتیبانی کامل از عکس، ویدیو، فایل، و متن فرمت‌دار تلگرام
"""

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument,
)
from sqlalchemy import text
from config import Session
from models import MainButton, SlideFavorite, Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

slideshow_router = Router(name="slideshow")


# === PATCH_TENANT_SCOPE_V1 ===
# همهٔ دکمه‌هایِ مدیریتِ اسلاید «sla:<action>:<button_id>[:<slide_id>…]» هستند و
# اسلاید/دکمه را فقط با id می‌خواندند/حذف می‌کردند؛ ادمینِ یک ربات با callbackِ
# دست‌ساز اسلایدهایِ رباتِ دیگری را حذف/ویرایش می‌کرد. این‌جا یک‌جا چک می‌شود
# که دکمه مالِ همین ربات و اسلاید مالِ همان دکمه است.
_SLA_PRODUCT_3RD = ("newprodpick",)   # در این‌ها جزءِ چهارم idِ محصول است، نه اسلاید


async def _sla_scope_mw(handler, event, data):
    cbd = getattr(event, "data", None) or ""
    if not cbd.startswith("sla:"):
        return await handler(event, data)
    parts = cbd.split(":")
    try:
        button_id = int(parts[2])
        slide_id = int(parts[3]) if len(parts) > 3 and parts[1] not in _SLA_PRODUCT_3RD else None
    except (IndexError, ValueError):
        await event.answer()
        return None
    from tenant import get_bot_row
    from models import MainButton
    row = await get_bot_row(data.get("bot"))
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
        ok = bool(row and btn and btn.bot_id == row.id)
        if ok and slide_id is not None:
            owner_btn = (await s.execute(text(
                "SELECT button_id FROM button_slides WHERE id=:sid"), {"sid": slide_id})).scalar()
            ok = owner_btn == button_id
    if not ok:
        await event.answer("یافت نشد.", show_alert=True)
        return None
    return await handler(event, data)


slideshow_router.callback_query.middleware(_sla_scope_mw)

# === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
# کمک‌تابع‌های سبکِ Setting — عمداً مستقیم از مدلِ Setting استفاده می‌کنن
# (نه ایمپورت از tenant/__init__.py) چون این ماژول از اونجا import می‌شه؛
# ایمپورتِ برعکس باعثِ circular import می‌شد.

async def _get_setting_local(bot_id: int, key: str, default: str = "") -> str:
    from sqlalchemy import select
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
    return r.value if (r and r.value is not None) else default


async def _set_setting_local(bot_id: int, key: str, value: str) -> None:
    from sqlalchemy import select
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
        if r:
            r.value = value
        else:
            s.add(Setting(bot_id=bot_id, key=key, value=value))
        await s.commit()


def _pin_start_key(bot_id: int) -> str:
    return "start_slideshow_btn_id"


def _support_key(button_id: int) -> str:
    return f"slideshow_support_{button_id}"


async def _slides_kb_flags(button_id: int) -> tuple[bool, bool]:
    """=== PATCH_SLIDE_LIST_TOGGLE_STATE_FIX_V1 ===
    قبلاً خیلی‌جا وقتی بعدِ یه کار (تغییرِ نام/مدیا/کپشن/حذفِ اسلاید) دوباره
    لیستِ اسلایدها ساخته می‌شد، به‌جای خوندنِ وضعیتِ واقعیِ «نمایشِ خودکار
    موقعِ /start» و «دکمهٔ پشتیبانی»، مقدارهای پیش‌فرض (هردو False/خاموش)
    پاس داده می‌شد — یعنی حتی اگه واقعاً روشن بودن، تو نمایش «خاموش» دیده
    می‌شدن (خودِ تنظیمات دست‌نخورده می‌موند، فقط نمایش اشتباه بود). این
    تابع وضعیتِ واقعی رو می‌خونه تا همه‌جا یکسان و درست استفاده بشه."""
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
    bot_id = btn.bot_id if btn else 0
    pinned = (await _get_setting_local(bot_id, _pin_start_key(bot_id), "")) == str(button_id)
    support_on = (await _get_setting_local(bot_id, _support_key(button_id), "0")) == "1"
    return pinned, support_on


async def is_favorited(user_telegram_id: int, slide_id: int) -> bool:
    from sqlalchemy import select
    async with Session() as s:
        r = await s.scalar(select(SlideFavorite.id).where(
            SlideFavorite.user_telegram_id == user_telegram_id,
            SlideFavorite.slide_id == slide_id))
    return r is not None


async def toggle_favorite(bot_id: int, user_telegram_id: int, button_id: int, slide_id: int) -> bool:
    """علاقه‌مندی را toggle می‌کند؛ خروجی وضعیتِ جدید (True=الان علاقه‌منده)."""
    from sqlalchemy import select
    async with Session() as s:
        r = await s.scalar(select(SlideFavorite).where(
            SlideFavorite.user_telegram_id == user_telegram_id,
            SlideFavorite.slide_id == slide_id))
        if r:
            await s.delete(r)
            await s.commit()
            return False
        s.add(SlideFavorite(bot_id=bot_id, user_telegram_id=user_telegram_id,
                            button_id=button_id, slide_id=slide_id))
        await s.commit()
        return True



# ─────────────────────── FSM ───────────────────────

class SlideFSM(StatesGroup):
    waiting_media     = State()   # ادمین در حال ارسال مدیا/متن
    waiting_caption   = State()   # ادمین در حال ویرایش کپشن یک اسلاید موجود
    waiting_btn_label = State()   # ادمین در حال تایپ برچسب دکمه لینک
    waiting_btn_url   = State()   # ادمین در حال تایپ لینک دکمه
    waiting_admin_name = State()  # === PATCH_SLIDE_ADMIN_NAME_V1 === ادمین در حال تایپ نامِ داخلیِ اسلاید


# ─────────────────────── helpers ───────────────────────

async def get_slides(button_id: int) -> list[dict]:
    """لیست اسلایدهای یک دکمه را برمی‌گرداند."""
    async with Session() as s:
        rows = (await s.execute(
            text("SELECT id,step_num,caption,media_file_id,media_type,"
                 "btn_url,btn_label,buy_product_id,admin_name "
                 "FROM button_slides WHERE button_id=:bid ORDER BY step_num"),
            {"bid": button_id}
        )).fetchall()
    return [
        {"id": r[0], "step_num": r[1], "caption": r[2],
         "media_file_id": r[3], "media_type": r[4],
         "btn_url": r[5], "btn_label": r[6], "buy_product_id": r[7],
         "admin_name": r[8]}
        for r in rows
    ]


async def save_slide_name(slide_id: int, admin_name: str | None) -> None:
    """=== PATCH_SLIDE_ADMIN_NAME_V1 ===
    فقط اسمِ داخلیِ اسلاید رو عوض می‌کنه — تابعِ جداگانه (نه بخشی از
    save_slide) تا هیچ ویرایشِ دیگه‌ای (مدیا/کپشن/دکمه) بی‌خبر این فیلد رو
    پاک نکنه."""
    async with Session() as s:
        await s.execute(text("UPDATE button_slides SET admin_name=:n WHERE id=:sid"),
                        {"n": admin_name, "sid": slide_id})
        await s.commit()


async def save_slide(button_id: int, step_num: int,
                     caption: str | None,
                     media_file_id: str | None,
                     media_type: str | None,
                     slide_id: int | None = None,
                     btn_url: str | None = None,
                     btn_label: str | None = None,
                     buy_product_id: int | None = None) -> int:
    async with Session() as s:
        if slide_id:
            await s.execute(text(
                "UPDATE button_slides SET caption=:c, media_file_id=:m, media_type=:mt,"
                "btn_url=:bu, btn_label=:bl, buy_product_id=:bpid "
                "WHERE id=:sid"
            ), {"c": caption, "m": media_file_id, "mt": media_type,
                "bu": btn_url, "bl": btn_label, "bpid": buy_product_id, "sid": slide_id})
        else:
            # === PATCH_SLIDE_INSERT_RETURNING_V1 ===
            # قبلاً از res.lastrowid استفاده می‌شد که فقط رویِ SQLite کار می‌کنه.
            # رویِ PostgreSQL/asyncpg خطای AttributeError می‌داد و به کاربر
            # پیامِ «خطای پیش‌بینی‌نشده» نشون می‌داد (خطاهای ERR-163907، ERR-163926).
            # RETURNING id هم روی PostgreSQL و هم روی SQLite 3.35+ کار می‌کنه.
            res = await s.execute(text(
                "INSERT INTO button_slides "
                "(button_id,step_num,caption,media_file_id,media_type,btn_url,btn_label,buy_product_id) "
                "VALUES (:bid,:sn,:c,:m,:mt,:bu,:bl,:bpid) RETURNING id"
            ), {"bid": button_id, "sn": step_num, "c": caption,
                "m": media_file_id, "mt": media_type,
                "bu": btn_url, "bl": btn_label, "bpid": buy_product_id})
            row = res.first()
            slide_id = row[0] if row else None
        await s.commit()
    return slide_id


async def save_slide_buttons(slide_id: int,
                              btn_url: str | None,
                              btn_label: str | None,
                              buy_product_id: int | None = None):
    """فقط فیلدهای دکمه‌های یک اسلاید را آپدیت می‌کند."""
    async with Session() as s:
        await s.execute(text(
            "UPDATE button_slides SET btn_url=:bu, btn_label=:bl, buy_product_id=:bpid WHERE id=:sid"
        ), {"bu": btn_url, "bl": btn_label, "bpid": buy_product_id, "sid": slide_id})
        await s.commit()


async def delete_slide(slide_id: int):
    # === PATCH_SLIDE_DELETE_FK_V1 ===
    # از error.log: حذفِ اسلاید با ForeignKeyViolationError شکست می‌خورد
    # («update or delete on table "button_slides" violates foreign key
    # constraint "slide_favorites_slide_id_fkey"») — چون اگه حتی یک کاربر
    # اون اسلاید رو به علاقه‌مندی‌هاش اضافه کرده بود، ردیفِ slide_favorites
    # هنوز بهش اشاره می‌کرد. الان اول علاقه‌مندی‌هایِ همون اسلاید پاک می‌شن.
    async with Session() as s:
        await s.execute(text("DELETE FROM slide_favorites WHERE slide_id=:sid"), {"sid": slide_id})
        await s.execute(text("DELETE FROM button_slides WHERE id=:sid"), {"sid": slide_id})
        await s.commit()


async def renumber_slides(button_id: int):
    """شماره‌گذاری مجدد پس از حذف."""
    async with Session() as s:
        rows = (await s.execute(
            text("SELECT id FROM button_slides WHERE button_id=:bid ORDER BY step_num"),
            {"bid": button_id}
        )).fetchall()
        for i, (sid,) in enumerate(rows, 1):
            await s.execute(
                text("UPDATE button_slides SET step_num=:n WHERE id=:sid"),
                {"n": i, "sid": sid}
            )
        await s.commit()


async def _get_product_info(product_id: int) -> dict | None:
    """اطلاعات کامل محصول را از دیتابیس می‌گیرد."""
    try:
        from models import Product
        async with Session() as s:
            p = await s.get(Product, product_id)
        if not p:
            return None
        return {
            "id": p.id,
            "title": p.title,
            "description": p.description,
            "price": p.price,
            # === PATCH_PRODUCT_GIFT_LINK_V1 ===
            "gift_link_url": getattr(p, "gift_link_url", None),
        }
    except Exception:
        return None


async def _get_product_title(product_id: int) -> str | None:
    """عنوان محصول را از دیتابیس می‌گیرد."""
    info = await _get_product_info(product_id)
    return info["title"] if info else None


def _product_caption_block(product: dict) -> str:
    """
    بلوک متنی اطلاعات محصول — راست‌چین با RLM
    ترتیب: عنوان، خط جدا، توضیحات، کد محصول، قیمت
    """
    rlm = "\u200f"  # Right-to-Left Mark
    sep = "┈┅━━⊷⊰✨⊱⊶━━┅┈"
    lines = []
    lines.append(f"{rlm}<b>{product['title']}</b>")
    lines.append(sep)
    if product.get("description"):
        lines.append(f"{rlm}{product['description']}")
        lines.append("")
    lines.append(f"{rlm}🔖 کد محصول: <code>#{product['id']}</code>")
    price = int(product["price"])
    if price > 0:
        lines.append(f"{rlm}💰 قیمت: <b>{price:,} تومان</b>")
    else:
        lines.append(f"{rlm}💰 قیمت: <b>رایگان</b>")
    return "\n".join(lines)


# ─────────────────────── نمایش اسلاید به کاربر ───────────────────────

def _user_slide_kb(button_id: int, idx: int, total: int,
                   back_cb: str = "mbtn:home",
                   slide: dict | None = None,
                   is_fav: bool = False,
                   support_btn: InlineKeyboardButton | None = None,
                   bot_username: str | None = None) -> InlineKeyboardMarkup:
    """کیبورد ناوبری کاربر با دکمه‌های شیشه‌ای و خرید.

    === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
    اضافه‌شده: ⭐ علاقه‌مندی، 📤 اشتراک‌گذاری، 💬 پشتیبانی (اختیاری/روشن‌خاموش‌شونده).
    """
    rows = []

    # ردیف ۱: دکمه لینک (تکی، بالا) — سبز
    if slide and slide.get("btn_url"):
        label = slide.get("btn_label") or "🔗 مشاهده"
        rows.append([InlineKeyboardButton(text=label, url=slide["btn_url"],
                                          style=ButtonStyle.SUCCESS)])

    # ردیف ۲: ناوبری قبلی / بعدی (همیشه نمایش داده می‌شن، کنار هم - RTL: بعدی چپ، قبلی راست) — آبی
    nav = []
    if idx < total - 1:
        nav.append(InlineKeyboardButton(
            text="بعدی ◀️", callback_data=f"sls:view:{button_id}:{idx + 1}:{back_cb}",
            style=ButtonStyle.PRIMARY))
    else:
        nav.append(InlineKeyboardButton(
            text="بعدی ◀️", callback_data="sls:noop",
            style=ButtonStyle.PRIMARY))
    if idx > 0:
        nav.append(InlineKeyboardButton(
            text="▶️ قبلی", callback_data=f"sls:view:{button_id}:{idx - 1}:{back_cb}",
            style=ButtonStyle.PRIMARY))
    else:
        nav.append(InlineKeyboardButton(
            text="▶️ قبلی", callback_data="sls:noop",
            style=ButtonStyle.PRIMARY))
    rows.append(nav)

    # ردیف ۳: دکمه خرید محصول — سبز (با اطلاعات بازگشت به اسلاید)
    if slide and slide.get("buy_product_id"):
        rows.append([InlineKeyboardButton(
            text="🛒 خرید محصول",
            callback_data=f"sls:buyprod:{button_id}:{idx}:{slide['buy_product_id']}:{back_cb}",
            style=ButtonStyle.SUCCESS
        )])

    # ردیف ۴: ⭐ علاقه‌مندی + 📤 اشتراک‌گذاری — کنار هم
    extra = []
    if slide:
        fav_label = "⭐️ در علاقه‌مندی‌هاست" if is_fav else "⭐ افزودن به علاقه‌مندی‌ها"
        extra.append(InlineKeyboardButton(
            text=fav_label, callback_data=f"sls:fav:{button_id}:{idx}:{back_cb}",
            style=(ButtonStyle.SUCCESS if is_fav else ButtonStyle.PRIMARY)))
    if bot_username and slide:
        share_deeplink = f"https://t.me/{bot_username}?start=slide_{button_id}_{slide.get('step_num', idx + 1)}"
        share_url = "https://t.me/share/url?url=" + share_deeplink
        extra.append(InlineKeyboardButton(text="📤 اشتراک‌گذاری", url=share_url))
    if extra:
        rows.append(extra)

    # ردیف ۵: پشتیبانی (اختیاری، فقط اگه ادمین روشن کرده و آدرس تنظیم شده باشه)
    if support_btn:
        rows.append([support_btn])

    # ردیف ۶: دکمه بازگشت با رنگ دانجر (پایین‌ترین)
    rows.append([InlineKeyboardButton(
        text="🏠 بازگشت", callback_data=back_cb, style=ButtonStyle.DANGER)])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _build_caption(sl: dict, idx: int | None = None, total: int | None = None) -> str:
    """کپشن نهایی اسلاید — اگه محصول وصله فقط مشخصات محصول نشون بده.

    === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
    اگه idx/total داده بشه، یه خطِ شمارشگر («📊 ۱ از ۲») بالای کپشن اضافه می‌شه.
    """
    counter = f"📊 {idx + 1} از {total}\n\n" if (idx is not None and total is not None and total > 1) else ""
    product_id = sl.get("buy_product_id")
    if product_id:
        product = await _get_product_info(int(product_id))
        if product:
            return counter + _product_caption_block(product)
    return counter + (sl.get("caption") or "")


async def send_slide(target: Message | CallbackQuery, slides: list[dict],
                     idx: int, bot: Bot, back_cb: str = "mbtn:home",
                     edit: bool = False):
    """ارسال یا ویرایش یک اسلاید."""
    sl = slides[idx]
    total = len(slides)
    caption = await _build_caption(sl)
    fid = sl["media_file_id"]
    mtype = sl["media_type"]

    # === PATCH_PRODUCT_GIFT_LINK_V1 ===
    # اگه محصولِ وصل‌شده به این اسلاید لینک گیفت پرمیوم داشته باشه، اسلاید
    # به‌جای عکس، به‌صورت متن + پیش‌نمایشِ بزرگِ لینک گیفت (تصویر متحرک)
    # نمایش داده می‌شه.
    gift_link = None
    if sl.get("buy_product_id"):
        _pinfo = await _get_product_info(int(sl["buy_product_id"]))
        if _pinfo and _pinfo.get("gift_link_url"):
            gift_link = _pinfo["gift_link_url"]
    if gift_link:
        caption = f"{gift_link}\n\n{caption}"

    kb = _user_slide_kb(
        int(target.data.split(":")[2]) if isinstance(target, CallbackQuery) and edit else
        (slides[0].get("_button_id", 0)),
        idx, total, back_cb, sl
    )

    msg = target.message if isinstance(target, CallbackQuery) else target

    if edit and isinstance(target, CallbackQuery):
        # با گیفت، پیام باید متنی باشه — edit_media جواب نمی‌ده؛ می‌ریم سراغِ حذف+ارسالِ مجدد
        if not gift_link:
            try:
                if fid and mtype == "photo":
                    await target.message.edit_media(
                        InputMediaPhoto(media=fid, caption=caption, parse_mode="HTML"),
                        reply_markup=kb)
                elif fid and mtype == "video":
                    await target.message.edit_media(
                        InputMediaVideo(media=fid, caption=caption, parse_mode="HTML"),
                        reply_markup=kb)
                elif fid and mtype == "document":
                    await target.message.edit_media(
                        InputMediaDocument(media=fid, caption=caption, parse_mode="HTML"),
                        reply_markup=kb)
                else:
                    await target.message.edit_text(caption or "—", reply_markup=kb,
                                                   parse_mode="HTML")
                return
            except Exception:
                logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        try:
            await target.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")

    # === PATCH_PRODUCT_GIFT_LINK_V1 ===
    if gift_link:
        from aiogram.types import LinkPreviewOptions
        await msg.answer(
            caption or "—", reply_markup=kb, parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                url=gift_link, prefer_large_media=True, show_above_text=False))
        return

    if fid and mtype == "photo":
        await msg.answer_photo(fid, caption=caption, reply_markup=kb, parse_mode="HTML")
    elif fid and mtype == "video":
        await msg.answer_video(fid, caption=caption, reply_markup=kb, parse_mode="HTML")
    elif fid and mtype == "document":
        await msg.answer_document(fid, caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        await msg.answer(caption or "—", reply_markup=kb, parse_mode="HTML")


async def show_slideshow_to_user(msg: Message, button_id: int, bot: Bot,
                                  back_cb: str = "mbtn:home", start_step: int | None = None):
    """نقطه ورود از _exec_main_button.

    === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
    start_step (اختیاری): اگه داده بشه، به‌جایِ همیشه از اسلایدِ اول، از
    همون step_num شروع می‌کنه (برای وقتی از دیپ‌لینکِ اشتراک‌گذاری میاد).
    """
    from models import Bot as BotRow
    slides = await get_slides(button_id)
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
    if not slides:
        fallback = (btn.message if btn else None) or "این بخش هنوز محتوایی ندارد."
        await msg.answer(fallback)
        return
    for sl in slides:
        sl["_button_id"] = button_id
    start_idx = 0
    if start_step is not None:
        for i, sl in enumerate(slides):
            if sl["step_num"] == start_step:
                start_idx = i
                break
    bot_id = btn.bot_id if btn else 0
    user_id = msg.from_user.id if msg.from_user else 0
    # === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
    support_btn = None
    if (await _get_setting_local(bot_id, _support_key(button_id), "0")) == "1":
        from tenant import support_button
        support_btn = await support_button(bot_id)
    fav = await is_favorited(user_id, slides[start_idx]["id"]) if user_id else False
    bot_username = None
    if bot_id:
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        bot_username = brow.username if brow else None
    kb = _user_slide_kb(button_id, start_idx, len(slides), back_cb, slides[start_idx],
                        is_fav=fav, support_btn=support_btn, bot_username=bot_username)
    sl = slides[start_idx]
    caption = await _build_caption(sl, start_idx, len(slides))
    fid = sl["media_file_id"]
    mtype = sl["media_type"]
    # === PATCH_PRODUCT_GIFT_LINK_V1 ===
    gift_link = None
    if sl.get("buy_product_id"):
        _pinfo = await _get_product_info(int(sl["buy_product_id"]))
        if _pinfo and _pinfo.get("gift_link_url"):
            gift_link = _pinfo["gift_link_url"]
    if gift_link:
        from aiogram.types import LinkPreviewOptions
        await msg.answer(
            f"{gift_link}\n\n{caption}" if caption else gift_link,
            reply_markup=kb, parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                url=gift_link, prefer_large_media=True, show_above_text=False))
        return
    if fid and mtype == "photo":
        await msg.answer_photo(fid, caption=caption, reply_markup=kb, parse_mode="HTML")
    elif fid and mtype == "video":
        await msg.answer_video(fid, caption=caption, reply_markup=kb, parse_mode="HTML")
    elif fid and mtype == "document":
        await msg.answer_document(fid, caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        await msg.answer(caption or "—", reply_markup=kb, parse_mode="HTML")


@slideshow_router.callback_query(F.data == "sls:noop")
async def sls_noop(cb: CallbackQuery):
    """دکمه قبلی/بعدی در اسلاید اول/آخر — هیچ‌کاری نمی‌کند."""
    await cb.answer()


@slideshow_router.callback_query(F.data.startswith("sls:view:"))
async def sls_view(cb: CallbackQuery, bot: Bot):
    """ناوبری کاربر بین اسلایدها."""
    from models import Bot as BotRow
    parts = cb.data.split(":")
    # sls:view:BUTTON_ID:IDX[:BACK_CB_PART1:BACK_CB_PART2]
    button_id = int(parts[2])
    idx = int(parts[3])
    # back_cb ممکنه از : داشته باشه (مثل smopen:123 یا mbtn:home)
    back_cb = ":".join(parts[4:]) if len(parts) > 4 else "mbtn:home"
    slides = await get_slides(button_id)
    if not slides:
        await cb.answer("محتوایی یافت نشد.", show_alert=True)
        return
    for sl in slides:
        sl["_button_id"] = button_id
    await cb.answer()
    sl = slides[idx]
    caption = await _build_caption(sl, idx, len(slides))
    fid = sl["media_file_id"]
    mtype = sl["media_type"]
    # === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
    bot_id = btn.bot_id if btn else 0
    user_id = cb.from_user.id if cb.from_user else 0
    support_btn = None
    if (await _get_setting_local(bot_id, _support_key(button_id), "0")) == "1":
        from tenant import support_button
        support_btn = await support_button(bot_id)
    fav = await is_favorited(user_id, sl["id"]) if user_id else False
    bot_username = None
    if bot_id:
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        bot_username = brow.username if brow else None
    kb = _user_slide_kb(button_id, idx, len(slides), back_cb=back_cb, slide=sl,
                        is_fav=fav, support_btn=support_btn, bot_username=bot_username)

    try:
        if fid and mtype == "photo":
            await cb.message.edit_media(
                InputMediaPhoto(media=fid, caption=caption, parse_mode="HTML"),
                reply_markup=kb)
        elif fid and mtype == "video":
            await cb.message.edit_media(
                InputMediaVideo(media=fid, caption=caption, parse_mode="HTML"),
                reply_markup=kb)
        elif fid and mtype == "document":
            await cb.message.edit_media(
                InputMediaDocument(media=fid, caption=caption, parse_mode="HTML"),
                reply_markup=kb)
        else:
            await cb.message.edit_text(caption or "—", reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        if fid and mtype == "photo":
            await cb.message.answer_photo(fid, caption=caption, reply_markup=kb, parse_mode="HTML")
        elif fid and mtype == "video":
            await cb.message.answer_video(fid, caption=caption, reply_markup=kb, parse_mode="HTML")
        elif fid and mtype == "document":
            await cb.message.answer_document(fid, caption=caption, reply_markup=kb, parse_mode="HTML")
        else:
            await cb.message.answer(caption or "—", reply_markup=kb, parse_mode="HTML")


# === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
@slideshow_router.callback_query(F.data.startswith("sls:fav:"))
async def sls_fav_toggle(cb: CallbackQuery):
    """⭐/⭐️ افزودن یا حذفِ این اسلاید از علاقه‌مندی‌های کاربر — کیبورد
    همون‌جا (بدونِ تغییرِ مدیا) آپدیت می‌شه."""
    from models import Bot as BotRow
    parts = cb.data.split(":")
    # sls:fav:BUTTON_ID:IDX[:BACK_CB...]
    button_id = int(parts[2])
    idx = int(parts[3])
    back_cb = ":".join(parts[4:]) if len(parts) > 4 else "mbtn:home"
    slides = await get_slides(button_id)
    if not slides or idx >= len(slides):
        await cb.answer("یافت نشد", show_alert=True)
        return
    sl = slides[idx]
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
    bot_id = btn.bot_id if btn else 0
    user_id = cb.from_user.id
    new_state = await toggle_favorite(bot_id, user_id, button_id, sl["id"])
    await cb.answer("⭐️ به علاقه‌مندی‌ها اضافه شد" if new_state else "علاقه‌مندی حذف شد")

    support_btn = None
    if (await _get_setting_local(bot_id, _support_key(button_id), "0")) == "1":
        from tenant import support_button
        support_btn = await support_button(bot_id)
    bot_username = None
    if bot_id:
        async with Session() as s:
            brow = await s.get(BotRow, bot_id)
        bot_username = brow.username if brow else None
    kb = _user_slide_kb(button_id, idx, len(slides), back_cb=back_cb, slide=sl,
                        is_fav=new_state, support_btn=support_btn, bot_username=bot_username)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")


@slideshow_router.callback_query(F.data.startswith("sls:buyprod:"))
async def sls_buyprod_view(cb: CallbackQuery, bot: Bot):
    """نمایش محصول متصل به اسلاید — با دکمه بازگشت به همان اسلاید."""
    from aiogram.types import WebAppInfo
    from aiogram.enums import ButtonStyle
    from models import Product
    parts = cb.data.split(":")
    # sls:buyprod:BUTTON_ID:IDX:PRODUCT_ID[:BACK_CB]
    button_id = int(parts[2])
    idx = int(parts[3])
    pid = int(parts[4])
    # back_cb for "بازگشت به اسلاید" should carry back_cb so slide also knows where to go
    back_cb = ":".join(parts[5:]) if len(parts) > 5 else "mbtn:home"

    async with Session() as s:
        p = await s.get(Product, pid)
        if p is not None and await _is_foreign(p, bot):   # PATCH_TENANT_SCOPE_V1
            p = None
    if not p:
        await cb.answer("محصول یافت نشد", show_alert=True)
        return

    def rial(n) -> str:
        return f"{int(n):,} تومان"

    stock_txt = "✅ موجود" if p.stock != 0 else "❌ ناموجود"
    if p.stock > 0:
        stock_txt = f"✅ موجود ({p.stock} عدد)"
    elif p.stock == -1:
        stock_txt = "✅ موجود (نامحدود)"

    text = (
        f"🛍 <b>{p.title}</b>\n"
        f"┈┅━━⊷⊰✨⊱⊶━━┅┈\n"
        f"{p.description or ''}\n\n"
        f"💰 قیمت: <b>{rial(p.price)}</b>\n"
        f"📦 موجودی: {stock_txt}"
    )
    kb_rows = []
    if p.stock != 0:
        kb_rows.append([
            InlineKeyboardButton(text="💳 خرید با کیف پول",
                                 callback_data=f"shop:buy:wallet:{pid}"),
            InlineKeyboardButton(text="💵 کارت‌به‌کارت",
                                 callback_data=f"shop:buy:card:{pid}"),
        ])
    if p.shop_link_url:
        if p.shop_link_mode == "webapp":
            kb_rows.append([InlineKeyboardButton(
                text="🛍 ورود به فروشگاه", web_app=WebAppInfo(url=p.shop_link_url))])
        else:
            kb_rows.append([InlineKeyboardButton(
                text="🛍 ورود به فروشگاه", url=p.shop_link_url)])
    # دکمه بازگشت به همان اسلایدی که از آن آمده‌ایم (back_cb هم منتقل می‌شه)
    kb_rows.append([InlineKeyboardButton(
        text="🔙 بازگشت به اسلاید",
        callback_data=f"sls:view:{button_id}:{idx}:{back_cb}",
        style=ButtonStyle.DANGER
    )])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    # ویرایش پیام فعلی (بدون سند جدید) — مثل ناوبری بین اسلایدها
    try:
        if p.photo_id:
            await cb.message.edit_media(
                InputMediaPhoto(media=p.photo_id, caption=text, parse_mode="HTML"),
                reply_markup=kb)
        else:
            await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        # اگه edit نشد (مثلاً نوع مدیا فرق داشت)، پیام قدیمی حذف و جدید سند می‌شه
        try:
            await cb.message.delete()
        except Exception:
            logger.exception("خطای نادیده‌گرفته‌شده (قبلاً فقط pass بود)")
        if p.photo_id:
            await cb.message.answer_photo(p.photo_id, caption=text, reply_markup=kb,
                                          parse_mode="HTML")
        else:
            await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()

# ═══════════════════════ ادمین — مدیریت اسلایدها ═══════════════════════

def _admin_slides_kb(button_id: int, slides: list[dict],
                     pinned: bool = False, support_on: bool = False) -> InlineKeyboardMarkup:
    """منوی ادمین برای مدیریت اسلایدهای یک دکمه."""
    rows = []
    for sl in slides:
        icon = "🖼"
        if sl["media_type"] == "video":
            icon = "🎬"
        elif sl["media_type"] == "document":
            icon = "📄"
        elif not sl["media_file_id"]:
            icon = "📝"
        # === PATCH_SLIDE_ADMIN_NAME_V1 ===
        if sl.get("admin_name"):
            label = f"{icon} اسلاید {sl['step_num']}: {sl['admin_name']}"
        else:
            label = f"{icon} اسلاید {sl['step_num']}"
        rows.append([
            InlineKeyboardButton(text=label,
                                 callback_data=f"sla:edit:{button_id}:{sl['id']}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton(text="🗑",
                                 callback_data=f"sla:del:{button_id}:{sl['id']}", style=ButtonStyle.DANGER),
        ])
    rows.append([InlineKeyboardButton(
        text="➕ اسلاید جدید", callback_data=f"sla:new:{button_id}", style=ButtonStyle.SUCCESS)])
    # === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
    pin_lbl = "📌 نمایشِ خودکار موقعِ /start: روشن ✅" if pinned else "📌 نمایشِ خودکار موقعِ /start: خاموش"
    rows.append([InlineKeyboardButton(text=pin_lbl, callback_data=f"sla:pinstart:{button_id}", style=ButtonStyle.PRIMARY)])
    sup_lbl = "💬 دکمهٔ پشتیبانی رو اسلاید: روشن ✅" if support_on else "💬 دکمهٔ پشتیبانی رو اسلاید: خاموش"
    rows.append([InlineKeyboardButton(text=sup_lbl, callback_data=f"sla:togglesupport:{button_id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text="⬅️ بازگشت به دکمه", callback_data=f"thb:show:{button_id}", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _admin_slide_edit_kb(button_id: int, slide_id: int,
                          has_url: bool = False,
                          has_buy: bool = False,
                          has_name: bool = False,
                          support_on: bool = False) -> InlineKeyboardMarkup:
    """کیبورد ویرایش یک اسلاید — شامل مدیریت دکمه‌های شیشه‌ای."""
    rows = [
        [InlineKeyboardButton(text="✏️ ویرایش متن/کپشن",
                              callback_data=f"sla:ecap:{button_id}:{slide_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="🖼 تغییر مدیا",
                              callback_data=f"sla:emedia:{button_id}:{slide_id}", style=ButtonStyle.PRIMARY)],
    ]
    # === PATCH_SLIDE_ADMIN_NAME_V1 ===
    name_label = "✏️ تغییرِ نامِ اسلاید (فقط برای خودت)" if has_name else "🏷 تعیینِ نامِ اسلاید (فقط برای خودت)"
    rows.append([InlineKeyboardButton(text=name_label,
                                      callback_data=f"sla:ename:{button_id}:{slide_id}", style=ButtonStyle.PRIMARY)])

    # دکمه لینک شیشه‌ای
    link_label = "🔗 ویرایش لینک دکمه" if has_url else "🔗 افزودن لینک (دکمه شیشه‌ای)"
    rows.append([InlineKeyboardButton(text=link_label,
                                      callback_data=f"sla:elink:{button_id}:{slide_id}", style=ButtonStyle.PRIMARY)])
    if has_url:
        rows.append([InlineKeyboardButton(text="🗑 حذف لینک دکمه",
                                          callback_data=f"sla:dellink:{button_id}:{slide_id}", style=ButtonStyle.DANGER)])

    # دکمه خرید
    buy_label = "🛒 ویرایش دکمه خرید" if has_buy else "🛒 افزودن دکمه خرید"
    rows.append([InlineKeyboardButton(text=buy_label,
                                      callback_data=f"sla:ebuy:{button_id}:{slide_id}", style=ButtonStyle.PRIMARY)])
    if has_buy:
        rows.append([InlineKeyboardButton(text="🗑 حذف دکمه خرید",
                                          callback_data=f"sla:delbuy:{button_id}:{slide_id}", style=ButtonStyle.DANGER)])

    # === PATCH_SLIDESHOW_ENHANCEMENTS_V1 (میان‌بر) ===
    # طبقِ درخواستِ صریح: امکانِ روشن/خاموش‌کردنِ پشتیبانی همین‌جا هم باشه،
    # نه فقط تو صفحهٔ لیستِ اسلایدها.
    sup_label = "💬 دکمهٔ پشتیبانی رو اسلاید: روشن ✅" if support_on else "💬 دکمهٔ پشتیبانی رو اسلاید: خاموش"
    rows.append([InlineKeyboardButton(text=sup_label, callback_data=f"sla:togglesupport:{button_id}", style=ButtonStyle.PRIMARY)])

    rows.append([InlineKeyboardButton(text="⬅️ بازگشت به لیست اسلایدها",
                                      callback_data=f"sla:list:{button_id}", style=ButtonStyle.DANGER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@slideshow_router.callback_query(F.data.startswith("sla:list:"))
async def sla_list(cb: CallbackQuery):
    button_id = int(cb.data.split(":")[2])
    slides = await get_slides(button_id)
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
    total = len(slides)
    bot_id = btn.bot_id if btn else 0
    # === PATCH_SLIDESHOW_ENHANCEMENTS_V1 ===
    pinned = (await _get_setting_local(bot_id, _pin_start_key(bot_id), "")) == str(button_id)
    support_on = (await _get_setting_local(bot_id, _support_key(button_id), "0")) == "1"
    text_msg = (
        f"🎞 <b>مدیریت اسلایدشو — دکمه «{btn.label if btn else button_id}»</b>\n\n"
        f"تعداد اسلایدها: <b>{total}</b>\n\n"
        "می‌توانی اسلاید جدید اضافه کنی یا هر مرحله را ویرایش/حذف کنی."
    )
    await cb.answer()
    kb = _admin_slides_kb(button_id, slides, pinned=pinned, support_on=support_on)
    try:
        await cb.message.edit_text(text_msg, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text_msg, reply_markup=kb, parse_mode="HTML")


@slideshow_router.callback_query(F.data.startswith("sla:pinstart:"))
async def sla_pinstart_toggle(cb: CallbackQuery):
    """روشن/خاموش‌کردنِ نمایشِ خودکارِ این اسلایدشو موقعِ /start کاربر.
    هر بات فقط یه اسلایدشوی پین‌شده می‌تونه داشته باشه — انتخابِ یه
    اسلایدشوی دیگه، خودکار قبلی رو جایگزین می‌کنه."""
    button_id = int(cb.data.split(":")[2])
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
    if not btn:
        await cb.answer("یافت نشد", show_alert=True)
        return
    bot_id = btn.bot_id
    cur = await _get_setting_local(bot_id, _pin_start_key(bot_id), "")
    if cur == str(button_id):
        await _set_setting_local(bot_id, _pin_start_key(bot_id), "")
        await cb.answer("📌 نمایشِ خودکار موقعِ /start خاموش شد")
    else:
        # === PATCH_SLIDESHOW_FIELD_RESTRICT_V1 ===
        # نمایشِ خودکارِ اسلایدشو موقعِ /start فقط برایِ ربات‌هایِ
        # تبلیغاتی/فروشگاهیه؛ برایِ بقیه (مثلاً VPN) فعال‌سازی رو اجازه
        # می‌دیم (تا اگه بعداً نوعِ ربات عوض شد کار کنه) ولی صریح خبر می‌دیم
        # که با این نوعِ ربات، الان نمایش داده نمی‌شه.
        from models import Bot as _BotRow
        async with Session() as s:
            row = await s.get(_BotRow, bot_id)
        await _set_setting_local(bot_id, _pin_start_key(bot_id), str(button_id))
        if row and row.field not in ("ad_bot", "digital_shop"):
            await cb.answer(
                "✅ پین شد، ولی نمایشِ خودکارِ /start فقط برایِ ربات‌هایِ «تبلیغاتی» یا "
                "«فروشگاه دیجیتال» فعاله — برایِ نوعِ فعلیِ این ربات نمایش داده نمی‌شه.",
                show_alert=True)
        else:
            await cb.answer("✅ این اسلایدشو الان موقعِ /start خودکار نشون داده می‌شه")
    await sla_list(cb)


@slideshow_router.callback_query(F.data.startswith("sla:togglesupport:"))
async def sla_togglesupport(cb: CallbackQuery):
    """روشن/خاموش‌کردنِ دکمهٔ «💬 پشتیبانی آنلاین» روی اسلایدهای این دکمه."""
    button_id = int(cb.data.split(":")[2])
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
    if not btn:
        await cb.answer("یافت نشد", show_alert=True)
        return
    bot_id = btn.bot_id
    cur = await _get_setting_local(bot_id, _support_key(button_id), "0")
    new_val = "0" if cur == "1" else "1"
    await _set_setting_local(bot_id, _support_key(button_id), new_val)
    await cb.answer("💬 دکمهٔ پشتیبانی روشن شد" if new_val == "1" else "دکمهٔ پشتیبانی خاموش شد")
    await sla_list(cb)


@slideshow_router.callback_query(F.data.startswith("sla:new:"))
async def sla_new(cb: CallbackQuery, state: FSMContext):
    """=== PATCH_SLIDE_NEW_FROM_PRODUCT_ENTRY_V1 ===
    قبلاً همین‌جا مستقیم می‌رفت سراغِ «عکس/ویدیو/متن بفرست» — یعنی برای
    ساختِ یه اسلایدِ محصولی، اول باید دستی عکس می‌فرستادی، بعد جداگانه از
    لیستِ محصولات وصلش می‌کردی. الان همون اول می‌پرسه از کجا شروع کنیم:
    مستقیم از رویِ یه محصول (که خودش عکس/عنوان/قیمتِ محصول رو می‌ذاره)،
    یا آپلودِ دستی (رفتارِ قبلی، برای بنرهای بدونِ محصول)."""
    button_id = int(cb.data.split(":")[2])
    slides = await get_slides(button_id)
    next_step = len(slides) + 1
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 انتخاب از محصولات", callback_data=f"sla:newfromproduct:{button_id}",
                              style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="📤 آپلودِ دستی (عکس/ویدیو/متن)", callback_data=f"sla:newmanual:{button_id}",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="❌ لغو", callback_data=f"sla:list:{button_id}", style=ButtonStyle.DANGER)],
    ])
    await cb.message.answer(
        f"📥 <b>اسلاید {next_step}</b>\n\n"
        "این اسلاید رو چطوری بسازیم؟",
        reply_markup=kb, parse_mode="HTML")


@slideshow_router.callback_query(F.data.startswith("sla:newmanual:"))
async def sla_new_manual(cb: CallbackQuery, state: FSMContext):
    """رفتارِ قبلیِ «اسلاید جدید» — آپلودِ دستیِ عکس/ویدیو/متن."""
    button_id = int(cb.data.split(":")[2])
    slides = await get_slides(button_id)
    next_step = len(slides) + 1
    await state.update_data(sla_button_id=button_id, sla_step=next_step, sla_slide_id=None)
    await state.set_state(SlideFSM.waiting_media)
    await cb.answer()
    await cb.message.answer(
        f"📥 <b>اسلاید {next_step}</b>\n\n"
        "عکس، ویدیو، فایل یا متن را ارسال کن.\n"
        "• برای اسلاید متنی فقط متن بنویس.\n"
        "• برای مدیا می‌توانی کپشن هم اضافه کنی.\n\n"
        "برای لغو /cancel را ارسال کن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"sla:list:{button_id}", style=ButtonStyle.DANGER)
        ]]),
        parse_mode="HTML"
    )


@slideshow_router.callback_query(F.data.startswith("sla:newfromproduct:"))
async def sla_new_from_product_picker(cb: CallbackQuery):
    """لیستِ محصولاتِ فعالِ فروشگاه رو برای ساختِ اسلایدِ مستقیم نشون می‌ده."""
    button_id = int(cb.data.split(":")[2])
    from models import Product
    from sqlalchemy import select as sa_select
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
        if not btn:
            await cb.answer("خطا", show_alert=True)
            return
        products = (await s.scalars(sa_select(Product).where(
            Product.bot_id == btn.bot_id, Product.is_active == True
        ).order_by(Product.sort_order, Product.id))).all()
    await cb.answer()
    if not products:
        await cb.message.answer(
            "❌ هیچ محصولِ فعالی تو فروشگاه نداری. اول از بخشِ فروشگاه یه محصول اضافه کن، "
            "یا از «📤 آپلودِ دستی» استفاده کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📤 آپلودِ دستی", callback_data=f"sla:newmanual:{button_id}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"sla:list:{button_id}", style=ButtonStyle.DANGER)]]))
        return
    rows = [[InlineKeyboardButton(text=f"🛍 {p.title}", callback_data=f"sla:newprodpick:{button_id}:{p.id}", style=ButtonStyle.PRIMARY)]
           for p in products]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"sla:new:{button_id}", style=ButtonStyle.DANGER)])
    await cb.message.answer(
        "🛍 <b>انتخاب از محصولات</b>\n\nکدوم محصول رو به‌عنوانِ اسلاید بسازیم؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@slideshow_router.callback_query(F.data.startswith("sla:newprodpick:"))
async def sla_new_from_product_create(cb: CallbackQuery):
    """اسلاید رو مستقیم از رویِ محصولِ انتخاب‌شده می‌سازه: عکسِ محصول
    (اگه داشته باشه) به‌عنوانِ مدیا، و buy_product_id ست می‌شه — بدونِ
    نیاز به آپلودِ دستیِ چیزی."""
    parts = cb.data.split(":")
    button_id, product_id = int(parts[2]), int(parts[3])
    from models import Product
    async with Session() as s:
        p = await s.get(Product, product_id)
        if p is not None and await _is_foreign(p, cb.bot):   # PATCH_TENANT_SCOPE_V1
            p = None
    if not p:
        await cb.answer("محصول یافت نشد", show_alert=True)
        return
    slides = await get_slides(button_id)
    next_step = len(slides) + 1
    media_file_id = p.photo_id or None
    media_type = "photo" if p.photo_id else None
    slide_id = await save_slide(button_id, next_step, caption=None,
                                media_file_id=media_file_id, media_type=media_type,
                                buy_product_id=product_id)
    await cb.answer(f"✅ اسلاید از رویِ «{p.title}» ساخته شد.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 اسلایدِ بعدی از محصولِ دیگه",
                              callback_data=f"sla:newfromproduct:{button_id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="➕ اسلایدِ بعدی (آپلودِ دستی)",
                              callback_data=f"sla:newmanual:{button_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="✅ پایان — برو به لیستِ اسلایدها", callback_data=f"sla:list:{button_id}", style=ButtonStyle.SUCCESS)],
    ])
    await cb.message.answer(
        f"✅ اسلاید {next_step} از رویِ محصولِ «{p.title}» ساخته و به همون محصول وصل شد.\n\n"
        "حالا می‌خوای اسلایدِ بعدی رو (با یه محصولِ دیگه، اگه خواستی) بسازی؟",
        reply_markup=kb)


@slideshow_router.callback_query(F.data.startswith("sla:emedia:"))
async def sla_edit_media(cb: CallbackQuery, state: FSMContext):
    _, _, btn_id_s, slide_id_s = cb.data.split(":")
    button_id = int(btn_id_s)
    slide_id = int(slide_id_s)
    await state.update_data(sla_button_id=button_id, sla_slide_id=slide_id)
    await state.set_state(SlideFSM.waiting_media)
    await cb.answer()
    await cb.message.answer(
        "🔄 مدیای جدید را ارسال کن (عکس / ویدیو / فایل / متن).\n"
        "برای لغو /cancel را بزن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"sla:list:{button_id}", style=ButtonStyle.DANGER)
        ]])
    )


@slideshow_router.callback_query(F.data.startswith("sla:ecap:"))
async def sla_edit_caption(cb: CallbackQuery, state: FSMContext):
    _, _, btn_id_s, slide_id_s = cb.data.split(":")
    button_id = int(btn_id_s)
    slide_id = int(slide_id_s)
    await state.update_data(sla_button_id=button_id, sla_slide_id=slide_id)
    await state.set_state(SlideFSM.waiting_caption)
    await cb.answer()
    await cb.message.answer(
        "✏️ متن/کپشن جدید را بنویس (از فرمت‌بندی تلگرام HTML پشتیبانی می‌شه).\n"
        "برای حذف متن /clear را بنویس.\n"
        "برای لغو /cancel را بزن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"sla:list:{button_id}", style=ButtonStyle.DANGER)
        ]])
    )


# === PATCH_SLIDE_ADMIN_NAME_V1 ===
@slideshow_router.callback_query(F.data.startswith("sla:ename:"))
async def sla_edit_name(cb: CallbackQuery, state: FSMContext):
    _, _, btn_id_s, slide_id_s = cb.data.split(":")
    button_id = int(btn_id_s)
    slide_id = int(slide_id_s)
    await state.update_data(sla_button_id=button_id, sla_slide_id=slide_id)
    await state.set_state(SlideFSM.waiting_admin_name)
    await cb.answer()
    await cb.message.answer(
        "🏷 <b>نامِ اسلاید (فقط برای خودت)</b>\n\n"
        "یه اسمِ کوتاه بفرست تا تو لیستِ اسلایدها بفهمی این اسلاید چیه "
        "(مثلاً «تدی گیفت» یا «کاور آیفون ۱۷»). به کاربر نشون داده نمی‌شه.\n\n"
        "برای پاک‌کردنِ نام /clear را بفرست.\nبرای لغو /cancel را بزن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"sla:edit:{button_id}:{slide_id}", style=ButtonStyle.DANGER)
        ]]), parse_mode="HTML")


@slideshow_router.message(SlideFSM.waiting_admin_name, F.text)
async def sla_recv_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    button_id = data.get("sla_button_id")
    slide_id = data.get("sla_slide_id")

    if msg.text.startswith("/cancel"):
        await state.clear()
        slides = await get_slides(button_id)
        sl = next((s for s in slides if s["id"] == slide_id), None)
        pinned, support_on = await _slides_kb_flags(button_id)
        await msg.answer("❌ لغو شد.", reply_markup=_admin_slides_kb(button_id, slides, pinned=pinned, support_on=support_on))
        return

    new_name = None if msg.text.strip() == "/clear" else msg.text.strip()[:60]
    await save_slide_name(slide_id, new_name)
    await state.clear()
    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    pinned, support_on = await _slides_kb_flags(button_id)
    await msg.answer(
        "✅ نامِ اسلاید ذخیره شد!" if new_name else "✅ نامِ اسلاید پاک شد.",
        reply_markup=_admin_slides_kb(button_id, slides, pinned=pinned, support_on=support_on))


# ─── ویرایش لینک دکمه شیشه‌ای ───

@slideshow_router.callback_query(F.data.startswith("sla:elink:"))
async def sla_edit_link(cb: CallbackQuery, state: FSMContext):
    _, _, btn_id_s, slide_id_s = cb.data.split(":")
    button_id = int(btn_id_s)
    slide_id = int(slide_id_s)
    await state.update_data(sla_button_id=button_id, sla_slide_id=slide_id,
                            sla_link_step="label")
    await state.set_state(SlideFSM.waiting_btn_label)
    await cb.answer()
    await cb.message.answer(
        "🔗 <b>دکمه شیشه‌ای لینک‌دار</b>\n\n"
        "ابتدا <b>متن دکمه</b> را بنویس:\n"
        "مثال: <code>🛍 مشاهده محصول</code>\n\n"
        "برای لغو /cancel را بزن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو", callback_data=f"sla:edit:{button_id}:{slide_id}", style=ButtonStyle.DANGER)
        ]]),
        parse_mode="HTML"
    )


@slideshow_router.callback_query(F.data.startswith("sla:dellink:"))
async def sla_del_link(cb: CallbackQuery):
    _, _, btn_id_s, slide_id_s = cb.data.split(":")
    button_id = int(btn_id_s)
    slide_id = int(slide_id_s)
    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    if sl:
        await save_slide_buttons(slide_id, btn_url=None, btn_label=None,
                                  buy_product_id=sl.get("buy_product_id"))
    await cb.answer("🗑 لینک دکمه حذف شد.")
    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    await _show_slide_edit(cb, button_id, slide_id, sl)


@slideshow_router.callback_query(F.data.startswith("sla:ebuy:"))
async def sla_edit_buy(cb: CallbackQuery, state: FSMContext):
    _, _, btn_id_s, slide_id_s = cb.data.split(":")
    button_id = int(btn_id_s)
    slide_id = int(slide_id_s)

    # لیست محصولات فعال این ربات
    from models import Product
    from sqlalchemy import select as sa_select
    async with Session() as s:
        # پیدا کردن bot_id از طریق button_id
        btn = await s.get(MainButton, button_id)
        if not btn:
            await cb.answer("خطا", show_alert=True)
            return
        products = (await s.scalars(
            sa_select(Product).where(
                Product.bot_id == btn.bot_id,
                Product.is_active == True
            ).order_by(Product.sort_order, Product.id)
        )).all()

    await cb.answer()
    if not products:
        await cb.message.answer(
            "❌ هیچ محصول فعالی در فروشگاه نداری.\n"
            "ابتدا از بخش فروشگاه محصول اضافه کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⬅️ بازگشت",
                                     callback_data=f"sla:edit:{button_id}:{slide_id}", style=ButtonStyle.DANGER)
            ]])
        )
        return

    # نمایش لیست محصولات
    rows = []
    for p in products:
        rows.append([InlineKeyboardButton(
            text=f"🛍 {p.title}",
            callback_data=f"sla:setbuy:{button_id}:{slide_id}:{p.id}", style=ButtonStyle.PRIMARY)])
    rows.append([InlineKeyboardButton(
        text="⬅️ بازگشت", callback_data=f"sla:edit:{button_id}:{slide_id}", style=ButtonStyle.DANGER)])
    await cb.message.answer(
        "🛒 <b>انتخاب محصول برای دکمه خرید</b>\n\n"
        "کدام محصول را می‌خواهی به این اسلاید وصل کنی؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@slideshow_router.callback_query(F.data.startswith("sla:setbuy:"))
async def sla_set_buy(cb: CallbackQuery):
    parts = cb.data.split(":")
    button_id = int(parts[2])
    slide_id = int(parts[3])
    product_id = int(parts[4])

    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    if sl:
        await save_slide_buttons(slide_id,
                                  btn_url=sl.get("btn_url"),
                                  btn_label=sl.get("btn_label"),
                                  buy_product_id=product_id)

    title = await _get_product_title(product_id)
    await cb.answer(f"✅ دکمه خرید «{title}» تنظیم شد.")
    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    await _show_slide_edit(cb, button_id, slide_id, sl)


# === PATCH_SLIDE_CREATE_PRODUCT_LINK_V1 ===
@slideshow_router.callback_query(F.data.startswith("sla:linkprod:"))
async def sla_linkprod_picker(cb: CallbackQuery):
    """دقیقاً مثلِ sla:ebuy: (لیستِ محصولات)، فقط برای مسیرِ «تازه ساختمش،
    می‌خوام همین‌جا وصلش کنم» — بعدِ انتخاب، به‌جای صفحهٔ ویرایش، پیشنهادِ
    «برو سراغِ اسلایدِ بعدی» می‌ده."""
    parts = cb.data.split(":")
    button_id, slide_id = int(parts[2]), int(parts[3])
    from models import Product
    from sqlalchemy import select as sa_select
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
        if not btn:
            await cb.answer("خطا", show_alert=True)
            return
        products = (await s.scalars(sa_select(Product).where(
            Product.bot_id == btn.bot_id, Product.is_active == True
        ).order_by(Product.sort_order, Product.id))).all()
    await cb.answer()
    if not products:
        await cb.message.answer(
            "❌ هیچ محصولِ فعالی تو فروشگاه نداری. اول از بخشِ فروشگاه یه محصول اضافه کن.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ اسلایدِ بعدی", callback_data=f"sla:new:{button_id}", style=ButtonStyle.PRIMARY)]]))
        return
    rows = [[InlineKeyboardButton(text=f"🛍 {p.title}",
                                  callback_data=f"sla:setbuy2:{button_id}:{slide_id}:{p.id}", style=ButtonStyle.PRIMARY)]
           for p in products]
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"sla:new:{button_id}", style=ButtonStyle.DANGER)])
    await cb.message.answer(
        "🛍 <b>انتخابِ محصول برای همین اسلاید</b>\n\nکدوم محصول؟",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@slideshow_router.callback_query(F.data.startswith("sla:setbuy2:"))
async def sla_setbuy2(cb: CallbackQuery):
    parts = cb.data.split(":")
    button_id, slide_id, product_id = int(parts[2]), int(parts[3]), int(parts[4])
    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    if sl:
        await save_slide_buttons(slide_id, btn_url=sl.get("btn_url"), btn_label=sl.get("btn_label"),
                                 buy_product_id=product_id)
    title = await _get_product_title(product_id)
    await cb.answer(f"✅ محصولِ «{title}» به این اسلاید وصل شد.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ اسلایدِ بعدی", callback_data=f"sla:new:{button_id}",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="✅ پایان — برو به لیستِ اسلایدها", callback_data=f"sla:list:{button_id}", style=ButtonStyle.SUCCESS)],
    ])
    await cb.message.answer(
        f"✅ اسلاید با محصولِ «{title}» تکمیل شد.\n\nحالا می‌خوای اسلایدِ بعدی رو (با یه محصولِ دیگه، اگه خواستی) بسازی؟",
        reply_markup=kb)


@slideshow_router.callback_query(F.data.startswith("sla:delbuy:"))
async def sla_del_buy(cb: CallbackQuery):
    _, _, btn_id_s, slide_id_s = cb.data.split(":")
    button_id = int(btn_id_s)
    slide_id = int(slide_id_s)
    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    if sl:
        await save_slide_buttons(slide_id,
                                  btn_url=sl.get("btn_url"),
                                  btn_label=sl.get("btn_label"),
                                  buy_product_id=None)
    await cb.answer("🗑 دکمه خرید حذف شد.")
    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    await _show_slide_edit(cb, button_id, slide_id, sl)


@slideshow_router.callback_query(F.data.startswith("sla:edit:"))
async def sla_edit(cb: CallbackQuery):
    _, _, btn_id_s, slide_id_s = cb.data.split(":")
    button_id = int(btn_id_s)
    slide_id = int(slide_id_s)
    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    if not sl:
        await cb.answer("یافت نشد", show_alert=True)
        return
    await cb.answer()
    await _show_slide_edit(cb, button_id, slide_id, sl)


async def _show_slide_edit(cb: CallbackQuery, button_id: int, slide_id: int,
                            sl: dict | None):
    if not sl:
        await cb.message.answer("❌ اسلاید یافت نشد.")
        return
    mtype_label = {"photo": "🖼 عکس", "video": "🎬 ویدیو",
                   "document": "📄 فایل"}.get(sl["media_type"] or "", "📝 متنی")

    # اطلاعات دکمه‌ها
    link_info = ""
    if sl.get("btn_url"):
        link_info = f"\n🔗 لینک: {sl['btn_url']}\n   برچسب: {sl.get('btn_label') or '🔗 مشاهده'}"

    buy_info = ""
    if sl.get("buy_product_id"):
        title = await _get_product_title(sl["buy_product_id"])
        buy_info = f"\n🛒 دکمه خرید: {title or sl['buy_product_id']}"

    # === PATCH_SLIDE_ADMIN_NAME_V1 ===
    name_info = f"\n🏷 نامِ اسلاید (فقط برای خودت): {sl['admin_name']}" if sl.get("admin_name") else ""
    bot_id = None
    async with Session() as s:
        btn = await s.get(MainButton, button_id)
        bot_id = btn.bot_id if btn else 0
    support_on = (await _get_setting_local(bot_id, _support_key(button_id), "0")) == "1"

    info = (
        f"⚙️ <b>ویرایش اسلاید {sl['step_num']}</b>"
        f"{name_info}\n\n"
        f"نوع: {mtype_label}\n"
        f"کپشن: {sl['caption'] or '—'}"
        f"{link_info}"
        f"{buy_info}\n\n"
        "چه کاری انجام بدم؟"
    )
    kb = _admin_slide_edit_kb(
        button_id, slide_id,
        has_url=bool(sl.get("btn_url")),
        has_buy=bool(sl.get("buy_product_id")),
        has_name=bool(sl.get("admin_name")),
        support_on=support_on,
    )
    try:
        await cb.message.edit_text(info, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(info, reply_markup=kb, parse_mode="HTML")


@slideshow_router.callback_query(F.data.startswith("sla:del:"))
async def sla_del(cb: CallbackQuery):
    _, _, btn_id_s, slide_id_s = cb.data.split(":")
    button_id = int(btn_id_s)
    slide_id = int(slide_id_s)
    await delete_slide(slide_id)
    await renumber_slides(button_id)
    slides = await get_slides(button_id)
    await cb.answer("🗑 اسلاید حذف شد.")
    pinned, support_on = await _slides_kb_flags(button_id)
    try:
        await cb.message.edit_text(
            f"✅ اسلاید حذف شد. تعداد باقی‌مانده: {len(slides)}",
            reply_markup=_admin_slides_kb(button_id, slides, pinned=pinned, support_on=support_on),
            parse_mode="HTML"
        )
    except Exception:
        await cb.message.answer(
            f"✅ اسلاید حذف شد. تعداد باقی‌مانده: {len(slides)}",
            reply_markup=_admin_slides_kb(button_id, slides, pinned=pinned, support_on=support_on)
        )


# ─── دریافت مدیا از ادمین ───

@slideshow_router.message(SlideFSM.waiting_media)
async def sla_recv_media(msg: Message, state: FSMContext):
    data = await state.get_data()
    button_id = data.get("sla_button_id")
    step_num = data.get("sla_step", 1)
    slide_id = data.get("sla_slide_id")

    if msg.text and msg.text.startswith("/cancel"):
        await state.clear()
        slides = await get_slides(button_id)
        pinned, support_on = await _slides_kb_flags(button_id)
        await msg.answer("❌ لغو شد.", reply_markup=_admin_slides_kb(button_id, slides, pinned=pinned, support_on=support_on))
        return

    media_file_id = None
    media_type = None
    caption = msg.caption or (msg.text if not msg.photo and not msg.video and not msg.document else None)

    if msg.photo:
        media_file_id = msg.photo[-1].file_id
        media_type = "photo"
    elif msg.video:
        media_file_id = msg.video.file_id
        media_type = "video"
    elif msg.document:
        media_file_id = msg.document.file_id
        media_type = "document"

    # اگه ویرایش اسلاید موجوده، اطلاعات دکمه‌های قبلی رو حفظ کن
    old_btn_url = None
    old_btn_label = None
    old_buy_product_id = None
    if slide_id:
        slides_all = await get_slides(button_id)
        sl_old = next((s for s in slides_all if s["id"] == slide_id), None)
        if sl_old:
            old_btn_url = sl_old.get("btn_url")
            old_btn_label = sl_old.get("btn_label")
            old_buy_product_id = sl_old.get("buy_product_id")

    await save_slide(button_id, step_num, caption, media_file_id, media_type,
                     slide_id, old_btn_url, old_btn_label, old_buy_product_id)
    await state.clear()
    slides = await get_slides(button_id)
    # === PATCH_SLIDE_CREATE_PRODUCT_LINK_V1 ===
    # قبلاً بعدِ ذخیره‌شدنِ اسلایدِ *تازه* (نه ویرایشِ یه اسلایدِ قدیمی)،
    # مستقیم می‌رفت لیستِ اسلایدها — اگه ادمین می‌خواست بهش محصول وصل کنه،
    # باید از لیست دوباره وارد ویرایشِ همون اسلاید می‌شد (چند کلیکِ اضافه).
    # الان همون‌جا، همون لحظه، می‌شه محصول وصل کرد و مستقیم رفت سراغِ
    # اسلایدِ بعدی — دقیقاً طبقِ همون فلوی خطی که خواسته شده بود.
    if not slide_id:
        new_slide = slides[-1] if slides else None
        new_slide_id = new_slide["id"] if new_slide else None
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 اتصالِ محصول به همین اسلاید",
                                  callback_data=f"sla:linkprod:{button_id}:{new_slide_id}",
                                  style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton(text="➕ اسلایدِ بعدی (بدونِ محصول)",
                                  callback_data=f"sla:new:{button_id}", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="✅ پایان — برو به لیستِ اسلایدها",
                                  callback_data=f"sla:list:{button_id}", style=ButtonStyle.SUCCESS)],
        ])
        await msg.answer(
            f"✅ اسلاید {step_num} ذخیره شد! (مجموع: {len(slides)})\n\n"
            "می‌خوای یه محصول به همین اسلاید وصل کنی، یا بری سراغِ اسلایدِ بعدی؟",
            reply_markup=kb)
        return
    action = "به‌روزرسانی"
    pinned, support_on = await _slides_kb_flags(button_id)
    await msg.answer(
        f"✅ اسلاید {action} شد! (مجموع: {len(slides)})",
        reply_markup=_admin_slides_kb(button_id, slides, pinned=pinned, support_on=support_on)
    )


@slideshow_router.message(SlideFSM.waiting_caption)
async def sla_recv_caption(msg: Message, state: FSMContext):
    data = await state.get_data()
    button_id = data.get("sla_button_id")
    slide_id = data.get("sla_slide_id")

    if msg.text and msg.text.startswith("/cancel"):
        await state.clear()
        slides = await get_slides(button_id)
        pinned, support_on = await _slides_kb_flags(button_id)
        await msg.answer("❌ لغو شد.", reply_markup=_admin_slides_kb(button_id, slides, pinned=pinned, support_on=support_on))
        return

    new_caption = None if (msg.text or "").strip() == "/clear" else msg.text

    slides_all = await get_slides(button_id)
    sl = next((s for s in slides_all if s["id"] == slide_id), None)
    if sl:
        await save_slide(button_id, sl["step_num"], new_caption,
                         sl["media_file_id"], sl["media_type"], slide_id,
                         sl.get("btn_url"), sl.get("btn_label"), sl.get("buy_product_id"))

    await state.clear()
    slides = await get_slides(button_id)
    pinned, support_on = await _slides_kb_flags(button_id)
    await msg.answer(
        "✅ کپشن به‌روز شد!",
        reply_markup=_admin_slides_kb(button_id, slides, pinned=pinned, support_on=support_on)
    )


# ─── دریافت برچسب و لینک دکمه شیشه‌ای ───

@slideshow_router.message(SlideFSM.waiting_btn_label)
async def sla_recv_btn_label(msg: Message, state: FSMContext):
    data = await state.get_data()
    button_id = data.get("sla_button_id")
    slide_id = data.get("sla_slide_id")

    if msg.text and msg.text.startswith("/cancel"):
        await state.clear()
        slides = await get_slides(button_id)
        sl = next((s for s in slides if s["id"] == slide_id), None)
        await msg.answer("❌ لغو شد.",
                         reply_markup=_admin_slide_edit_kb(
                             button_id, slide_id,
                             has_url=bool(sl and sl.get("btn_url")),
                             has_buy=bool(sl and sl.get("buy_product_id"))
                         ))
        return

    label = (msg.text or "").strip()
    await state.update_data(sla_btn_label=label)
    await state.set_state(SlideFSM.waiting_btn_url)
    await msg.answer(
        f"✅ برچسب تنظیم شد: <b>{label}</b>\n\n"
        "حالا <b>لینک</b> را وارد کن:\n"
        "مثال: <code>https://example.com</code>\n\n"
        "برای لغو /cancel را بزن.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ لغو",
                                 callback_data=f"sla:edit:{button_id}:{slide_id}", style=ButtonStyle.DANGER)
        ]]),
        parse_mode="HTML"
    )


@slideshow_router.message(SlideFSM.waiting_btn_url)
async def sla_recv_btn_url(msg: Message, state: FSMContext):
    data = await state.get_data()
    button_id = data.get("sla_button_id")
    slide_id = data.get("sla_slide_id")
    btn_label = data.get("sla_btn_label", "🔗 مشاهده")

    if msg.text and msg.text.startswith("/cancel"):
        await state.clear()
        slides = await get_slides(button_id)
        sl = next((s for s in slides if s["id"] == slide_id), None)
        await msg.answer("❌ لغو شد.",
                         reply_markup=_admin_slide_edit_kb(
                             button_id, slide_id,
                             has_url=bool(sl and sl.get("btn_url")),
                             has_buy=bool(sl and sl.get("buy_product_id"))
                         ))
        return

    url = (msg.text or "").strip()
    if not url.startswith(("http://", "https://", "tg://")):
        await msg.answer(
            "❌ لینک معتبر نیست! باید با https:// یا http:// شروع شه.\n"
            "دوباره وارد کن یا /cancel بزن."
        )
        return

    # ذخیره
    slides_all = await get_slides(button_id)
    sl = next((s for s in slides_all if s["id"] == slide_id), None)
    buy_pid = sl.get("buy_product_id") if sl else None
    await save_slide_buttons(slide_id, btn_url=url, btn_label=btn_label,
                              buy_product_id=buy_pid)
    await state.clear()

    slides = await get_slides(button_id)
    sl = next((s for s in slides if s["id"] == slide_id), None)
    await msg.answer(
        f"✅ دکمه شیشه‌ای تنظیم شد!\n🔗 <b>{btn_label}</b> → {url}",
        reply_markup=_admin_slide_edit_kb(
            button_id, slide_id,
            has_url=True,
            has_buy=bool(sl and sl.get("buy_product_id"))
        ),
        parse_mode="HTML"
    )


async def _is_foreign(obj, bot) -> bool:
    """=== PATCH_TENANT_SCOPE_V1 === True اگر رکوردی که با idِ داخلِ
    callback_data/state خوانده شده مالِ رباتِ جاریِ این آپدیت نباشد."""
    if bot is None or getattr(obj, "bot_id", None) is None:
        return False
    from tenant import get_bot_row as _gbr
    _r = await _gbr(bot)
    return bool(_r) and obj.bot_id != _r.id
