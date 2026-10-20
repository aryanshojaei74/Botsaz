"""شرط‌بندی دونفرهٔ گروهی بات‌ساز اصلی با کیف‌پول: ایکس‌او، سنگ‌کاغذ‌قیچی و تاس.

=== PATCH_PLATFORM_WAGER_V1 ===
• کاملا اختیاری: مدیر اصلی باید «تنظیمات شرط‌بندی گروه» را روشن کند (پیش‌فرض خاموش) و هر بازی را
  جداگانه می‌تواند خاموش کند. فقط در گروه متصل به محافظ گروه کار می‌کند. هر کاربر فقط وقتی وارد می‌شود که
  خودش دستور ساخت را بنویسد یا روی «قبول و پیوستن» بزند. امتیازی در کار نیست؛ فقط کیف‌پول.
• نام در ربات اصلی «شرط‌بندی» است؛ در ربات‌های مشتری «بازی» (با همان هشدار ریسک مالی و نمایش مبلغ‌ها؛ چیزی از
  بازیکن پنهان نمی‌شود). === PATCH_WAGER_UPGRADE_V1 === ایکس‌او: سازنده نماد خودش را (❌ یا ⭕) انتخاب می‌کند؛
  تاس: سازنده تعیین می‌کند عدد بزرگ‌تر برنده است یا کوچک‌تر؛ سنگ‌کاغذ‌قیچی: چندراندی، هر کس زودتر ۳ راند ببرد
  برنده است (راند مساوی تکرار می‌شود)؛ تاس و سنگ‌کاغذ‌قیچی ۵ دقیقه مهلت هر مرحله دارند.
• امنیت مالی: مبلغ هر دو نفر هنگام ساخت/پیوستن از کیف‌پول رزرو می‌شود؛ تسویه/برگشت با قفل ردیف و
  فقط یک‌بار انجام می‌شود (وضعیت pending/playing → پایانی). کارمزد، حداقل/حداکثر و مهلت انتظار همان
  تنظیمات «شرط‌بندی گروه» است. مساوی ⇒ برگشت مبلغ هر دو بدون کارمزد. مهلت هر حرکت هست؛ کسی که در مهلت
  بازی نکند می‌بازد (اگر هیچ‌کدام بازی نکرده باشند مبلغ به هر دو برمی‌گردد). یک کاربر حداکثر ۳ شرط هم‌زمان دارد.
• عدالت: ایکس‌او مهارتی است (نفر اول تصادفی)، سنگ‌کاغذ‌قیچی با انتخاب مخفی تا لحظهٔ هر دو انتخاب، و تاس را
  خود تلگرام (sendDice) می‌اندازد؛ ربات عددی نمی‌سازد.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ButtonStyle, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select

from config import PLATFORM_ADMIN_ID, Session
from models import BotUser, PlatformDiceGame, PlatformTxn, PlatformUser, PlatformWager, WalletTxn
from platform_group_money import (
    ANON_NOTICE, _bet_enabled, _digits, _edit_settings, _group_allowed, _int, _money, _name, _pset,
    _reply_target, _rial, _set, _user, _admin, admin_hint_blocked,
)
from premium_emoji import hx_button

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_wager_router = Router(name="platform_wager")

GAMES = ("xo", "rps", "dice")
GAME_FA = {"xo": "ایکس‌او", "rps": "سنگ‌کاغذ‌قیچی", "dice": "تاس"}
ACTIVE = ("pending", "playing")
MAX_ACTIVE = 3                 # شرط هم‌زمان هر کاربر (ساخته‌شده یا پذیرفته‌شده)
DICE_RECLAIM_SECS = 30         # اگر پرتابی گیر کرد (قطعی پروسه) بعد از این مدت دوباره مجاز است
# === PATCH_WAGER_UPGRADE_V1 ===
RPS_WINS = 3                   # هر کس زودتر ۳ راند ببرد برنده است (راند مساوی حساب نمی‌شود و تکرار می‌شود)
RPS_MAX_ROUNDS = 15            # سقف راندها؛ اگر کسی به ۳ برد نرسید ⇒ مساوی و برگشت مبلغ هر دو
MOVE_SIM_DEFAULT = 300         # مهلت هر مرحلهٔ بازی‌های هم‌زمان (تاس / سنگ‌کاغذ‌قیچی) = ۵ دقیقه
OPT_XO = ("X", "O")            # انتخاب سازنده در ایکس‌او: نماد خودش
OPT_DICE = ("hi", "lo")        # انتخاب سازنده در تاس: عدد بزرگ‌تر برنده است (hi) یا کوچک‌تر (lo)
SYM = {"X": "❌", "O": "⭕"}
RPS_FA = {"r": "🪨 سنگ", "p": "📄 کاغذ", "s": "✂️ قیچی"}
_BEATS = {"r": "s", "p": "r", "s": "p"}
_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))
_NOTICE = "⚠️ شرط‌بندی ریسک مالی دارد؛ فقط با پولی شرط ببند که از دست‌دادنش برایت مشکلی نیست."

# === PATCH_WAGER_UPGRADE_V1 === نام‌گذاری: ربات اصلی «bet»، ربات‌های مشتری «game». متن‌ها با {bet} / {stake} نوشته
# و هنگام نمایش جایگزین می‌شوند؛ هشدار ریسک مالی در هر دو حالت هست.
MAIN_WORDING = "bet"
_WORDS = {
    "bet": {"bet": "شرط‌بندی", "stake": "مبلغ شرط", "icon": "🎰", "notice": _NOTICE},
    "game": {"bet": "بازی", "stake": "مبلغ ورودی", "icon": "🎮",
             "notice": "⚠️ بازی ریسک مالی دارد؛ فقط با پولی بازی کن که از دست‌دادنش برایت مشکلی نیست."},
}


def words(bot_id: int = 0) -> dict:
    return _WORDS["game" if bot_id else MAIN_WORDING]


def _t(text: str, bot_id: int = 0) -> str:
    wd = words(bot_id)
    return text.replace("{bet}", wd["bet"]).replace("{stake}", wd["stake"])

_ZW = dict.fromkeys(map(ord, "‌‍‎‏"), None)
_AMT = r"([۰-۹0-9,،]+)"
_PREFIX = r"(?:(?:شرط\s*بندی|بازی)\s+)?"
_PATTERNS = (
    ("xo", re.compile(rf"^{_PREFIX}(?:xo|اکس\s*او|ایکس\s*او|دوز)\s+{_AMT}$", re.IGNORECASE)),
    ("rps", re.compile(rf"^{_PREFIX}(?:سنگ\s*کاغذ\s*قیچی|rps)\s+{_AMT}$", re.IGNORECASE)),
    ("dice", re.compile(rf"^{_PREFIX}تاس\s+{_AMT}$", re.IGNORECASE)),
)

_wlocks: dict[int, asyncio.Lock] = {}


def _wlock(wid: int) -> asyncio.Lock:
    lk = _wlocks.get(wid)
    if lk is None:
        if len(_wlocks) > 2000:                     # قفل‌های بی‌استفاده را خالی کن
            for k in [k for k, v in _wlocks.items() if not v.locked()]:
                _wlocks.pop(k, None)
        lk = _wlocks[wid] = asyncio.Lock()
    return lk


def _norm(text: str | None) -> str:
    return (text or "").translate(_ZW).replace("ي", "ی").replace("ك", "ک").strip()


def parse_command(text: str | None) -> tuple[str, int] | None:
    """«xo 10000» / «اکس او ۱۰۰۰۰» / «سنگ کاغذ قیچی ۵۰۰۰» / «تاس ۲۰۰۰» (با یا بدون پیشوند «شرط‌بندی» / «بازی»)."""
    t = _norm(text)
    for game, rx in _PATTERNS:
        m = rx.match(t)
        if m:
            amount = _int(_digits(m.group(1)))
            return (game, amount) if amount > 0 else None
    return None


# ───────────────────────── منطق خالص بازی‌ها ─────────────────────────

def xo_result(board: list[str]) -> str | None:
    """'X' | 'O' | 'draw' | None (ادامه دارد)."""
    for a, b, c in _LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if all(board) else None


def rps_result(a: str, b: str) -> int:
    """۰ مساوی، ۱ برنده a، ۲ برنده b."""
    if a == b:
        return 0
    return 1 if _BEATS[a] == b else 2


# ───────────────────────── تنظیمات ─────────────────────────

async def _cfg_get(bot_id: int, name: str, default: str) -> str:
    """=== PATCH_WAGER_TENANT_V1 === bot_id=۰: تنظیمات ربات اصلی (pset)؛ وگرنه تنظیمات همان ربات مستأجر
    (کلیدهای group_betting_* که صفحهٔ «تنظیمات بازی» مالک هم می‌نویسد)."""
    if not bot_id:
        return await _pset({"enabled": "platform_group_betting_enabled", "fee_on": "platform_group_betting_fee_enabled",
                            "fee_pct": "platform_group_betting_fee_pct", "min": "platform_group_betting_min",
                            "max": "platform_group_betting_max", "ttl": "platform_group_betting_ttl",
                            "move": "platform_wager_move_secs",
                            "move_sim": "platform_wager_move_sim_secs"}.get(name) or f"platform_wager_{name}", default)
    from tenant import get_setting
    return await get_setting(bot_id, {"enabled": "group_betting_enabled", "fee_on": "group_betting_fee_enabled",
                                      "fee_pct": "group_betting_fee_pct", "min": "group_betting_min",
                                      "max": "group_betting_max", "ttl": "group_betting_ttl",
                                      "move": "wager_move_secs",
                                      "move_sim": "wager_move_sim_secs"}.get(name) or f"wager_game_{name}", default)


def _on(v) -> bool:
    return str(v).strip().lower() in ("on", "1", "true")


async def master_enabled(bot_id: int = 0) -> bool:
    return _on(await _cfg_get(bot_id, "enabled", "off" if not bot_id else "0"))


async def load_cfg(bot_id: int = 0) -> dict:
    fee_on = _on(await _cfg_get(bot_id, "fee_on", "off"))
    pct = Decimal(str(await _cfg_get(bot_id, "fee_pct", "0") or 0)) if fee_on else Decimal("0")
    return {
        "min": max(1, _int(_digits(await _cfg_get(bot_id, "min", "1000")), 1000)),
        "max": _int(_digits(await _cfg_get(bot_id, "max", "0"))),
        "fee_pct": pct,
        "ttl": max(60, min(_int(_digits(await _cfg_get(bot_id, "ttl", "300")), 300), 86400)),
        "move": max(30, min(_int(_digits(await _cfg_get(bot_id, "move", "180")), 180), 3600)),
        "move_sim": max(30, min(_int(_digits(await _cfg_get(bot_id, "move_sim", str(MOVE_SIM_DEFAULT))),
                                   MOVE_SIM_DEFAULT), 3600)),
    }


async def game_enabled(game: str, bot_id: int = 0) -> bool:
    return _on(await _cfg_get(bot_id, game, "on"))


def _now() -> datetime:
    return datetime.utcnow()


def _st(w: PlatformWager) -> dict:
    try:
        d = json.loads(w.state or "{}")
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def _needs_setup(w: PlatformWager) -> bool:
    """=== PATCH_WAGER_UPGRADE_V1 === ایکس‌او و تاس تا وقتی سازنده انتخابش را نکرده (نماد / بزرگ‌تر یا کوچک‌تر)
    قابل پیوستن نیستند."""
    return w.status == "pending" and w.game in ("xo", "dice") and not _st(w).get("opt")


def _xo_sym(w: PlatformWager, uid: int | None) -> str:
    """نماد این بازیکن در ایکس‌او؛ بازی‌های قدیمی بدون انتخاب: سازنده X."""
    opt = _st(w).get("opt")
    mine = opt if opt in OPT_XO else "X"
    if uid == w.creator_id:
        return mine
    return "O" if mine == "X" else "X"


def _dice_mode(w: PlatformWager) -> str:
    return "lo" if _st(w).get("opt") == "lo" else "hi"


def _fee_for(amount: int, pct: Decimal) -> int:
    return int((Decimal(amount * 2) * pct / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


R = SimpleNamespace   # نتیجهٔ عملیات: w (در صورت تغییر باید پیام تازه شود)، err (هشدار به کاربر)، note (اعلان کوتاه)


def _res(w=None, err=None, note=None):
    return R(w=w, err=err, note=note)


# ───────────────────────── عملیات مالی ─────────────────────────

async def _lock_user(s, uid: int, bot_id: int = 0):
    if bot_id:
        return await s.scalar(select(BotUser).where(BotUser.bot_id == bot_id, BotUser.telegram_id == uid).with_for_update())
    return await s.scalar(select(PlatformUser).where(PlatformUser.telegram_id == uid).with_for_update())


def _add_txn(s, bot_id: int, u, amount: int, kind: str, note: str) -> None:
    """ثبت در دفتر کیف‌پول درست: ربات اصلی ⇒ PlatformTxn، ربات مستأجر ⇒ WalletTxn."""
    if bot_id:
        s.add(WalletTxn(bot_id=bot_id, user_id=u.id, amount=amount, kind=kind[:20], note=_t(note, bot_id)[:200]))
    else:
        s.add(PlatformTxn(user_telegram_id=u.telegram_id, amount=amount, kind=kind[:20], note=_t(note, bot_id)[:200]))


async def _credit(s, uid: int, amount: int, kind: str, note: str, bot_id: int = 0) -> None:
    u = await _lock_user(s, uid, bot_id)
    if u is None:                                     # نباید پیش بیاید؛ ولی پول گم نشود
        u = (BotUser(bot_id=bot_id, telegram_id=uid, full_name=str(uid), balance=0) if bot_id
             else PlatformUser(telegram_id=uid, full_name=str(uid), balance=0))
        s.add(u)
        await s.flush()
    u.balance = Decimal(str(u.balance or 0)) + Decimal(amount)
    _add_txn(s, bot_id, u, amount, kind, note)


def _finish(w: PlatformWager, status: str, reason: str, winner: int | None = None) -> None:
    w.status, w.end_reason, w.winner_id = status, reason, winner
    w.settled_at, w.expires_at, w.turn_id = _now(), None, None


async def _settle_win(s, w: PlatformWager, winner_id: int, reason: str = "win") -> None:
    await _credit(s, winner_id, _money(w.payout_amount), "wager_prize", f"جایزهٔ {{bet}} #{w.id}", w.bot_id or 0)
    if _money(w.fee_amount):
        u = await _lock_user(s, winner_id, w.bot_id or 0)
        if u is not None:
            _add_txn(s, w.bot_id or 0, u, 0, "wager_fee", f"کارمزد {{bet}} #{w.id}: {_money(w.fee_amount)}")
    _finish(w, "settled", reason, winner_id)


async def _refund_both(s, w: PlatformWager, status: str, reason: str) -> None:
    for uid in sorted({w.creator_id, w.opponent_id or 0} - {0}):
        await _credit(s, uid, _money(w.amount), "wager_refund", f"برگشت مبلغ {{bet}} #{w.id}", w.bot_id or 0)
    _finish(w, status, reason)


async def _refund_creator(s, w: PlatformWager, status: str, reason: str) -> None:
    await _credit(s, w.creator_id, _money(w.amount), "wager_refund", f"برگشت مبلغ {{bet}} #{w.id}", w.bot_id or 0)
    _finish(w, status, reason)


async def _forfeit_or_refund(s, w: PlatformWager, who_acted: dict) -> None:
    """مهلت بازی همزمان (سنگ‌کاغذ‌قیچی/تاس): کسی که نکرده می‌بازد؛ اگر هیچ‌کس نکرده برگشت هر دو."""
    a, b = str(w.creator_id) in who_acted, str(w.opponent_id) in who_acted
    if a and not b:
        await _settle_win(s, w, w.creator_id, "forfeit")
    elif b and not a:
        await _settle_win(s, w, w.opponent_id, "forfeit")
    else:
        await _refund_both(s, w, "expired", "expire")


async def _timeout_locked(s, w: PlatformWager) -> None:
    """مهلت w گذشته (ردیف قفل است): تسویه یا برگشت مبلغ."""
    if w.status == "pending":
        await _refund_creator(s, w, "expired", "expire")
        return
    if w.status != "playing":
        return
    st = _st(w)
    if w.game == "xo":
        loser = w.turn_id
        winner = w.opponent_id if loser == w.creator_id else w.creator_id
        await _settle_win(s, w, winner, "forfeit")
    elif w.game == "rps":
        await _forfeit_or_refund(s, w, st.get("picks") or {})
    else:
        await _forfeit_or_refund(s, w, st.get("rolls") or {})


async def _active_count(s, uid: int, bot_id: int = 0) -> int:
    return int(await s.scalar(select(func.count(PlatformWager.id)).where(
        PlatformWager.bot_id == bot_id, PlatformWager.status.in_(ACTIVE),
        or_(PlatformWager.creator_id == uid, PlatformWager.opponent_id == uid))) or 0)


# ───────────────────────── ساخت / پیوستن / لغو ─────────────────────────

async def create_wager(game: str, chat_id: int, creator: tuple, amount: int, target: tuple | None, cfg: dict,
                       bot_id: int = 0, opt: str | None = None):
    """creator/target = (telegram_id, full_name, username). خروجی: (wager|None, پیام خطا|None).
    opt: انتخاب از‌پیش‌تعیین‌شدهٔ سازنده (X/O برای ایکس‌او، hi/lo برای تاس)؛ None = بعد از ساخت از سازنده پرسیده می‌شود."""
    if amount < cfg["min"] or (cfg["max"] and amount > cfg["max"]):
        return None, f"❌ مبلغ باید بین {_rial(cfg['min'])} و {(_rial(cfg['max']) if cfg['max'] else 'بدون سقف')} باشد."
    fee = _fee_for(amount, cfg["fee_pct"])
    payout = amount * 2 - fee
    if payout <= 0:
        return None, "❌ کارمزد باید کمتر از مجموع مبلغ دو نفر باشد."
    cid, cname, cuser = creator
    async with Session() as s:
        u = await _lock_user(s, cid, bot_id)
        if not u or u.is_banned:
            return None, "❌ موجودی کافی نیست."
        if _money(u.balance) < amount:
            return None, f"❌ موجودی کافی نیست. نیاز: {_rial(amount)}"
        if await _active_count(s, cid, bot_id) >= MAX_ACTIVE:
            return None, _t(f"❌ هم‌زمان حداکثر {MAX_ACTIVE} {{bet}} باز می‌توانی داشته باشی.", bot_id)
        u.balance = Decimal(str(u.balance)) - Decimal(amount)
        w = PlatformWager(
            bot_id=bot_id, chat_id=chat_id, game=game, creator_id=cid, creator_name=cname or "", creator_username=cuser or None,
            target_id=target[0] if target else 0, target_name=(target[1] if target else "") or "",
            target_username=(target[2] if target else None) or None, amount=amount, fee_pct=cfg["fee_pct"], fee_amount=fee,
            payout_amount=payout, status="pending", expires_at=_now() + timedelta(seconds=cfg["ttl"]),
            state=json.dumps({"opt": opt} if opt in {"xo": OPT_XO, "dice": OPT_DICE}.get(game, ()) else {}))
        s.add(w)
        await s.flush()
        _add_txn(s, bot_id, u, -amount, "wager_reserve", f"رزرو {{bet}} #{w.id}")
        await s.commit()
        return w, None


async def join_wager(wid: int, user: tuple, move_secs: int, bot_id: int = 0, sim_secs: int | None = None):
    """sim_secs: مهلت هر مرحلهٔ بازی‌های هم‌زمان (تاس / سنگ‌کاغذ‌قیچی)؛ None = همان move_secs."""
    uid, uname, uuser = user
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        if not w or (w.bot_id or 0) != bot_id or w.status != "pending":
            return _res(err=_t("این {bet} دیگر در انتظار نیست.", bot_id))
        if w.creator_id == uid:
            return _res(err=_t("سازنده نمی‌تواند خودش به {bet} بپیوندد.", bot_id))
        if w.target_id and w.target_id != uid:
            return _res(err=_t("این {bet} برای کاربری است که پیامش ریپلای شده.", bot_id))
        if w.expires_at and w.expires_at <= _now():
            await _timeout_locked(s, w)
            await s.commit()
            return _res(w=w, err=_t("مهلت این {bet} تمام شده؛ مبلغ به سازنده برگشت.", bot_id))
        if _needs_setup(w):
            return _res(err="سازنده هنوز انتخابش را نکرده؛ چند لحظه بعد دوباره بزن.")
        opp = await _lock_user(s, uid, bot_id)
        if not opp or opp.is_banned or _money(opp.balance) < _money(w.amount):
            return _res(err=f"موجودی کافی نیست؛ نیاز به {_rial(w.amount)} داری.")
        if await _active_count(s, uid, bot_id) >= MAX_ACTIVE:
            return _res(err=_t(f"هم‌زمان حداکثر {MAX_ACTIVE} {{bet}} باز می‌توانی داشته باشی.", bot_id))
        opp.balance = Decimal(str(opp.balance)) - Decimal(str(w.amount))
        _add_txn(s, bot_id, opp, -_money(w.amount), "wager_stake", f"پیوستن به {{bet}} #{w.id}")
        w.opponent_id, w.opponent_name, w.opponent_username = uid, uname or "", uuser or None
        w.status = "playing"
        st = _st(w)                                    # انتخاب سازنده (opt) حفظ می‌شود
        if w.game == "xo":
            st["board"] = [""] * 9
            w.turn_id = secrets.choice([w.creator_id, uid])
            w.expires_at = _now() + timedelta(seconds=move_secs)
        elif w.game == "rps":
            st.update({"picks": {}, "score": {str(w.creator_id): 0, str(uid): 0}, "round": 1, "hist": []})
            w.expires_at = _now() + timedelta(seconds=sim_secs or move_secs)
        else:
            st.update({"rolls": {}, "rolling": {}})
            w.expires_at = _now() + timedelta(seconds=sim_secs or move_secs)
        w.state = json.dumps(st)
        await s.commit()
        return _res(w=w)


async def set_option(wid: int, uid: int, val: str, bot_id: int = 0):
    """سازنده انتخابش را ثبت می‌کند: ایکس‌او ⇒ X/O، تاس ⇒ hi (عدد بزرگ‌تر برنده) / lo (کوچک‌تر برنده)."""
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        if not w or (w.bot_id or 0) != bot_id or w.status != "pending":
            return _res(err=_t("این {bet} دیگر در انتظار نیست.", bot_id))
        if w.creator_id != uid:
            return _res(err="فقط سازنده می‌تواند این را انتخاب کند.")
        if w.expires_at and w.expires_at <= _now():
            await _timeout_locked(s, w)
            await s.commit()
            return _res(w=w, err=_t("مهلت این {bet} تمام شده؛ مبلغ به سازنده برگشت.", bot_id))
        if val not in {"xo": OPT_XO, "dice": OPT_DICE}.get(w.game, ()):
            return _res(err="انتخاب نامعتبر.")
        st = _st(w)
        if st.get("opt"):
            return _res(w=w, err="قبلا انتخاب کرده‌ای.")
        st["opt"] = val
        w.state = json.dumps(st)
        await s.commit()
        return _res(w=w, note="ثبت شد؛ منتظر حریف.")


async def cancel_wager(wid: int, uid: int, bot_id: int = 0):
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        if not w or (w.bot_id or 0) != bot_id or w.status != "pending":
            return _res(err=_t("این {bet} قبلا تعیین‌تکلیف شده است.", bot_id))
        if w.creator_id != uid:
            return _res(err="فقط سازنده می‌تواند لغو کند.")
        await _refund_creator(s, w, "cancelled", "cancel")
        await s.commit()
        return _res(w=w)


async def expire_one(wid: int):
    """اگر مهلت w گذشته باشد تسویه/برگشت می‌کند و w را برمی‌گرداند؛ وگرنه None."""
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        if not w or w.status not in ACTIVE or not w.expires_at or w.expires_at > _now():
            return None
        await _timeout_locked(s, w)
        await s.commit()
        return w


# ───────────────────────── حرکت‌ها ─────────────────────────

def _playing(w, uid: int, game: str, bot_id: int = 0):
    if not w or (w.bot_id or 0) != bot_id or w.status != "playing" or w.game != game:
        return _t("این {bet} در حال بازی نیست.", bot_id)
    if uid not in (w.creator_id, w.opponent_id):
        return _t("تو بازیکن این {bet} نیستی.", bot_id)
    return None


async def xo_move(wid: int, uid: int, cell: int, move_secs: int, bot_id: int = 0):
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        err = _playing(w, uid, "xo", bot_id)
        if err:
            return _res(err=err)
        if w.expires_at and w.expires_at <= _now():
            await _timeout_locked(s, w)
            await s.commit()
            return _res(w=w, err="مهلت حرکت تمام شده بود؛ بازی تمام شد.")
        if w.turn_id != uid:
            return _res(err="نوبت تو نیست.")
        st = _st(w)
        board = st.get("board") or [""] * 9
        if not 0 <= cell < 9 or board[cell]:
            return _res(err="این خانه پر است.")
        board[cell] = _xo_sym(w, uid)
        st["board"] = board
        w.state = json.dumps(st)
        out = xo_result(board)
        if out in ("X", "O"):
            await _settle_win(s, w, w.creator_id if out == _xo_sym(w, w.creator_id) else w.opponent_id, "win")
        elif out == "draw":
            await _refund_both(s, w, "draw", "draw")
        else:
            w.turn_id = w.opponent_id if uid == w.creator_id else w.creator_id
            w.expires_at = _now() + timedelta(seconds=move_secs)
        await s.commit()
        return _res(w=w)


async def rps_pick(wid: int, uid: int, choice: str, bot_id: int = 0, sim_secs: int = MOVE_SIM_DEFAULT):
    """=== PATCH_WAGER_UPGRADE_V1 === چندراندی: هر کس زودتر RPS_WINS راند ببرد برنده است؛ راند مساوی حساب نمی‌شود.
    انتخاب‌های هر راند تا لحظهٔ انتخاب هر دو مخفی است؛ بعدش در تاریخچه فاش می‌شود. مهلت هر راند sim_secs."""
    if choice not in _BEATS:
        return _res(err="انتخاب نامعتبر.")
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        err = _playing(w, uid, "rps", bot_id)
        if err:
            return _res(err=err)
        if w.expires_at and w.expires_at <= _now():
            await _timeout_locked(s, w)
            await s.commit()
            return _res(w=w, err="مهلت تمام شده بود؛ بازی تمام شد.")
        st = _st(w)
        picks = st.get("picks") or {}
        if str(uid) in picks:
            return _res(err="قبلا انتخاب کرده‌ای.")
        picks[str(uid)] = choice
        st["picks"] = picks
        if len(picks) == 2:
            a, b = picks[str(w.creator_id)], picks[str(w.opponent_id)]
            out = rps_result(a, b)
            score = st.get("score") or {}
            hist = st.get("hist") or []
            winner = w.creator_id if out == 1 else (w.opponent_id if out == 2 else 0)
            if winner:
                score[str(winner)] = int(score.get(str(winner), 0)) + 1
            hist.append({"c": a, "o": b, "w": winner})
            st.update({"score": score, "hist": hist, "picks": {}, "round": len(hist) + 1})
            w.state = json.dumps(st)
            if winner and int(score[str(winner)]) >= RPS_WINS:
                await _settle_win(s, w, winner, "win")
            elif len(hist) >= RPS_MAX_ROUNDS:
                await _refund_both(s, w, "draw", "draw")
            else:
                w.expires_at = _now() + timedelta(seconds=sim_secs)
        else:
            w.state = json.dumps(st)
        await s.commit()
        return _res(w=w, note=f"انتخابت ثبت شد: {RPS_FA[choice]}")


async def dice_claim(wid: int, uid: int, bot_id: int = 0):
    """نوبت پرتاب uid را رزرو می‌کند (جلوی پرتاب دوباره). خطا ⇒ err."""
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        err = _playing(w, uid, "dice", bot_id)
        if err:
            return _res(err=err)
        if w.expires_at and w.expires_at <= _now():
            await _timeout_locked(s, w)
            await s.commit()
            return _res(w=w, err="مهلت تمام شده بود؛ بازی تمام شد.")
        st = _st(w)
        rolls, rolling = st.get("rolls") or {}, st.get("rolling") or {}
        if str(uid) in rolls:
            return _res(err="قبلا تاس انداخته‌ای.")
        since = rolling.get(str(uid))
        if since and (_now() - datetime.fromisoformat(since)).total_seconds() < DICE_RECLAIM_SECS:
            return _res(err="تاس تو در حال پرتاب است.")
        rolling[str(uid)] = _now().isoformat()
        st["rolls"], st["rolling"] = rolls, rolling
        w.state = json.dumps(st)
        await s.commit()
        return _res(w=w)


async def dice_release(wid: int, uid: int, bot_id: int = 0) -> None:
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        if not w or (w.bot_id or 0) != bot_id or w.status != "playing":
            return
        st = _st(w)
        (st.get("rolling") or {}).pop(str(uid), None)
        w.state = json.dumps(st)
        await s.commit()


async def dice_record(wid: int, uid: int, value: int, bot_id: int = 0):
    async with Session() as s:
        w = await s.get(PlatformWager, wid, with_for_update=True)
        if not w or (w.bot_id or 0) != bot_id or w.status != "playing":
            return _res(err=_t("این {bet} دیگر در حال بازی نیست.", bot_id))
        st = _st(w)
        rolls, rolling = st.get("rolls") or {}, st.get("rolling") or {}
        if str(uid) in rolls:
            return _res(w=w)
        rolls[str(uid)] = int(value)
        rolling.pop(str(uid), None)
        st["rolls"], st["rolling"] = rolls, rolling
        w.state = json.dumps(st)
        if len(rolls) == 2:
            a, b = rolls[str(w.creator_id)], rolls[str(w.opponent_id)]
            if a == b:
                await _refund_both(s, w, "draw", "draw")
            else:
                creator_wins = (a > b) if _dice_mode(w) == "hi" else (a < b)    # انتخاب سازنده: بزرگ‌تر یا کوچک‌تر
                await _settle_win(s, w, w.creator_id if creator_wins else w.opponent_id, "win")
        await s.commit()
        return _res(w=w)


async def sweep_expired(bot: Bot | None = None, bot_id: int | None = 0, overdue_secs: int = 0) -> int:
    """شرط‌های باز که مهلتشان گذشته را تسویه/برگشت می‌دهد (حلقهٔ پس‌زمینه و قبل هر ساخت). تعداد را برمی‌گرداند.
    bot_id: ۰ = ربات اصلی، عدد ربات = همان ربات مستأجر، None = همه‌چیز؛ overdue_secs: فقط آن‌هایی که این‌قدر دیرتر شده‌اند
    (شبکهٔ اطمینان ربات اصلی برای تسویهٔ شرط‌های مستأجری که پروسهٔ خودش بالا نیست)."""
    cutoff = _now() - timedelta(seconds=overdue_secs)
    q = select(PlatformWager.id).where(
        PlatformWager.status.in_(ACTIVE), PlatformWager.expires_at.is_not(None), PlatformWager.expires_at <= cutoff)
    if bot_id is not None:
        q = q.where(PlatformWager.bot_id == bot_id)
    async with Session() as s:
        ids = (await s.scalars(q)).all()
    done = 0
    for wid in ids:
        try:
            async with _wlock(wid):
                w = await expire_one(wid)
            if w is None:
                continue
            done += 1
            if bot is not None and w.message_id:
                text, kb = render(w)
                try:
                    await bot.edit_message_text(text, chat_id=w.chat_id, message_id=w.message_id,
                                                reply_markup=kb, parse_mode="HTML")
                except Exception as e:  # noqa: BLE001 — پیام پاک شده یا بدون تغییر؛ مبلغ‌ها قبلا برگشته‌اند
                    logger.info("wager #%s: final message edit skipped: %s", wid, e)
        except Exception:  # noqa: BLE001
            logger.exception("wager sweep #%s failed", wid)
    return done


async def wager_sweep_loop(platform_bot=None, interval_sec: int = 20):
    await asyncio.sleep(20)
    while True:
        try:
            await sweep_expired(platform_bot, 0)
            # شبکهٔ اطمینان: شرط ربات مستأجر که ۱۰ دقیقه از مهلتش گذشته (پروسهٔ آن ربات کار نمی‌کند) را بی‌ویرایش پیام برگردان
            await sweep_expired(None, None, overdue_secs=600)
        except Exception:  # noqa: BLE001
            logger.exception("wager_sweep_loop error")
        await asyncio.sleep(interval_sec)


# ───────────────────────── نمایش ─────────────────────────

def _who(w: PlatformWager, uid: int | None) -> str:
    if uid == w.creator_id:
        return _name(w.creator_id, w.creator_username, w.creator_name)
    return _name(w.opponent_id or 0, w.opponent_username, w.opponent_name)


def _head(w: PlatformWager) -> str:
    wd = words(w.bot_id or 0)
    return f"{wd['icon']} <b>{wd['bet']} {GAME_FA[w.game]}</b>"


def _money_lines(w: PlatformWager) -> str:
    wd = words(w.bot_id or 0)
    return (f"💰 {wd['stake']} هر نفر: <b>{_rial(w.amount)}</b>\n"
            f"💸 کارمزد: <b>{_rial(w.fee_amount)}</b> ({float(w.fee_pct or 0):g}٪)\n"
            f"🏆 جایزهٔ برنده: <b>{_rial(w.payout_amount)}</b>")


def _px(w: PlatformWager) -> str:
    """پیشوند callback_data: pgw = ربات اصلی، twg = ربات مستأجر (هندلرهای جدا)."""
    return "twg" if w.bot_id else "pgw"


def _btn(w: PlatformWager, text: str, **kw):
    """ربات اصلی: دکمهٔ شکلک‌دار (hx_button)؛ مستأجر: دکمهٔ ساده (آیدی ایموجی پرمیوم بات‌ساز برای ربات‌های مشتری معنا ندارد)."""
    return InlineKeyboardButton(text=text, **kw) if w.bot_id else hx_button(text, **kw)


def _board_kb(w: PlatformWager, live: bool) -> InlineKeyboardMarkup:
    board = _st(w).get("board") or [""] * 9
    rows = []
    for r in range(3):
        rows.append([InlineKeyboardButton(text=({"": "⬜", **SYM})[board[r * 3 + c]],
                                          callback_data=(f"{_px(w)}:x:{w.id}:{r * 3 + c}" if live else f"{_px(w)}:noop"))
                     for c in range(3)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _mins_left(w: PlatformWager) -> str:
    """«تا N دقیقه» برای مهلت مرحلهٔ جاری (لحظهٔ ساخت پیام)."""
    if not w.expires_at:
        return ""
    secs = max(0, int((w.expires_at - _now()).total_seconds()))
    return f"{max(1, -(-secs // 60))} دقیقه"


def _rule_line(w: PlatformWager) -> str:
    """قانونی که سازنده انتخاب کرده / قانون ثابت بازی؛ در همهٔ مرحله‌ها نشان داده می‌شود."""
    st = _st(w)
    if w.game == "xo" and st.get("opt") in OPT_XO:
        mine = st["opt"]
        return f"🎯 نماد سازنده: {SYM[mine]} | نماد حریف: {SYM['O' if mine == 'X' else 'X']}"
    if w.game == "dice" and st.get("opt") in OPT_DICE:
        return "📜 قانون: " + ("عدد <b>بزرگ‌تر</b> برنده است." if st["opt"] == "hi" else "عدد <b>کوچک‌تر</b> برنده است.")
    if w.game == "rps":
        return f"📜 قانون: هر کس زودتر <b>{RPS_WINS} راند</b> ببرد برنده است (راند مساوی تکرار می‌شود)."
    return ""


def _rps_hist_lines(w: PlatformWager, last: int | None = None) -> list[str]:
    hist = _st(w).get("hist") or []
    rows = hist if last is None else hist[-last:]
    base = len(hist) - len(rows)
    out = []
    for i, h in enumerate(rows, start=base + 1):
        res = f"{_who(w, h['w'])} برد" if h.get("w") else "مساوی"
        out.append(f"راند {i}: {_who(w, w.creator_id)} {RPS_FA[h['c']].split()[0]} ⚔️ "
                   f"{RPS_FA[h['o']].split()[0]} {_who(w, w.opponent_id)} ← {res}")
    return out


def _final_text(w: PlatformWager) -> str:
    st = _st(w)
    wd = words(w.bot_id or 0)
    lines = [_head(w), ""]
    if w.status == "cancelled":
        return "\n".join(lines + ["❌ لغو شد و مبلغ رزروشده به سازنده برگشت."])
    if w.status == "expired" and not w.opponent_id:
        return "\n".join(lines + ["⏰ حریفی پیدا نشد؛ مبلغ سازنده برگشت."])
    lines.append(f"{_who(w, w.creator_id)}  ⚔️  {_who(w, w.opponent_id)}")
    rule = _rule_line(w)
    if rule:
        lines.append(rule)
    if w.game == "rps":
        score = st.get("score") or {}
        lines.append(f"📊 امتیاز: {_who(w, w.creator_id)} <b>{int(score.get(str(w.creator_id), 0))}</b> — "
                     f"<b>{int(score.get(str(w.opponent_id), 0))}</b> {_who(w, w.opponent_id)}")
        lines += _rps_hist_lines(w)
        picks = st.get("picks") or {}
        for uid in (w.creator_id, w.opponent_id):          # راند نیمه‌تمامی که با مهلت بسته شد
            if str(uid) in picks and w.end_reason in ("forfeit", "expire"):
                lines.append(f"{_who(w, uid)}: انتخاب راند آخر ثبت شده بود")
    elif w.game == "dice":
        rolls = st.get("rolls") or {}
        for uid in (w.creator_id, w.opponent_id):
            if str(uid) in rolls:
                lines.append(f"{_who(w, uid)}: 🎲 {rolls[str(uid)]}")
    elif w.game == "xo":
        lines.append(f"{SYM[_xo_sym(w, w.creator_id)]} {_who(w, w.creator_id)}   {SYM[_xo_sym(w, w.opponent_id)]} {_who(w, w.opponent_id)}")
    lines.append("")
    if w.status == "settled":
        why = "\n⏱ حریف در مهلت بازی نکرد." if w.end_reason == "forfeit" else ""
        lines += [f"🏁 برنده: <b>{_who(w, w.winner_id)}</b>{why}",
                  f"🎁 دریافتی برنده: <b>{_rial(w.payout_amount)}</b> (کارمزد {_rial(w.fee_amount)})"]
    elif w.status == "draw":
        if w.game == "rps" and w.end_reason == "draw" and len(st.get("hist") or []) >= RPS_MAX_ROUNDS:
            lines.append(f"🤝 بعد از {RPS_MAX_ROUNDS} راند کسی به {RPS_WINS} برد نرسید؛ {wd['stake']} به هر دو نفر برگشت (بدون کارمزد).")
        else:
            lines.append(f"🤝 مساوی شد؛ {wd['stake']} به هر دو نفر برگشت (بدون کارمزد).")
    else:
        lines.append(f"⏰ مهلت تمام شد و کسی بازی نکرد؛ {wd['stake']} به هر دو نفر برگشت.")
    return "\n".join(lines)


def _opt_keyboard(w: PlatformWager) -> list[list[InlineKeyboardButton]]:
    """دکمه‌های انتخاب سازنده (فقط سازنده می‌تواند بزند)."""
    px = _px(w)
    if w.game == "xo":
        return [[_btn(w, "❌ ضربدر (X)", callback_data=f"{px}:o:{w.id}:X", style=ButtonStyle.PRIMARY),
                 _btn(w, "⭕ دایره (O)", callback_data=f"{px}:o:{w.id}:O", style=ButtonStyle.PRIMARY)]]
    return [[_btn(w, "🔼 عدد بزرگ‌تر برنده", callback_data=f"{px}:o:{w.id}:hi", style=ButtonStyle.PRIMARY)],
            [_btn(w, "🔽 عدد کوچک‌تر برنده", callback_data=f"{px}:o:{w.id}:lo", style=ButtonStyle.PRIMARY)]]


def _rps_playing_text(w: PlatformWager, head: str) -> str:
    st = _st(w)
    score, picks = st.get("score") or {}, st.get("picks") or {}
    rnd = int(st.get("round") or 1)
    lines = [head.rstrip(), _rule_line(w), ""]
    for uid in (w.creator_id, w.opponent_id):
        mark = "✅ انتخاب کرد" if str(uid) in picks else "⏳ منتظر انتخاب"
        lines.append(f"👤 {_who(w, uid)} — امتیاز: <b>{int(score.get(str(uid), 0))}</b>\n      {mark}")
    hist = _rps_hist_lines(w, last=4)
    if hist:
        lines += ["", *hist]
    lines += ["", f"🔔 راند {rnd}: هر دو نفر انتخاب کنید؛ انتخاب‌ها تا پایان راند مخفی است.",
              f"⏱ مهلت این راند: {_mins_left(w)}؛ نکردن = باخت کل بازی."]
    return "\n".join(lines)


def render(w: PlatformWager) -> tuple[str, InlineKeyboardMarkup]:
    st = _st(w)
    wd = words(w.bot_id or 0)
    if w.status == "pending":
        target = (f"🎯 حریف تعیین‌شده: {_name(w.target_id, w.target_username, w.target_name)}\n"
                  if w.target_id else "👥 اولین نفری که «قبول و پیوستن» بزند حریف است.\n")
        cancel = _btn(w, "❌ لغو", callback_data=f"{_px(w)}:cancel:{w.id}", style=ButtonStyle.DANGER)
        if _needs_setup(w):
            ask = ("سازنده، با کدام نماد بازی می‌کنی؟ حریف نماد دیگر را می‌گیرد."
                   if w.game == "xo" else "سازنده، کدام عدد برنده است؟ بزرگ‌تر یا کوچک‌تر؟")
            text = (f"{_head(w)}\n\n👤 سازنده: {_who(w, w.creator_id)}\n{target}{_money_lines(w)}\n\n"
                    f"🎛 <b>{ask}</b>\n(فقط سازنده می‌تواند انتخاب کند؛ بعد از انتخاب، حریف می‌تواند بپیوندد.)")
            return text, InlineKeyboardMarkup(inline_keyboard=[*_opt_keyboard(w), [cancel]])
        rule = _rule_line(w)
        text = (f"{_head(w)}\n\n👤 سازنده: {_who(w, w.creator_id)}\n{target}"
                + (f"{rule}\n" if rule else "")
                + f"{_money_lines(w)}\n\n⏳ منتظر حریف…\n\n{wd['notice']}")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            _btn(w, "✅ قبول و پیوستن", callback_data=f"{_px(w)}:join:{w.id}", style=ButtonStyle.SUCCESS), cancel]])
        return text, kb
    if w.status != "playing":
        kb = _board_kb(w, False) if w.game == "xo" and w.opponent_id else InlineKeyboardMarkup(inline_keyboard=[])
        return _final_text(w), kb
    head = f"{_head(w)}\n\n{_money_lines(w)}\n\n"
    if w.game == "xo":
        text = (head + f"{SYM[_xo_sym(w, w.creator_id)]} {_who(w, w.creator_id)}   "
                f"{SYM[_xo_sym(w, w.opponent_id)]} {_who(w, w.opponent_id)}\n"
                f"🎯 نوبت: <b>{_who(w, w.turn_id)}</b>\n⏱ هر حرکت مهلت محدود دارد ({_mins_left(w)})؛ نکردن = باخت.")
        return text, _board_kb(w, True)
    if w.game == "rps":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=RPS_FA[k], callback_data=f"{_px(w)}:r:{w.id}:{k}") for k in ("r", "p", "s")]])
        return _rps_playing_text(w, head), kb
    rolls = st.get("rolls") or {}
    mark = lambda uid: f"🎲 {rolls[str(uid)]}" if str(uid) in rolls else "⏳ منتظر پرتاب"  # noqa: E731
    text = (head + f"{_rule_line(w)}\n\n"
            f"👤 {_who(w, w.creator_id)}: {mark(w.creator_id)}\n👤 {_who(w, w.opponent_id)}: {mark(w.opponent_id)}\n\n"
            "هر نفر یک‌بار «پرتاب تاس» را بزند؛ تاس را خود تلگرام می‌اندازد.\n"
            f"⏱ هر نفر تا {_mins_left(w)} فرصت دارد؛ کسی که نیندازد می‌بازد.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎲 پرتاب تاس", callback_data=f"{_px(w)}:d:{w.id}")]])
    return text, kb


def _mins(secs: int) -> str:
    return f"{max(1, -(-int(secs) // 60))} دقیقه"


def help_text(enabled: dict[str, bool], bot_id: int = 0, cfg: dict | None = None) -> str:
    """=== PATCH_WAGER_HELP_V1 === راهنمای کامل بازی‌ها؛ وقتی کسی فقط «بازی» / «شرطبندی» را بنویسد نشان داده می‌شود.
    bot_id=۰ ربات اصلی (نام «شرط‌بندی»)، وگرنه ربات مشتری (نام «بازی»). cfg (load_cfg): حداقل/حداکثر، کارمزد و مهلت‌ها."""
    wd = words(bot_id)
    cfg = cfg or {}
    first = next((g for g in GAMES if enabled.get(g)), None)
    example = {"xo": "xo ۱۰۰۰۰", "rps": "سنگ کاغذ قیچی ۱۰۰۰۰", "dice": "تاس ۱۰۰۰۰"}.get(first, "بازی ۱۰۰۰۰")
    lines = [f"{wd['icon']} <b>{wd['bet']} با کیف‌پول</b>",
             "(اختیاری؛ فقط برای کسی که خودش بخواهد)", "",
             "<b>مراحل کار</b>",
             f"1️⃣ دستور بازی را با مبلغ بنویس؛ مثلا «{example}». مبلغ به تومان و برای هر نفر است.",
             "2️⃣ همان لحظه مبلغ تو از کیف‌پول کنار گذاشته می‌شود و یک پیام با دکمهٔ «✅ قبول و پیوستن» می‌آید.",
             "3️⃣ اولین نفری که قبول کند حریف تو می‌شود و او هم همان مبلغ را می‌دهد. برای بازی با یک نفر مشخص، "
             "دستور را روی پیام همان نفر ریپلای کن.",
             "4️⃣ برنده کل مبلغ دو نفر را می‌گیرد" + (" (منهای کارمزد)" if float(cfg.get("fee_pct") or 0) > 0 else "")
             + ". اگر مساوی شود یا کسی نپیوندد، پول کامل برمی‌گردد. تا قبل از پیوستن حریف، سازنده می‌تواند با «❌ لغو» "
             "بازی را لغو کند.", "", "<b>بازی‌ها</b>"]
    if enabled.get("xo"):
        lines += ["❌⭕ <b>ایکس‌او</b>: «xo ۱۰۰۰۰» یا «ایکس‌او ۱۰۰۰۰»",
                  "اول سازنده نماد خودش (❌ یا ⭕) را انتخاب می‌کند و حریف نماد دیگر را می‌گیرد. نوبتی روی خانه‌ها "
                  "بزنید؛ هر کس سه‌تا را در یک ردیف بسازد برنده است. پر شدن همهٔ خانه‌ها بدون برنده = مساوی.", ""]
    if enabled.get("rps"):
        lines += ["✊ <b>سنگ‌کاغذ‌قیچی</b>: «سنگ کاغذ قیچی ۱۰۰۰۰»",
                  f"هر کس زودتر {RPS_WINS} راند ببرد برنده است. انتخاب هر راند مخفی است و وقتی هر دو نفر انتخاب کردند "
                  "نشان داده می‌شود؛ راند مساوی تکرار می‌شود. امتیاز هر نفر زیر پیام نوشته می‌شود.", ""]
    if enabled.get("dice"):
        lines += ["🎲 <b>تاس</b>: «تاس ۱۰۰۰۰»",
                  "اول سازنده انتخاب می‌کند عدد بزرگ‌تر برنده است یا عدد کوچک‌تر. بعد هر نفر یک‌بار دکمهٔ «🎲 پرتاب تاس» "
                  "را می‌زند (تاس را خود تلگرام می‌اندازد). عدد مساوی = برگشت پول.", ""]
    lines += ["🪙 <b>شیر یا خط</b>: «بازی ۱۰۰۰۰» یا «شرطبندی ۱۰۰۰۰»",
              "نتیجه کاملا شانسی است و با قرعه معلوم می‌شود؛ برنده کل مبلغ را می‌گیرد.", "",
              "<b>قوانین مهم</b>"]
    rules = []
    if cfg.get("move"):
        parts = []
        if enabled.get("xo"):
            parts.append(f"ایکس‌او: هر حرکت {_mins(cfg['move'])}")
        sim = [GAME_FA[g] for g in ("rps", "dice") if enabled.get(g)]
        if sim:
            parts.append(" و ".join(sim) + f": هر مرحله {_mins(cfg.get('move_sim') or cfg['move'])}")
        if parts:
            rules.append("⏱ مهلت: " + "؛ ".join(parts) + ". کسی که در مهلت بازی نکند می‌بازد "
                         "(اگر هیچ‌کدام بازی نکنند پول هر دو برمی‌گردد).")
    if cfg.get("ttl"):
        rules.append(f"⏳ اگر تا {_mins(cfg['ttl'])} کسی نپیوندد، بازی خودکار لغو و پولت برمی‌گردد.")
    if cfg.get("min"):
        rules.append(f"💰 مبلغ هر نفر: از {_rial(cfg['min'])} تا {_rial(cfg['max'])}." if cfg.get("max")
                     else f"💰 مبلغ هر نفر: حداقل {_rial(cfg['min'])} (بدون سقف).")
    if float(cfg.get("fee_pct") or 0) > 0:
        rules.append(f"💸 کارمزد: {float(cfg['fee_pct']):g}٪ از جایزهٔ برنده کم می‌شود (در مساوی و لغو کارمزد نیست).")
    if first:
        rules.append(f"🔢 هر نفر هم‌زمان حداکثر {MAX_ACTIVE} بازی باز دارد.")
    rules.append("🕵️ با «ادمین ناشناس» یا هویت کانال نمی‌شود بازی ساخت؛ حالت ناشناس را خاموش کن.")
    lines += ["• " + r for r in rules]
    lines += ["", wd["notice"]]
    return "\n".join(lines)


async def _show(cb_or_msg, w: PlatformWager) -> None:
    text, kb = render(w)
    try:
        await cb_or_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("wager edit failed: %s", e)


# ───────────────────────── هندلرها ─────────────────────────

def _parse_filter(m: Message) -> bool:
    return parse_command(m.text) is not None


@platform_wager_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_parse_filter))
async def wager_create(msg: Message):
    # === PATCH_WAGER_DIAG_V1 === هیچ مسیری نباید بی‌صدا تمام شود: دلیل را به مدیر می‌گوییم و خطاها را لاگ می‌کنیم
    if not msg.from_user:
        raise SkipHandler
    if not await _group_allowed(msg.chat.id):
        logger.info("wager: chat %s is not the guarded group; ignoring %r", msg.chat.id, (msg.text or "")[:30])
        await admin_hint_blocked(msg)
        raise SkipHandler                 # گروه‌های دیگر: پیام را به بقیهٔ روترها بده
    if msg.sender_chat:
        await msg.reply(ANON_NOTICE)
        return
    game, amount = parse_command(msg.text)
    try:
        await _create_and_post(msg, game, amount)
    except SkipHandler:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("wager: create failed (chat=%s user=%s game=%s amount=%s)", msg.chat.id, msg.from_user.id, game, amount)
        extra = f"\n({type(e).__name__}: {str(e)[:150]})" if msg.from_user.id == PLATFORM_ADMIN_ID else ""
        try:
            await msg.reply("❌ ساخت شرط‌بندی با خطا روبه‌رو شد؛ چند لحظه بعد دوباره امتحان کن." + extra)
        except Exception:  # noqa: BLE001
            logger.exception("wager: could not even report the failure")


async def _create_and_post(msg: Message, game: str, amount: int) -> None:
    if not await _bet_enabled():
        await msg.reply("❌ شرط‌بندی در بات‌ساز اصلی فعال نیست.")
        return
    if not await game_enabled(game):
        await msg.reply(f"❌ شرط‌بندی {GAME_FA[game]} فعال نیست.")
        return
    target = _reply_target(msg)
    if target and (target.is_bot or target.id == msg.from_user.id):
        await msg.reply("❌ سازنده نباید خودش یا یک ربات را هدف بگیرد.")
        return
    await sweep_expired(msg.bot)
    cfg = await load_cfg()
    await _user(msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or "")
    w, err = await create_wager(
        game, msg.chat.id, (msg.from_user.id, msg.from_user.full_name or "", msg.from_user.username or ""),
        amount, (target.id, target.full_name or "", target.username or "") if target else None, cfg)
    if err:
        await msg.reply(err)
        return
    text, kb = render(w)
    try:
        sent = await msg.reply(text, reply_markup=kb, parse_mode="HTML")
    except Exception:  # noqa: BLE001 — پیام نرفت: مبلغ را برگردان
        logger.exception("wager #%s: sending the message failed; refunding", w.id)
        async with _wlock(w.id):
            await cancel_wager(w.id, w.creator_id)
        raise
    async with Session() as s:
        row = await s.get(PlatformWager, w.id)
        row.message_id = sent.message_id
        await s.commit()


async def _cb_guard(cb: CallbackQuery) -> bool:
    if not cb.message or not await _group_allowed(cb.message.chat.id):
        await cb.answer("قابلیت فعال نیست.", show_alert=True)
        return False
    return True


def _wid(cb: CallbackQuery, idx: int = 2) -> int:
    parts = (cb.data or "").split(":")
    return _int(parts[idx]) if len(parts) > idx else 0


@platform_wager_router.callback_query(F.data == "pgw:noop")
async def wager_noop(cb: CallbackQuery):
    await cb.answer()


@platform_wager_router.callback_query(F.data.startswith("pgw:join:"))
async def wager_join(cb: CallbackQuery):
    if not await _cb_guard(cb):
        return
    if not await _bet_enabled():
        await cb.answer("شرط‌بندی فعال نیست.", show_alert=True)
        return
    wid = _wid(cb)
    cfg = await load_cfg()
    await _user(cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or "")
    async with _wlock(wid):
        row = await _peek(wid)
        if row is not None and not await game_enabled(row.game):
            await cb.answer("این بازی فعال نیست.", show_alert=True)
            return
        r = await join_wager(wid, (cb.from_user.id, cb.from_user.full_name or "", cb.from_user.username or ""), cfg["move"],
                             sim_secs=cfg["move_sim"])
    if r.w is not None:
        await _show(cb.message, r.w)
    await cb.answer(r.err or "به شرط‌بندی پیوستی؛ موفق باشی!", show_alert=bool(r.err))


async def _peek(wid: int):
    async with Session() as s:
        return await s.get(PlatformWager, wid)


@platform_wager_router.callback_query(F.data.startswith("pgw:o:"))
async def wager_option(cb: CallbackQuery):
    """=== PATCH_WAGER_UPGRADE_V1 === انتخاب سازنده: نماد ایکس‌او / برنده‌بودن عدد بزرگ‌تر یا کوچک‌تر در تاس."""
    if not await _cb_guard(cb):
        return
    parts = (cb.data or "").split(":")
    wid, val = _wid(cb), (parts[3] if len(parts) > 3 else "")
    async with _wlock(wid):
        r = await set_option(wid, cb.from_user.id, val)
    if r.w is not None and not r.err:
        await _show(cb.message, r.w)
    await cb.answer(r.err or r.note or "", show_alert=bool(r.err))


@platform_wager_router.callback_query(F.data.startswith("pgw:cancel:"))
async def wager_cancel(cb: CallbackQuery):
    wid = _wid(cb)
    async with _wlock(wid):
        r = await cancel_wager(wid, cb.from_user.id)
    if r.w is not None:
        await _show(cb.message, r.w)
    await cb.answer(r.err or "لغو شد", show_alert=bool(r.err))


@platform_wager_router.callback_query(F.data.startswith("pgw:x:"))
async def wager_xo(cb: CallbackQuery):
    parts = (cb.data or "").split(":")
    wid, cell = _wid(cb), (int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else -1)
    cfg = await load_cfg()
    async with _wlock(wid):
        r = await xo_move(wid, cb.from_user.id, cell, cfg["move"])
    if r.w is not None:
        await _show(cb.message, r.w)
    await cb.answer(r.err or "", show_alert=bool(r.err))


@platform_wager_router.callback_query(F.data.startswith("pgw:r:"))
async def wager_rps(cb: CallbackQuery):
    parts = (cb.data or "").split(":")
    wid, choice = _wid(cb), (parts[3] if len(parts) > 3 else "")
    cfg = await load_cfg()
    async with _wlock(wid):
        r = await rps_pick(wid, cb.from_user.id, choice, sim_secs=cfg["move_sim"])
    if r.w is not None:
        await _show(cb.message, r.w)
    await cb.answer(r.err or r.note or "", show_alert=bool(r.err))


@platform_wager_router.callback_query(F.data.startswith("pgw:d:"))
async def wager_dice(cb: CallbackQuery, bot: Bot):
    wid, uid = _wid(cb), cb.from_user.id
    async with _wlock(wid):
        r = await dice_claim(wid, uid)
    if r.err:
        if r.w is not None:
            await _show(cb.message, r.w)
        await cb.answer(r.err, show_alert=True)
        return
    await cb.answer("🎲")
    try:
        sent = await bot.send_dice(cb.message.chat.id, emoji="🎲")
        value = int(sent.dice.value)
    except Exception:  # noqa: BLE001 — پرتاب نشد: دوباره اجازه بده
        logger.exception("wager #%s: send_dice failed", wid)
        await dice_release(wid, uid)
        await cb.message.answer("❌ پرتاب تاس انجام نشد؛ دوباره امتحان کن.")
        return
    async with _wlock(wid):
        r = await dice_record(wid, uid, value)
    if r.w is not None:
        if r.w.status != "playing":
            await asyncio.sleep(4)            # بگذار انیمیشن تاس تمام شود
        await _show(cb.message, r.w)


# ───────────────────────── تنظیمات مدیر ─────────────────────────

@platform_wager_router.callback_query(F.data.startswith("pgwset:"))
async def wager_game_toggle(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    game = (cb.data or "").split(":", 1)[1]
    if game not in GAMES:
        await cb.answer()
        return
    key = f"platform_wager_{game}"
    await _set(key, "off" if (await _pset(key, "on")) == "on" else "on")
    await _edit_settings(cb, "bet")


# ───────────────────────── صفحهٔ مشترک «بازی‌ها و شرط‌بندی» ─────────────────────────

async def hub_screen() -> tuple[str, InlineKeyboardMarkup]:
    """هر دو بخش کنار هم: شرط‌بندی بین بازیکن‌ها + بازی‌های خود تلگرام (بازی کن پول بگیر)."""
    master = await _bet_enabled()
    flags = {g: await game_enabled(g) for g in GAMES}
    async with Session() as s:
        games = (await s.scalars(select(PlatformDiceGame).order_by(PlatformDiceGame.id))).all()
    active = [g for g in games if g.active]
    bet_line = ("✅ روشن" if master else "❌ خاموش") + " | " + "، ".join(
        f"{'✅' if flags[g] else '❌'} {GAME_FA[g]}" for g in GAMES) + " | شیر یا خط"
    text = (
        "🎮 <b>بازی‌ها و شرط‌بندی</b>\n\n"
        "🎰 <b>شرط‌بندی</b> (بازیکن در برابر بازیکن، با کیف پول؛ در گروه محافظ‌شده)\n"
        f"{bet_line}\n\n"
        "🎲 <b>بازی کن پول بگیر</b> (بازی‌های خود تلگرام: 🎲 🎯 🏀 ⚽ 🎳 🎰؛ جایزه از طرف شما به بازیکن می‌رسد "
        "و نتیجه را سرور تلگرام تعیین می‌کند)\n"
        f"{len(active)} بازی فعال از {len(games)} بازی ثبت‌شده")
    # === PATCH_WAGER_ADDON_V1 === فروش به ربات‌های مشتری (قیمت / روشن‌خاموش)
    from platform_wager_sale import hub_sale_section
    sale_text, sale_rows = await hub_sale_section()
    text += sale_text
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [hx_button("🎰 تنظیمات شرط‌بندی گروه", callback_data="gg:bet_settings@hub", style=ButtonStyle.PRIMARY)],
        [hx_button("🎲 بازی کن پول بگیر (بازی‌های تلگرام)", callback_data="padg:menu@hub", style=ButtonStyle.PRIMARY)],
        *sale_rows,
        [hx_button("⬅️ بازگشت به پنل", callback_data="adm:back", style=ButtonStyle.DANGER)],
    ])
    return text, kb


@platform_wager_router.callback_query(F.data == "pgw:hub")
async def wager_hub(cb: CallbackQuery):
    if not _admin(cb):
        await cb.answer("فقط مدیر اصلی", show_alert=True)
        return
    text, kb = await hub_screen()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning("wager hub edit failed: %s", e)
    await cb.answer()
