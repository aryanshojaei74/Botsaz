"""price_lookup.py — «🔎 قیمت‌یاب» (PATCH_PRICE_LOOKUP_V1، درخواست سینا؛ نمونه: @PriceifyBot).

کاربر در پیوی یا گروه می‌نویسد «تون»، «5 تون»، «2 گرم طلا»، «5 دلار»، «سکه امامی»، «بیت کوین» و ربات قیمت دلاری و تومانی
(و برای کریپتو تغییر روزانه) را می‌فرستد؛ زیرش دکمه‌های شیشه‌ای که مالک تنظیم کرده.

قابلیت (نه نوع ربات): برای هر ربات جدا روشن/خاموش می‌شود (پیش‌فرض خاموش)؛ مسیر: پنل ← 🎛 دکمه‌ها و تنظیمات ← 🔎 قیمت‌یاب.

منابع:
  • دلار به تومان: همان crypto_pay.get_usd_to_toman_rate (BrsApi / والکس / …) که برای فروش هم استفاده می‌شود.
  • کریپتو: CoinGecko (عمومی، بدون کلید) — قیمت دلاری + درصد تغییر ۲۴ ساعته.
  • طلا، سکه و ارزهای دیگر: BrsApi (با کلید «brsapi_key» ربات یا پلتفرم؛ بدون کلید، نسخهٔ رایگانش). اگر در دسترس نبود
    پیام «فعلاً در دسترس نیست» می‌رود و هیچ عدد ساختگی ساخته نمی‌شود.
فقط پیام‌هایی جواب می‌گیرند که دقیقاً «عدد + نام» یا «نام» یکی از دارایی‌ها باشند؛ گفتگوی عادی دست نمی‌خورد.
"""
from __future__ import annotations

import asyncio
import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)
price_router = Router(name="price_lookup")

ON_KEY = "pq_on"
BTN_KEY = "pq_buttons"
CACHE_SEC = 60

# key → (نوع، شناسه در منبع، عنوان نمایشی، ایموجی، نام‌ها)
ASSETS: dict[str, tuple[str, str, str, str, tuple[str, ...]]] = {
    "ton": ("crypto", "the-open-network", "GRAM (ton)", "📈", ("تون", "ton", "گرم تون", "gram", "تونکوین", "toncoin")),
    "btc": ("crypto", "bitcoin", "Bitcoin", "📈", ("بیت کوین", "بیتکوین", "btc", "bitcoin")),
    "eth": ("crypto", "ethereum", "Ethereum", "📈", ("اتریوم", "eth", "ethereum")),
    "usdt": ("crypto", "tether", "Tether (USDT)", "📈", ("تتر", "usdt", "tether")),
    "trx": ("crypto", "tron", "Tron (TRX)", "📈", ("ترون", "trx", "tron")),
    "not": ("crypto", "notcoin", "Notcoin", "📈", ("نات", "نات کوین", "notcoin", "not")),
    "sol": ("crypto", "solana", "Solana", "📈", ("سولانا", "sol", "solana")),
    "doge": ("crypto", "dogecoin", "Dogecoin", "📈", ("دوج", "دوج کوین", "doge", "dogecoin")),
    "bnb": ("crypto", "binancecoin", "BNB", "📈", ("بی ان بی", "bnb")),
    "usd": ("fiat", "دلار", "دلار آمریکا", "🇺🇸", ("دلار", "دلار آمریکا", "usd")),
    "eur": ("fiat", "یورو", "یورو", "🇪🇺", ("یورو", "eur", "euro")),
    "gbp": ("fiat", "پوند", "پوند انگلیس", "🇬🇧", ("پوند", "gbp")),
    "aed": ("fiat", "درهم", "درهم امارات", "🇦🇪", ("درهم", "aed")),
    "try": ("fiat", "لیر", "لیر ترکیه", "🇹🇷", ("لیر", "لیر ترکیه", "try")),
    "gold18": ("gold", "18", "طلای 18 عیار", "⚜️", ("طلا", "گرم طلا", "طلای 18", "طلای 18 عیار", "طلا 18")),
    "gold24": ("gold", "24", "طلای 24 عیار", "⚜️", ("طلای 24", "طلای 24 عیار", "طلا 24")),
    "emami": ("gold", "امامی", "سکه امامی", "🪙", ("سکه", "سکه امامی", "سکه تمام")),
    "nim": ("gold", "نیم", "نیم سکه", "🪙", ("نیم سکه",)),
    "rob": ("gold", "ربع", "ربع سکه", "🪙", ("ربع سکه",)),
    "ons": ("gold", "انس", "انس طلا", "🌐", ("انس", "انس طلا", "اونس", "اونس طلا")),
}
_UNIT_WORDS = ("عدد", "تا", "واحد", "گرم")
_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _norm(t: str) -> str:
    t = (t or "").translate(_FA).replace("ي", "ی").replace("ك", "ک").replace("‌", " ").replace("ِ", "")
    return " ".join(t.lower().split())


