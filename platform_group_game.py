"""platform_group_game.py — «🎯 حدس عدد» (بازیِ گروهیِ رایگان) فقط در گروهِ متصل به محافظِ بات‌ساز اصلی.

=== PATCH_GROUP_GUESS_GAME_V1 ===
• شروع: هر عضو بنویسد «حدس عدد» (یا /guess). ربات عددی بین ۱ تا ۱۰۰ انتخاب می‌کند (secrets)؛ اعضا فقط عدد می‌نویسند و
  ربات «⬆️ بالاتر» / «⬇️ پایین‌تر» می‌گوید. اولین نفرِ درست برنده است؛ ۵ دقیقه مهلت.
• امتیاز: برنده «شیرینی 🍬» می‌گیرد (۵ تا؛ اگر زیرِ ۳۰ ثانیه حدس بزند +۳). پول/کیف‌پول در کار نیست.
• «جدول» (یا /top): ۱۰ نفرِ برتر. روشن/خاموش: مدیر از پنلِ «🎃 تم هالوین» (کلید pgg_on، پیش‌فرض روشن).
• فقط در گروهِ محافظ؛ هر گروهِ دیگر دست‌نخورده (پیام به بقیهٔ هندلرها می‌رسد).

=== PATCH_GROUP_RACE_GAMES_V1 === دو بازیِ «مسابقهٔ سرعت» هم کنارش:
  ⚡️ «سرعت» (/speed): ربات یک کلمهٔ هالوینی می‌فرستد؛ اولین نفری که دقیقاً همان را بنویسد برنده است (۳ 🍬).
  🧮 «ریاضی» (/math): یک محاسبهٔ ساده؛ اولین جوابِ درست برنده است (۳ 🍬، زیرِ ۱۰ ثانیه ۵ 🍬).
  مهلتِ هر مسابقه ۶۰ ثانیه؛ پاسخِ غلط بی‌صدا نادیده گرفته می‌شود (گروه شلوغ نشود). در هر لحظه فقط یک بازی (از هر نوع).
"""
from __future__ import annotations

import asyncio
import html as _html
import re
import secrets
import time

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import Message
from sqlalchemy import select

from config import Session
from models import Setting
from platform_group_money import _pset, blocked_reasons

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

platform_group_game_router = Router(name="platform_group_game")

LOW, HIGH, TIMEOUT = 1, 100, 300
START_COOLDOWN, GUESS_GAP = 45, 2.5
BASE_POINTS, FAST_BONUS, FAST_SECS = 4, 2, 30      # PATCH_CANDY_REWARD_V1: کمی سخت‌تر (جایزهٔ ۱۰۰ شیرینی)
KEY_ON = "pgg_on"

_GAMES: dict[int, dict] = {}              # chat_id → {"n", "t0", "guesses", "mid"}
_RACES: dict[int, dict] = {}              # chat_id → {"kind", "answer", "t0"}
RACE_TIMEOUT, RACE_POINTS, RACE_FAST_BONUS, RACE_FAST_SECS = 60, 2, 1, 10
# PATCH_SPEED_WORDS_V1: فقط ۱۸ کلمه بود و توی گروه مدام تکراری می‌آمد. حالا فهرستِ بزرگِ کلمه‌هایِ ساده و قابلِ تایپ،
# و برایِ هر گروه «کلماتِ اخیر» تکرار نمی‌شوند (pick_word).
_HALLOWEEN = ["کدوتقلبی", "جادوگر", "خون‌آشام", "اسکلت", "شبح", "عنکبوت", "خفاش", "قبرستان", "طلسم", "گربه‌سیاه",
              "قلعهٔ‌متروک", "جاروپرنده", "مومیایی", "دیوانه", "شیرینی‌هالوین", "TrickOrTreat", "HappyHalloween", "Boo"]
