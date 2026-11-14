"""
perfctx.py — «زمان کجا خرج شد؟» برایِ هر آپدیتِ تلگرام. فقط اندازه می‌گیرد و رفتار را تغییر نمی‌دهد.

هر آپدیت (در main_tenant._process_tenant) یک شمارنده می‌گیرد؛ سه دسته جمع می‌شود:
    tg   = تماس با تلگرام (با retry و تایم‌اوت)
    db   = اجرایِ کوئری‌هایِ دیتابیس
    http = تماس با سرویس‌هایِ بیرونی (پنل VPN، درگاه، API…؛ بدونِ تلگرام)
    wait = انتظارِ عمدیِ خودِ کد (مثلاً محدودکنندهٔ لاگینِ پنل یا صفِ قفل)؛ با نامِ هاست
و در لاگِ «slow update» چاپ می‌شود:  | tg 4×9.8s(max 9.1s SendMessage) db 31×0.4s http 2×1.2s(max 0.9s panel.x.com)
هرچه از کلِ زمان کم بماند یعنی انتظار برایِ CPU/لوپ یا sleep.  خاموش: PERFCTX=0
"""
import contextvars
import os
import time

_acc: contextvars.ContextVar = contextvars.ContextVar("botsaz_perf_acc", default=None)
_installed = {"http": False, "db": set()}


def begin() -> dict:
    d = {"tg": [0, 0.0, 0.0, ""], "db": [0, 0.0, 0.0, ""], "http": [0, 0.0, 0.0, ""], "wait": [0, 0.0, 0.0, ""]}
    _acc.set(d)
    return d


def add(kind: str, dur: float, label: str = "") -> None:
    d = _acc.get()
    if d is None:
        return
    e = d[kind]
    e[0] += 1
    e[1] += dur
    if dur > e[2]:
        e[2] = dur
        e[3] = label


def fmt(d: dict | None) -> str:
    if not d:
        return ""
    out = []
    for k in ("tg", "db", "http", "wait"):
        n, tot, mx, lab = d[k]
        if not n:
            continue
        s = f"{k} {n}×{tot:.1f}s"
        if mx >= 1.0:
            s += f"(max {mx:.1f}s {lab})"[:60]
        out.append(s)
    return " ".join(out) or "بدونِ تماسِ خارجی/دیتابیس"


def install_http() -> None:
    """تماس‌هایِ aiohttp غیرِ تلگرام را می‌شمارد (تلگرام جداگانه در make_request شمرده می‌شود)."""
    if _installed["http"] or os.getenv("PERFCTX", "1") == "0":
        return
    import aiohttp
    orig = aiohttp.ClientSession._request

    async def _timed(self, method, str_or_url, *a, **kw):
        if _acc.get() is None:
            return await orig(self, method, str_or_url, *a, **kw)
        t = time.monotonic()
        try:
            return await orig(self, method, str_or_url, *a, **kw)
        finally:
            host = str(getattr(str_or_url, "host", None) or str(str_or_url).split("/")[2:3] or "")
            if "telegram.org" not in host:
                add("http", time.monotonic() - t, host[:30])

    aiohttp.ClientSession._request = _timed

    # زمانِ خواندنِ «بدنهٔ» پاسخ هم بخشی از انتظار است (هدر زود می‌رسد ولی بدنه کند می‌آید)؛
    # بدونِ این، یک پنلِ کندِ بدنه در شکستِ زمان «غایب» دیده می‌شد.
    orig_read = aiohttp.ClientResponse.read

    async def _read_timed(self):
        if _acc.get() is None:
            return await orig_read(self)
        t = time.monotonic()
        try:
            return await orig_read(self)
        finally:
            host = str(getattr(getattr(self, "url", None), "host", "") or "")
            if "telegram.org" not in host:
                dur = time.monotonic() - t
                e = _acc.get()["http"]
                e[1] += dur            # فقط به زمانِ http اضافه می‌شود؛ تعدادِ تماس دوباره شمرده نمی‌شود
                if dur > e[2]:
                    e[2] = dur
                    e[3] = (host[:24] + " body")

    aiohttp.ClientResponse.read = _read_timed
    _installed["http"] = True


def install_db(engine) -> None:
    if os.getenv("PERFCTX", "1") == "0" or id(engine) in _installed["db"]:
        return
    from sqlalchemy import event
    se = engine.sync_engine
    _installed["db"].add(id(engine))

    @event.listens_for(se, "before_cursor_execute")
    def _b(conn, cursor, statement, parameters, context, executemany):
        if _acc.get() is not None:
            conn.info["_pf_t"] = time.monotonic()

    @event.listens_for(se, "after_cursor_execute")
    def _a(conn, cursor, statement, parameters, context, executemany):
        t = conn.info.pop("_pf_t", None)
        if t is not None:
            add("db", time.monotonic() - t, " ".join(statement.split())[:24])