_NAME_TO_KEY: dict[str, str] = {}
for _k, (_t, _id, _title, _em, _names) in ASSETS.items():
    for _n in _names:
        _NAME_TO_KEY[_norm(_n)] = _k
_NUM = r"(\d+(?:[.,/٫]\d+)?)"


def parse(text: str | None) -> tuple[float, str] | None:
    """«5 تون» ⇒ (5, "ton")؛ «تون» ⇒ (1, "ton")؛ «2 گرم طلا» ⇒ (2, "gold18»)؛ متن عادی ⇒ None."""
    t = _norm(text or "")
    if not t or len(t) > 40:
        return None
    qty = 1.0
    m = re.fullmatch(_NUM + r"\s*(.+)", t) or None
    name = t
    if m:
        qty, name = float(m.group(1).replace(",", ".").replace("/", ".").replace("٫", ".")), m.group(2)
    else:
        m2 = re.fullmatch(r"(.+?)\s+" + _NUM, t)
        if m2:
            name, qty = m2.group(1), float(m2.group(2).replace(",", ".").replace("/", ".").replace("٫", "."))
    name = name.strip()
    key = _NAME_TO_KEY.get(name)
    if key is None:
        for w in _UNIT_WORDS:                        # «2 گرم طلا» / «3 عدد سکه»
            if name.startswith(w + " ") and name[len(w) + 1:] in _NAME_TO_KEY:
                key = _NAME_TO_KEY[name[len(w) + 1:]]
                break
    if key is None or qty <= 0 or qty > 10_000_000:
        return None
    return qty, key


# ───────────────────────── منابع داده (با کش) ─────────────────────────
_CACHE: dict[str, tuple[float, object]] = {}


async def _cached(key: str, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SEC:
        return hit[1]
    val = await fn()
    if val:
        _CACHE[key] = (time.time(), val)
    return val


async def _http_json(url: str):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
        async with s.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}) as r:
            if r.status != 200:
                return None
            return await r.json(content_type=None)


