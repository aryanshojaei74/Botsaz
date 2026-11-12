"""word_bank.py — PATCH_WORD_GAMES_V1

فرهنگ‌لغتِ کوچکِ دست‌ساز (کلماتِ رایجِ فارسی) برایِ دو مسابقهٔ پولیِ گروه:
  • «کلمه‌سازی»: چند حرف داده می‌شود؛ باید چند کلمهٔ مشخص (مثلاً یکی ۲ حرفی و دوتا ۳ حرفی) با همان حروف ساخت.
  • «درهم»: حروفِ یک کلمه درهم‌ریخته می‌شود؛ اولین نفری که کلمه را درست بنویسد برنده است.

اصلِ مهم: هر پازل از روی همین فرهنگ ساخته می‌شود، پس همیشه حل‌شدنی است؛ و فقط کلماتِ همین فرهنگ پذیرفته می‌شوند
(کلمهٔ درستِ نادر ممکن است نپذیرد — در قوانینِ بازی نوشته شده «کلماتِ رایج»).
نیم‌فاصله/فاصله/حروفِ عربی‌شکل (ي، ك) و اعراب هنگامِ مقایسه یکسان می‌شوند.
"""
from __future__ import annotations

import secrets
from collections import Counter

_NORM = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی", "ۀ": "ه", "ة": "ه", "أ": "ا", "إ": "ا"})
_STRIP = dict.fromkeys(map(ord, "‌‍‏‎ ـًٌٍَُِّْ\t\n"), None)
_PERSIAN = set("آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی")


def norm(w: str | None) -> str:
    return (w or "").translate(_NORM).translate(_STRIP)


def is_word_shape(w: str | None) -> bool:
    """فقط حروفِ فارسی (بدونِ عدد/نشانه) و طولِ معقول."""
    t = norm(w)
    return 1 < len(t) <= 8 and all(c in _PERSIAN for c in t)


_W2 = """آب آن او از با بر به تا تو من در را که چه نه یا هر هم سر پا دو سه شب نو ده دل گل بد کم لب نی ما بی تن جا
خر دم زن مو پر رو رد سد شک فن قد کی گر لا نم وا کف می""".split()

_W3 = """آبی آتش ابر اسب امن باد بار بال بام بزم بوم بیم بید پدر پسر پیر تار تب تخم تیر تور جان جام جای جنگ خار خال
خاک خشم خواب دار داد دام دانه درد دست دشت دوا دور دوش ریز راه رنگ روز ریه زار زرد زمین زور ساز سال سیب
سبز سرد سفر سنگ شاد شام شیر صاف صبر طلا عدد عسل عصر علم غار غذا فال فرش فیل قاب قند قلب کار کاه کوه
کیف گاز گرم گنج لانه لب ماه مار مال مرد مست مشت مژه موج میز میخ ناز نام نان نخ نور نیم هوا وام ورد
ولی یار یخ یک اثر ارث اسم الک انس اوج ایل ایر بخت بدن برف برگ بوی بلند پاس پول پوست پیک تاب تاج
تپه تخت ترس تلخ تنگ توپ توت ثروت جار جمع جیب چاه چای چرخ چشم چوب چیز حال حبس حکم حیف خدا خرس خوب
خیس دره دریا دزد دلو دوست دیو ذرت راز رخ رای ریش زبان زخم زنگ زیر ژاله سبد سحر سخت سرو سقف سلام
سوز سیر شال شاخ شبنم شمع شهر شیب صدا صنم ضرب طبل طوفان ظرف عاج عکس عمر عید غم غول فاش فرد فصل
فکر فوت قاصد قصر قفل قهر کتاب کفش کلید کمک کنار کیک گاو گرد گفت گوش گوی لاک لجن لحن لرز لک
لیمو مادر مغز مگس موز موش مهر میوه نخل نرم نسیم نشان نقش نمک نوا نوک وزن وسط وقت هدف هفت همه
هنر هیچ هیزم یاد یتیم یکی""".split()

