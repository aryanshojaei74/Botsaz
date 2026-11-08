"""hosting_disk_guard.py — سقفِ فضا و پاکسازیِ خودکارِ فایل‌هایِ ربات‌هایِ «سرور فعال‌سازی» (PATCH_HOSTING_DISK_GUARD_V1).

دو کار (هر ساعت با deploy/botsaz-disk-guard.timer):
  ۱) پاک‌کردنِ فایل‌هایِ دانلودی/آپلودیِ قدیمی: فقط داخلِ پوشه‌هایی که نامشان در فهرستِ «پوشه‌هایِ موقت» است (downloads، uploads، tmp، cache، …)
     و فقط فایل‌هایی که بیش از N روز (پیش‌فرض ۷) تغییر نکرده‌اند. کدِ پروژه، .env، venv و node_modules هرگز دست نمی‌خورد.
  ۲) سقفِ فضا برایِ هر اپ (پیش‌فرض ۵ گیگ): بعد از پاکسازی اگر هنوز بیشتر بود ⇒ هشدارِ مالک و (طبقِ تنظیم) خاموش‌کردنِ کانتینر.
     هشدارِ زودهنگام در ۸۰٪ سقف. گزارش در تنظیماتِ پلتفرم (hosting_disk_report) می‌نشیند و در پنلِ مدیر نمایش داده می‌شود.

  ۳) کشِ build و ایمیج‌هایِ بی‌صاحبِ داکر (علتِ اصلیِ پُرشدنِ دیسک در عمل: هر build چند صد مگ کش می‌گذارد): هر ساعت
     `docker builder prune --filter until=48h` و `docker image prune` (فقط dangling). کانتینر/volumeهایِ در حالِ‌استفاده دست‌نخورده.

تنظیمات (Setting با bot_id=0): hdg_enabled=1 · hdg_cap_mb=5120 · hdg_days=7 · hdg_clean=1 · hdg_action=stop|warn · hdg_dirs=…
  · hdg_docker=1 · hdg_docker_hours=48
اجرای دستی:  python -m hosting_disk_guard [--dry-run]
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from sqlalchemy import select

import config
from models import HostedApp, Setting

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

APPS_ROOT = Path(os.getenv("HOSTING_APPS_DIR", "/opt/botsazvip/apps"))
CONTAINER_PREFIX = "bsapp-"
DEFAULTS = {"hdg_enabled": "1", "hdg_cap_mb": "5120", "hdg_days": "7", "hdg_clean": "1", "hdg_action": "stop",
            "hdg_dirs": "downloads,download,uploads,upload,tmp,temp,cache,.cache,temporary",
            "hdg_docker": "1", "hdg_docker_hours": "48"}
PROTECTED = {"venv", ".venv", "node_modules", ".git", "site-packages", "__pycache__"}
WARN_AT = 0.8
WARN_EVERY_SEC = 24 * 3600
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
REPORT_KEY = "hosting_disk_report"


# ───────────────────────── تنظیمات ─────────────────────────
async def get_cfg() -> dict:
    out = dict(DEFAULTS)
    async with config.Session() as s:
        rows = (await s.scalars(select(Setting).where(Setting.bot_id == 0, Setting.key.in_(list(DEFAULTS))))).all()
    for r in rows:
        if r.value not in (None, ""):
            out[r.key] = r.value
    try:
        cap = max(1, int(float(out["hdg_cap_mb"])))
    except (TypeError, ValueError):
        cap = 5120
    try:
        days = max(1, int(float(out["hdg_days"])))
    except (TypeError, ValueError):
        days = 7
    names = {x.strip().lower() for x in str(out["hdg_dirs"]).split(",") if x.strip()}
    try:
        dh = max(1, int(float(out["hdg_docker_hours"])))
    except (TypeError, ValueError):
        dh = 48
    return {"docker": out["hdg_docker"] != "0", "docker_hours": dh, "enabled": out["hdg_enabled"] != "0", "cap_bytes": cap * 1024 * 1024, "cap_mb": cap, "days": days,
            "clean": out["hdg_clean"] != "0", "action": "warn" if out["hdg_action"] == "warn" else "stop", "dirs": names}


async def set_cfg(key: str, value: str) -> None:
    async with config.Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == key))
        if r:
            r.value = value
        else:
            s.add(Setting(bot_id=0, key=key, value=value))
        await s.commit()


async def load_report() -> dict | None:
    async with config.Session() as s:
        r = await s.scalar(select(Setting).where(Setting.bot_id == 0, Setting.key == REPORT_KEY))
    try:
        return json.loads(r.value) if r and r.value else None
    except ValueError:
        return None


async def _save_report(rep: dict) -> None:
    await set_cfg(REPORT_KEY, json.dumps(rep, ensure_ascii=False))


# ───────────────────────── فایل‌سیستم ─────────────────────────
def dir_size(path: Path) -> int:
    """مجموعِ حجمِ فایل‌ها (بدونِ دنبال‌کردنِ symlink)."""
    total = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def clean_old(app_dir: Path, days: int, names: set[str], *, dry_run: bool = False, now: float | None = None) -> tuple[int, int]:
    """فایل‌هایِ قدیمیِ پوشه‌هایِ موقت را پاک می‌کند. (تعداد، بایت). dry_run ⇒ فقط می‌شمارد."""
    cutoff = (now if now is not None else time.time()) - days * 86400
    count = freed = 0
    root = str(app_dir)
    for cur, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = [d for d in dirs if d.lower() not in PROTECTED]
        in_temp = any(part.lower() in names for part in Path(cur).relative_to(root).parts)
        if not in_temp:
            continue
        for f in files:
            p = os.path.join(cur, f)
            try:
                st = os.lstat(p)
                if not os.path.isfile(p) or os.path.islink(p) or st.st_mtime > cutoff:
                    continue
                if not dry_run:
                    os.unlink(p)
                count += 1
                freed += st.st_size
            except OSError:
                continue
    if not dry_run:                                   # پوشه‌هایِ خالیِ ساخته‌شده توسطِ فایل‌هایِ پاک‌شده (خودِ پوشهٔ موقت می‌ماند)
        for cur, dirs, files in os.walk(root, topdown=False, followlinks=False):
            if Path(cur).name.lower() in PROTECTED or Path(cur) == app_dir or Path(cur).name.lower() in names:
                continue
            try:
                if not os.listdir(cur):
                    os.rmdir(cur)
            except OSError:
                continue
    return count, freed


async def _run(*cmd: str) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), timeout=60)
    return p.returncode or 0, ((out or b"") + (err or b"")).decode(errors="replace")


def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ───────────────────────── کشِ داکر ─────────────────────────
_RECLAIMED_RE = re.compile(r"Total reclaimed space:\s*([\d.]+\s*[KMGT]?i?B)", re.I)


async def docker_prune(run, hours: int = 48) -> dict:
    """کشِ build (قدیمیِ‌تر از hours ساعت) و ایمیج‌هایِ dangling را پاک می‌کند. خروجی: {'builder': '25.2GB', 'images': '0B'} (خطا ⇒ 'خطا')."""
    out = {}
    for key, cmd in (("builder", ("docker", "builder", "prune", "-f", "--filter", f"until={int(hours)}h")),
                     ("images", ("docker", "image", "prune", "-f"))):
        try:
            code, text = await run(*cmd)
        except Exception:      # noqa: BLE001
            logger.exception("hdg: docker prune (%s) ناموفق", key)
            out[key] = "خطا"
            continue
        m = _RECLAIMED_RE.search(text or "")
        out[key] = m.group(1).replace(" ", "") if m else ("خطا" if code else "0B")
    return out


# ───────────────────────── اسکن و اعمال ─────────────────────────
async def _notify(bot, chat_id, text: str) -> None:
    if bot is None or not chat_id:
        return
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:      # noqa: BLE001
        logger.warning("hdg: اطلاع به %s ممکن نشد", chat_id, exc_info=True)


async def run_once(*, dry_run: bool = False, bot=None, run=None, now: float | None = None) -> dict:
    """یک دور: پاکسازی ⇒ اندازه‌گیری ⇒ هشدار/خاموشی. گزارش را ذخیره و برمی‌گرداند."""
    cfg = await get_cfg()
    run = run or _run
    now = now if now is not None else time.time()
    prev = await load_report() or {}
    prev_warn = {a["name"]: a.get("warned_at", 0) for a in prev.get("apps", []) if isinstance(a, dict)}
    async with config.Session() as s:
        apps = (await s.scalars(select(HostedApp).order_by(HostedApp.id))).all()
    by_name = {a.name: a for a in apps}
    rows, orphans = [], []
    summary_lines = []
    for name in sorted(p.name for p in APPS_ROOT.iterdir() if p.is_dir()) if APPS_ROOT.is_dir() else []:
        path = APPS_ROOT / name
        a = by_name.get(name)
        if not _NAME_RE.match(name) or path.resolve().parent != APPS_ROOT.resolve():
            continue
        cleaned_n = cleaned_b = 0
        if cfg["enabled"] and cfg["clean"]:
            cleaned_n, cleaned_b = await asyncio.to_thread(clean_old, path, cfg["days"], cfg["dirs"], dry_run=dry_run, now=now)
        size = await asyncio.to_thread(dir_size, path)
        if dry_run:
            size = max(0, size)                       # dry-run: اندازهٔ فعلی؛ cleaned_b «قابلِ آزادسازی» است
        row = {"name": name, "id": a.id if a else None, "owner_id": a.owner_id if a else None,
               "status": a.status if a else "orphan", "bytes": size, "cleaned_files": cleaned_n, "cleaned_bytes": cleaned_b,
               "over": size > cfg["cap_bytes"], "warned_at": prev_warn.get(name, 0), "stopped": False}
        if a is None:
            orphans.append(name)
        elif cfg["enabled"] and not dry_run:
            if size > cfg["cap_bytes"]:
                msg = (f"⛔️ فضای اپِ <b>{name}</b> ({fmt_bytes(size)}) از سقفِ {fmt_bytes(cfg['cap_bytes'])} گذشته؛ "
                       "فایل‌هایِ دانلودی/آپلودیِ قدیمی پاک شد ولی هنوز زیادِ است. ")
                if cfg["action"] == "stop" and a.status in ("running", "building"):
                    code, out = await run("docker", "stop", f"{CONTAINER_PREFIX}{name}")
                    if code == 0:
                        row["stopped"] = True
                        async with config.Session() as s:
                            dbo = await s.get(HostedApp, a.id)
                            if dbo:
                                dbo.status = "stopped"
                                dbo.last_error = f"فضایِ اپ ({fmt_bytes(size)}) از سقفِ {fmt_bytes(cfg['cap_bytes'])} گذشت؛ فایل‌هایِ اضافه را پاک کن و دوباره روشنش کن."
                                await s.commit()
                        msg += "ربات خاموش شد؛ فایل‌هایِ اضافه را از «فایل‌هایِ پروژه» پاک کن و دوباره روشنش کن."
                    else:
                        msg += f"(خاموش‌کردن ممکن نشد: {out[-120:]})"
                else:
                    msg += "لطفاً فایل‌هایِ اضافه را پاک کن."
                if now - row["warned_at"] > WARN_EVERY_SEC or row["stopped"]:
                    await _notify(bot, a.owner_id, msg)
                    await _notify(bot, config.PLATFORM_ADMIN_ID, f"💽 {msg}")
                    row["warned_at"] = now
                summary_lines.append(f"{name}: {fmt_bytes(size)} {'(خاموش شد)' if row['stopped'] else '(بالای سقف)'}")
            elif size > cfg["cap_bytes"] * WARN_AT and now - row["warned_at"] > WARN_EVERY_SEC:
                await _notify(bot, a.owner_id, f"⚠️ فضایِ اپِ <b>{name}</b> به {fmt_bytes(size)} از سقفِ {fmt_bytes(cfg['cap_bytes'])} رسیده "
                                                f"(بیش از {int(WARN_AT * 100)}٪). فایل‌هایِ اضافه را پاک کن؛ وگرنه بعد از سقف خاموش می‌شود.")
                row["warned_at"] = now
        rows.append(row)
    rows.sort(key=lambda r: -r["bytes"])
    docker_res = None
    if cfg["enabled"] and cfg["docker"] and not dry_run:
        docker_res = await docker_prune(run, cfg["docker_hours"])
    try:
        du = shutil.disk_usage("/")
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = {}
    rep = {"at": dt.datetime.fromtimestamp(now).isoformat(timespec="seconds"), "dry_run": dry_run, "cap_bytes": cfg["cap_bytes"],
           "days": cfg["days"], "action": cfg["action"], "clean": cfg["clean"], "enabled": cfg["enabled"], "disk": disk,
           "apps": rows, "orphans": orphans, "docker": docker_res,
           "freed_bytes": sum(r["cleaned_bytes"] for r in rows) if not dry_run else 0,
           "reclaimable_bytes": sum(r["cleaned_bytes"] for r in rows) if dry_run else 0}
    if not dry_run:
        await _save_report(rep)
    if summary_lines and not dry_run:
        logger.warning("hdg: " + " | ".join(summary_lines))
    return rep


def report_text(rep: dict | None, limit: int = 12) -> str:
    if not rep:
        return "هنوز اسکنی انجام نشده. «🧹 اسکن و پاکسازی» را بزن."
    d = rep.get("disk") or {}
    lines = []
    if d.get("total"):
        pct = int(d["used"] * 100 / d["total"])
        lines.append(f"💽 دیسکِ سرور: {fmt_bytes(d['used'])} از {fmt_bytes(d['total'])} ({pct}٪) — آزاد: {fmt_bytes(d['free'])}")
    apps = rep.get("apps", [])
    tot = sum(a["bytes"] for a in apps)
    lines.append(f"📦 جمعِ اپ‌هایِ میزبانی: {fmt_bytes(tot)} در {len(apps)} اپ · سقفِ هر اپ: {fmt_bytes(rep.get('cap_bytes', 0))}")
    dk = rep.get("docker")
    if dk:
        lines.append(f"🐳 آخرین پاکسازیِ داکر — کشِ build: {dk.get('builder', '—')} · ایمیج‌هایِ بی‌صاحب: {dk.get('images', '—')}")
    if rep.get("dry_run"):
        lines.append(f"🔍 (فقط اسکن) قابلِ آزادسازی با پاکسازی: {fmt_bytes(rep.get('reclaimable_bytes', 0))}")
    elif rep.get("freed_bytes"):
        lines.append(f"🧹 آخرین پاکسازی: {fmt_bytes(rep['freed_bytes'])} آزاد شد")
    lines.append("")
    for a in apps[:limit]:
        flag = "⛔️" if a.get("stopped") else ("🔴" if a.get("over") else ("🟠" if a["bytes"] > rep.get("cap_bytes", 1) * WARN_AT else "▫️"))
        lines.append(f"{flag} <code>{a['name']}</code> — {fmt_bytes(a['bytes'])}"
                     + (f" (+{fmt_bytes(a['cleaned_bytes'])} پاک شد)" if a.get("cleaned_bytes") and not rep.get("dry_run") else "")
                     + (f" · مالک {a['owner_id']}" if a.get("owner_id") else ""))
    if rep.get("orphans"):
        lines.append("\n❔ پوشه‌هایِ بدونِ اپ در DB: " + "، ".join(rep["orphans"][:8]))
    lines.append(f"\n🕒 {rep.get('at', '')}")
    return "\n".join(lines)


async def _amain(dry: bool) -> None:
    bot = None
    try:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        if config.PLATFORM_BOT_TOKEN:
            bot = Bot(token=config.PLATFORM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except Exception:      # noqa: BLE001
        logger.exception("hdg: ساختِ ربات برایِ اطلاع‌رسانی ناموفق؛ بدونِ پیام ادامه می‌دهم")
    try:
        rep = await run_once(dry_run=dry, bot=bot)
        print(report_text(rep))
    finally:
        if bot is not None:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(_amain("--dry-run" in sys.argv))