async def crypto_quotes() -> dict:
    """{coingecko_id: (usd, change_pct_24h)}"""
    ids = ",".join(sorted({a[1] for a in ASSETS.values() if a[0] == "crypto"}))

    async def go():
        d = await _http_json(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true")
        if not isinstance(d, dict):
            return None
        return {k: (float(v.get("usd") or 0), float(v.get("usd_24h_change") or 0)) for k, v in d.items() if isinstance(v, dict)}
    try:
        return await _cached("cg", go) or {}
    except Exception:  # noqa: BLE001
        logger.info("price_lookup: CoinGecko در دسترس نیست", exc_info=True)
        return {}


def _num(v) -> float | None:
    try:
        x = float(str(v).replace(",", "").replace("٬", "").strip())
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def parse_brs(data) -> list[dict]:
    """پاسخ BrsApi (طلا/ارز/…) ⇒ [{name, price, change}] با قیمت تومانی. ریال (عدد خیلی بزرگ) ÷ ۱۰ نمی‌شود چون BrsApi تومان می‌دهد؛
    فقط اگر فیلد unit «ریال» بود تقسیم می‌شود."""
    items = []
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                items += [x for x in v if isinstance(x, dict)]
    elif isinstance(data, list):
        items = [x for x in data if isinstance(x, dict)]
    out = []
    for it in items:
        name = str(it.get("name") or it.get("title") or "")
        price = _num(it.get("price") if it.get("price") is not None else it.get("value"))
        if not name or price is None:
            continue
        if "ریال" in str(it.get("unit") or ""):
            price /= 10
        out.append({"name": name, "price": price, "change": _num(it.get("change_percent")) if it.get("change_percent") is not None else None,
                    "unit": str(it.get("unit") or "")})
    return out


async def brs_items(bot_id: int) -> list[dict]:
    from tenant import get_setting
    key = (await get_setting(bot_id, "brsapi_key", "") or "").strip()
    if not key:
        try:
            from platform_bot import get_pset
            key = (await get_pset("brsapi_key", "") or "").strip()
        except Exception:  # noqa: BLE001
            key = ""
    urls = ([f"https://brsapi.ir/Api/Market/Gold_Currency.php?key={key}"] if key else []) + [
        "https://brsapi.ir/FreeTsetmcBourseApi/Api_Free_Gold_Currency_v2.json"]

    async def go():
        for u in urls:
            try:
                rows = parse_brs(await _http_json(u))
                if rows:
                    return rows
            except Exception:  # noqa: BLE001
                logger.info("price_lookup: BrsApi پاسخ نداد (%s)", u.split("?")[0], exc_info=True)
        return None
    return await _cached(f"brs:{key[:6]}", go) or []


def find_brs(rows: list[dict], kind: str, ident: str) -> dict | None:
    """یافتن ردیف طلا/سکه/ارز بر اساس نام فارسی."""
    def has(n, *ws):
        return all(w in n for w in ws)
    for r in rows:
        n = _norm(r["name"])
        if kind == "fiat":
            if ident in n and "مبادله" not in n:
                return r
        elif ident == "18" and has(n, "طلا", "18"):
            return r
        elif ident == "24" and has(n, "طلا", "24"):
            return r
        elif ident == "امامی" and has(n, "سکه") and ("امامی" in n or "تمام" in n) and "نیم" not in n and "ربع" not in n:
            return r
        elif ident in ("نیم", "ربع") and has(n, ident, "سکه"):
            return r
        elif ident == "انس" and ("انس" in n or "اونس" in n):
            return r
    return None


# ───────────────────────── ساخت پیام ─────────────────────────
def _fmt_usd(x: float) -> str:
    return f"${x:,.2f}" if x >= 1 else f"${x:,.6f}".rstrip("0").rstrip(".")


def _fmt_q(q: float) -> str:
    return str(int(q)) if float(q).is_integer() else f"{q:g}"


def _now_fa() -> str:
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=3, minutes=30)
    try:
        import jdatetime
        return jdatetime.datetime.fromgregorian(datetime=now).strftime("%Y/%m/%d | %H:%M:%S")
    except Exception:  # noqa: BLE001
        return now.strftime("%Y/%m/%d | %H:%M:%S")