_COMMON = """
آسمان ستاره ماهتاب خورشید باران برف طوفان رعد آذرخش مه ابر باد سایه نسیم دریا اقیانوس رودخانه چشمه آبشار دریاچه
جنگل کوهستان دشت بیابان صحرا جزیره ساحل صخره غار دره تپه مزرعه باغ گلستان چمن بوستان نخل چنار سرو صنوبر بلوط
گل‌سرخ بنفشه نرگس لاله یاس شقایق ریحان نعنا زعفران انگور سیب انار پرتقال هلو گیلاس آلبالو توت انجیر خرما
هندوانه خربزه گلابی زردآلو آلو نارگیل موز آناناس لیمو نارنج گوجه خیار هویج کاهو کلم بادمجان کدو سیبزمینی
پیاز سیر فلفل قارچ لوبیا عدس نخود برنج نان پنیر کره عسل شیر ماست دوغ چای قهوه شربت بستنی شکلات کیک
شیرینی نبات گز سوهان باقلوا حلوا زولبیا کباب جوجه قورمه فسنجان آش سوپ املت پیتزا ساندویچ سالاد
شیر ببر پلنگ خرس روباه گرگ آهو غزال اسب الاغ شتر گاو گوسفند بز مرغ خروس اردک غاز کبوتر عقاب شاهین
جغد طوطی بلبل قناری پروانه زنبور مورچه مگس پشه ملخ لاک‌پشت قورباغه ماهی دلفین نهنگ کوسه خرچنگ اختاپوس
میمون فیل زرافه کرگدن اسب‌آبی کانگورو پاندا پنگوئن خرگوش سنجاب موش گربه سگ
کتاب قلم مداد دفتر کیف کلاس معلم مدرسه دانشگاه امتحان نمره تخته نقاشی موسیقی آواز ترانه شعر داستان
فیلم سینما تئاتر بازیگر کارگردان عکس دوربین تلفن رایانه صفحه‌کلید ماوس مانیتور اینترنت پیام ایمیل
ماشین اتوبوس قطار هواپیما کشتی دوچرخه موتور تاکسی خیابان کوچه میدان پل تونل فرودگاه ایستگاه بندر
خانه آشپزخانه اتاق پنجره دیوار سقف حیاط پله آینه فرش مبل میز صندلی چراغ ساعت تقویم کلید قفل
دوست رفیق همسایه مهمان خانواده برادر خواهر مادر پدر پدربزرگ دختر پسر کودک نوجوان جوان
شادی امید آرزو آرامش دوستی مهربانی صداقت شجاعت پشتکار موفقیت پیروزی قهرمان افتخار
سفر ماجراجویی گنج نقشه قطب‌نما دوربین‌دوچشمی چادر آتش هیزم شمع فانوس مشعل
شطرنج فوتبال والیبال بسکتبال تنیس شنا دویدن کشتی ورزش مربی مسابقه جام مدال
الماس طلا نقره مروارید یاقوت زمرد فیروزه عقیق انگشتر گردنبند دستبند تاج
""".split()
_EXTRA = """
کهکشان سیاره ماهواره فضانورد موشک بالگرد قایق‌موتوری زیردریایی خیابان پارکینگ کارخانه بیمارستان داروخانه کتابفروشی نانوایی
سوپرمارکت رستوران آرایشگاه آموزشگاه دبستان دبیرستان آزمایشگاه کتابخانه موزه تماشاخانه ورزشگاه استخر باشگاه سرسره
گنجشک مرغابی فلامینگو شترمرغ طاووس سنجاقک کفشدوزک هزارپا سوسمار مارمولک تمساح دایناسور اژدها سمندر راکون گورکن شغال
کفتار یوزپلنگ گوزن عقرب مرجان لاک‌پشت ماهیگیر دستمال تلویزیون یخچال جاروبرقی ظرفشویی مایکروویو بخاری کولر پنکه
گوشی تبلت لپ‌تاپ کیبورد هدفون بلندگو میکروفون پرینتر اسکنر شارژر باتری چلوکباب شیرینی بستنی شکلات نوشابه آبمیوه
سوسیس کالباس همبرگر ماکارونی لازانیا آتشفشان زلزله صاعقه رنگین‌کمان غروب طلوع نیمه‌شب بامداد پگاه شامگاه
دلنشین زیبایی مهربان شادمانی خوشحال سرگرمی هیجان ماجراجو کنجکاو خلاقیت نوآوری پشتکار تلاشگر سخاوت بخشندگی
احترام همدلی همکاری وفاداری پزشک پرستار مهندس معمار نقاش نویسنده خبرنگار آتش‌نشان خلبان راننده نانوا قصاب کشاورز
باغبان دامپزشک برنامه‌نویس طراح عکاس آوازخوان نوازنده بازیگر نرم‌افزار سخت‌افزار الگوریتم رباتیک آسمان‌خراش
تابستان پاییز زمستان بهار پنجشنبه چهارشنبه جمعه شنبه یکشنبه دوشنبه صبحانه ناهار شام میان‌وعده سفره قاشق چنگال بشقاب
لیوان فنجان قوری سماور کتری قابلمه ماهیتابه سینی کاسه نمکدان شکرپاش خانواده فرزند عروسی تولد جشنواره
چادر شب‌نشینی کوله‌پشتی دوچرخه‌سواری کوه‌نوردی ماهیگیری شنا ورزشکار قهرمان مدالیست بازیکن داور تماشاگر
پرچم سرود وطن ایران تهران اصفهان شیراز مشهد تبریز کرمان یزد اهواز رشت قزوین همدان ارومیه
""".split()
MIN_WORD_LEN, MAX_WORD_LEN = 5, 10         # فدایی: کلمه‌ها ۳–۴ حرفی بود؛ حداقل ۵ حرف (بدونِ نیم‌فاصله/فاصله)


def _wlen(w: str) -> int:
    return len(w.replace("\u200c", "").replace(" ", ""))


