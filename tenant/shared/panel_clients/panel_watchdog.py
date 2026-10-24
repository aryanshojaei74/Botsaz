"""
panel_watchdog.py — هشدارِ سریع به مالکِ ربات وقتی اتصالِ یک پنلِ VPN قطع
(یا بعد از قطعی، وصل) می‌شود.

=== PATCH_PANEL_WATCHDOG_V1 ===
درخواستِ ادمین: «اگه پنلی به‌صورتِ اتفاقی یا توسطِ ادمینِ فرعی قطع شد،
حتماً سریع یه پیام تو ربات برایِ ادمین بره». panel_autosync.py برایِ این کار
مناسب نبود چون فقط پنل‌هایی که auto_sync=True دارن رو، هر ۲ ساعت، بدونِ
هیچ پیامی به کسی چک می‌کنه.

این ماژول همه‌ی پنل‌هایِ enabled=True را هر چند دقیقه با همان
test_connection() که خودِ ادمین موقعِ تنظیمِ پنل استفاده می‌کند چک می‌کند
(از panel_pool برایِ سرعت استفاده می‌شود)، و فقط در لحظه‌ی تغییرِ وضعیت —
سالم→قطع یا قطع→سالم — به مالکِ ربات پیام می‌دهد، نه هر دور، که اسپم نشود.
"""
from __future__ import annotations

import html as _html
import asyncio
import json
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import Session
from models import Bot as BotRow, Setting, VpnPanel

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

_LOOP_INTERVAL_SEC = 300          # هر ۵ دقیقه
_BETWEEN_PANELS_SLEEP = 1.5       # فشار نیاوردن به پنل‌ها/دیتابیس

# === PATCH_PANEL_WATCHDOG_FLAP_V2 ===
# گزارشِ ادمین: «همیشه میاد» — پیامِ قطع/وصل مدام تکرار می‌شد.
# علت: نسخهٔ اول به‌محضِ *یک* شکستِ چک هشدار می‌داد. یک تایم‌اوتِ گذرا،
# ری‌استارتِ کوتاهِ پنل، یا کندیِ لحظه‌ایِ شبکه کافی بود تا وضعیت عوض شود
# و دورِ بعد دوباره برگردد — یعنی هر ۵ دقیقه یک جفت پیامِ بی‌فایده.
#
# حالا هشدار فقط وقتی می‌رود که قطعی هم *به‌اندازهٔ کافی طول کشیده باشد*
# و هم *چند بارِ پیاپی* تکرار شده باشد. پیامِ «وصل شد» هم فقط وقتی
# فرستاده می‌شود که قبلاً واقعاً هشدارِ قطعی رفته باشد — وگرنه کاربر
# پیامِ «وصل شد» می‌گیرد بدونِ اینکه اصلاً بداند قطع بوده.
_DOWN_ALERT_AFTER_SEC = 20 * 60   # ۲۰ دقیقه قطعیِ پیوسته
_DOWN_ALERT_MIN_FAILS = 3         # و دستِ‌کم ۳ چکِ پیاپیِ ناموفق

# خطاهایی که یعنی «پیکربندی غلط است»، نه «سرور موقتاً در دسترس نیست».
# این‌ها با گذرِ زمان خودبه‌خود درست نمی‌شوند، پس منتظرِ آستانهٔ ۲۰ دقیقه
# نمی‌مانیم و همان بارِ اول (بعد از حداقلِ تکرار) خبر می‌دهیم.
_CONFIG_ERROR_HINTS = ("نام کاربری", "رمز", "پسورد", "unauthorized", "forbidden",
                       "401", "403", "لاگین", "login")


def _looks_like_config_error(err: str) -> bool:
    e = (err or "").lower()
    return any(h.lower() in e for h in _CONFIG_ERROR_HINTS)


