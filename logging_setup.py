"""
پیکربندی مرکزیِ logging برای کل پروژه.

قبلاً بیشترِ خطایابی‌ها و پیام‌های وضعیت با print() خام چاپ می‌شدند که:
  - نه سطح‌بندی داشتند (INFO/WARNING/ERROR)
  - نه زمان دقیق ثبت می‌شد
  - و بسته به نحوهٔ اجرا (systemd/uvicorn) ممکن بود فقط توی journal گم شوند،
    بدون اینکه در error.log (که logrotate هم رویش تنظیم شده) ثبت شوند.

این ماژول:
  1) یک‌بار در ابتدای اجرا (از config.py) صدا زده می‌شود و root logger را
     با دو handler پیکربندی می‌کند: کنسول (stdout, سطح INFO به بالا) و
     فایل error.log (سطح WARNING به بالا؛ همان فایلی که botsaz-logrotate.conf
     مدیریتش می‌کند).
  2) get_logger(name) یک logging.Logger معمولی برمی‌گرداند.
  3) PrintLogger کلاسی است که امضای print() را تقلید می‌کند (args, sep, end,
     flush) تا بشود در هر فایل با یک خط «print = PrintLogger(__name__)»،
     همهٔ فراخوانی‌های print() موجود را — بدون تغییر تک‌تک‌شان — از مسیر
     logging عبور داد.
"""
from __future__ import annotations

import logging
import os
import sys

_LOG_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_FILE = os.path.join(_LOG_DIR, "error.log")

_configured = False


# === PATCH_BENIGN_TG_ERRORS_V1 ===
# لاگِ سرور: «message is not modified» (۱۰۴ بار ERROR با traceback کامل در ۲۴ ساعت)،
# «message to edit not found» و «message can't be deleted» خطای واقعی نیستند — وقتی
# کاربر دوبار پشت‌سرهم دکمه را می‌زند یا پیام قبلاً پاک شده، تلگرام همین را می‌گوید.
# این رکوردها دیگر error.log را پر نمی‌کنند تا خطاهای واقعی دیده شوند.
_BENIGN_TG = ("message is not modified", "message to edit not found",
              "message can't be deleted", "message to delete not found")


class _BenignTelegramFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ei = record.exc_info
        if ei and ei[1] is not None:
            msg = str(ei[1]).lower()
            if any(s in msg for s in _BENIGN_TG):
                return False
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """پیکربندی root logger. اگر قبلاً پیکربندی شده باشد، کاری نمی‌کند
    (idempotent) تا در صورت import شدنِ چندبارهٔ config در تست‌ها یا
    worker های مختلف، handler تکراری اضافه نشود."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
        file_handler.setLevel(logging.WARNING)
        file_handler.setFormatter(fmt)
        file_handler.addFilter(_BenignTelegramFilter())
        root.addHandler(file_handler)
    except OSError:
        # اگر مسیر فایل قابل نوشتن نبود (مثلاً در محیط تست)، فقط از کنسول
        # لاگ می‌گیریم؛ نباید کل برنامه به‌خاطر این fail کند.
        pass

    # کتابخانه‌های پرحرف را کمی ساکت‌تر می‌کنیم تا لاگِ خودِ برنامه گم نشود.
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class PrintLogger:
    """جایگزینِ print() که همان امضای print(*args, sep=" ", end="\\n",
    flush=False) را می‌پذیرد ولی به‌جای نوشتن مستقیم روی stdout، پیام را
    با سطح INFO از logging عبور می‌دهد.

    استفاده (در ابتدای هر فایل، بعد از importهای دیگر):
        from logging_setup import get_logger, PrintLogger
        logger = get_logger(__name__)
        print = PrintLogger(__name__)   # شادو کردنِ عمدیِ builtin در همین ماژول
    """

    __slots__ = ("_logger",)

    def __init__(self, name: str):
        self._logger = logging.getLogger(name)

    def __call__(self, *args, sep: str = " ", end: str = "\n",
                 file=None, flush: bool = False) -> None:
        msg = sep.join(str(a) for a in args)
        self._logger.info(msg)