WORDS = [w for w in dict.fromkeys(_COMMON + _EXTRA + _HALLOWEEN) if MIN_WORD_LEN <= _wlen(w) <= MAX_WORD_LEN]
_RECENT_WORDS: dict[tuple, list] = {}          # (bot_id, chat_id) → آخرین کلمه‌هایِ داده‌شده


def pick_word(scope: tuple = (0, 0)) -> str:
    """کلمهٔ تصادفی که در همین گروه به‌تازگی نیامده باشد (حافظهٔ ۲/۳ِ فهرست؛ بعد از پُرشدن قدیمی‌ترین‌ها آزاد می‌شوند)."""
    recent = _RECENT_WORDS.setdefault(scope, [])
    keep = max(1, (len(WORDS) * 2) // 3)
    pool = [w for w in WORDS if w not in recent] or list(WORDS)
    word = secrets.choice(pool)
    recent.append(word)
    del recent[:-keep]
    if len(_RECENT_WORDS) > 2000:                    # سقفِ حافظه: قدیمی‌ترین گروه‌ها کنار می‌روند
        for k in list(_RECENT_WORDS)[:500]:
            _RECENT_WORDS.pop(k, None)
    return word


_LAST_END: dict[int, float] = {}
_LAST_GUESS: dict[tuple[int, int], float] = {}

_AR = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_START = re.compile(r"^\s*(?:/guess(?:@\w+)?|(?:بازی[\s‌]+)?حدس[\s‌]*عدد)\s*$", re.I)
_HELP = re.compile(r"^\s*(?:/games(?:@\w+)?|بازی|بازی[\s‌]*ها|راهنمای[\s‌]*بازی(?:[\s‌]*ها)?)\s*$", re.I)
HELP_COOLDOWN = 20
_LAST_HELP: dict[int, float] = {}
_SPEED = re.compile(r"^\s*(?:/speed(?:@\w+)?|(?:بازی[\s‌]+)?سرعت)\s*$", re.I)
_MATH = re.compile(r"^\s*(?:/math(?:@\w+)?|(?:بازی[\s‌]+)?ریاضی)\s*$", re.I)
_TOP = re.compile(r"^\s*(?:/top(?:@\w+)?|جدول(?:[\s‌]*شیرینی(?:[\s‌]*ها)?)?)\s*$", re.I)


def _paid_player(m: Message) -> bool:
    """این کاربر الان بازیکنِ یک مسابقهٔ پولیِ زنده در همین گروه است؟ (عدد/کلمه‌اش مالِ مسابقهٔ پولی است، نه بازیِ رایگان)"""
    try:
        from platform_wager_race import player_race
        return bool(m.from_user and player_race(m.chat.id, m.from_user.id))
    except Exception:  # noqa: BLE001
        return False


def parse_guess(text: str | None) -> int | None:
    t = (text or "").strip().translate(_AR)
    if not t.isdigit() or len(t) > 3:
        return None
    n = int(t)
    return n if LOW <= n <= HIGH else None


def points_for(elapsed: float) -> int:
    return BASE_POINTS + (FAST_BONUS if elapsed <= FAST_SECS else 0)


# === PATCH_GAMES_GROUP_ALLOW_V1 === (گزارش: «سرعت/حدس عدد کار نمی‌کند با اینکه روشن است»)
# قبلاً بازیِ رایگان هم مثلِ شرط‌بندی فقط وقتی کار می‌کرد که «گروه متصل به محافظ» باشد *و محافظ روشن*؛ اگر یکی نبود، بی‌صدا
# رد می‌شد. حالا: گروهِ متصل به محافظ (روشن یا خاموش) یا گروهی که مدیر با «/gameson» ثبت کرده؛ و اگر مدیر در گروهِ غیرمجاز
# دستورِ بازی بنویسد، دلیلش را می‌بیند. (مسابقه‌هایِ پولی همچنان شرطِ سخت‌گیرانهٔ شرط‌بندی را دارند.)
KEY_CHAT = "pgg_chat_id"


async def allowed_chat(chat_id: int) -> bool:
    for key in (KEY_CHAT, "group_guard_chat_id"):
        v = ((await _pset(key, "")) or "").strip()
        if v and v == str(chat_id):
            return True
    return False


_group_allowed = allowed_chat      # نامِ قدیمی (تست‌ها آن را جایگزین می‌کنند)


async def _hint_admin_blocked(msg: Message) -> None:
    from config import PLATFORM_ADMIN_ID
    if not msg.from_user or msg.from_user.id != PLATFORM_ADMIN_ID:
        return
    guard = ((await _pset("group_guard_chat_id", "")) or "").strip()
    why = (blocked_reasons(guard, True, msg.chat.id) or ["این گروه ثبت نشده"])[0]
    await msg.reply(f"ℹ️ بازی‌ها در این گروه فعال نیستند چون: {why}.\n"
                    "برایِ فعال‌کردنِ بازی‌ها در همین گروه، همین‌جا بنویس: <code>/gameson</code>\n"
                    "(این پیام فقط برایِ مدیرِ اصلی نمایش داده می‌شود.)", parse_mode="HTML")


async def enabled() -> bool:
    return (await _pset(KEY_ON, "1")) != "0"


def _akey(uid: int) -> str:
    return f"pgg:{uid}"


async def add_points(uid: int, name: str, pts: int) -> int:
    """امتیازِ کلِ کاربر را زیاد می‌کند (Setting پلتفرم؛ مقدار «امتیاز|نام»)."""
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == _akey(uid)))
        cur = 0
        if r and "|" in (r.value or ""):
            try:
                cur = int(r.value.split("|", 1)[0])
            except ValueError:
                cur = 0
        val = f"{cur + pts}|{name[:40]}"
        if r:
            r.value = val
        else:
            s.add(Setting(bot_id=0, key=_akey(uid), value=val))
        await s.commit()
    return cur + pts