async def _check_panel_once(panel_id: int) -> None:
    async with Session() as s:
        p = await s.get(VpnPanel, panel_id)
        if not p or not p.enabled:
            return
        # === PATCH_PANEL_WATCHDOG_FLAP_V2 ===
        # همهٔ چیزهایِ لازم همین‌جا کپی می‌شوند؛ نسخهٔ قبلی بعد از بسته‌شدنِ
        # سشن هنوز به p.base_url و … دست می‌زد که رویِ یک آبجکتِ detached
        # شکننده است.
        bot_id = p.bot_id
        panel_key = p.panel_key
        base_url, username, password = p.base_url, p.username, p.password
        sub_base, inbound_ids, api_key = (p.sub_base or ""), p.inbound_ids, p.api_key
        fail_since = p.health_fail_since
        fail_count = int(p.health_fail_count or 0)
        alerted = bool(p.health_alerted)

    from tenant.shared.panel_clients import panel_pool as _pool
    from tenant.shared.panel_clients.vpn_panel import PanelError

    ok = False
    err_text = ""
    try:
        client = _pool.get_sync(
            panel_key, base_url, username, password,
            sub_base=sub_base, inbound_ids_str=inbound_ids, api_key=api_key)
        await client.test_connection()
        ok = True
    except PanelError as e:
        err_text = str(e)[:200]
    except Exception as e:                                   # noqa: BLE001
        logger.exception("panel_watchdog: بررسیِ پنل %s (bot %s) ناموفق", panel_key, bot_id)
        err_text = str(e)[:200]

    now = datetime.utcnow()
    notify: str | None = None       # "down" | "up" | None
    down_for_min = 0

    if ok:
        new_since, new_count = None, 0
        # فقط اگر واقعاً بابتِ این قطعی هشدار داده بودیم، خبرِ برگشت بده
        if alerted:
            notify = "up"
        new_alerted = False
    else:
        new_since = fail_since or now
        new_count = fail_count + 1
        new_alerted = alerted
        if not alerted:
            down_sec = (now - new_since).total_seconds()
            long_enough = down_sec >= _DOWN_ALERT_AFTER_SEC
            config_err = _looks_like_config_error(err_text)
            if new_count >= _DOWN_ALERT_MIN_FAILS and (long_enough or config_err):
                notify = "down"
                new_alerted = True
                down_for_min = int(down_sec // 60)

    async with Session() as s:
        p2 = await s.get(VpnPanel, panel_id)
        if not p2:
            return
        p2.last_health_ok = ok
        p2.last_health_checked_at = now
        p2.health_fail_since = new_since
        p2.health_fail_count = new_count
        p2.health_alerted = new_alerted
        await s.commit()

    if not notify:
        return

    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow or not brow.owner_id:
        return

    if notify == "up":
        text = (f"✅ <b>اتصال به پنل برقرار شد</b>\n\n"
                f"🔑 پنل: <code>{_html.escape(str(panel_key))}</code>\n"
                f"اتصال به این پنل که قطع شده بود، الان دوباره برقراره.")
    else:
        since_line = (f"⏱ از {down_for_min} دقیقه پیش پیوسته قطع است "
                      f"({new_count} بررسیِ ناموفقِ پشتِ‌هم).\n"
                      if down_for_min else
                      f"❗️ {new_count} بررسیِ ناموفقِ پشتِ‌هم.\n")
        text = (f"⚠️ <b>اتصال به پنل قطع شد!</b>\n\n"
                f"🔑 پنل: <code>{_html.escape(str(panel_key))}</code>\n"
                + since_line
                + (f"❗️ خطا: <code>{_html.escape(err_text)}</code>\n" if err_text else "")
                + "\nهرچه سریع‌تر بررسی کن — تا وصل نشه، خرید/تمدید رویِ این پنل کار نمی‌کنه."
                + "\n\n<i>تا وقتی وصل نشود دیگر پیامِ تکراری نمی‌فرستم.</i>")

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🛠 مدیریتِ پنل‌ها", callback_data=f"tvpn:panel:{panel_key}")]])
    try:
        from aiogram.client.default import DefaultBotProperties
        tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode="HTML"))
        try:
            await tbot.send_message(brow.owner_id, text, reply_markup=kb)
        finally:
            await tbot.session.close()
    except Exception:                                        # noqa: BLE001
        logger.exception("panel_watchdog: ارسالِ هشدار به مالکِ ربات %s ناموفق", bot_id)


