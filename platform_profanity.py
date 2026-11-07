"""
platform_profanity.py — PATCH_GROUP_PROFANITY_V1

تشخیصِ فحش در پیام‌هایِ گروهِ متصل (فارسی، فینگلیش و انگلیسی). عمداً «توکن‌محور» است تا واژه‌هایِ عادی
(مثلاً «هیچ کس»، «کسب‌وکار»، «کیرگیزستان») اشتباهاً گرفته نشوند: فقط وقتی کلمه (یا عبارت) کامل و جدا بیاید
و پیشوند/پسوندِ ضمیری (م، ت، ش، مون، تون، شون، ی، ه) داشته باشد مطابقت می‌خورد.
"""
from __future__ import annotations

import re

_FA_FIX = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "أ": "ا", "إ": "ا", "ؤ": "و",
                         "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4", "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9"})
_STRIP = re.compile("[ً-ٰٟـ‌‍‎‏‪-‮⁦-⁩️]")
_REPEAT = re.compile(r"(.)\1{2,}")

# ریشه‌هایِ فحشِ قطعی (فارسی). پسوندِ ضمیری مجاز است.
_FA_STEMS = [
    "کیر", "کون", "کونی", "کونده", "جنده", "جندگی", "کصکش", "کسکش", "کسخل", "کصخل", "کسده", "کصده",
    "خارکسه", "خارکصه", "خارکسده", "خارکصده", "گاییدن", "گاییدم", "گایید", "گاییدی", "بگایید", "بگاییدم", "میگامت",
    "گوه", "حرومزاده", "حرومزادگی", "پدرسگ", "پدرسگی", "عنتر", "دیوث", "بیناموس", 
    "ننه جنده", "ننه کیر", "ننت جنده", "ننتو میگام", "ننتو گاییدم", "مادر جنده", "مادرجنده", "مادر قحبه", "مادرقحبه", "قحبه", "کس ننه", "کسننه", "کس ننت", "کسننت",
    "کس مادر", "کسمادر", "کس عمت", "کس خواهر", "کسخواهر", "کیر خر", "کیرخر", "تخمی", "ممه لیس", "لاشی", "جاکش",
    "پفیوز", "کسشر", "کس شر", "ساک بزن", "ساکزن", "بخور کیرم", 
]
# فینگلیش/انگلیسی
_EN_STEMS = [
    "fuck", "fucking", "fucker", "motherfucker", "mf", "shit", "bitch", "asshole", "dick", "pussy", "cunt", "bastard", "whore", "slut",
    "kir", "kiri", "kos", "kose", "koskesh", "kosnane", "kosnanat", "kooni", "kon", "jende", "jendeh", "madarjende", "madar jende",
    "pedarsag", "pedar sag", "haroomzade", "haramzade", "gaidam", "gayidam", "bgaidam", "kiram", "kirmitoo",
]
# فقط وقتی «تنها» بیایند فحش‌اند، نه وسطِ جمله‌هایِ عادی
_STANDALONE_ONLY = {"kon", "kos", "kir", "کون", "کوس", "گوه", "کیر", "mf", "تخمی", "لاشی"}

_SUFFIX = r"(?:م|ت|ش|مون|تون|شون|ی|ه|ها|ام|ات|اش|ی‌?ها)?"
_cache: tuple | None = None


def norm(text: str) -> str:
    t = (text or "").translate(_FA_FIX)
    t = _STRIP.sub("", t).lower()
    t = _REPEAT.sub(r"\1\1", t)                      # کییییر → کییر؛ دو تکرار را نگه می‌داریم (گاییدم)
    t = re.sub(r"[^\w\s]", " ", t)                   # نقطه/خط‌تیره/… میانِ حروف → فاصله
    return re.sub(r"\s+", " ", t).strip()


def _squash(t: str) -> str:
    """کییر / کیییر → کیر (برایِ مقایسه) — تکرارِ پشتِ‌سرهمِ هر حرف یکی می‌شود."""
    return re.sub(r"(.)\1+", r"\1", t)


def _compile():
    global _cache
    if _cache:
        return _cache
    words = [norm(w) for w in _FA_STEMS + _EN_STEMS]
    words = [_squash(w) for w in words]
    uniq = sorted(set(w for w in words if w), key=len, reverse=True)
    pat = "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in uniq)
    rx = re.compile(rf"(?<![\w])(?:{pat}){_SUFFIX}(?![\w])")
    _cache = (rx, {_squash(norm(w)) for w in _STANDALONE_ONLY})
    return _cache


def find_profanity(text: str, extra: list[str] | None = None) -> str | None:
    """اولین فحشِ پیدا‌شده (نرمال‌شده) یا None. extra = واژه‌هایِ اضافیِ مدیر."""
    t = _squash(norm(text))
    if not t:
        return None
    rx, standalone = _compile()
    for m in rx.finditer(t):
        w = m.group(0)
        base = re.sub(_SUFFIX + r"$", "", w) or w
        if base in standalone or w in standalone:
            # این واژه‌هایِ کوتاه/دوپهلو فقط وقتی کلِ پیام (یا تقریباً کلِ پیام) همان باشد
            if len(t.split()) > 4:
                continue
        return w
    for w in (extra or []):
        wn = _squash(norm(w))
        if wn and re.search(rf"(?<![\w]){re.escape(wn)}(?![\w])", t):
            return wn
    return None