async def top(limit: int = 10) -> list[tuple[str, int]]:
    async with Session() as s:
        rows = (await s.scalars(select(Setting).where(Setting.bot_id == 0, Setting.key.like("pgg:%")))).all()
    out = []
    for r in rows:
        try:
            p, name = (r.value or "0|").split("|", 1)
            if int(p) > 0:
                out.append((name or "ناشناس", int(p)))
        except ValueError:
            continue
    out.sort(key=lambda x: -x[1])
    return out[:limit]


async def _award(uid: int, name: str, pts: int) -> tuple[int, int, str]:
    """سقفِ روزانه ⇒ امتیاز ⇒ جایزه؛ خروجی: (امتیازِ داده‌شده، مجموع، متنِ اضافه برایِ پیامِ برنده)."""
    import halloween as HW
    import platform_candy as C
    if not await C.candy_active():                 # مودِ شیرینی تمام/خاموش: امتیازی داده نمی‌شود
        return 0, await _total(uid), ""
    c = await C.cfg()
    day = HW._tehran_day()
    got = await C.apply_cap(uid, pts, day, c["cap"])
    total = await add_points(uid, name, got) if got else await _total(uid)
    extra = ""
    if got < pts:
        extra += f"\n⏳ سقفِ شیرینیِ امروزت ({c['cap']}) پر شد؛ فردا ادامه بده."
    rw = await C.try_reward(uid, total)
    if rw["status"] == "paid":
        extra += (f"\n\n🎉💎 <b>تبریک!</b> {c['goal']} شیرینی جمع کردی؛ <b>{rw['amount']:,} تومان</b> به کیف پولِ بات‌سازت اضافه شد!")
    elif rw["status"] == "no_user" :
        extra += "\n\n💎 به هدفِ جایزه رسیدی! برایِ دریافت، یک بار ربات اصلیِ بات‌ساز را استارت کن و در گروه «شیرینی من» را بنویس."
    elif rw["status"] == "budget":
        extra += "\n\n💎 جایزه‌هایِ این دوره تمام شد؛ امتیازت در جدول می‌ماند."
    return got, total, extra


async def _total(uid: int) -> int:
    async with Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == _akey(uid)))
    try:
        return int((r.value or "0|").split("|", 1)[0]) if r else 0
    except ValueError:
        return 0


def _is_start(m: Message) -> bool:
    return bool(m.text and _START.match(m.text))


def _is_top(m: Message) -> bool:
    return bool(m.text and _TOP.match(m.text))


def _is_guess(m: Message) -> bool:
    return m.chat.id in _GAMES and parse_guess(m.text) is not None and not _paid_player(m)


async def _gate(msg: Message) -> bool:
    if not msg.from_user or msg.from_user.is_bot or msg.sender_chat:
        raise SkipHandler
    if not await _group_allowed(msg.chat.id):
        logger.info("games: چت %s گروهِ بازی نیست؛ نادیده (برایِ فعال‌سازی /gameson)", msg.chat.id)
        await _hint_admin_blocked(msg)
        raise SkipHandler
    if not await enabled():
        from config import PLATFORM_ADMIN_ID
        if msg.from_user.id == PLATFORM_ADMIN_ID:
            await msg.reply("ℹ️ بازی‌هایِ رایگان خاموش‌اند (کلید pgg_on). برایِ روشن‌کردن: <code>/gameson</code>",
                            parse_mode="HTML")
        raise SkipHandler
    return True


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_start))
async def gg_start(msg: Message):
    await _gate(msg)
    cid, now = msg.chat.id, time.monotonic()
    if cid in _RACES:
        return await msg.reply("🎮 یک مسابقهٔ سرعت جریان دارد؛ بعد از تمام‌شدنش دوباره بنویس.")
    if cid in _GAMES:
        return await msg.reply("🎯 یک بازیِ حدس عدد جریان دارد؛ عددت را بنویس (۱ تا ۱۰۰).")
    wait = START_COOLDOWN - (now - _LAST_END.get(cid, -1e9))
    if wait > 0:
        return await msg.reply(f"⏳ {int(wait) + 1} ثانیهٔ دیگر می‌شود دوباره شروع کرد.")
    g = {"n": LOW + secrets.randbelow(HIGH - LOW + 1), "t0": now, "guesses": 0}
    _GAMES[cid] = g
    sent = await msg.reply(
        "🎃 <b>حدس عدد!</b>\n\nیک عدد بین <b>۱ تا ۱۰۰</b> در ذهنم است. فقط عدد را بنویس؛ "
        "می‌گویم بالاتر است یا پایین‌تر.\n🍬 اولین نفرِ درست، شیرینی می‌گیرد (زیرِ ۳۰ ثانیه جایزهٔ بیشتر!)\n"
        f"⏱ مهلت: {TIMEOUT // 60} دقیقه", parse_mode="HTML")
    g["mid"] = sent.message_id
    asyncio.create_task(_expire(msg.bot, cid, g))


