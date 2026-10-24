"""
reseller.py — «نمایندگیِ خودکار» (پله‌های تخفیف بر اساس شارژ/خرید).

=== PATCH_RESELLER_TIERS_V1 ===

فرقش با tenant/vpn_partners.py:
  • vpn_partners = همکارِ *دستی*. ادمین آیدیِ یک نفر را می‌نویسد و درصدش را
    خودش تعیین می‌کند.
  • این ماژول = نمایندگیِ *خودکار*. ادمین پله تعریف می‌کند («۱ میلیون شارژ
    کن → ۱۵٪»، «۳ میلیون → ۲۵٪») و هر کاربری که به آن پله برسد، بدونِ
    دخالتِ ادمین تخفیفش اعمال می‌شود.

هر دو در نهایت از یک مسیر رد می‌شوند: partner_discount_percent() در
vpn_partners.py هر دو را حساب می‌کند و *بزرگ‌ترش* را برمی‌گرداند. پس همهٔ
نقاطِ محاسبهٔ قیمت که از قبل تخفیفِ همکار را می‌فهمیدند، خودکار نمایندگی را
هم می‌فهمند و لازم نبود منطقِ قیمت در چند جا دست بخورد.

مبنای محاسبه (قابل انتخاب توسط ادمین):
  • topup    — مجموعِ شارژهای کیف پول (kind در charge/swap_charge/admin)
  • purchase — مجموعِ خریدهای پرداخت‌شده (WalletTxn.kind == "purchase")
  • both     — بیشترینِ این دو (نه جمعشان؛ وگرنه یک پولِ واحد دوبار شمرده
               می‌شد: یک‌بار موقعِ شارژ و یک‌بار موقعِ خرج‌کردنِ همان شارژ)

تنظیمات (Setting، per-bot):
  reseller_enabled  — "0"/"1"
  reseller_tiers    — "1000000:15,3000000:25"  (آستانهٔ تومان : درصد)
  reseller_basis    — "topup" | "purchase" | "both"

callbackها با پیشوند: rsl:
"""
from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import Session
from models import BotUser, WalletTxn

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

reseller_router = Router()

_TOPUP_KINDS = ("charge", "swap_charge", "admin")
_BASIS_LABELS = {
    "topup": "مجموعِ شارژِ کیف پول",
    "purchase": "مجموعِ خرید",
    "both": "بیشترینِ شارژ یا خرید",
}

# === PATCH_RESELLER_GB_MODE_V1 ===
# حالتِ نمایندگیِ خودکار: "percent" یعنی پله‌هایِ درصدیِ سراسری (رفتارِ
# قدیمی، نیاز به تعریفِ پله دارد)؛ "gb" یعنی فقط تعرفهٔ گیگی/روزانه‌یِ
# per-panel (rs_price_gb/day) که نیازی به پله ندارد. تعرفهٔ گیگی همیشه
# مستقل از reseller_enabled اعمال می‌شود؛ این حالت فقط تعیین می‌کند که
# دکمهٔ «فعال‌کردن» برایِ فعال‌شدن به پله نیاز دارد یا نه.
_MODE_LABELS = {
    "percent": "🎯 پله‌ایِ درصدی (نیاز به تعریفِ پله)",
    "gb": "💵 فقط تعرفهٔ گیگی/روزانه (بدونِ پله)",
}


async def get_reseller_mode(bot_id: int) -> str:
    from tenant import get_setting
    mode = await get_setting(bot_id, "reseller_mode", "percent")
    return mode if mode in _MODE_LABELS else "percent"


async def set_reseller_mode(bot_id: int, mode: str) -> None:
    from tenant import set_setting
    if mode not in _MODE_LABELS:
        mode = "percent"
    await set_setting(bot_id, "reseller_mode", mode)


class ResellerFSM(StatesGroup):
    tiers = State()
    panel_pct = State()   # === PATCH_RESELLER_AUTO_PER_PANEL_V1 === data: rsl_panel_key
    panel_gb = State()    # === PATCH_RESELLER_SETTINGS_MOVED_V1 === تعرفهٔ گیگی
    signup_min = State()  # === PATCH_RESELLER_SELF_SIGNUP_V1 ===


# ─────────────── خواندن/نوشتنِ تنظیمات ───────────────

def parse_tiers(raw: str | None) -> list[tuple[int, int]]:
    """«1000000:15,3000000:25» → [(1000000, 15), (3000000, 25)] مرتب‌شده."""
    out: list[tuple[int, int]] = []
    if not raw:
        return out
    for part in str(raw).split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        a, _, b = part.partition(":")
        try:
            need = int(float(a.strip()))
            pct = int(float(b.strip()))
        except ValueError:
            continue
        if need > 0 and 1 <= pct <= 100:
            out.append((need, pct))
    out.sort(key=lambda x: x[0])
    return out


