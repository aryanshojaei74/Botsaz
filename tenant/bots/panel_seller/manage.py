"""
manage.py — «🛠 مدیریتِ کاملِ پنل» برایِ مالکِ رباتِ پنل‌فروشی (PATCH_PSEL_MANAGE_V1).

خواستهٔ Whale Support: «پنل را بشود با تمامِ امکاناتِ مینی‌اپ داخلِ ربات مدیریت کرد». همهٔ عملیات از همان کلاینتِ پنل
(make_panel_client) می‌آید که فروشِ VPN هم استفاده می‌کند، پس برایِ هر پنلِ متصل کار می‌کند:

  • داشبورد: تعدادِ کاربران (فعال/غیرفعال/منقضی/تمام‌حجم/نامحدود)، مصرفِ کل، آنلاین‌ها، پرمصرف‌ها، رو به انقضا.
  • کاربران: فهرستِ ۱۰تایی با فیلتر، جستجو، کارتِ کاربر (مصرف/انقضا/لینک) و عملیات: ±حجم، +روز، حجم/زمانِ نامحدود،
    فعال/غیرفعال، ریستِ مصرف، ریستِ لینکِ اشتراک، حذف (با تأیید).
  • ساختِ کاربر: نام، حجم (۰ = نامحدود)، روز (۰ = نامحدود).
  • ادمین‌ها (خانوادهٔ مرزبان): فهرست + خاموش/روشنِ همهٔ کاربرانِ یک ادمین + ریستِ مصرفِ ادمین.

callbackها: psel:mg (انتخابِ پنل) · psel:mgP:{panel} · psel:mgu:{panel}:{فیلتر}:{صفحه} · psel:mgv:{panel}:{نام} ·
psel:mgg/mgd (±حجم/روز) · psel:mgi (نامحدود) · psel:mgt (فعال/غیرفعال) · psel:mgr (ریستِ مصرف) · psel:mgl (لینک) ·
psel:mgx (حذف) · psel:mgs (جستجو) · psel:mgn (ساخت) · psel:mga* (ادمین‌ها) · psel:mgk (گزارش)
"""
from __future__ import annotations

import re
import time

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from config import Session
from models import VpnPanel

from tenant.bots.panel_seller import adapter as AD
from tenant.bots.panel_seller.common import back_row, btn, edit_or_send, esc, fa, kb

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

psel_manage_router = Router(name="psel_manage")
PAGE = 10
GB = 1024 ** 3
ONLINE_SEC = 300
FILTERS = {"a": "همه", "e": "🟢 فعال", "d": "🔴 غیرفعال", "x": "⏰ منقضی/تمام‌حجم", "n": "♾ حجمِ نامحدود"}
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
_TR = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class PselMgFSM(StatesGroup):
    amount = State()        # ±حجم / +روز
    search = State()
    new_name = State()
    new_gb = State()
    new_days = State()


# ───────────────────────── ابزارها ─────────────────────────

async def _guard(event, bot: Bot):
    from tenant.bots.panel_seller.admin import _guard as g
    return await g(event, bot)


async def _panel_of(row, pid: int):
    async with Session() as s:
        p = await s.get(VpnPanel, int(pid))
    return p if p is not None and p.bot_id == row.id else None


def _open(panel):
    """کلاینتِ پنل (همان کلاینتِ فروشِ VPN)."""
    from tenant.shared.panel_clients.vpn_panel_factory import make_panel_client
    return make_panel_client(panel)


async def _close(c) -> None:
    try:
        await c.close()
    except Exception:      # noqa: BLE001
        pass


def _gib(b) -> str:
    return fa(f"{float(b or 0) / GB:.2f}".rstrip("0").rstrip("."))


def _norm(c: dict, now: float | None = None) -> dict:
    """کاربرِ پنل به شکلِ یکنواخت. exp_ms: ۰ = بدونِ انقضا."""
    now = now or time.time()
    t = c.get("traffic") or {}
    used = int(t.get("up", 0) or 0) + int(t.get("down", 0) or 0)
    total = int(c.get("totalGB") or t.get("total") or 0)
    try:
        exp_ms = int(c.get("expiryTime") or 0)
    except (TypeError, ValueError):
        exp_ms = 0
    st = str(c.get("status") or ("active" if c.get("enable", True) else "disabled"))
    if st in ("active", "on_hold"):
        if exp_ms > 0 and exp_ms / 1000 < now:
            st = "expired"
        elif total > 0 and used >= total:
            st = "limited"
    return {"name": str(c.get("email") or c.get("username") or ""), "used": used, "total": total, "exp_ms": exp_ms,
            "status": st, "online_ms": int(c.get("last_online_ms") or 0), "created_ms": int(c.get("created_ms") or 0)}