async def _expire(bot, cid: int, g: dict) -> None:
    await asyncio.sleep(TIMEOUT)
    if _GAMES.get(cid) is g:
        _GAMES.pop(cid, None)
        _LAST_END[cid] = time.monotonic()
        try:
            await bot.send_message(cid, f"⌛ وقت تمام شد! عدد <b>{g['n']}</b> بود 👻", parse_mode="HTML")
        except Exception:
            logger.debug("guess-game: پیامِ پایانِ مهلت نرفت")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_guess))
async def gg_guess(msg: Message):
    await _gate(msg)
    cid, uid, now = msg.chat.id, msg.from_user.id, time.monotonic()
    g = _GAMES.get(cid)
    if not g:
        raise SkipHandler
    if now - _LAST_GUESS.get((cid, uid), -1e9) < GUESS_GAP:
        return                                   # فاصلهٔ بینِ حدس‌هایِ یک نفر؛ بی‌صدا
    _LAST_GUESS[(cid, uid)] = now
    n = parse_guess(msg.text)
    g["guesses"] += 1
    if n < g["n"]:
        return await msg.reply("⬆️ بالاتر")
    if n > g["n"]:
        return await msg.reply("⬇️ پایین‌تر")
    if _GAMES.get(cid) is not g:                 # هم‌زمان نفرِ دیگری برد
        return
    _GAMES.pop(cid, None)
    _LAST_END[cid] = now
    pts = points_for(now - g["t0"])
    name = msg.from_user.full_name or str(uid)
    got, total, extra = await _award(uid, name, pts)
    import platform_candy as _C
    if await _C.candy_active():
        await msg.reply(f"🎉 <b>{_html.escape(name)}</b> درست حدس زد: <b>{g['n']}</b>!\n"
                        f"🍬 +{got} شیرینی (مجموع: {total}) · {g['guesses']} حدس{extra}\n\n«جدول» را بنویس تا برترین‌ها را ببینی.",
                        parse_mode="HTML")
    else:
        await msg.reply(f"🎉 <b>{_html.escape(name)}</b> درست حدس زد: <b>{g['n']}</b>! ({g['guesses']} حدس)",
                        parse_mode="HTML")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_top))
async def gg_top(msg: Message):
    await _gate(msg)
    rows = await top()
    medals = ["🥇", "🥈", "🥉"] + ["🔸"] * 7
    lines = ["🏆 <b>جدولِ شیرینی‌ها</b>\n"]
    if not rows:
        lines.append("هنوز کسی امتیاز نگرفته؛ «حدس عدد» را بنویس و اولین نفر باش 🎃")
    for i, (name, p) in enumerate(rows):
        lines.append(f"{medals[i]} {_html.escape(name)} — {p} 🍬")
    await msg.reply("\n".join(lines), parse_mode="HTML")


# ───────────────────────── مسابقهٔ سرعت (کلمه / ریاضی) ─────────────────────────

def _norm_answer(t: str | None) -> str:
    return (t or "").strip().translate(_AR).replace("\u200c", "").replace(" ", "").lower()


def make_math(rnd=secrets) -> tuple[str, str]:
    a, b = 2 + rnd.randbelow(18), 2 + rnd.randbelow(18)
    if rnd.randbelow(3) == 0:
        return f"{a} × {b % 10 + 2}", str(a * (b % 10 + 2))
    if rnd.randbelow(2):
        return f"{a} + {b}", str(a + b)
    hi, lo = max(a, b) + 10, min(a, b)
    return f"{hi} − {lo}", str(hi - lo)


def _is_speed(m: Message) -> bool:
    return bool(m.text and _SPEED.match(m.text))


def _is_math(m: Message) -> bool:
    return bool(m.text and _MATH.match(m.text))