def format_tiers(tiers: list[tuple[int, int]]) -> str:
    return ",".join(f"{need}:{pct}" for need, pct in tiers)


def percent_for(tiers: list[tuple[int, int]], have: int) -> int:
    """بزرگ‌ترین درصدی که کاربر با مبلغِ «have» واجدِ آن است."""
    best = 0
    for need, pct in tiers:
        if have >= need and pct > best:
            best = pct
    return best


async def plan_allows(bot_id: int) -> bool:
    """=== PATCH_RESELLER_TIERS_V1 === این قابلیت فقط پلنِ حرفه‌ای است.

    عمداً در پایین‌ترین لایه چک می‌شود، نه فقط موقعِ نمایشِ منو: اگر رباتی
    از حرفه‌ای به رشد برگردد، پله‌هایش در تنظیمات باقی می‌مانند و بدونِ این
    چک همچنان به مشتری‌ها تخفیف می‌داد.
    """
    from models import Bot as BotRow
    from plans import Feature, has_feature
    async with Session() as s:
        r = await s.get(BotRow, bot_id)
    return bool(r) and has_feature(r.plan, Feature.RESELLER_TIERS)


async def get_config(bot_id: int) -> tuple[bool, list[tuple[int, int]], str]:
    from tenant import get_setting
    if not await plan_allows(bot_id):
        return False, [], "topup"
    enabled = await get_setting(bot_id, "reseller_enabled", "0") == "1"
    tiers = parse_tiers(await get_setting(bot_id, "reseller_tiers", ""))
    basis = await get_setting(bot_id, "reseller_basis", "topup")
    if basis not in _BASIS_LABELS:
        basis = "topup"
    return enabled, tiers, basis


# ─────────────── محاسبهٔ مبلغِ واجدِ شرایط ───────────────

async def qualifying_amount(bot_id: int, telegram_id: int, basis: str | None = None) -> int:
    """مبلغی که مبنایِ تعیینِ پلهٔ این کاربر است (تومان)."""
    if basis is None:
        _, _, basis = await get_config(bot_id)
    async with Session() as s:
        u = await s.scalar(select(BotUser).where(
            BotUser.bot_id == bot_id, BotUser.telegram_id == telegram_id))
        if not u:
            return 0
        topup = 0
        purchase = 0
        if basis in ("topup", "both"):
            topup = int(await s.scalar(
                select(func.coalesce(func.sum(WalletTxn.amount), 0)).where(
                    WalletTxn.bot_id == bot_id, WalletTxn.user_id == u.id,
                    WalletTxn.kind.in_(_TOPUP_KINDS), WalletTxn.amount > 0)) or 0)
        if basis in ("purchase", "both"):
            # خریدها با مبلغِ منفی ثبت می‌شوند؛ قدرِ مطلق را می‌گیریم.
            purchase = int(await s.scalar(
                select(func.coalesce(func.sum(func.abs(WalletTxn.amount)), 0)).where(
                    WalletTxn.bot_id == bot_id, WalletTxn.user_id == u.id,
                    WalletTxn.kind == "purchase")) or 0)
    if basis == "topup":
        return topup
    if basis == "purchase":
        return purchase
    # both — بیشترینِ دو، نه جمعشان (یک پول دوبار شمرده نشود)
    return max(topup, purchase)