# === PATCH_PANEL_NODES_WATCHDOG_V1 ===
# گزارشِ WHALE SHOP: «کانفیگ‌هایِ وایرگاردِ ربات کار نمی‌کند». علتِ واقعی: هر ۱۰ نودِ وایرگاردِ پنل در حالتِ error بودند
# (Connection refused / Request timed out) — پنل بالا بود و test_connection سالم، پس هیچ هشداری نمی‌رفت و پیرِ کاربرانِ جدید
# رویِ نودها اعمال نمی‌شد. حالا برایِ پنل‌هایِ خانوادهٔ پاسارگارد وضعیتِ نودها هم چک می‌شود و اگر نودی مدتِ کافی در error بماند،
# به مالکِ ربات خبر می‌رود (یک‌بار؛ هر ۱۲ ساعت یادآوری؛ با «وصل شد» وقتی همه سالم شدند).
_NODE_ALERT_AFTER_SEC = 20 * 60
_NODE_REMIND_AFTER_SEC = 12 * 3600
_NODE_FAMILY = ("pasargad", "pasarguard")


def _bad_nodes(res) -> list[dict]:
    items = res if isinstance(res, list) else ((res.get("nodes") or res.get("items") or []) if isinstance(res, dict) else [])
    return [n for n in items if isinstance(n, dict) and str(n.get("status") or "").lower() == "error"]


def _node_reason_hint(msg: str) -> str:
    m = (msg or "").lower()
    if "refused" in m:
        return "سرویسِ نود خاموش است یا پورتش بسته است"
    if "timed out" in m or "timeout" in m:
        return "نود از پنل در دسترس نیست (فایروال/شبکه/سرورِ خاموش)"
    return ""


async def _node_state(bot_id: int, key: str) -> dict:
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
    try:
        return json.loads(row.value) if row and row.value else {}
    except ValueError:
        return {}


async def _node_state_save(bot_id: int, key: str, state: dict) -> None:
    async with Session() as s:
        row = await s.scalar(select(Setting).where(Setting.bot_id == bot_id, Setting.key == key))
        val = json.dumps(state) if state else ""
        if row:
            row.value = val
        else:
            s.add(Setting(bot_id=bot_id, key=key, value=val))
        await s.commit()