def _is_race_answer(m: Message) -> bool:
    r = _RACES.get(m.chat.id)
    return bool(r and m.text and _norm_answer(m.text) == r["answer"] and not _paid_player(m))


async def _start_race(msg: Message, kind: str) -> None:
    await _gate(msg)
    cid, now = msg.chat.id, time.monotonic()
    if cid in _GAMES or cid in _RACES:
        return await msg.reply("🎮 یک بازی جریان دارد؛ بعد از تمام‌شدنش دوباره بنویس.")
    wait = START_COOLDOWN - (now - _LAST_END.get(cid, -1e9))
    if wait > 0:
        return await msg.reply(f"⏳ {int(wait) + 1} ثانیهٔ دیگر می‌شود دوباره شروع کرد.")
    if kind == "word":
        word = pick_word((0, cid))
        race = {"kind": kind, "answer": _norm_answer(word), "shown": word, "t0": now}
        text = (f"⚡️ <b>مسابقهٔ سرعت!</b>\n\nاولین نفری که دقیقاً این را بنویسد برنده است:\n\n"
                f"👉 <b>{_html.escape(word)}</b>\n\n⏱ {RACE_TIMEOUT} ثانیه")
    else:
        q, ans = make_math()
        race = {"kind": kind, "answer": ans, "shown": ans, "t0": now}
        text = f"🧮 <b>ریاضیِ سریع!</b>\n\nجوابِ این چیست؟\n\n👉 <b>{q} = ؟</b>\n\n⏱ {RACE_TIMEOUT} ثانیه"
    _RACES[cid] = race
    await msg.reply(text, parse_mode="HTML")
    asyncio.create_task(_expire_race(msg.bot, cid, race))


async def _expire_race(bot, cid: int, race: dict) -> None:
    await asyncio.sleep(RACE_TIMEOUT)
    if _RACES.get(cid) is race:
        _RACES.pop(cid, None)
        _LAST_END[cid] = time.monotonic()
        try:
            await bot.send_message(cid, f"⌛ وقت تمام شد؛ کسی نتوانست! جواب: <b>{_html.escape(str(race['shown']))}</b> 👻",
                                   parse_mode="HTML")
        except Exception:
            logger.debug("race: پیامِ پایانِ مهلت نرفت")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_speed))
async def gg_speed(msg: Message):
    await _start_race(msg, "word")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_math))
async def gg_math(msg: Message):
    await _start_race(msg, "math")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_race_answer))
async def gg_race_answer(msg: Message):
    await _gate(msg)
    cid, now = msg.chat.id, time.monotonic()
    race = _RACES.get(cid)
    if not race or _norm_answer(msg.text) != race["answer"]:
        raise SkipHandler
    _RACES.pop(cid, None)                       # اولین پاسخِ درست برنده است؛ هم‌زمان‌ها دیگر چیزی نمی‌بینند
    _LAST_END[cid] = now
    pts = RACE_POINTS + (RACE_FAST_BONUS if race["kind"] == "math" and now - race["t0"] <= RACE_FAST_SECS else 0)
    name = msg.from_user.full_name or str(msg.from_user.id)
    got, total, extra = await _award(msg.from_user.id, name, pts)
    import platform_candy as _C
    candy_line = f"\n🍬 +{got} شیرینی (مجموع: {total}){extra}" if await _C.candy_active() else ""
    await msg.reply(f"🏁 <b>{_html.escape(name)}</b> اول شد! ({now - race['t0']:.1f} ثانیه){candy_line}",
                    parse_mode="HTML")


# ───────────────────────── راهنمای بازی‌ها («بازی») ─────────────────────────
# === PATCH_GROUP_GAMES_HELP_V1 === هر کس در گروه بنویسد «بازی»، فهرستِ بازی‌ها و طرزِ شروعشان را می‌بیند.

def _is_help(m: Message) -> bool:
    return bool(m.text and _HELP.match(m.text))