def render(qty: float, key: str, usd: float | None, toman: float | None, change: float | None,
           usd_toman: float | None) -> str:
    kind, _id, title, em, _n = ASSETS[key]
    q = _fmt_q(qty)
    if kind == "gold":
        head = f"{em} قیمت {q} گرم {title}" if key.startswith("gold") else f"{em} قیمت {q} {title}"
    elif kind == "fiat":
        head = f"{em} {q} {title}"
    else:
        head = f"{em} قیمت {q} {title}" if qty != 1 else f"{em} قیمت {title}"
    lines = [f"<b>{head}</b>", "", "💰 جزئیات قیمت:"]
    lines.append(f"┤💵 قیمت دلاری: {_fmt_usd(usd) if usd else '—'}")
    lines.append(f"┙🇮🇷 قیمت تومانی: {f'{toman:,.0f} تومان' if toman else '—'}")
    if change is not None:
        up = change >= 0
        lines += ["", "📊 تغییرات روزانه:", f"┤{'🟢' if up else '🔴'} درصد تغییر: {change:+.2f}%"]
        if usd:
            d_usd = abs(usd - usd / (1 + change / 100))
            d_tm = f" / {d_usd * usd_toman:,.0f} تومان" if usd_toman else ""
            lines.append(f"┙{'🔺 مقدار سود' if up else '🔻 مقدار ضرر'}: {_fmt_usd(d_usd)}{d_tm}")
    lines += ["", f"⏰ {_now_fa()}"]
    return "\n".join(lines)


async def quote(bot_id: int, qty: float, key: str) -> str:
    """متن پیام قیمت (هرگز استثنا نمی‌دهد)."""
    from tenant.gateways import crypto_pay
    kind, ident, title, _em, _n = ASSETS[key]
    try:
        usd_toman = await asyncio.wait_for(crypto_pay.get_usd_to_toman_rate(), timeout=10)
    except Exception:  # noqa: BLE001
        usd_toman = None
    if kind == "crypto":
        q = (await crypto_quotes()).get(ident)
        if not q or not q[0]:
            return f"⚠️ قیمت {title} فعلاً در دسترس نیست؛ کمی بعد دوباره امتحان کن."
        usd = q[0] * qty
        return render(qty, key, usd, usd * usd_toman if usd_toman else None, q[1], usd_toman)
    if key == "usd":
        if not usd_toman:
            return "⚠️ نرخ دلار فعلاً در دسترس نیست."
        return render(qty, key, qty, qty * usd_toman, None, usd_toman)
    row = find_brs(await brs_items(bot_id), kind, ident)
    if not row:
        return f"⚠️ قیمت {title} فعلاً در دسترس نیست؛ کمی بعد دوباره امتحان کن."
    if key == "ons":                                       # انس جهانی به دلار است
        usd = row["price"] * qty
        return render(qty, key, usd, usd * usd_toman if usd_toman else None, row.get("change"), usd_toman)
    toman = row["price"] * qty
    return render(qty, key, toman / usd_toman if usd_toman else None, toman, row.get("change"), usd_toman)


# ───────────────────────── تنظیمات مالک ─────────────────────────
class PqFSM(StatesGroup):
    buttons = State()


async def enabled(bot_id: int) -> bool:
    from tenant import get_setting
    return (await get_setting(bot_id, ON_KEY, "0")) == "1"


def parse_buttons(raw: str) -> list[tuple[str, str]]:
    """هر خط: «متن | لینک» (فقط http/https و t.me)."""
    out = []
    for line in (raw or "").splitlines():
        if "|" not in line:
            continue
        text, url = (p.strip() for p in line.split("|", 1))
        if text and re.match(r"^(https?://|tg://)", url):
            out.append((text[:40], url[:300]))
    return out[:8]


async def buttons_kb(bot_id: int) -> InlineKeyboardMarkup | None:
    from tenant import get_setting
    btns = parse_buttons(await get_setting(bot_id, BTN_KEY, ""))
    if not btns:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, url=u)] for t, u in btns])


async def _owner(event, bot: Bot):
    from tenant import get_bot_row, is_owner_or_admin
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, event.from_user.id, row.owner_id):
        return None
    return row


