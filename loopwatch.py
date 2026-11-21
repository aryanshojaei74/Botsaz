"""
loopwatch.py — تشخیصِ «گیرِ رویدادلوپ». فقط لاگ می‌نویسد؛ رفتارِ ربات را تغییر نمی‌دهد.

یک تسکِ ضربان هر ۰٫۲۵ث زمان را ثبت می‌کند و یک thread جدا چک می‌کند؛ اگر لوپ بیش از LOOPWATCH_MS (پیش‌فرض ۸۰۰ms)
نَتپیده باشد، stackِ فعلیِ thread اصلی (یعنی دقیقاً کدی که لوپ را بسته) را یک‌بار در لاگ می‌گذارد:
    event loop blocked 1.8s [pid=…] stack: …
و بعد از رفع، «event loop unblocked after X.Xs». خاموش: LOOPWATCH=0
"""
import asyncio
import logging
import os
import sys
import threading
import time
import traceback

logger = logging.getLogger("loopwatch")
_started = False


def format_stack(frame, tail: int = 6, project: int = 4) -> str:
    """آخرین `tail` فریم + آخرین `project` فریمِ *کدِ خودمان* (نه site-packages).

    وقتی لوپ داخلِ SQLAlchemy/aiohttp گیر کرده، شش فریمِ آخر همه کتابخانه‌اند و معلوم
    نیست کدام تابعِ ما آن را صدا زده؛ فریم‌هایِ پروژه‌ای این را روشن می‌کنند."""
    frames = traceback.extract_stack(frame)
    last = traceback.format_list(frames[-tail:])
    mine = [f for f in frames if "site-packages" not in f.filename and "/lib/python" not in f.filename][-project:]
    if not mine:
        return "".join(last)
    return "".join(last) + "  ── کدِ پروژه ──\n" + "".join(traceback.format_list(mine))


def start(name: str = "") -> None:
    global _started
    if _started or os.getenv("LOOPWATCH", "1") == "0":
        return
    _started = True
    limit = float(os.getenv("LOOPWATCH_MS", "800")) / 1000.0
    main_id = threading.main_thread().ident
    state = {"beat": time.monotonic(), "reported": False}

    async def _beat():
        while True:
            state["beat"] = time.monotonic()
            await asyncio.sleep(0.25)

    loop = asyncio.get_running_loop()

    def _dog():
        while True:
            time.sleep(0.25)
            if loop.is_closed():
                return
            if not loop.is_running():
                continue
            lag = time.monotonic() - state["beat"]
            if lag > limit and not state["reported"]:
                state["reported"] = True
                try:
                    fr = sys._current_frames().get(main_id)
                    stack = format_stack(fr) if fr else "?"
                except Exception as e:  # noqa: BLE001
                    stack = f"({type(e).__name__})"
                logger.warning("event loop blocked %.1fs [%s pid=%s] stack:\n%s", lag, name, os.getpid(), stack)
            elif state["reported"] and lag <= limit:
                state["reported"] = False
                logger.warning("event loop unblocked [%s pid=%s]", name, os.getpid())

    loop.create_task(_beat())
    threading.Thread(target=_dog, name="loopwatch", daemon=True).start()