async def _check_nodes_once(panel_id: int) -> None:
    async with Session() as s:
        p = await s.get(VpnPanel, panel_id)
        if not p or not p.enabled or not any(f in (p.panel_key or "") for f in _NODE_FAMILY):
            return
        bot_id, panel_key = p.bot_id, p.panel_key
        base_url, username, password = p.base_url, p.username, p.password
        sub_base, inbound_ids, api_key = (p.sub_base or ""), p.inbound_ids, p.api_key
    from tenant.shared.panel_clients import panel_pool as _pool
    try:
        client = _pool.get_sync(panel_key, base_url, username, password,
                                sub_base=sub_base, inbound_ids_str=inbound_ids, api_key=api_key)
        res = await client._request("GET", "/api/nodes")
    except Exception:                                        # noqa: BLE001
        logger.debug("panel_watchdog: خواندنِ نودهایِ پنل %s ممکن نبود (دسترسی/نسخه)", panel_key, exc_info=True)
        return
    if res is None:
        return          # این نسخه/دسترسی /api/nodes ندارد
    bad = _bad_nodes(res)
    key = f"node_health_{panel_id}"
    st = await _node_state(bot_id, key)
    now = datetime.utcnow().timestamp()
    notify = None
    if bad:
        since = float(st.get("since") or now)
        alerted_at = float(st.get("alerted_at") or 0)
        if not alerted_at and now - since >= _NODE_ALERT_AFTER_SEC:
            notify, alerted_at = "down", now
        elif alerted_at and now - alerted_at >= _NODE_REMIND_AFTER_SEC:
            notify, alerted_at = "remind", now
        await _node_state_save(bot_id, key, {"since": since, "alerted_at": alerted_at})
    else:
        if st.get("alerted_at"):
            notify = "up"
        if st:
            await _node_state_save(bot_id, key, {})
    if not notify:
        return
    async with Session() as s:
        brow = await s.get(BotRow, bot_id)
    if not brow or not brow.owner_id:
        return
    if notify == "up":
        text = (f"✅ <b>نودهایِ پنل دوباره وصل شدند</b>\n\n🔑 پنل: <code>{_html.escape(str(panel_key))}</code>\n"
                "همهٔ نودها الان سالم‌اند؛ کانفیگ‌هایِ وایرگارد/لوکیشن‌ها باید دوباره کار کنند.")
    else:
        lines = []
        for n in bad[:10]:
            msg = str(n.get("message") or "")[:80]
            hint = _node_reason_hint(msg)
            lines.append(f"• {_html.escape(str(n.get('name') or n.get('id'))[:28])} — "
                         f"{_html.escape(str(n.get('address') or '')[:24])}\n"
                         f"   <code>{_html.escape(msg)}</code>" + (f"\n   ↳ {hint}" if hint else ""))
        more = f"\n… و {len(bad) - 10} نودِ دیگر" if len(bad) > 10 else ""
        head = "⚠️ <b>نودهایِ پنل قطع/خطادارند!</b>" if notify == "down" else "⏰ <b>یادآوری: نودهایِ پنل هنوز خطا دارند</b>"
        text = (f"{head}\n\n🔑 پنل: <code>{_html.escape(str(panel_key))}</code>\n❌ {len(bad)} نود در حالتِ error:\n\n"
                + "\n".join(lines) + more
                + "\n\nتا وقتی این نودها وصل نشوند، کانفیگ‌هایِ جدید/تمدیدشده روی لوکیشن‌هایِ مربوط کار نمی‌کنند "
                  "(پنل بالاست ولی به نودها نمی‌رسد). سرویس/پورتِ نودها یا فایروالِ سرورشان را چک کن.")
    try:
        from aiogram.client.default import DefaultBotProperties
        tbot = Bot(token=brow.token, default=DefaultBotProperties(parse_mode="HTML"))
        try:
            await tbot.send_message(brow.owner_id, text)
        finally:
            await tbot.session.close()
    except Exception:                                        # noqa: BLE001
        logger.exception("panel_watchdog: ارسالِ هشدارِ نودها به مالکِ ربات %s ناموفق", bot_id)


async def panel_watchdog_once() -> None:
    async with Session() as s:
        ids = (await s.scalars(select(VpnPanel.id).where(VpnPanel.enabled == True))).all()  # noqa: E712
    for pid in ids:
        try:
            await _check_panel_once(pid)
        except Exception:
            logger.exception("panel_watchdog: بررسیِ پنلِ %s ناموفق", pid)
        try:
            await _check_nodes_once(pid)        # PATCH_PANEL_NODES_WATCHDOG_V1
        except Exception:
            logger.exception("panel_watchdog: بررسیِ نودهایِ پنلِ %s ناموفق", pid)
        await asyncio.sleep(_BETWEEN_PANELS_SLEEP)


async def panel_watchdog_loop(interval_sec: int = _LOOP_INTERVAL_SEC) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await panel_watchdog_once()
        except Exception:
            logger.exception("panel_watchdog_loop error")
        await asyncio.sleep(interval_sec)
