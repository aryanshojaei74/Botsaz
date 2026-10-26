"""inactive_users.py — «کاربرانِ بدونِ اتصال» برای پنل‌یار (PATCH_PANEL_YAR_IDLE_V1).

کاربرانِ فعال و منقضی‌نشدهٔ یک پنل که بیش از N روز به سرویس وصل نشده‌اند (یا اصلاً وصل نشده‌اند) را همراه با
نام‌کاربری و «کد سرویس» (توکنِ اشتراک؛ نبود ⇒ uuid) فهرست می‌کند.

منبعِ «آخرین اتصال»:
  • مرزبان/ربکا/پاسارگاد/مرزنشین/هیدیفای: خودِ list_clients کلیدِ last_online_ms را می‌دهد (۰ = هرگز وصل نشده).
  • 3X-UI/سنایی و بقیه: اگر list کلید را نداشت، برایِ کاربرانِ فعال get_usage (lastOnline) خوانده می‌شود
    (حداکثر MAX_PROBE نفر، هم‌زمانیِ کم تا پنل فشار نبیند).
  • پنلی که هیچ‌جا زمانِ اتصال نمی‌دهد ⇒ نتیجهٔ «پشتیبانی نمی‌شود» (نه فهرستِ غلطِ «همه بی‌اتصال»).
"""
from __future__ import annotations

import asyncio
import datetime as _dt

DAY_MS = 86_400_000
MAX_PROBE = 400
_PROBE_CONCURRENCY = 6
_BAD_STATUS = ("disabled", "limited", "expired")


def parse_ms(v) -> int:
    """epoch (ثانیه/میلی‌ثانیه) یا رشتهٔ ISO ⇒ میلی‌ثانیه؛ نامعتبر/خالی ⇒ ۰."""
    if v in (None, "", 0, "0"):
        return 0
    if isinstance(v, (int, float)):
        n = int(v)
    else:
        s = str(v).strip()
        try:
            n = int(float(s))
        except ValueError:
            try:
                dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_dt.timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                return 0
    if n <= 0:
        return 0
    return n if n >= 10 ** 11 else n * 1000          # ثانیه ⇒ میلی‌ثانیه


def has_last_online_field(clients: list[dict]) -> bool:
    return any(isinstance(c, dict) and "last_online_ms" in c for c in clients)


def _code(c: dict) -> str:
    for k in ("sub_id", "subId", "uuid", "id"):
        v = c.get(k)
        if v:
            return str(v)
    return "—"


def _name(c: dict) -> str:
    return str(c.get("email") or c.get("username") or "").strip()


def classify(clients: list[dict], now_ms: int, days: int = 2) -> dict:
    """{'rows': [...], 'total': n, 'active': n} — rows مرتب‌شده: هرگز وصل‌نشده‌ها اول، بعد قدیمی‌ترین اتصال."""
    limit = days * DAY_MS
    rows, active = [], 0
    for c in clients:
        if not isinstance(c, dict) or not _name(c):
            continue
        if c.get("enable") is False or str(c.get("status") or "").lower() in _BAD_STATUS:
            continue
        exp = parse_ms(c.get("expiryTime"))
        if exp and exp <= now_ms:
            continue
        active += 1
        last = parse_ms(c.get("last_online_ms"))
        created = parse_ms(c.get("created_ms"))
        if last:
            if now_ms - last <= limit:
                continue
        elif created and now_ms - created <= limit:
            continue                                      # تازه ساخته شده؛ هنوز فرصت نداشته
        rows.append({"name": _name(c), "code": _code(c), "last_ms": last})
    rows.sort(key=lambda r: (r["last_ms"] != 0, r["last_ms"]))
    return {"rows": rows, "total": len(clients), "active": active}


async def probe_last_online(client, clients: list[dict], cap: int = MAX_PROBE) -> tuple[int, int]:
    """برایِ کاربرانِ فعالِ بدونِ کلیدِ last_online_ms، get_usage را می‌خواند و last_online_ms را در dict می‌گذارد.
    خروجی: (تعدادِ بررسی‌شده، تعدادِ ردشده به‌خاطرِ سقف)."""
    todo = [c for c in clients if isinstance(c, dict) and _name(c) and c.get("enable") is not False
            and "last_online_ms" not in c]
    skipped = max(0, len(todo) - cap)
    todo = todo[:cap]
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def one(c: dict) -> None:
        async with sem:
            try:
                u = await client.get_usage(_name(c))
            except Exception:                              # noqa: BLE001 — یک کاربر خراب کلِ گزارش را نمی‌اندازد
                u = None
        c["last_online_ms"] = parse_ms((u or {}).get("last_online_ms")) if isinstance(u, dict) else 0
    await asyncio.gather(*(one(c) for c in todo))
    return len(todo), skipped


def supported(clients: list[dict]) -> bool:
    """بعد از probe: آیا حداقل برایِ یک کاربر زمانِ اتصال به‌دست آمد؟ (وگرنه پنل این داده را نمی‌دهد)"""
    return any(parse_ms(c.get("last_online_ms")) for c in clients if isinstance(c, dict))


def fmt_last(ms: int, now_ms: int) -> str:
    if not ms:
        return "هرگز وصل نشده"
    days = max(0, (now_ms - ms) // DAY_MS)
    return f"{days} روز پیش"


def render_txt(label: str, days: int, res: dict, now_ms: int) -> str:
    lines = [f"{label} — کاربرانِ فعال بدونِ اتصال بیش از {days} روز: {len(res['rows'])} از {res['active']} کاربرِ فعال",
             "", "username | کد سرویس | آخرین اتصال", "-" * 48]
    lines += [f"{r['name']} | {r['code']} | {fmt_last(r['last_ms'], now_ms)}" for r in res["rows"]]
    return "\n".join(lines) + "\n"