async def _menu(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import get_setting
    on = await enabled(bot_id)
    btns = parse_buttons(await get_setting(bot_id, BTN_KEY, ""))
    text = ("🔎 <b>قیمت‌یاب</b>\n\n"
            "کاربر در پیوی یا گروه بنویسد «تون»، «5 تون»، «2 گرم طلا»، «5 دلار»، «سکه امامی»، «بیت کوین» … و ربات قیمت دلاری و "
            "تومانی را می‌فرستد.\n\n"
            f"وضعیت: <b>{'✅ روشن' if on else '❌ خاموش'}</b>\n"
            f"دکمه‌های زیر پیام: <b>{len(btns)}</b>\n\n"
            "قیمت طلا، سکه و ارزهای غیر دلار از BrsApi خوانده می‌شود؛ برای دقیق‌تر بودن کلید BrsApi را در تنظیمات ثبت کنید.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("🔴 خاموش کردن" if on else "🟢 روشن کردن"), callback_data="pq:toggle",
                              style=ButtonStyle.DANGER if on else ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(text="🔘 دکمه‌های زیر پیام قیمت", callback_data="pq:btns", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:settings", style=ButtonStyle.DANGER)]])
    return text, kb


async def _show(target, bot_id: int):
    text, kb = await _menu(bot_id)
    m = getattr(target, "message", None) or target
    try:
        if getattr(target, "message", None) is not None:
            await m.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
    except Exception:  # noqa: BLE001
        logger.debug("price_lookup: ویرایش منو ممکن نشد", exc_info=True)
    await m.answer(text, reply_markup=kb, parse_mode="HTML")


@price_router.callback_query(F.data.in_({"pq:menu", "pq:toggle", "pq:btns"}))
async def pq_cb(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _owner(cb, bot)
    if not row:
        await cb.answer("فقط مالک یا ادمین ربات", show_alert=True)
        return
    if cb.data == "pq:toggle":
        await set_setting(row.id, ON_KEY, "0" if await enabled(row.id) else "1")
    elif cb.data == "pq:btns":
        await state.set_state(PqFSM.buttons)
        await cb.message.answer("دکمه‌های زیر پیام قیمت را بفرست؛ هر خط یک دکمه به شکل «متن | لینک». مثال:\n"
                                "<code>خرید استارز کف قیمت | https://t.me/mybot?start=stars</code>\n"
                                "<code>کانال ما | https://t.me/mychannel</code>\n\n"
                                "برای حذف همه: «پاک» · انصراف: /cancel", parse_mode="HTML")
        await cb.answer()
        return
    await _show(cb, row.id)
    await cb.answer()


@price_router.message(PqFSM.buttons)
async def pq_buttons_input(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting
    row = await _owner(msg, bot)
    if not row:
        await state.clear()
        return
    raw = (msg.text or "").strip()
    if raw in ("/cancel", "لغو"):
        await state.clear()
        await _show(msg, row.id)
        return
    if raw == "پاک":
        await set_setting(row.id, BTN_KEY, "")
    else:
        btns = parse_buttons(raw)
        if not btns:
            await msg.answer("❌ هیچ خط درستی پیدا نشد. هر خط: «متن | https://…». دوباره بفرست یا /cancel.")
            return
        await set_setting(row.id, BTN_KEY, "\n".join(f"{t} | {u}" for t, u in btns))
    await state.clear()
    await msg.answer("✅ ذخیره شد.")
    await _show(msg, row.id)


# ───────────────────────── پاسخ به پیام کاربر ─────────────────────────
async def maybe_answer(msg: Message, bot: Bot, bot_id: int) -> bool:
    """True ⇒ پیام، درخواست قیمت بود و جواب داده شد."""
    p = parse(msg.text)
    if p is None or not await enabled(bot_id):
        return False
    text = await quote(bot_id, p[0], p[1])
    await msg.reply(text, parse_mode="HTML", reply_markup=await buttons_kb(bot_id), disable_web_page_preview=True)
    return True


async def pq_open(msg: Message, bot: Bot):
    """ورودی منو از حالت کیبوردی (_settings_handlers)."""
    row = await _owner(msg, bot)
    if row:
        await _show(msg, row.id)
