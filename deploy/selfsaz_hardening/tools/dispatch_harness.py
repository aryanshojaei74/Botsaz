"""آزمونِ یکپارچهٔ worker سلف‌ساز با کلاینت ساختگی (به pyrogram نیاز دارد، به تلگرام نه).

  python dispatch_harness.py /مسیر/کد-پچ‌شده  → JSON در stdout

هر دستورِ پنل از همان مسیر واقعیِ _dispatch_saved_command اجرا می‌شود، و سناریوهای «پاسخ عمومی»
(هندلر زنده + پولینگ) با دیالوگ‌های ساختگی. خروجی برای تست خودکار و عیب‌یابی دستی است.
"""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime

code_dir = os.path.abspath(sys.argv[1])
sys.argv = sys.argv[:1]
work = tempfile.mkdtemp()
os.chdir(work)                                          # فایل‌های نسبی worker داخل پوشهٔ موقت
sys.path.insert(0, code_dir)
os.environ.update(SELFSAZ_USER_ID="7", SELFSAZ_API_ID="1", SELFSAZ_API_HASH="abcdef",
                  SELFSAZ_SESSION_STRING="x", SELFSaz_HOME=work)
spec = importlib.util.spec_from_file_location("selfsaz_worker", os.path.join(code_dir, "selfsaz_worker.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["selfsaz_worker"] = m
with contextlib.redirect_stdout(io.StringIO()):
    spec.loader.exec_module(m)


class U:
    def __init__(s, id=7, first="Ali", bot=False):
        s.id, s.first_name, s.is_bot = id, first, bot

    def __getattr__(s, name):
        return None


class Chat:
    def __init__(s, id=7, type=None):
        s.id = id
        s.type = type or m.enums.ChatType.PRIVATE
        s.title = "t"


class Msg:
    def __init__(s, text, id=100, chat=None, sender=None, date=None):
        s.text, s.caption, s.id = text, None, id
        s.chat = chat or Chat()
        s.from_user = sender or U()
        s.date = date or datetime.now()
        s.reply_to_message = s.reply_to_message_id = s.forward_date = s.entities = None
        s.photo = s.video = s.voice = s.document = s.sticker = s.animation = None
        s.outgoing, s.edits, s.replies = True, [], []

    async def edit(s, text, *a, **k): s.edits.append(text); return s
    async def edit_text(s, text, *a, **k): s.edits.append(text); return s
    async def reply(s, text, *a, **k): s.replies.append(text)
    async def reply_text(s, text, *a, **k): s.replies.append(text)
    async def delete(s, *a, **k): pass


class Client:
    def __init__(s):
        s.sent, s.dialogs = [], []

    async def get_me(s): return U()
    async def get_users(s, *a, **k): return U(555, "Target")
    async def get_chat_member(s, *a, **k): return types.SimpleNamespace(status="administrator")

    async def send_message(s, chat_id, text, **k):
        s.sent.append((chat_id, text))
        return Msg(text, chat=Chat(chat_id))

    def get_dialogs(s, limit=100):
        async def gen():
            for d in s.dialogs[:limit]:
                yield d
        return gen()

    def get_chat_history(s, *a, **k):
        async def gen():
            if False:
                yield
        return gen()

    def __getattr__(s, name):
        async def f(*a, **k): return None
        return f


CASES = [
    "پاسخ عمومی تنظیم سلام خوش اومدی", "پاسخ عمومی روشن", "پاسخ عمومی خاموش", "پاسخ افزودن سلام|علیک", "پاسخ لیست",
    "پاسخ حذف سلام", "سلف دشمن لیست", "سلف دشمن افزودن @someone", "سلف دشمن حذف @someone",
    "سلف عملیات بن -1001234567890 @user", "سلف عملیات کیک -1001234567890 @user", "سلف عملیات سکوت -1001234567890 @user",
    "سلف عملیات حذف سکوت -1001234567890 @user", "سلف عملیات پین -1001234567890 12", "سلف عملیات آنپین -1001234567890 12",
    "تنظیم نام علی رضایی", "تنظیم بیو سلام", "ریکت 😊 @someone", "تایم روشن", "تایم خاموش", "تنظیم فونت 2", "لیست فونت",
    "پنل", "آنلاین روشن", "آنلاین خاموش", "گاست", "همه روشن", "همه خاموش", "مدیا روشن", "مدیا خاموش", "استیکر خاموش",
    "فوروارد خاموش", "ویس خاموش", "پیام خاموش", "فایل خاموش", "پیام فردا یادم باشه", "همه چیز خوبه", "ایدی",
    "دشمنان", "لیست دشمن", "پاک کردن دشمنان", "قیمت", "ریکت متن",
]


async def run_cases():
    rows = []
    c = Client()
    for t in CASES:
        msg, buf = Msg(t), io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                handled = await m._dispatch_saved_command(c, msg)
            err = ""
        except Exception as e:
            handled, err = None, f"{type(e).__name__}: {str(e)[:90]}"
        rows.append({"case": t, "handled": handled, "error": err, "out": ((msg.edits + msg.replies)[-1:] or [""])[0][:120]})
    return rows, {k: dict(m.lock_settings)[k] for k in m.lock_settings}


async def reply_scenarios():
    res = {}
    m.general_auto_reply_enabled, m.general_auto_reply_text = True, "سلام، فعلاً در دسترس نیستم"
    m._gr_gate.done.clear(); m._gr_gate.last.clear()
    c = Client()
    old = Msg("old", id=5, chat=Chat(901), sender=U(901), date=datetime.fromtimestamp(time.time() - 3600))
    fresh = Msg("hi", id=9, chat=Chat(902), sender=U(902))
    mine = Msg("my own", id=3, chat=Chat(903), sender=U(7))
    bot = Msg("bot", id=4, chat=Chat(904), sender=U(904, bot=True))
    group = Msg("g", id=2, chat=Chat(905, m.enums.ChatType.GROUP), sender=U(905))
    Dialog = lambda msg: types.SimpleNamespace(chat=msg.chat, top_message=msg)
    c.dialogs = [Dialog(x) for x in (old, fresh, mine, bot, group)]
    ticks = {"n": 0}
    real_sleep = asyncio.sleep

    async def fake_sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:                               # بین دور ۱ و ۲ یک پیام تازه به گفتگوی قدیمی برسد
            new = Msg("new!", id=6, chat=Chat(901), sender=U(901))
            c.dialogs[0] = Dialog(new)
        if ticks["n"] >= 4:
            raise asyncio.CancelledError
        await real_sleep(0)
    m.asyncio.sleep = fake_sleep
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            await m._general_auto_reply_private_poll_loop(c)
    except asyncio.CancelledError:
        pass
    finally:
        m.asyncio.sleep = real_sleep
    res["poll_sent_to"] = sorted(chat for chat, _ in c.sent)
    res["poll_sent_count"] = len(c.sent)

    # هندلر زنده + پولینگ ⇒ فقط یک جواب
    m._gr_gate.done.clear(); m._gr_gate.last.clear()
    c2 = Client()
    live = Msg("hello", id=50, chat=Chat(950), sender=U(950))
    with contextlib.redirect_stdout(io.StringIO()):
        await m.global_message_handler(c2, live)
    c2.dialogs = [Dialog(live)]
    ticks["n"] = 0
    m.asyncio.sleep = fake_sleep
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            await m._general_auto_reply_private_poll_loop(c2)
    except asyncio.CancelledError:
        pass
    finally:
        m.asyncio.sleep = real_sleep
    res["live_plus_poll_sent"] = len(c2.sent)

    # خاموش ⇒ هیچ جوابی
    m.general_auto_reply_enabled = False
    m._gr_gate.done.clear(); m._gr_gate.last.clear()
    c3 = Client()
    c3.dialogs = [Dialog(fresh)]
    ticks["n"] = 0
    m.asyncio.sleep = fake_sleep
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            await m._general_auto_reply_private_poll_loop(c3)
    except asyncio.CancelledError:
        pass
    finally:
        m.asyncio.sleep = real_sleep
    res["disabled_sent"] = len(c3.sent)
    return res


async def main():
    rows, locks = await run_cases()
    scen = await reply_scenarios()
    print(json.dumps({"cases": rows, "locks_after": locks, "reply": scen}, ensure_ascii=False))

asyncio.run(main())