async def help_text(bet_games: bool = True) -> str:
    """راهنمای بازی‌هایِ رایگان + طرزِ گرفتنِ شیرینی. bet_games=False: فهرستِ xo/سنگ‌کاغذ‌قیچی/تاس حذف می‌شود
    (راهنمای شرط‌بندی خودش جدا آمده)؛ فقط بخشِ مسابقه‌هایِ پولی (حدس عدد/سرعت/ریاضی) می‌ماند."""
    import platform_candy as _CA
    if not await _CA.candy_active():                 # مودِ شیرینی تمام: راهنما بدونِ شیرینی
        lines = ["🎮 <b>بازی‌هایِ گروه</b>\n",
                 "🆓 <b>رایگان (بدونِ جایزه — مودِ شیرینی تمام شده)</b>",
                 "🎯 <b>حدس عدد</b> — بنویس «حدس عدد»؛ عددی بین ۱ تا ۱۰۰ را حدس بزن، ربات می‌گوید بالاتر/پایین‌تر.",
                 "⚡️ <b>سرعت</b> — بنویس «سرعت»؛ اولین نفری که کلمهٔ ربات را دقیقاً بنویسد برنده است.",
                 "🧮 <b>ریاضی</b> — بنویس «ریاضی»؛ اولین جوابِ درست برنده است."]
        _candy = False
    else:
        _candy = True
        lines = ["🎮 <b>بازی‌هایِ گروه</b>\n",
                 "🆓 <b>رایگان (جایزه: شیرینی 🍬)</b>",
                 "🎯 <b>حدس عدد</b> — بنویس «حدس عدد»؛ عددی بین ۱ تا ۱۰۰ را حدس بزن، ربات می‌گوید بالاتر/پایین‌تر. اولین نفرِ درست {B} 🍬 (زیرِ ۳۰ ثانیه {BF} 🍬).",
                 "⚡️ <b>سرعت</b> — بنویس «سرعت»؛ اولین نفری که کلمهٔ ربات را دقیقاً بنویسد {R} 🍬 می‌گیرد.",
                 "🧮 <b>ریاضی</b> — بنویس «ریاضی»؛ اولین جوابِ درست {R} 🍬 (زیرِ ۱۰ ثانیه {RF} 🍬).",
                 "🏆 <b>جدول</b> — بنویس «جدول» تا ۱۰ نفرِ برترِ شیرینی‌ها را ببینی.",
                 "🍬 <b>شیرینی من</b> — شیرینی‌ها و پیشرفتِ تو تا جایزه.",
                 "",
                 "🍬 <b>شیرینی چطور گرفته می‌شود؟</b> فقط با بردنِ همین سه بازیِ رایگان (اول شدن در حدس عدد / سرعت / ریاضی). "
                 "شیرینی خرید و فروش و انتقال ندارد و از مسابقه‌هایِ پولی به دست نمی‌آید."]
    if _candy:
        lines = [ln.replace("{B}", str(BASE_POINTS)).replace("{BF}", str(BASE_POINTS + FAST_BONUS)).replace("{R}", str(RACE_POINTS))
                 .replace("{RF}", str(RACE_POINTS + RACE_FAST_BONUS)) for ln in lines]
    try:
        import platform_candy as C, halloween as HW
        c = await C.cfg()
        if _candy and c["on"] and await HW.is_on():
            lines += [f"💎 <b>جایزهٔ هالوین:</b> با جمع‌کردنِ {c['goal']} 🍬 مبلغِ <b>{c['amount']:,} تومان</b> خودکار به کیف پولِ ربات اصلی‌ات "
                      f"اضافه می‌شود (روزی حداکثر {c['cap']} 🍬؛ فقط {c['max']} نفرِ اول؛ هر نفر یک بار). "
                      "برای دریافت، یک بار ربات اصلی را استارت کرده باشی."]
    except Exception:
        logger.debug("games help: بخشِ جایزه خوانده نشد")
    try:
        from platform_wager import _bet_enabled, game_enabled, load_cfg, example_amount
        if await _bet_enabled():
            ex = example_amount(await load_cfg())
            parts = []
            if bet_games:
                if await game_enabled("xo"):
                    parts.append(f"ایکس‌او: «xo {ex}»")
                if await game_enabled("rps"):
                    parts.append(f"سنگ‌کاغذ‌قیچی: «سنگ کاغذ قیچی {ex}»")
                if await game_enabled("dice"):
                    parts.append(f"تاس: «تاس {ex}»")
            try:
                from platform_wager_race import enabled as _race_on, help_section as _race_help
                if await _race_on():
                    if bet_games:
                        parts += [f"حدس عدد: «شرطبندی حدس عدد {ex}»", f"سرعت: «شرطبندی سرعت {ex}»",
                                  f"ریاضی: «شرطبندی ریاضی {ex}»"]
                    else:
                        sec = await _race_help()
                        if sec:
                            lines += ["", sec]
            except Exception:
                logger.debug("games help: بخشِ مسابقهٔ پولی خوانده نشد")
            if parts:
                lines += ["", "💰 <b>دونفره با کیف پول</b> (عددِ آخر مبلغِ هر نفر به تومان؛ روی پیامِ حریف ریپلای کن تا مستقیم بازی کنید):",
                          "• " + "\n• ".join(parts),
                          "⚠️ ریسکِ مالی دارد؛ مسئولیتش با خودِ توست."]
    except Exception:
        logger.debug("games help: بخشِ شرط‌بندی خوانده نشد")
    lines += ["", "ℹ️ بازیِ رایگان هم‌زمان یکی است؛ مسابقه‌هایِ پولی را هر چند نفر بخواهند هم‌زمان می‌سازند."]
    return "\n".join(lines)


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_help))
async def gg_help(msg: Message):
    await _gate(msg)
    now = time.monotonic()
    if now - _LAST_HELP.get(msg.chat.id, -1e9) < HELP_COOLDOWN:
        return                                   # تکرارِ پشتِ‌سرهم: بی‌صدا (گروه شلوغ نشود)
    _LAST_HELP[msg.chat.id] = now
    await msg.reply(await help_text(), parse_mode="HTML")


