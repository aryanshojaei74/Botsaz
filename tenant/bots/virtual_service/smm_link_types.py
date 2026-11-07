"""
smm_link_types.py — انواعِ لینکِ مجازِ هر دسته (فعلاً فقط تلگرام و اینستاگرام).

=== PATCH_SMM_CATEGORY_LINK_TYPES_V1 ===
درخواستِ ادمین (فدایی): «توی هر دسته گزینهٔ "تنظیمِ لینکِ این دسته" باشد؛ بزنیم صفحه‌ای باز
شود که انواعِ لینکِ همان پلتفرم را (تلگرام: خصوصی، عمومی، @، پست، بوست — اینستاگرام: پروفایل،
@، پست، ریلز، استوری) نشان بدهد و بشود چندتا را هم‌زمان فعال کرد؛ و وقتی مشتری لینکِ اشتباه
می‌فرستد، بگوید کدام نوع‌ها قابلِ قبول است».

این‌جا هیچ router/handler و هیچ دسترسیِ دیتابیسی نیست؛ فقط منطقِ خالص (تشخیصِ نوعِ لینک +
متنِ راهنما) تا مستقل تست شود. رابطِ ادمین و ذخیره‌سازی در smm.py است.

اصولِ طراحی:
  • classify() یک «مجموعه» برمی‌گرداند نه یک نوع: بعضی لینک‌ها ذاتاً مبهم‌اند (مثلاً
    instagram.com/share/<code> هم پست است هم ریلز)؛ لینک وقتی پذیرفته می‌شود که *حداقل یکی*
    از معناهایش مجاز باشد، تا لینکِ قانونی به‌خاطرِ ابهام رد نشود.
  • لینکی که به هیچ نوعی نخورد مجموعهٔ خالی می‌دهد (و وقتی ادمین نوع‌ها را تنظیم کرده رد
    می‌شود و راهنما می‌آید).
"""
from __future__ import annotations

import html as _html
import re

# پلتفرم → فهرستِ (کلید، برچسب، توضیح، نمونه)
TYPES: dict[str, list[tuple[str, str, str, str]]] = {
    "telegram": [
        ("private", "🔒 لینک خصوصی", "لینک‌هایِ خصوصیِ کانال/گروه", "https://t.me/+AbCdEfGh"),
        ("public",  "🌐 لینک عمومی", "لینک‌هایِ عمومیِ کانال/گروه", "https://t.me/channel"),
        ("at",      "@ شروع با @",   "یوزرنیم باید با @ شروع شود", "@channel"),
        ("post",    "📋 لینک پست",  "لینکِ مستقیمِ پست‌ها", "https://t.me/channel/123"),
        ("boost",   "🚀 لینک بوست", "لینکِ بوستِ کانال", "https://t.me/boost/channel"),
    ],
    "instagram": [
        ("profile", "👤 لینک پروفایل", "لینکِ صفحهٔ پروفایل", "https://instagram.com/username"),
        ("at",      "@ شروع با @",      "یوزرنیم باید با @ شروع شود", "@username"),
        ("post",    "📷 لینک پست",     "لینکِ مستقیمِ پست", "https://instagram.com/p/AbCdEfG/"),
        ("reel",    "🎬 لینک ریلز",    "لینکِ مستقیمِ ریلز", "https://instagram.com/reel/AbCdEfG/"),
        ("story",   "📖 لینک استوری",  "لینکِ استوری/هایلایت", "https://instagram.com/stories/username/123"),
    ],
}

SUPPORTED_PLATFORMS = tuple(TYPES)

_TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
_IG_HOSTS = {"instagram.com", "instagr.am"}

# نامِ کاربریِ تلگرام: حرف، بعد حرف/رقم/_ (۳ تا ۳۲ کاراکتر؛ عمداً کمی سخاوتمندانه)
_TG_USER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,31}$")
# نامِ کاربریِ اینستاگرام: حرف/رقم/نقطه/_ تا ۳۰ کاراکتر
_IG_USER = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# مسیرهایِ رزروشدهٔ تلگرام که «نامِ کانال» نیستند
_TG_RESERVED = {"boost", "joinchat", "addstickers", "addemoji", "addlist", "addtheme", "proxy",
                "socks", "share", "c", "s", "iv", "login", "setlanguage", "confirmphone", "bg",
                "invoice", "m", "contact", "msg", "giftcode", "web", "k", "a"}
# مسیرهایِ رزروشدهٔ اینستاگرام که «نامِ کاربری» نیستند
_IG_RESERVED = {"explore", "accounts", "direct", "about", "legal", "developer", "web", "challenge",
                "emails", "oauth", "api", "directory", "reels", "reel", "p", "tv", "stories", "s",
                "share", "tags", "locations", "popular", "session", "download", "lite", "help"}


def supported(platform: str | None) -> bool:
    return (platform or "") in TYPES


def type_defs(platform: str) -> list[tuple[str, str, str, str]]:
    return TYPES.get(platform or "", [])


def valid_keys(platform: str) -> list[str]:
    return [k for k, *_ in type_defs(platform)]