def _days_left(exp_ms: int, now: float | None = None) -> str:
    if not exp_ms:
        return "♾ بدونِ انقضا"
    d = (exp_ms / 1000 - (now or time.time())) / 86400
    if d < 0:
        return f"⏰ {fa(int(-d))} روز پیش منقضی شد"
    return f"{fa(int(d))} روز مانده" if d >= 1 else "کمتر از یک روز مانده"


def _icon(u: dict) -> str:
    return {"active": "🟢", "on_hold": "🟡", "disabled": "🔴", "expired": "⏰", "limited": "📛"}.get(u["status"], "⚪️")


def _bar(used: int, total: int) -> str:
    if not total:
        return "♾"
    p = max(0.0, min(1.0, used / total))
    k = int(round(p * 10))
    return "▓" * k + "░" * (10 - k) + f" {fa(int(p * 100))}٪"


def _line(u: dict) -> str:
    vol = f"{_gib(u['used'])}/{'♾' if not u['total'] else _gib(u['total'])}G"
    return f"{_icon(u)} {u['name']} · {vol} · {_days_left(u['exp_ms']).split(' ')[0] if u['exp_ms'] else '♾'}"[:60]


async def _users(panel) -> list[dict]:
    c = _open(panel)
    try:
        raw = await c.list_clients()
    finally:
        await _close(c)
    now = time.time()
    return [u for u in (_norm(x, now) for x in (raw or []) if isinstance(x, dict)) if u["name"]]


def _matches(u: dict, f: str) -> bool:
    if f == "e":
        return u["status"] in ("active", "on_hold")
    if f == "d":
        return u["status"] == "disabled"
    if f == "x":
        return u["status"] in ("expired", "limited")
    if f == "n":
        return not u["total"]
    return True


# ───────────────────────── انتخابِ پنل + داشبورد ─────────────────────────