_W4 = """آتش آرام آمار آزاد آسان آسمان آشنا آفتاب آهنگ ابزار اتاق ارزش استاد اسرار افسوس امید امروز انار انسان انگور
اوقات ایران بابا بازی بالا بانو باران بدون برابر برنج بزرگ بستنی بلبل بهار بهشت بیمار بیرون پارک پاییز
پایین پرده پرنده پروانه پزشک پنجره پنیر پیراهن پیام تابلو تازه تبریک تپش ترانه تشنه تلفن تماس تنها
توپ جاده جدید جزیره جشن جلسه جنگل جهان جوان جوهر چادر چراغ چمدان چنار خانه خاطره خبر خداحافظ
خرما خروس خسته خندان خنده خورشید خوشبو خیابان دانش دختر دریا دستمال دشمن دفتر دکتر دلتنگ دلیل
دنیا دوچرخه دهان دیروز دیوار راست راننده رسم رفیق روباه روشن رویا زبان زرافه زمان زمستان زنبور
زیبا ساحل ساعت ساده سایه سپید ستاره سرما سرود سفید سلام سوال سیاه سیب شاعر شادی شانس شاهین شب
شبانه شجاع شکار شکر شکل شلوار شمال شنبه شهرت شیرین شیشه صبح صحرا صندلی صورت ضعیف طبیعت
طرح طلوع طوطی ظهر عاشق عروس عشق عطر علاقه عینک غروب غزال فردا فرزند فرشته فرصت فروش فنجان
فوتبال قاشق قایق قدیمی قرمز قصه قلعه قلم قهوه کاغذ کبوتر کتاب کجا کدو کرم کشور کلاس کلمه
کمان کودک کوچک کوهستان کیسه گذشته گربه گردو گرسنه گروه گرما گزارش گسترده گلاب گلدان گندم
گوسفند گوشت لاله لباس لیوان مادر ماست ماشین مدرسه مرداد مروارید مسافر مسجد مشتری معلم
مقصد ملت منزل مهمان مهربان میدان میز نارنج ناهار نان نشانه نقاشی نگاه نمره نوجوان نوشته
نیرو هدیه هزار هفته همسر همین هنوز هوشیار هیجان وادی واقعی وطن وقت ورزش وزیر یادگار یاس
یخچال یکشنبه""".split()


def _clean(words):
    out = []
    for w in words:
        n = norm(w)
        if 2 <= len(n) <= 7 and all(c in _PERSIAN for c in n):
            out.append(n)
    return list(dict.fromkeys(out))


_ALL = _clean(_W2 + _W3 + _W4)
DICT: frozenset[str] = frozenset(_ALL)
BY_LEN: dict[int, list[str]] = {}
for _w in _ALL:
    BY_LEN.setdefault(len(_w), []).append(_w)

# کلماتِ پایه برایِ پازل (۵ تا ۶ حرف)؛ همه از فرهنگ‌اند
BASES = [w for w in _ALL if 5 <= len(w) <= 6]


def can_build(word: str, letters) -> bool:
    c, have = Counter(norm(word)), Counter(letters)
    return all(have[k] >= v for k, v in c.items())


def candidates(letters, length: int) -> list[str]:
    return [w for w in BY_LEN.get(length, []) if can_build(w, letters)]


_SPECS = [(2, 3, 3), (3, 3, 4), (2, 3, 4), (3, 3, 3), (2, 3), (3, 4), (2, 2, 3)]


def make_word_puzzle(rnd=secrets) -> dict | None:
    """پازلِ «کلمه‌سازی»: {letters:[...], need:[2,3,3], total}. همیشه حل‌شدنی (هر طولِ لازم ≥ تعدادِ لازم + ۱ کلمهٔ ممکن)."""
    for _ in range(300):
        base = BASES[rnd.randbelow(len(BASES))]
        letters = list(base)
        # بُرزدنِ حروف؛ اگر ترتیب دقیقاً خودِ کلمه شد دوباره
        for _k in range(5):
            for i in range(len(letters) - 1, 0, -1):
                j = rnd.randbelow(i + 1)
                letters[i], letters[j] = letters[j], letters[i]
            if "".join(letters) != base:
                break
        feas = []
        for spec in _SPECS:
            ok = True
            for L, n in Counter(spec).items():
                cand = [w for w in candidates(letters, L) if w != base] if L == len(base) else candidates(letters, L)
                if len(cand) < n + 1:
                    ok = False
                    break
            if ok:
                feas.append(spec)
        if feas:
            spec = feas[rnd.randbelow(len(feas))]
            return {"letters": letters, "need": list(spec), "total": len(spec)}
    return None


def make_scramble(rnd=secrets) -> dict:
    """پازلِ «درهم»: {q: حروفِ درهم (با فاصله)، a: کلمه، alts: بقیهٔ کلماتِ هم‌حروفِ فرهنگ}."""
    pool = [w for w in _ALL if 4 <= len(w) <= 6]
    word = pool[rnd.randbelow(len(pool))]
    letters = list(word)
    for _k in range(8):
        for i in range(len(letters) - 1, 0, -1):
            j = rnd.randbelow(i + 1)
            letters[i], letters[j] = letters[j], letters[i]
        if "".join(letters) != word:
            break
    alts = [w for w in BY_LEN.get(len(word), []) if w != word and sorted(w) == sorted(word)]
    return {"q": " ".join(letters), "a": word, "alts": alts}


def check_word(word: str, letters, need: list[int], found: list[str]) -> tuple[str, str]:
    """نتیجهٔ یک کلمهٔ بازیکن: (کد، کلمهٔ نرمال).
    کدها: ok | dup | slots_full | not_in_dict | bad_letters | bad_shape"""
    w = norm(word)
    if not is_word_shape(w):
        return "bad_shape", w
    if not can_build(w, letters):
        return "bad_letters", w
    if w not in DICT:
        return "not_in_dict", w
    if w in found:
        return "dup", w
    have = Counter(len(x) for x in found)
    need_c = Counter(need)
    if have[len(w)] >= need_c.get(len(w), 0):
        return "slots_full", w
    return "ok", w