# === PATCH_RESELLER_AUTO_PER_PANEL_V1 ===
# درخواستِ ادمین‌ها: «برایِ پاسارگاد ۱۰٪ و برایِ یوپنل ۲۵٪». پله‌ها همان
# می‌مانند؛ این ضریب فقط می‌گوید درصدِ به‌دست‌آمده رویِ کدام پنل چقدر
# اعمال شود. ذخیره در Setting به‌صورتِ «key:pct,key:pct».
async def get_panel_overrides(bot_id: int) -> dict:
    from tenant import get_setting
    raw = (await get_setting(bot_id, "reseller_panel_pct", "")).strip()
    out = {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        k, v = k.strip(), v.strip()
        if k and v.isdigit():
            out[k] = max(0, min(100, int(v)))
    return out


async def set_panel_override(bot_id: int, panel_key: str, pct: int | None) -> None:
    from tenant import set_setting
    d = await get_panel_overrides(bot_id)
    if pct is None:
        d.pop(panel_key, None)
    else:
        d[panel_key] = max(0, min(100, int(pct)))
    await set_setting(bot_id, "reseller_panel_pct",
                      ",".join(f"{k}:{v}" for k, v in d.items()))


async def reseller_percent(bot_id: int, telegram_id: int, panel_key: str = "") -> int:
    """درصدِ تخفیفِ نمایندگیِ خودکارِ این کاربر؛ اگر واجد نبود ۰.

    اگر برایِ این پنل درصدِ اختصاصی تعریف شده باشد، همان جایگزینِ درصدِ
    پله می‌شود (نه رویِ آن) — وگرنه تخفیف‌ها رویِ هم انباشته می‌شدند.
    """
    enabled, tiers, basis = await get_config(bot_id)
    if not enabled or not tiers:
        return 0
    have = await qualifying_amount(bot_id, telegram_id, basis)
    pct = max(0, min(100, percent_for(tiers, have)))
    if pct <= 0 or not panel_key:
        return pct
    over = await get_panel_overrides(bot_id)
    if panel_key in over:
        return over[panel_key]
    return pct



async def user_status(bot_id: int, telegram_id: int) -> dict:
    """وضعیتِ نمایندگیِ کاربر برایِ نمایش به خودش (پلهٔ فعلی و پلهٔ بعدی)."""
    enabled, tiers, basis = await get_config(bot_id)
    if not enabled or not tiers:
        return {"enabled": False}
    have = await qualifying_amount(bot_id, telegram_id, basis)
    pct = percent_for(tiers, have)
    nxt = None
    for need, npct in tiers:
        if have < need:
            nxt = (need, npct, need - have)
            break
    return {"enabled": True, "have": have, "percent": pct, "next": nxt,
            "basis": basis, "tiers": tiers}


# ─────────────── پنلِ ادمین ───────────────

async def _panel(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    from tenant import rial
    enabled, tiers, basis = await get_config(bot_id)
    # === PATCH_RESELLER_GB_MODE_V1 ===
    _mode = await get_reseller_mode(bot_id)
    status = "🟢 فعال" if enabled else "🔴 غیرفعال"
    if tiers:
        lines = "\n".join(
            f"  • از {rial(need)} به بالا → <b>{pct}٪</b> تخفیف" for need, pct in tiers)
    else:
        lines = "  (هنوز پله‌ای تعریف نشده)"
    # === PATCH_RESELLER_MERGE_V1 === شمارشِ نمایندگانِ دستی برایِ همین صفحه
    from models import VpnPartner
    async with Session() as _s_m:
        manual_total = int(await _s_m.scalar(select(func.count()).select_from(VpnPartner).where(
            VpnPartner.bot_id == bot_id)) or 0)
        manual_on = int(await _s_m.scalar(select(func.count()).select_from(VpnPartner).where(
            VpnPartner.bot_id == bot_id, VpnPartner.active == True)) or 0)
    # === PATCH_RESELLER_AUTO_PER_PANEL_V1 ===
    _over = await get_panel_overrides(bot_id)
    if _over:
        from tenant.bots.vpn.vpn_admin import panel_label as _plabel
        _parts = [f"{await _plabel(bot_id, k)}: {v}٪" for k, v in _over.items()]
        _over_line = "استثنای پنل‌ها: " + "، ".join(_parts) + "\n"
    else:
        _over_line = ""
    body = (
        "🏷 <b>نمایندگی</b>\n\n"
        "دو راه برای دادنِ نمایندگی داری و هر دو از همین‌جا تنظیم می‌شوند:\n\n"
        "<b>۱) خودکار (پله‌ای)</b> — هر کاربری که به آستانه برسد، بدونِ کدِ تخفیف "
        "و بدونِ دخالتِ تو، درصدِ همان پله را می‌گیرد.\n"
        f"وضعیت: {status}\n"
        f"مبنای محاسبه: <b>{_BASIS_LABELS[basis]}</b>\n"
        f"پله‌ها:\n{lines}\n"
        f"{_over_line}\n"
        "<b>۲) دستی</b> — آیدیِ عددیِ یک نفر را ثبت می‌کنی و درصدش را خودت "
        "می‌گذاری. برای هر نفر جدا مشخص می‌کنی روی کدام پنل‌ها، کدام نوعِ خرید، "
        "یا رویِ همهٔ محصولاتِ ربات اعمال شود.\n"
        f"نمایندگانِ دستی: <b>{manual_on}</b> فعال از <b>{manual_total}</b>\n\n"
        "ℹ️ اگر کسی هم‌زمان مشمولِ هر دو باشد، هرکدام که درصدش بیشتر است اعمال "
        "می‌شود — با هم جمع نمی‌شوند."
    )
    # === PATCH_RESELLER_SELF_SIGNUP_V1 ===
    from tenant.shared.reseller_signup import min_balance as _rsp_min
    _mb_val = await _rsp_min(bot_id)
    _signup_min_label = f"{_mb_val:,} تومان" if _mb_val else "بدونِ شرط"
    rows = [
        [InlineKeyboardButton(
            text=("🔴 غیرفعال‌کردنِ نمایندگیِ خودکار" if enabled
                  else "🟢 فعال‌کردنِ نمایندگیِ خودکار"),
            callback_data="rsl:toggle",
            style=(ButtonStyle.DANGER if enabled else ButtonStyle.SUCCESS))],
        [InlineKeyboardButton(text="✏️ تعریف/ویرایش پله‌ها", callback_data="rsl:edit",
                              style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(text=f"🔀 مبنا: {_BASIS_LABELS[basis]}", callback_data="rsl:basis",
                              style=ButtonStyle.PRIMARY)],
        # === PATCH_RESELLER_GB_MODE_V1 ===
        [InlineKeyboardButton(text=f"🎛 حالت: {_MODE_LABELS[_mode]}", callback_data="rsl:mode",
                              style=ButtonStyle.PRIMARY)],
        # === PATCH_RESELLER_AUTO_PER_PANEL_V1 ===
        [InlineKeyboardButton(text="🏷 تخفیفِ جدا برایِ هر پنل", callback_data="rsl:panels",
                              style=ButtonStyle.SUCCESS)],
        # === PATCH_RESELLER_SETTINGS_MOVED_V1 ===
        # از صفحهٔ «فروش آنلاین»ِ هر پنل به اینجا منتقل شد تا همهٔ تنظیماتِ
        # مربوط به نماینده یک‌جا باشد و با تنظیماتِ فروش به کاربرِ عادی
        # قاطی نشود.
        [InlineKeyboardButton(text="💵 تعرفهٔ گیگیِ نمایندگان", callback_data="rsl:gb",
                              style=ButtonStyle.SUCCESS)],
        # === PATCH_RESELLER_SELF_SIGNUP_V1 ===
        # شرطِ موجودی برایِ ثبت‌نامِ خودکارِ نمایندگی (دکمهٔ «🏷 پنلِ نمایندگی»).
        [InlineKeyboardButton(text=f"🎚 حداقلِ موجودی برایِ ثبت‌نام: {_signup_min_label}",
                              callback_data="rsl:signupmin", style=ButtonStyle.SUCCESS)],
        # === PATCH_RESELLER_MERGE_V1 === نمایندگیِ دستی زیرِ همین هاب
        # === PATCH_VPN_PARTNER_REPLY_KB_V1 === origin="rsl" تا «بازگشت» درست بشه
        [InlineKeyboardButton(text=f"👤 نمایندگانِ دستی ({manual_total})",
                              callback_data="vprt:list:rsl", style=ButtonStyle.SUCCESS)],
        # === PATCH_RESELLER_MOVE_TO_ADMINS_V1 ===
        # این بخش از «تنظیمات» به «ادمین‌ها و پشتیبانی» منتقل شد؛ دکمهٔ
        # بازگشت هم باید به همان‌جا برگردد، وگرنه کاربر در منویِ اشتباهی
        # سر درمی‌آورد.
        [InlineKeyboardButton(text="⬅️ بازگشت", callback_data="tadm:sec:admins",
                              style=ButtonStyle.DANGER)],
    ]
    return body, InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(cb: CallbackQuery, bot_id: int):
    body, kb = await _panel(bot_id)
    try:
        await cb.message.edit_text(body, reply_markup=kb)
    except Exception:
        await cb.message.answer(body, reply_markup=kb)


@reseller_router.callback_query(F.data == "rsl:menu")
async def rsl_menu(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await plan_allows(row.id):
        await cb.answer("این قابلیت فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return
    await cb.answer()
    await _show(cb, row.id)


@reseller_router.callback_query(F.data == "rsl:toggle")
async def rsl_toggle(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, get_setting, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await plan_allows(row.id):
        await cb.answer("این قابلیت فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return
    cur = await get_setting(row.id, "reseller_enabled", "0")
    new = "0" if cur == "1" else "1"
    # === PATCH_RESELLER_GB_MODE_V1 ===
    # اگه حالتِ نمایندگی رویِ "gb" باشه (یعنی ادمین فقط از تعرفهٔ
    # گیگی/روزانه استفاده می‌کنه)، نیازی به تعریفِ پله نیست — چون
    # تعرفهٔ گیگی خودش مستقل از این Setting اعمال می‌شه؛ این چک فقط
    # مخصوصِ حالتِ پله‌ایِ درصدیه.
    mode = await get_reseller_mode(row.id)
    if new == "1" and mode == "percent":
        _, tiers, _ = await get_config(row.id)
        if not tiers:
            await cb.answer("اول حداقل یک پله تعریف کن، یا از دکمهٔ 🎛 حالت به "
                            "«فقط تعرفهٔ گیگی» سوییچ کن.", show_alert=True)
            return
    await set_setting(row.id, "reseller_enabled", new)
    await cb.answer("🟢 فعال شد" if new == "1" else "🔴 غیرفعال شد")
    await _show(cb, row.id)


# === PATCH_RESELLER_GB_MODE_V1 ===
@reseller_router.callback_query(F.data == "rsl:mode")
async def rsl_mode(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await plan_allows(row.id):
        await cb.answer("این قابلیت فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return
    order = ["percent", "gb"]
    cur = await get_reseller_mode(row.id)
    new = order[(order.index(cur) + 1) % len(order)]
    await set_reseller_mode(row.id, new)
    await cb.answer(f"حالت شد: {_MODE_LABELS[new]}")
    await _show(cb, row.id)


# === PATCH_RESELLER_AUTO_PER_PANEL_V1 ===
@reseller_router.callback_query(F.data == "rsl:panels")
async def rsl_panels(cb: CallbackQuery, bot: Bot):
    """فهرستِ پنل‌ها با درصدِ اختصاصیِ هرکدام."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    from models import VpnPanel
    from tenant.bots.vpn.vpn_admin import panel_label
    async with Session() as s:
        panels = list((await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == row.id))).all())
    over = await get_panel_overrides(row.id)
    rows = []
    for p in panels:
        pct = over.get(p.panel_key)
        rows.append([InlineKeyboardButton(
            text=(f"{await panel_label(row.id, p.panel_key)} — "
                  + (f"{pct}٪" if pct is not None else "طبقِ پله")),
            callback_data=f"rsl:panelpct:{p.panel_key}")])
    if not panels:
        rows.append([InlineKeyboardButton(text="⚠️ هنوز پنلی متصل نشده",
                                          callback_data="rsl:noop")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="rsl:menu",
                                      style=ButtonStyle.DANGER)])
    await cb.answer()
    txt = ("🏷 <b>تخفیفِ جدا برایِ هر پنل</b>\n\n"
           "پله‌ها همان‌طور که هستند می‌مانند؛ اینجا فقط می‌گویی درصدِ "
           "به‌دست‌آمده رویِ کدام پنل چقدر اعمال شود.\n\n"
           "«طبقِ پله» یعنی همان درصدی که کاربر از پله‌اش گرفته.\n"
           "عددِ اختصاصی <b>جایگزینِ</b> درصدِ پله می‌شود، نه اضافهٔ آن.")
    try:
        await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@reseller_router.callback_query(F.data == "rsl:noop")
async def rsl_noop(cb: CallbackQuery):
    await cb.answer()


@reseller_router.callback_query(F.data.startswith("rsl:panelpct:"))
async def rsl_panel_pct_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    from tenant.bots.vpn.vpn_admin import panel_label
    cur = (await get_panel_overrides(row.id)).get(key)
    await state.set_state(ResellerFSM.panel_pct)
    await state.update_data(rsl_panel_key=key)
    await cb.answer()
    await cb.message.answer(
        f"🏷 <b>{await panel_label(row.id, key)}</b>\n\n"
        f"مقدارِ فعلی: <b>{f'{cur}٪' if cur is not None else 'طبقِ پله'}</b>\n\n"
        "درصدِ تازه را بفرست (۰ تا ۱۰۰).\n"
        "برایِ برگشت به حالتِ «طبقِ پله»، کلمهٔ <code>حذف</code> را بفرست.",
        # === PATCH_RESELLER_MISSING_CANCEL_BUTTONS_V1 ===
        reply_markup=_cancel_kb())


@reseller_router.message(ResellerFSM.panel_pct, F.text)
async def rsl_panel_pct_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant.bots.vpn.vpn_admin import _owner_msg
    row = await _owner_msg(msg, bot)
    if not row:
        await state.clear()
        return
    key = (await state.get_data()).get("rsl_panel_key")
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "لغو"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    if raw in ("حذف", "-"):
        await state.clear()
        await set_panel_override(row.id, key, None)
        await msg.answer("✅ برداشته شد؛ این پنل از درصدِ پلهٔ کاربر پیروی می‌کند.")
        return
    raw = raw.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    if not raw.isdigit() or int(raw) > 100:
        await msg.answer("عددی بین ۰ تا ۱۰۰ بفرست (یا <code>حذف</code>):")
        return
    await state.clear()
    await set_panel_override(row.id, key, int(raw))
    from tenant.bots.vpn.vpn_admin import panel_label
    await msg.answer(f"✅ تخفیفِ نمایندگیِ خودکار رویِ "
                     f"<b>{await panel_label(row.id, key)}</b> روی "
                     f"<b>{int(raw)}٪</b> تنظیم شد.")


# === PATCH_RESELLER_SETTINGS_MOVED_V1 ===
@reseller_router.callback_query(F.data == "rsl:gb")
async def rsl_gb_list(cb: CallbackQuery, bot: Bot):
    """تعرفهٔ گیگی/روزانهٔ نمایندگان، به تفکیکِ پنل.

    برایِ مالکانی که به نماینده گیگی می‌فروشند نه پلنی. این تعرفه
    *به‌جایِ* درصدِ تخفیف می‌نشیند، نه رویِ آن.
    """
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    from models import VpnPanel
    from tenant.bots.vpn.vpn_admin import panel_label
    from tenant.bots.vpn.vpn_user import _vc
    async with Session() as s:
        panels = list((await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == row.id))).all())
    rows = []
    for p in panels:
        g = (await _vc(row.id, "rs_price_gb", p.panel_key)).strip()
        d = (await _vc(row.id, "rs_price_day", p.panel_key)).strip()
        has = g.isdigit() and int(g) > 0
        rows.append([InlineKeyboardButton(
            text=(f"{await panel_label(row.id, p.panel_key)} — "
                  + (f"{int(g):,}ت/گیگ، {int(d or 0):,}ت/روز" if has else "تنظیم نشده")),
            callback_data=f"rsl:gbset:{p.panel_key}")])
    if not panels:
        rows.append([InlineKeyboardButton(text="⚠️ هنوز پنلی متصل نشده",
                                          callback_data="rsl:noop")])
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="rsl:menu",
                                      style=ButtonStyle.DANGER)])
    await cb.answer()
    txt = ("💵 <b>تعرفهٔ گیگیِ نمایندگان</b>\n\n"
           "اگر به نمایندگانت گیگی می‌فروشی نه پلنی، اینجا قیمتِ هر گیگ و "
           "هر روز را برایِ آن‌ها جدا تعیین کن.\n\n"
           "ℹ️ این تعرفه <b>جایگزینِ</b> درصدِ تخفیفِ نمایندگی می‌شود، نه "
           "اضافهٔ آن — وگرنه تخفیف دوبار حساب می‌شد.\n"
           "«تنظیم نشده» یعنی همان درصدِ تخفیف اعمال می‌شود.")
    try:
        await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@reseller_router.callback_query(F.data.startswith("rsl:gbset:"))
async def rsl_gb_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    key = cb.data.split(":", 2)[2]
    from tenant.bots.vpn.vpn_admin import panel_label
    from tenant.bots.vpn.vpn_user import _vc, _vc_all
    cfg = await _vc_all(row.id, key)
    g = (await _vc(row.id, "rs_price_gb", key)).strip()
    d = (await _vc(row.id, "rs_price_day", key)).strip()
    await state.set_state(ResellerFSM.panel_gb)
    await state.update_data(rsl_gb_key=key)
    await cb.answer()
    await cb.message.answer(
        f"💵 <b>{await panel_label(row.id, key)}</b>\n\n"
        f"تعرفهٔ عادی (همهٔ کاربران): {int(cfg['vc_price_gb']):,} تومان/گیگ، "
        f"{int(cfg['vc_price_day']):,} تومان/روز\n"
        f"تعرفهٔ نمایندگان: "
        + (f"{int(g):,} تومان/گیگ، {int(d or 0):,} تومان/روز" if g.isdigit() and int(g) > 0
           else "تنظیم نشده") + "\n\n"
        "دو عدد با فاصله بفرست: <b>قیمت‌گیگ قیمت‌روز</b>\n"
        "مثال: <code>700 300</code>\n\n"
        "برایِ برداشتنِ تعرفه و برگشت به حالتِ درصدی، <code>0 0</code> بفرست.",
        # === PATCH_RESELLER_MISSING_CANCEL_BUTTONS_V1 ===
        reply_markup=_cancel_kb())


@reseller_router.message(ResellerFSM.panel_gb, F.text)
async def rsl_gb_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant.bots.vpn.vpn_admin import _owner_msg, _vc_set, fa_to_int, panel_label
    row = await _owner_msg(msg, bot)
    if not row:
        await state.clear()
        return
    key = (await state.get_data()).get("rsl_gb_key")
    raw = (msg.text or "").strip()
    if raw in ("❌ لغو", "لغو"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    parts = raw.split()
    if len(parts) != 2 or any(fa_to_int(x) is None for x in parts):
        await msg.answer("دو عدد با فاصله بفرست: قیمت‌گیگ قیمت‌روز")
        return
    pg, pd = [max(0, fa_to_int(x)) for x in parts]
    await state.clear()
    await _vc_set(row.id, "rs_price_gb", str(pg), key)
    await _vc_set(row.id, "rs_price_day", str(pd), key)
    label = await panel_label(row.id, key)
    if pg > 0:
        await msg.answer(f"✅ تعرفهٔ نمایندگان رویِ <b>{label}</b>: "
                         f"<b>{pg:,}</b> تومان/گیگ و <b>{pd:,}</b> تومان/روز.")
    else:
        await msg.answer(f"✅ تعرفهٔ نمایندگان رویِ <b>{label}</b> برداشته شد؛ "
                         "از این پس درصدِ تخفیف اعمال می‌شود.")


@reseller_router.callback_query(F.data == "rsl:basis")
async def rsl_basis(cb: CallbackQuery, bot: Bot):
    from tenant import _owner_ok, set_setting
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await plan_allows(row.id):
        await cb.answer("این قابلیت فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return
    _, _, basis = await get_config(row.id)
    order = ["topup", "purchase", "both"]
    new = order[(order.index(basis) + 1) % len(order)]
    await set_setting(row.id, "reseller_basis", new)
    await cb.answer(f"مبنا: {_BASIS_LABELS[new]}")
    await _show(cb, row.id)


def _cancel_kb() -> InlineKeyboardMarkup:
    """=== PATCH_RESELLER_OWN_CANCEL_V1 ===
    قبلاً cancel_kb از tenant.bots.vpn.vpn_admin قرض گرفته می‌شد، ولی آن دکمه
    callback_data="tvpn:cancel" دارد و هندلرش vpn_menu را باز می‌کند — برای
    همین «لغو» کاربر را به بخشِ اتصالِ پنل‌ها پرت می‌کرد. حالا لغوِ خودمان.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ لغو", callback_data="rsl:cancel",
                             style=ButtonStyle.DANGER)]])


@reseller_router.callback_query(F.data == "rsl:cancel")
async def rsl_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    """لغوِ ویرایشِ پله‌ها → برگشت به همان پنلِ نمایندگی."""
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    await state.clear()
    await cb.answer("لغو شد")
    body, kb = await _panel(row.id)
    await cb.message.answer(body, reply_markup=kb)


@reseller_router.callback_query(F.data == "rsl:edit")
async def rsl_edit_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await plan_allows(row.id):
        await cb.answer("این قابلیت فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return
    _, tiers, _ = await get_config(row.id)
    cur = "\n".join(f"{need}:{pct}" for need, pct in tiers) or "(خالی)"
    await state.set_state(ResellerFSM.tiers)
    await state.update_data(bot_id=row.id)
    await cb.answer()
    await cb.message.answer(
        "✏️ <b>تعریف پله‌های نمایندگی</b>\n\n"
        "هر پله را در یک خط بنویس، به شکلِ <code>مبلغ:درصد</code>.\n"
        "مبلغ به تومان است و درصد بین ۱ تا ۱۰۰.\n\n"
        "مثال:\n"
        "<code>1000000:15\n3000000:25</code>\n\n"
        "یعنی «۱ میلیون → ۱۵٪» و «۳ میلیون → ۲۵٪».\n\n"
        f"📋 پله‌های فعلی:\n<code>{cur}</code>\n\n"
        "برای پاک‌کردنِ همهٔ پله‌ها، کلمهٔ <code>حذف</code> را بفرست.",
        reply_markup=_cancel_kb())


@reseller_router.message(ResellerFSM.tiers, F.text)
async def rsl_edit_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import set_setting, rial
    from tenant.bots.vpn.vpn_admin import _owner_msg
    row = await _owner_msg(msg, bot)
    if not row:
        return
    if not await plan_allows(row.id):
        await state.clear()
        await msg.answer("این قابلیت فقط در پلنِ حرفه‌ای فعال است.")
        return
    txt = (msg.text or "").strip()
    if txt in ("لغو", "❌ لغو", "/cancel"):
        await state.clear()
        # === PATCH_RESELLER_OWN_CANCEL_V1 ===
        # همان مقصدِ دکمهٔ لغوِ شیشه‌ای: برگشت به خودِ پنلِ نمایندگی، نه
        # پرت‌شدن به منویِ دیگر.
        from tenant import _sec_admins_kb
        await msg.answer("لغو شد.", reply_markup=await _sec_admins_kb(row.id, row.plan))
        body, kb = await _panel(row.id)
        await msg.answer(body, reply_markup=kb)
        return
    if txt == "حذف":
        await state.clear()
        await set_setting(row.id, "reseller_tiers", "")
        await set_setting(row.id, "reseller_enabled", "0")
        from tenant import _sec_admins_kb
        await msg.answer("✅ همهٔ پله‌ها حذف و نمایندگی غیرفعال شد.",
                         reply_markup=await _sec_admins_kb(row.id, row.plan))
        return
    # هم خط‌به‌خط و هم جداشده با کاما را قبول کن
    tiers = parse_tiers(txt.replace("\n", ","))
    if not tiers:
        await msg.answer(
            "❌ چیزی که فرستادی خوانده نشد.\n\n"
            "هر خط باید به شکلِ <code>مبلغ:درصد</code> باشد، مثلاً "
            "<code>1000000:15</code>. دوباره بفرست یا «لغو» بزن.")
        return
    await state.clear()
    await set_setting(row.id, "reseller_tiers", format_tiers(tiers))
    lines = "\n".join(f"  • از {rial(n)} به بالا → {p}٪" for n, p in tiers)
    # === PATCH_RESELLER_MOVE_TO_ADMINS_V1 ===
    # در حالتِ کیبوردی، کیبوردِ پایین باید به زیرمنویِ «ادمین‌ها و پشتیبانی»
    # برگردد (جایی که این دکمه حالا زندگی می‌کند)، نه صفحهٔ اصلی.
    from tenant import _sec_admins_kb
    await msg.answer(f"✅ پله‌ها ذخیره شد:\n{lines}",
                     reply_markup=await _sec_admins_kb(row.id, row.plan))
    body, kb = await _panel(row.id)
    await msg.answer(body, reply_markup=kb)


# ═══════════════════════════════════════════
# === PATCH_RESELLER_SELF_SIGNUP_V1 ===
# حداقلِ موجودیِ لازم برایِ اینکه کاربر خودش نمایندگی بگیرد. *شرط* است،
# نه هزینه — چیزی از کیفِ پول کم نمی‌شود.
# ═══════════════════════════════════════════

@reseller_router.callback_query(F.data == "rsl:signupmin")
async def rsl_signup_min_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    from tenant import _owner_ok
    row = await _owner_ok(cb, bot)
    if not row:
        return
    if not await plan_allows(row.id):
        await cb.answer("این قابلیت فقط در پلنِ حرفه‌ای فعال است.", show_alert=True)
        return
    from tenant.shared.reseller_signup import min_balance as _rsp_min
    cur = await _rsp_min(row.id)
    await state.set_state(ResellerFSM.signup_min)
    await cb.message.answer(
        "🎚 <b>حداقلِ موجودی برایِ ثبت‌نامِ نمایندگی</b>\n\n"
        f"مقدارِ فعلی: <b>{cur:,} تومان</b>"
        f"{' (بدونِ شرط)' if not cur else ''}\n\n"
        "عددِ جدید را به تومان بفرست. <b>۰</b> یعنی هرکسی بتواند بدونِ "
        "شرطِ موجودی نمایندگی بگیرد.\n\n"
        "ℹ️ این مبلغ فقط شرط است و از کیفِ پولِ کاربر کم نمی‌شود.",
        parse_mode="HTML",
        # === PATCH_RESELLER_MISSING_CANCEL_BUTTONS_V1 ===
        reply_markup=_cancel_kb())
    await cb.answer()


@reseller_router.message(ResellerFSM.signup_min, F.text)
async def rsl_signup_min_save(msg: Message, bot: Bot, state: FSMContext):
    from tenant import get_bot_row, is_owner_or_admin, fa_to_int
    row = await get_bot_row(bot)
    if not row or not await is_owner_or_admin(row.id, msg.from_user.id, row.owner_id):
        return
    raw = (msg.text or "").strip()
    if raw in ("لغو", "❌ لغو", "/cancel"):
        await state.clear()
        await msg.answer("لغو شد.")
        return
    val = fa_to_int(raw)
    if val is None or val < 0:
        await msg.answer("❌ فقط عددِ مثبت (یا ۰) بفرست. دوباره:")
        return
    await state.clear()
    from tenant.shared.reseller_signup import _set as _rsp_set
    await _rsp_set(row.id, "rsl_signup_min_balance", str(int(val)))
    await msg.answer(
        f"✅ حداقلِ موجودی رویِ <b>{int(val):,} تومان</b> تنظیم شد."
        if val else "✅ شرطِ موجودی برداشته شد؛ هرکسی می‌تواند نمایندگی بگیرد.",
        parse_mode="HTML")
    body, kb = await _panel(row.id)
    await msg.answer(body, reply_markup=kb, parse_mode="HTML")