def parse_keys(raw: str | None, platform: str) -> set[str]:
    """رشتهٔ ذخیره‌شده («private,post») → مجموعهٔ کلیدهایِ معتبرِ همین پلتفرم."""
    ok = set(valid_keys(platform))
    return {k for k in (raw or "").split(",") if k in ok}


def dump_keys(keys: set[str], platform: str) -> str:
    """مجموعه → رشتهٔ پایدار با ترتیبِ ثابتِ رجیستری."""
    return ",".join(k for k in valid_keys(platform) if k in keys)


def label_of(platform: str, key: str) -> str:
    for k, label, *_ in type_defs(platform):
        if k == key:
            return label
    return key


def _split(url: str) -> tuple[str, list[str]] | None:
    """(host با حروفِ کوچک بدونِ www./m.، بخش‌هایِ مسیر) یا None اگر شبیهِ URL نبود."""
    u = (url or "").strip()
    if not u or " " in u or "\n" in u:
        return None
    m = re.match(r"(?i)^(?:https?://)?(?:www\.|m\.)?([a-z0-9.\-]+)((?:/[^?#\s]*)?)(?:[?#].*)?$", u)
    if not m:
        return None
    host = m.group(1).lower()
    segs = [s for s in m.group(2).split("/") if s]
    return host, segs


def _classify_telegram(link: str) -> set[str]:
    u = (link or "").strip()
    if u.startswith("@"):
        return {"at"} if _TG_USER.match(u[1:]) else set()
    parsed = _split(u)
    if not parsed:
        return set()
    host, segs = parsed
    if host not in _TG_HOSTS or not segs:
        return set()
    s0 = segs[0]
    low = s0.lower()
    if low == "boost":
        return {"boost"}                         # t.me/boost/<name> یا t.me/boost?c=<id>
    if s0.startswith("+") or low == "joinchat":
        return {"private"} if len(segs) >= (1 if s0.startswith("+") else 2) else set()
    if low == "c":                               # پستِ کانالِ خصوصی: t.me/c/<id>/<n>
        return {"post"} if len(segs) >= 3 and segs[2].isdigit() else set()
    if low == "s":                               # نسخهٔ وب: t.me/s/<name>/<n>
        return {"post"} if len(segs) >= 3 and segs[2].isdigit() else set()
    if low in _TG_RESERVED or not _TG_USER.match(s0):
        return set()
    if len(segs) >= 2 and segs[1].isdigit():
        return {"post"}                          # t.me/<name>/<n>
    if len(segs) == 1:
        return {"public"}                        # t.me/<name>
    return set()


def _classify_instagram(link: str) -> set[str]:
    u = (link or "").strip()
    if u.startswith("@"):
        return {"at"} if _IG_USER.match(u[1:]) else set()
    parsed = _split(u)
    if not parsed:
        return set()
    host, segs = parsed
    if host not in _IG_HOSTS or not segs:
        return set()
    s0 = segs[0].lower()
    if s0 in ("p", "tv"):
        return {"post"} if len(segs) >= 2 else set()
    if s0 in ("reel", "reels"):
        return {"reel"} if len(segs) >= 2 and segs[1].lower() != "audio" else set()
    if s0 == "stories":
        return {"story"} if len(segs) >= 2 else set()
    if s0 == "share":
        if len(segs) >= 3 and segs[1].lower() in ("p", "tv"):
            return {"post"}
        if len(segs) >= 3 and segs[1].lower() in ("reel", "reels"):
            return {"reel"}
        # instagram.com/share/<code> — نوعش از رویِ خودِ لینک معلوم نیست؛ پست یا ریلز
        return {"post", "reel"} if len(segs) == 2 else set()
    if s0 in _IG_RESERVED or not _IG_USER.match(segs[0]):
        return set()
    if len(segs) == 1:
        return {"profile"}
    if len(segs) >= 3 and segs[1].lower() in ("p", "tv"):
        return {"post"}                          # instagram.com/<user>/p/<code>
    if len(segs) >= 3 and segs[1].lower() in ("reel", "reels"):
        return {"reel"}
    return set()


def classify(platform: str, link: str) -> set[str]:
    """مجموعهٔ نوع‌هایی که این لینک می‌تواند باشد (خالی = هیچ‌کدام/ناشناخته)."""
    if platform == "telegram":
        return _classify_telegram(link)
    if platform == "instagram":
        return _classify_instagram(link)
    return set()


def hint_html(platform: str, keys: set[str]) -> str:
    """راهنمایِ «لینک‌هایِ قابلِ‌قبول» برایِ پیامِ خطا — فقط نوع‌هایِ فعالِ این دسته."""
    lines = ["✅ لینک‌هایِ قابلِ‌قبول برایِ این سرویس:"]
    for k, label, _desc, example in type_defs(platform):
        if k in keys:
            lines.append(f"• {label} — <code>{_html.escape(example)}</code>")
    return "\n".join(lines)


def short_labels(platform: str, keys: set[str]) -> str:
    """فهرستِ کوتاهِ برچسب‌ها (بدونِ توضیح) برایِ یک خط در صفحهٔ محصول/دسته."""
    return "، ".join(label for k, label, *_ in type_defs(platform) if k in keys)
