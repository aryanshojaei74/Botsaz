"""
server_ip.py — «IP سرور» برای ثبت در پنل درگاه‌های پرداخت (=== PATCH_GATEWAY_SERVER_IP_V1 ===).

زرین‌پال موقع ساخت درگاه «آی‌پی سرور» (۱ تا ۵ آی‌پی) می‌خواهد و فقط درخواست‌هایی را
می‌پذیرد که از همین آی‌پی‌ها بیایند؛ وگرنه خطای «Terminal is not valid, please check
merchant_id or ip address» (کد -10) می‌دهد. درخواست پرداخت همهٔ ربات‌ها از سرور بات‌ساز
ارسال می‌شود، پس مالک ربات باید آی‌پی خروجی همین سرور را در پنل زرین‌پال ثبت کند.
تا امروز هیچ‌جای ربات این آی‌پی را نشان نمی‌داد و مالک‌ها از پشتیبانی می‌پرسیدند.

منبع آی‌پی، به ترتیب:
  ۱) Setting(bot_id=0, key="payment_server_ips") یا متغیر محیطی PAYMENT_SERVER_IPS
     (با کاما جدا شود؛ برای وقتی که ترافیک خروجی از NAT/پروکسی می‌گذرد یا چند سرور هست)
  ۲) آی‌پی خروجی واقعی که یک سرویس «آی‌پی من چیست» می‌بیند (فقط IPv4)
  ۳) آی‌پی(های) رکورد A دامنهٔ پلتفرم (حدسی؛ همان که برای DNS دامنهٔ اختصاصی نشان داده می‌شود)
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import time

from sqlalchemy import select

from config import Session
from models import Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

MAX_IPS = 5                                    # سقف زرین‌پال
_ECHO_URLS = ("https://api.ipify.org", "https://ipv4.icanhazip.com", "https://ifconfig.me/ip")
_OK_TTL, _FAIL_TTL = 600.0, 60.0               # موفق ۱۰ دقیقه، ناموفق ۱ دقیقه کش می‌شود
_CACHE: dict = {"until": 0.0, "v": ([], "")}


def clean_ips(raw, public_only: bool = False) -> list[str]:
    """«1.2.3.4, 5.6.7.8،bad» → فقط IPهای معتبر، یکتا، حداکثر ۵ تا (public_only: بدون خصوصی/رزروشده)."""
    out: list[str] = []
    text = str(raw or "")
    for ch in ("،", ";", "\n", "\t", " "):
        text = text.replace(ch, ",")
    for part in text.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            ip = ipaddress.ip_address(p)
        except ValueError:
            continue
        if public_only and not ip.is_global:
            continue
        if str(ip) not in out:
            out.append(str(ip))
    return out[:MAX_IPS]


async def _override() -> list[str]:
    try:
        async with Session() as s:
            row = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == "payment_server_ips"))
        ips = clean_ips(row.value if row else "")
    except Exception:
        logger.exception("server_ip: خواندن تنظیم payment_server_ips ناموفق بود")
        ips = []
    return ips or clean_ips(os.getenv("PAYMENT_SERVER_IPS", ""))


async def _detect() -> str:
    """آی‌پی IPv4 عمومی خروجی همین سرور؛ خالی اگر هیچ سرویسی جواب نداد."""
    try:
        import aiohttp
    except Exception:
        return ""
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(family=socket.AF_INET),
                                         timeout=aiohttp.ClientTimeout(total=3)) as s:
            for url in _ECHO_URLS:
                try:
                    async with s.get(url) as r:
                        got = clean_ips((await r.text()).strip(), public_only=True)
                    if got:
                        return got[0]
                except Exception as e:
                    logger.info("server_ip: %s ناموفق بود (%s)", url, type(e).__name__)
    except Exception:
        logger.exception("server_ip: تشخیص آی‌پی خروجی ناموفق بود")
    return ""


async def _dns_guess() -> list[str]:
    try:
        from tenant.gateways.custom_domain import get_dns_target
        _host, ips = await get_dns_target()
        return clean_ips(",".join(ips), public_only=True)
    except Exception:
        logger.exception("server_ip: خواندن آی‌پی از دامنهٔ پلتفرم ناموفق بود")
        return []


async def get_server_ips(force: bool = False) -> tuple[list[str], str]:
    """(آی‌پی‌ها, منبع) — منبع: "override" | "detected" | "dns" | "" (هیچ‌کدام)."""
    now = time.monotonic()
    if not force and _CACHE["until"] > now:
        return _CACHE["v"]
    ips, src = await _override(), "override"
    if not ips:
        try:
            ip = await asyncio.wait_for(_detect(), timeout=6)   # صفحهٔ تنظیمات نباید معطل بماند
        except (asyncio.TimeoutError, TimeoutError):
            logger.info("server_ip: تشخیص آی‌پی خروجی از مهلت گذشت")
            ip = ""
        ips, src = ([ip], "detected") if ip else ([], "")
    if not ips:
        ips, src = await _dns_guess(), "dns"
    if not ips:
        src = ""
    _CACHE["until"] = now + (_OK_TTL if ips else _FAIL_TTL)
    _CACHE["v"] = (ips, src)
    return ips, src


def ip_line(ips: list[str], src: str, gateway: str = "زرین‌پال") -> str:
    """یک خط HTML برای صفحهٔ تنظیم درگاه."""
    head = f"🌐 IP سرور (برای ثبت در پنل {gateway}): "
    if not ips:
        return head + "⚠️ الان خوانده نشد؛ از پشتیبانی بپرس"
    tail = " <i>(حدسی؛ اگر خطای IP گرفتی از پشتیبانی بپرس)</i>" if src == "dns" else ""
    return head + "، ".join(f"<code>{ip}</code>" for ip in ips) + tail


_IP_ERROR_RE = re.compile(r"(?<![a-z])ip(?![a-z])|آی‌?پی|ای\s?پی|ایپی|آیپی|terminal is not valid", re.I)


def is_ip_error(text) -> bool:
    """پیام خطای زرین‌پال مربوط به آی‌پی/ترمینال نامعتبر است؟ (کد -10)"""
    return bool(_IP_ERROR_RE.search(str(text or "")))