@psel_manage_router.callback_query(F.data == "psel:mg")
async def mg_home(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    async with Session() as s:
        panels = list((await s.scalars(select(VpnPanel).where(VpnPanel.bot_id == row.id, VpnPanel.enabled == True)   # noqa: E712
                                       .order_by(VpnPanel.id))).all())
    if not panels:
        await edit_or_send(cb, "هنوز پنلی متصل نکرده‌ای. از «🗂 پنل‌هایِ متصل» اتصال بده.", kb(back_row("psel:adm")))
        return await cb.answer()
    if len(panels) == 1:
        cb.data = f"psel:mgP:{panels[0].id}"
        return await mg_dashboard(cb, bot)
    rows = [[btn(f"🖥 {p.panel_key} — {p.base_url.replace('https://', '').replace('http://', '')[:30]}", f"psel:mgP:{p.id}",
                 style=ButtonStyle.PRIMARY)] for p in panels]
    rows.append(back_row("psel:adm"))
    await edit_or_send(cb, "🛠 <b>مدیریتِ پنل</b>\n\nکدام پنل؟", kb(*rows))
    await cb.answer()


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgP:\d+$"))
async def mg_dashboard(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    panel = await _panel_of(row, pid)
    if not panel:
        return await cb.answer("پنل پیدا نشد.", show_alert=True)
    await cb.answer("در حالِ خواندنِ پنل…")
    try:
        users = await _users(panel)
    except Exception as e:      # noqa: BLE001
        logger.info("psel manage: خواندنِ کاربرانِ پنل %s ناموفق: %s", pid, e)
        return await edit_or_send(cb, f"❌ پنل جواب نداد:\n<code>{esc(str(e)[:200])}</code>", kb(back_row("psel:adm")))
    now = time.time()
    c = {k: sum(1 for u in users if u["status"] == k) for k in ("active", "on_hold", "disabled", "expired", "limited")}
    online = sum(1 for u in users if u["online_ms"] and now - u["online_ms"] / 1000 <= ONLINE_SEC)
    unl = sum(1 for u in users if not u["total"])
    used = sum(u["used"] for u in users)
    soon = sum(1 for u in users if u["status"] in ("active", "on_hold") and u["exp_ms"] and 0 < u["exp_ms"] / 1000 - now <= 3 * 86400)
    text = (f"🛠 <b>مدیریتِ پنل</b> — {esc(panel.panel_key)}\n<code>{esc(panel.base_url)}</code>\n\n"
            f"👥 کاربران: <b>{fa(len(users))}</b>\n"
            f"🟢 فعال: {fa(c['active'] + c['on_hold'])} · 🔴 غیرفعال: {fa(c['disabled'])}\n"
            f"⏰ منقضی: {fa(c['expired'])} · 📛 تمام‌حجم: {fa(c['limited'])}\n"
            f"♾ حجمِ نامحدود: {fa(unl)} · ⏳ رو به انقضا (≤۳ روز): {fa(soon)}\n"
            f"🟣 آنلاین (۵ دقیقهٔ اخیر): {fa(online)}\n"
            f"📊 مصرفِ کلِ کاربران: <b>{_gib(used)}</b> گیگ")
    await edit_or_send(cb, text, kb(
        [btn("👥 کاربران", f"psel:mgu:{pid}:a:0", style=ButtonStyle.PRIMARY),
         btn("🔎 جستجو", f"psel:mgs:{pid}", style=ButtonStyle.PRIMARY)],
        [btn("➕ ساختِ کاربر", f"psel:mgn:{pid}", style=ButtonStyle.SUCCESS),
         btn("📊 گزارش (پرمصرف/رو به انقضا)", f"psel:mgk:{pid}", style=ButtonStyle.PRIMARY)],
        [btn("👨‍💼 ادمین‌ها", f"psel:mga:{pid}", style=ButtonStyle.PRIMARY),
         btn("🔄 به‌روزرسانی", f"psel:mgP:{pid}")],
        back_row("psel:adm")))


# ───────────────────────── فهرست و گزارش ─────────────────────────

async def _list_view(row, pid: int, f: str, page: int, query: str = ""):
    panel = await _panel_of(row, pid)
    if not panel:
        return None
    users = await _users(panel)
    if query:
        q = query.lower()
        users = [u for u in users if q in u["name"].lower()]
    users = [u for u in users if _matches(u, f)]
    users.sort(key=lambda u: (u["created_ms"], u["name"]), reverse=True)
    pages = max(1, (len(users) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = users[page * PAGE:(page + 1) * PAGE]
    head = f"👥 <b>کاربرانِ پنل</b> — {FILTERS.get(f, 'همه')}"
    if query:
        head += f"\n🔎 «{esc(query)}»"
    text = head + f"\nمجموع: <b>{fa(len(users))}</b> — صفحهٔ {fa(page + 1)} از {fa(pages)}"
    if not chunk:
        text += "\n\nکاربری پیدا نشد."
    rows = [[btn(_line(u), f"psel:mgv:{pid}:{u['name']}"[:64])] for u in chunk]
    if not query:
        rows.append([btn(("✅ " if k == f else "") + lbl, f"psel:mgu:{pid}:{k}:0") for k, lbl in list(FILTERS.items())[:3]])
        rows.append([btn(("✅ " if k == f else "") + lbl, f"psel:mgu:{pid}:{k}:0") for k, lbl in list(FILTERS.items())[3:]])
        nav = []
        if page > 0:
            nav.append(btn("⬅️ قبلی", f"psel:mgu:{pid}:{f}:{page - 1}"))
        if page + 1 < pages:
            nav.append(btn("بعدی ➡️", f"psel:mgu:{pid}:{f}:{page + 1}"))
        if nav:
            rows.append(nav)
    rows.append([btn("🔎 جستجو", f"psel:mgs:{pid}")])
    rows.append(back_row(f"psel:mgP:{pid}"))
    return text, kb(*rows)


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgu:\d+:[aedxn]:\d+$"))
async def mg_users(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    _, _, pid, f, page = cb.data.split(":")
    try:
        view = await _list_view(row, int(pid), f, int(page))
    except Exception as e:      # noqa: BLE001
        return await cb.answer("پنل جواب نداد: " + str(e)[:150], show_alert=True)
    if view is None:
        return await cb.answer("پنل پیدا نشد.", show_alert=True)
    await edit_or_send(cb, *view)
    await cb.answer()


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgk:\d+$"))
async def mg_report(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    panel = await _panel_of(row, pid)
    if not panel:
        return await cb.answer("پنل پیدا نشد.", show_alert=True)
    try:
        users = await _users(panel)
    except Exception as e:      # noqa: BLE001
        return await cb.answer("پنل جواب نداد: " + str(e)[:150], show_alert=True)
    now = time.time()
    top = sorted(users, key=lambda u: u["used"], reverse=True)[:10]
    soon = sorted([u for u in users if u["status"] in ("active", "on_hold") and u["exp_ms"] and 0 < u["exp_ms"] / 1000 - now <= 3 * 86400],
                  key=lambda u: u["exp_ms"])[:10]
    lines = ["📊 <b>گزارشِ پنل</b>\n", "🔥 <b>پرمصرف‌ترین کاربران</b>"]
    lines += [f"{i + 1}. {esc(u['name'])} — {_gib(u['used'])} گیگ" for i, u in enumerate(top)] or ["—"]
    lines += ["\n⏳ <b>رو به انقضا (≤۳ روز)</b>"]
    lines += [f"• {esc(u['name'])} — {_days_left(u['exp_ms'], now)}" for u in soon] or ["—"]
    await edit_or_send(cb, "\n".join(lines), kb(back_row(f"psel:mgP:{pid}")))
    await cb.answer()


# ───────────────────────── جستجو ─────────────────────────

@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgs:\d+$"))
async def mg_search_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    await state.clear()
    await state.set_state(PselMgFSM.search)
    await state.update_data(pid=pid)
    await edit_or_send(cb, "🔎 بخشی از نامِ کاربری را بفرست:", kb(back_row(f"psel:mgP:{pid}", "✖️ لغو")))
    await cb.answer()


@psel_manage_router.message(StateFilter(PselMgFSM.search), F.text)
async def mg_search_run(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return await state.clear()
    d = await state.get_data()
    await state.clear()
    pid, q = int(d.get("pid") or 0), (msg.text or "").strip()
    if not pid or q in ("/cancel", "لغو"):
        return await msg.answer("لغو شد.")
    try:
        view = await _list_view(row, pid, "a", 0, q[:32])
    except Exception as e:      # noqa: BLE001
        return await msg.answer("❌ پنل جواب نداد: " + str(e)[:150])
    if view is None:
        return await msg.answer("پنل پیدا نشد.")
    await msg.answer(view[0], reply_markup=view[1], parse_mode="HTML")


# ───────────────────────── کارتِ کاربر ─────────────────────────

async def _user_card(row, pid: int, name: str):
    panel = await _panel_of(row, pid)
    if not panel:
        return None
    c = _open(panel)
    try:
        usage = await c.get_usage(name)
        cur = await c.get_client(name)
    finally:
        await _close(c)
    if not usage:
        return "missing"
    now = time.time()
    u = _norm({"email": name, "traffic": {"up": usage.get("up", 0), "down": usage.get("down", 0)}, "totalGB": usage.get("total", 0),
               "expiryTime": usage.get("expiryTime", 0), "enable": usage.get("enable", True),
               "status": (cur or {}).get("status")}, now)
    sub = (cur or {}).get("sub_link") or ""
    online = usage.get("last_online_ms")
    text = (f"👤 <b>{esc(name)}</b>\n\n{_icon(u)} وضعیت: <b>{esc(u['status'])}</b>\n"
            f"📊 مصرف: {_gib(u['used'])} / {'♾' if not u['total'] else _gib(u['total'])} گیگ\n{_bar(u['used'], u['total'])}\n"
            f"⏳ انقضا: {_days_left(u['exp_ms'], now)}")
    if online:
        ago = max(0, int(now - int(online) / 1000))
        text += "\n🟣 آخرین اتصال: " + ("هم‌اکنون آنلاین" if ago <= ONLINE_SEC else
                                       f"{fa(ago // 3600)} ساعت پیش" if ago < 86400 else f"{fa(ago // 86400)} روز پیش")
    if sub:
        text += f"\n\n🔗 لینکِ اشتراک:\n<code>{esc(sub)}</code>"
    enabled = u["status"] not in ("disabled",)
    markup = kb(
        [btn("➕ حجم", f"psel:mgg:{pid}:{name}:p", style=ButtonStyle.SUCCESS),
         btn("➖ حجم", f"psel:mgg:{pid}:{name}:m", style=ButtonStyle.DANGER)],
        [btn("➕ روز", f"psel:mgd:{pid}:{name}", style=ButtonStyle.SUCCESS)],
        [btn("♾ حجمِ نامحدود", f"psel:mgi:{pid}:{name}:g"), btn("♾ زمانِ نامحدود", f"psel:mgi:{pid}:{name}:d")],
        [btn("⛔️ غیرفعال‌کردن" if enabled else "✅ فعال‌کردن", f"psel:mgt:{pid}:{name}", style=ButtonStyle.PRIMARY),
         btn("🔁 ریستِ مصرف", f"psel:mgr:{pid}:{name}")],
        [btn("🔗 ریستِ لینکِ اشتراک", f"psel:mgl:{pid}:{name}")],
        [btn("🗑 حذفِ کاربر", f"psel:mgx:{pid}:{name}", style=ButtonStyle.DANGER)],
        back_row(f"psel:mgu:{pid}:a:0", "⬅️ بازگشت به فهرست"))
    return text, markup


async def _show_card(target, row, pid: int, name: str, note: str = "") -> None:
    try:
        card = await _user_card(row, pid, name)
    except Exception as e:      # noqa: BLE001
        return await edit_or_send(target, f"❌ پنل جواب نداد:\n<code>{esc(str(e)[:200])}</code>", kb(back_row(f"psel:mgP:{pid}")))
    if card is None:
        return await edit_or_send(target, "پنل پیدا نشد.", kb(back_row("psel:mg")))
    if card == "missing":
        return await edit_or_send(target, "این کاربر روی پنل پیدا نشد (شاید حذف شده).", kb(back_row(f"psel:mgu:{pid}:a:0")))
    text, markup = card
    await edit_or_send(target, (note + "\n\n" if note else "") + text, markup)


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgv:\d+:.+$"))
async def mg_user(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    await state.clear()
    _, _, pid, name = cb.data.split(":", 3)
    await cb.answer()
    await _show_card(cb, row, int(pid), name)


async def _act(cb: CallbackQuery, bot: Bot, what: str):
    """اجرایِ یک عملیاتِ تک‌مرحله‌ای رویِ کاربر؛ بعد کارتِ تازه نشان داده می‌شود."""
    row = await _guard(cb, bot)
    if not row:
        return None
    parts = cb.data.split(":", 4)
    pid, name = int(parts[2]), parts[3]
    panel = await _panel_of(row, pid)
    if not panel:
        await cb.answer("پنل پیدا نشد.", show_alert=True)
        return None
    return row, panel, pid, name, (parts[4] if len(parts) > 4 else "")


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgt:\d+:.+$"))
async def mg_toggle(cb: CallbackQuery, bot: Bot):
    got = await _act(cb, bot, "toggle")
    if not got:
        return
    row, panel, pid, name, _ = got
    c = _open(panel)
    try:
        usage = await c.get_usage(name)
        if not usage:
            return await cb.answer("کاربر پیدا نشد.", show_alert=True)
        enable = not bool(usage.get("enable", True))
        await c.set_enabled(name, enable)
    except Exception as e:      # noqa: BLE001
        return await cb.answer("انجام نشد: " + str(e)[:150], show_alert=True)
    finally:
        await _close(c)
    await cb.answer("✅ فعال شد" if enable else "⛔️ غیرفعال شد")
    await _show_card(cb, row, pid, name)


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgr:\d+:.+$"))
async def mg_reset_usage(cb: CallbackQuery, bot: Bot):
    got = await _act(cb, bot, "reset")
    if not got:
        return
    row, panel, pid, name, _ = got
    c = _open(panel)
    try:
        await c.reset_usage(name)
    except Exception as e:      # noqa: BLE001
        return await cb.answer("انجام نشد: " + str(e)[:150], show_alert=True)
    finally:
        await _close(c)
    await cb.answer("✅ مصرف ریست شد")
    await _show_card(cb, row, pid, name)


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgl:\d+:.+$"))
async def mg_reset_sub(cb: CallbackQuery, bot: Bot):
    got = await _act(cb, bot, "sub")
    if not got:
        return
    row, panel, pid, name, _ = got
    c = _open(panel)
    try:
        new = await c.reset_sub(name)
    except Exception as e:      # noqa: BLE001
        return await cb.answer("انجام نشد: " + str(e)[:150], show_alert=True)
    finally:
        await _close(c)
    await cb.answer("✅ لینکِ جدید صادر شد (قبلی باطل)")
    await _show_card(cb, row, pid, name, note=("🔗 <b>لینکِ جدید:</b>\n<code>" + esc(str(new)) + "</code>") if new else "")


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgi:\d+:.+:[gd]$"))
async def mg_unlimited(cb: CallbackQuery, bot: Bot):
    got = await _act(cb, bot, "unl")
    if not got:
        return
    row, panel, pid, name, kind = got
    c = _open(panel)
    try:
        mod = getattr(c, "_modify", None)
        if mod is None:
            return await cb.answer("این نوع پنل از «نامحدودکردن» پشتیبانی نمی‌کند.", show_alert=True)
        await mod(name, **({"data_limit": 0} if kind == "g" else {"expire": 0}))
    except Exception as e:      # noqa: BLE001
        return await cb.answer("انجام نشد: " + str(e)[:150], show_alert=True)
    finally:
        await _close(c)
    await cb.answer("♾ انجام شد")
    await _show_card(cb, row, pid, name)


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgx(ok)?:\d+:.+$"))
async def mg_delete(cb: CallbackQuery, bot: Bot):
    got = await _act(cb, bot, "del")
    if not got:
        return
    row, panel, pid, name, _ = got
    if cb.data.startswith("psel:mgxok:"):
        c = _open(panel)
        try:
            await c.delete_account(name)
        except Exception as e:      # noqa: BLE001
            return await cb.answer("حذف نشد: " + str(e)[:150], show_alert=True)
        finally:
            await _close(c)
        await cb.answer("🗑 حذف شد")
        return await edit_or_send(cb, f"🗑 کاربرِ <b>{esc(name)}</b> از پنل حذف شد.", kb(back_row(f"psel:mgu:{pid}:a:0", "⬅️ بازگشت به فهرست")))
    await edit_or_send(cb, f"⚠️ کاربرِ <b>{esc(name)}</b> برایِ همیشه از پنل حذف شود؟ این کار برگشت‌پذیر نیست.",
                       kb([btn("⚠️ بله، حذف کن", f"psel:mgxok:{pid}:{name}", style=ButtonStyle.DANGER)],
                          back_row(f"psel:mgv:{pid}:{name}", "✖️ انصراف")))
    await cb.answer()


# ───────────────────────── ±حجم / +روز (با ورودیِ عدد) ─────────────────────────

@psel_manage_router.callback_query(F.data.regexp(r"^psel:mg[gd]:\d+:.+$"))
async def mg_amount_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    got = await _act(cb, bot, "amount")
    if not got:
        return
    row, panel, pid, name, sign = got
    kind = "g" if cb.data.startswith("psel:mgg:") else "d"
    sign = "m" if sign == "m" else "p"
    await state.clear()
    await state.set_state(PselMgFSM.amount)
    await state.update_data(pid=pid, name=name, kind=kind, sign=sign)
    what = ("چند گیگ " + ("کم" if sign == "m" else "اضافه")) if kind == "g" else "چند روز اضافه"
    await edit_or_send(cb, f"✏️ {what} شود؟ یک عددِ صحیح بفرست.", kb(back_row(f"psel:mgv:{pid}:{name}", "✖️ لغو")))
    await cb.answer()


@psel_manage_router.message(StateFilter(PselMgFSM.amount), F.text)
async def mg_amount_run(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return await state.clear()
    d = await state.get_data()
    raw = (msg.text or "").strip().translate(_TR).replace(",", "")
    if raw in ("/cancel", "لغو"):
        await state.clear()
        return await msg.answer("لغو شد.")
    if not raw.isdigit() or not (1 <= int(raw) <= 100000):
        return await msg.answer("❌ یک عددِ صحیح (۱ تا ۱۰۰٬۰۰۰) بفرست یا /cancel.")
    amt = int(raw)
    await state.clear()
    pid, name, kind, sign = int(d["pid"]), d["name"], d["kind"], d["sign"]
    panel = await _panel_of(row, pid)
    if not panel:
        return await msg.answer("پنل پیدا نشد.")
    c = _open(panel)
    try:
        usage = await c.get_usage(name)
        if not usage:
            return await msg.answer("کاربر پیدا نشد.")
        if kind == "g":
            total = int(usage.get("total", 0) or 0)
            if not total:
                return await msg.answer("حجمِ این کاربر نامحدود است؛ اول سقف بگذار (یا از «➕ حجم» روی کاربرِ دارایِ سقف استفاده کن).")
            if sign == "m" and total - amt * GB <= 0:
                return await msg.answer("❌ حجم نمی‌تواند صفر یا منفی شود.")
            await c.add_traffic(name, -amt if sign == "m" else amt)
        else:
            exp = int(usage.get("expiryTime", 0) or 0)
            if not exp:
                return await msg.answer("زمانِ این کاربر نامحدود است؛ افزودنِ روز بی‌معنی است.")
            await c.renew(name, amt)
    except Exception as e:      # noqa: BLE001
        return await msg.answer("❌ انجام نشد: " + str(e)[:200])
    finally:
        await _close(c)
    card = await _user_card(row, pid, name)
    if isinstance(card, tuple):
        await msg.answer("✅ انجام شد.\n\n" + card[0], reply_markup=card[1], parse_mode="HTML")
    else:
        await msg.answer("✅ انجام شد.")


# ───────────────────────── ساختِ کاربر ─────────────────────────

@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgn:\d+$"))
async def mg_new_ask(cb: CallbackQuery, bot: Bot, state: FSMContext):
    row = await _guard(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    await state.clear()
    await state.set_state(PselMgFSM.new_name)
    await state.update_data(pid=pid)
    await edit_or_send(cb, "➕ <b>ساختِ کاربر</b>\n\nنامِ کاربری را بفرست (۳ تا ۳۲ نویسه؛ فقط حروفِ انگلیسی، عدد و _):",
                       kb(back_row(f"psel:mgP:{pid}", "✖️ لغو")))
    await cb.answer()


def _num(txt: str, lo: int, hi: int) -> int | None:
    raw = (txt or "").strip().translate(_TR).replace(",", "")
    return int(raw) if raw.isdigit() and lo <= int(raw) <= hi else None


@psel_manage_router.message(StateFilter(PselMgFSM.new_name), F.text)
async def mg_new_name(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard(msg, bot):
        return await state.clear()
    t = (msg.text or "").strip()
    if t in ("/cancel", "لغو"):
        await state.clear()
        return await msg.answer("لغو شد.")
    if not _NAME_RE.match(t):
        return await msg.answer("❌ نام ۳ تا ۳۲ نویسه و فقط حروفِ انگلیسی/عدد/_ باشد.")
    await state.update_data(name=t)
    await state.set_state(PselMgFSM.new_gb)
    await msg.answer("حجم چند گیگ؟ (۰ = نامحدود)")


@psel_manage_router.message(StateFilter(PselMgFSM.new_gb), F.text)
async def mg_new_gb(msg: Message, bot: Bot, state: FSMContext):
    if not await _guard(msg, bot):
        return await state.clear()
    if (msg.text or "").strip() in ("/cancel", "لغو"):
        await state.clear()
        return await msg.answer("لغو شد.")
    v = _num(msg.text, 0, 100000)
    if v is None:
        return await msg.answer("❌ یک عددِ صحیح بفرست (۰ = نامحدود).")
    await state.update_data(gb=v)
    await state.set_state(PselMgFSM.new_days)
    await msg.answer("مدت چند روز؟ (۰ = بدونِ انقضا)")


@psel_manage_router.message(StateFilter(PselMgFSM.new_days), F.text)
async def mg_new_days(msg: Message, bot: Bot, state: FSMContext):
    row = await _guard(msg, bot)
    if not row:
        return await state.clear()
    if (msg.text or "").strip() in ("/cancel", "لغو"):
        await state.clear()
        return await msg.answer("لغو شد.")
    v = _num(msg.text, 0, 3650)
    if v is None:
        return await msg.answer("❌ یک عددِ صحیح بفرست (۰ = بدونِ انقضا).")
    d = await state.get_data()
    await state.clear()
    panel = await _panel_of(row, int(d["pid"]))
    if not panel:
        return await msg.answer("پنل پیدا نشد.")
    from tenant.shared.panel_clients.vpn_panel import gen_sub_id, gen_uuid
    c = _open(panel)
    try:
        res = await c.create_account(d["name"], int(d["gb"]), v, uuid_str=gen_uuid(), sub_id=gen_sub_id())
    except Exception as e:      # noqa: BLE001
        return await msg.answer("❌ ساخته نشد: " + str(e)[:250])
    finally:
        await _close(c)
    link = (res or {}).get("sub_link") if isinstance(res, dict) else ""
    await msg.answer(f"✅ کاربرِ <b>{esc(d['name'])}</b> ساخته شد.\n📦 {('نامحدود' if not d['gb'] else fa(d['gb']) + ' گیگ')} · "
                     f"{('بدونِ انقضا' if not v else fa(v) + ' روز')}" + (f"\n\n🔗 <code>{esc(link)}</code>" if link else ""),
                     reply_markup=kb([btn("👤 کارتِ کاربر", f"psel:mgv:{d['pid']}:{d['name']}", style=ButtonStyle.PRIMARY)],
                                     back_row(f"psel:mgP:{d['pid']}")), parse_mode="HTML")


# ───────────────────────── ادمین‌ها ─────────────────────────

@psel_manage_router.callback_query(F.data.regexp(r"^psel:mga:\d+$"))
async def mg_admins(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    pid = int(cb.data.split(":")[2])
    panel = await _panel_of(row, pid)
    if not panel:
        return await cb.answer("پنل پیدا نشد.", show_alert=True)
    if not AD.supports(panel.panel_key):
        return await cb.answer("این نوع پنل مفهومِ «ادمین» ندارد.", show_alert=True)
    c = _open(panel)
    try:
        admins = await c.list_admins()
    except Exception as e:      # noqa: BLE001
        return await cb.answer("پنل جواب نداد: " + str(e)[:150], show_alert=True)
    finally:
        await _close(c)
    admins = [a for a in (admins or []) if isinstance(a, dict) and a.get("username")]
    lines = [f"👨‍💼 <b>ادمین‌هایِ پنل</b> — مجموع {fa(len(admins))}\n"]
    rows = []
    for a in admins[:20]:
        used = a.get("users_usage") if a.get("users_usage") is not None else a.get("used_traffic")
        cnt = a.get("total_users")
        extra = []
        if cnt is not None:
            extra.append(f"{fa(cnt)} کاربر")
        if used is not None:
            try:
                extra.append(f"{_gib(int(float(used)))} گیگ")
            except (TypeError, ValueError):
                pass
        tag = "👑" if a.get("is_sudo") else "👤"
        lines.append(f"{tag} {esc(a['username'])}" + (f" — {' · '.join(extra)}" if extra else ""))
        rows.append([btn(f"{tag} {a['username']}", f"psel:mgah:{pid}:{a['username']}"[:64])])
    rows.append(back_row(f"psel:mgP:{pid}"))
    await edit_or_send(cb, "\n".join(lines), kb(*rows))
    await cb.answer()


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgah:\d+:.+$"))
async def mg_admin_card(cb: CallbackQuery, bot: Bot):
    row = await _guard(cb, bot)
    if not row:
        return
    _, _, pid, name = cb.data.split(":", 3)
    await edit_or_send(cb, f"👤 <b>ادمین {esc(name)}</b>\n\nعملیات رویِ همهٔ کاربرانِ این ادمین:",
                       kb([btn("⛔️ خاموش‌کردنِ همهٔ کاربران", f"psel:mgao:{pid}:{name}:0", style=ButtonStyle.DANGER),
                           btn("✅ روشن‌کردنِ همهٔ کاربران", f"psel:mgao:{pid}:{name}:1", style=ButtonStyle.SUCCESS)],
                          [btn("🔄 ریستِ مصرفِ ادمین", f"psel:mgao:{pid}:{name}:r")],
                          back_row(f"psel:mga:{pid}")))
    await cb.answer()


@psel_manage_router.callback_query(F.data.regexp(r"^psel:mgao:\d+:.+:[01r]$"))
async def mg_admin_op(cb: CallbackQuery, bot: Bot):
    got = await _act(cb, bot, "adm")
    if not got:
        return
    row, panel, pid, name, op = got
    c = _open(panel)
    api = AD.PanelAdminAPI(c)
    try:
        if op == "0":
            await api.disable_users(name)
        elif op == "1":
            await api.activate_users(name)
        else:
            await api.reset_usage(name)
    except Exception as e:      # noqa: BLE001
        return await cb.answer("انجام نشد: " + str(e)[:150], show_alert=True)
    finally:
        await _close(c)
    await cb.answer({"0": "⛔️ همه خاموش شد", "1": "✅ همه روشن شد", "r": "🔄 مصرف ریست شد"}[op], show_alert=True)