# ───────────────────────── «شیرینی من» و تنظیمِ جایزه ─────────────────────────
# === PATCH_CANDY_REWARD_V1 ===
_MYC = re.compile(r"^\s*(?:/mycandy(?:@\w+)?|شیرینی[\s‌]*(?:من|های[\s‌]*من))\s*$", re.I)


def _is_myc(m: Message) -> bool:
    return bool(m.text and _MYC.match(m.text))


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.func(_is_myc))
async def gg_mycandy(msg: Message):
    await _gate(msg)
    import halloween as HW
    import platform_candy as C
    if not await C.candy_active():
        return await msg.reply("🍬 مودِ شیرینی تمام شده؛ بازی‌ها ادامه دارند ولی شیرینی داده نمی‌شود.")
    uid = msg.from_user.id
    c = await C.cfg()
    total = await _total(uid)
    used = await C.used_today(uid, HW._tehran_day())
    lines = [f"🍬 <b>شیرینی‌هایِ {_html.escape(msg.from_user.full_name or str(uid))}</b>",
             f"مجموع: <b>{total}</b> از {c['goal']}",
             f"امروز: {used} از {c['cap']}"]
    rw = await C.try_reward(uid, total)
    if rw["status"] == "paid":
        lines.append(f"\n🎉💎 جایزه پرداخت شد: <b>{rw['amount']:,} تومان</b> به کیف پولت اضافه شد!")
    elif rw["status"] == "already":
        lines.append("\n💎 جایزهٔ این دوره را قبلاً گرفته‌ای.")
    elif rw["status"] == "no_user":
        lines.append("\n💎 به هدف رسیدی! یک بار ربات اصلی را استارت کن و دوباره «شیرینی من» را بنویس.")
    elif rw["status"] == "budget":
        lines.append("\n💎 جایزه‌هایِ این دوره تمام شد.")
    elif rw["status"] == "below" and c["on"]:
        lines.append(f"\n💎 {max(0, c['goal'] - total)} شیرینیِ دیگر تا <b>{c['amount']:,} تومان</b>!")
    await msg.reply("\n".join(lines), parse_mode="HTML")


@platform_group_game_router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(r"^/hwreward\b"))
async def hw_reward_cmd(msg: Message):
    from config import PLATFORM_ADMIN_ID
    if not msg.from_user or msg.from_user.id != PLATFORM_ADMIN_ID:
        raise SkipHandler
    import platform_candy as C
    parts = (msg.text or "").replace("،", " ").split()[1:]
    nums = [int(p.translate(_AR)) for p in parts if p.translate(_AR).isdigit()]
    if len(nums) == 4:
        await C.set_numbers(*nums)
    c = await C.cfg()
    await msg.answer(f"💎 <b>جایزهٔ شیرینی</b> ({'✅ روشن' if c['on'] else '⛔️ خاموش'})\n"
                     f"مبلغ: <b>{c['amount']:,}</b> تومان · هدف: <b>{c['goal']}</b> 🍬 · سقفِ نفرات: <b>{c['max']}</b> · سقفِ روزانه: <b>{c['cap']}</b> 🍬\n"
                     f"پرداخت‌شده تاکنون: {await C.paid_count()} نفر\n\n"
                     "تغییر: <code>/hwreward مبلغ هدف سقفِ‌نفرات سقفِ‌روزانه</code> — مثال: <code>/hwreward 7000 100 20 25</code>",
                     parse_mode="HTML")


@platform_group_game_router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text.regexp(r"^/games(on|off)(@\w+)?\s*$"))
async def gg_admin_switch(msg: Message):
    """مدیرِ اصلی: «/gameson» همین گروه را گروهِ بازی‌ها می‌کند، «/gamesoff» برش می‌دارد."""
    from config import PLATFORM_ADMIN_ID
    if not msg.from_user or msg.from_user.id != PLATFORM_ADMIN_ID:
        raise SkipHandler
    from platform_bot import set_pset
    on = "gameson" in (msg.text or "").lower()
    await set_pset(KEY_CHAT, str(msg.chat.id) if on else "")
    if on:
        await set_pset(KEY_ON, "1")                  # کلیدِ اصلی هم روشن شود
    await msg.reply("✅ بازی‌هایِ رایگان (حدس عدد · سرعت · ریاضی) در این گروه فعال شد. بنویس «بازی» تا راهنما بیاید."
                    if on else "⛔️ این گروه از گروهِ بازی‌ها برداشته شد (اگر به محافظ وصل باشد هنوز بازی‌ها فعال است).")
