"""
vpn_api.py — APIِ فروشِ VPN برایِ رباتِ «فروش VPN» (PATCH_VPN_API_V1).

تا الان «API ربات» فقط برایِ رباتِ خدماتِ مجازی (SMM) وجود داشت؛ رباتِ فروشِ VPN
بخشِ API نداشت (دکمه‌اش پنهان بود و /api/v1 فقط سرویس‌هایِ SMM را می‌شناخت). این ماژول همان
کلیدها (کلیدِ ربات + کلیدِ شخصیِ کاربر) و همان کیف‌پول را برایِ فروشِ کانفیگ هم کار می‌اندازد.

یک اندپوینتِ ساده با قالبِ key/action (مثلِ v2 برایِ SMM):

    POST|GET  /api/vpn            پارامترها: key, action, ...
    POST|GET  /api/v1/vpn/{action}  (کلید در هدرِ X-API-Key یا پارامترِ key)

اکشن‌ها:
    plans     فهرستِ پلن‌هایِ قابلِ خرید + قیمتِ نهایی برایِ این کلید
    balance   موجودیِ کیف‌پول
    buy       خریدِ پلن از کیف‌پول → ساختِ کانفیگ روی پنل (plan_id[, ref])
    services  فهرستِ سرویس‌هایِ کاربر
    status    وضعیتِ زندهٔ یک سرویس (service = کدِ سرویس / نام کاربریِ پنل / شناسه)

کلیدِ کاملِ ربات (Bot.api_key) برایِ عملیاتِ کاربری پارامترِ telegram_id می‌خواهد؛ کلیدِ شخصیِ کاربر فقط
به خودِ همان کاربر دسترسی دارد و تخفیفِ اختصاصیِ API (api_discount_pct) را می‌گیرد.

خطاها: {"ok": false, "error": "<کد>"} با HTTP 200 (مثلِ v2) — کدها: invalid_api_key، plan_not_found،
insufficient_balance، panel_error، ...
"""
from __future__ import annotations

import asyncio
import time

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from config import Session
from models import (
    Bot as BotRow, BotUser, Order, PanelAccount, VpnPanel, VpnPlan, WalletTxn,
)
from tenant.shared.public_api import (
    AuthCtx, _authed, _v2_params, public_api_router,
)

from fastapi import HTTPException
from logging_setup import get_logger as _get_logger

logger = _get_logger(__name__)

# سقفِ نرخِ ساده (حافظه‌ایِ هر پروسه): جلوگیریِ از حلقهٔ بی‌پایانِ خرید با یک کلیدِ لو رفته/باگ‌دار
_RATE: dict[str, list[float]] = {}
_BUY_PER_MIN = 20
_ANY_PER_MIN = 120


def _rate_ok(key: str, limit: int) -> bool:
    now = time.monotonic()
    arr = [t for t in _RATE.get(key, []) if now - t < 60]
    if len(arr) >= limit:
        _RATE[key] = arr
        return False
    arr.append(now)
    _RATE[key] = arr
    return True


def _err(code: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": code, **extra})


def _fmt_dt(dt) -> str | None:
    return dt.isoformat() if dt else None


async def _target_user(ctx: AuthCtx, p: dict) -> BotUser | None:
    """کاربرِ هدفِ عملیات: کلیدِ شخصی = خودش؛ کلیدِ ربات = telegram_id پارامتر."""
    if not ctx.full_access:
        return ctx.user
    raw = (p.get("telegram_id") or "").strip()
    if not raw.lstrip("-").isdigit():
        return None
    async with Session() as s:
        return await s.scalar(select(BotUser).where(
            BotUser.bot_id == ctx.bot_id, BotUser.telegram_id == int(raw)))


def _final_price(ctx: AuthCtx, base: float) -> int:
    price = float(base)
    if ctx.discount_pct:
        price = price * (100 - ctx.discount_pct) / 100
    return max(0, int(round(price)))


async def _plans(ctx: AuthCtx, user: BotUser | None) -> list[dict]:
    from tenant.bots.vpn.vpn_user import _plan_audience_filter
    aud, _is_rs = await _plan_audience_filter(ctx.bot_id, user.telegram_id if user else None)
    async with Session() as s:
        panels = {p.panel_key: p for p in (await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == ctx.bot_id, VpnPanel.enabled == True))).all()}  # noqa: E712
        plans = (await s.scalars(select(VpnPlan).where(
            VpnPlan.bot_id == ctx.bot_id, VpnPlan.active == True, aud  # noqa: E712
        ).order_by(VpnPlan.sort_order, VpnPlan.id))).all()
    out = []
    for pl in plans:
        if pl.panel_key not in panels:
            continue
        out.append({
            "id": pl.id,
            "title": pl.title,
            "volume_gb": int(pl.volume_gb or 0),      # 0 = نامحدود
            "days": int(pl.days or 0),                # 0 = بی‌انقضا
            "price": _final_price(ctx, float(pl.price)),
            "base_price": int(float(pl.price)),
            "category": pl.category,
            "panel": pl.panel_key,
            "ip_limit": int(pl.limit_ip or 0),
        })
    return out


def _svc_payload(a: PanelAccount) -> dict:
    return {
        "id": a.id,
        "code": a.svc_code,
        "email": a.email,
        "plan": a.plan_title,
        "volume_gb": int(a.volume_gb or 0),
        "volume_mb": a.volume_mb,
        "days": int(a.days or 0),
        "panel": a.panel_key,
        "sub_link": a.sub_link,
        "expires_at": _fmt_dt(a.expires_at),
        "status": a.status,
        "created_at": _fmt_dt(a.created_at),
    }


async def _find_account(ctx: AuthCtx, user: BotUser | None, ident: str) -> PanelAccount | None:
    ident = (ident or "").strip()
    if not ident:
        return None
    async with Session() as s:
        q = select(PanelAccount).where(PanelAccount.bot_id == ctx.bot_id)
        if user is not None:
            q = q.where(PanelAccount.user_telegram_id == user.telegram_id)
        elif not ctx.full_access:
            return None
        conds = [PanelAccount.svc_code == ident, PanelAccount.email == ident]
        if ident.isdigit():
            conds.append(PanelAccount.id == int(ident))
        from sqlalchemy import or_
        return await s.scalar(q.where(or_(*conds)))


async def _do_buy(ctx: AuthCtx, user: BotUser, p: dict) -> JSONResponse:
    raw_plan = (p.get("plan_id") or p.get("plan") or "").strip()
    if not raw_plan.isdigit():
        return _err("plan_id_required")
    plan_id = int(raw_plan)
    ref = (p.get("ref") or "").strip()[:40]

    # idempotency: همان ref دوباره آمد → همان سرویس را برگردان، دوباره پول نگیر
    if ref:
        async with Session() as s:
            prev = await s.scalar(select(WalletTxn).where(
                WalletTxn.bot_id == ctx.bot_id, WalletTxn.user_id == user.id,
                WalletTxn.note.like(f"%[ref:{ref}]%")).order_by(WalletTxn.id.desc()))
        if prev and prev.note and "svc:" in prev.note:
            code = prev.note.split("svc:")[-1].strip().split()[0]
            async with Session() as s:
                acc = await s.scalar(select(PanelAccount).where(
                    PanelAccount.bot_id == ctx.bot_id, PanelAccount.svc_code == code))
            if acc:
                return JSONResponse({"ok": True, "duplicate": True, "service": _svc_payload(acc)})
            return _err("duplicate_ref_in_progress")

    # ۱) اعتبارسنجیِ پلن/پنل و کسرِ اتمیکِ موجودی
    from tenant.bots.vpn.vpn_user import _plan_audience_filter
    aud, _ = await _plan_audience_filter(ctx.bot_id, user.telegram_id)
    async with Session() as s:
        plan = await s.get(VpnPlan, plan_id)
        panel = None
        if plan and plan.bot_id == ctx.bot_id:
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == ctx.bot_id, VpnPanel.panel_key == plan.panel_key))
            ok_aud = await s.scalar(select(VpnPlan.id).where(VpnPlan.id == plan_id, aud))
        else:
            ok_aud = None
        if not plan or plan.bot_id != ctx.bot_id or not plan.active or not panel or not panel.enabled or not ok_aud:
            return _err("plan_not_found")
        price = _final_price(ctx, float(plan.price))
        u = await s.scalar(select(BotUser).where(
            BotUser.id == user.id).with_for_update())
        if not u or u.is_banned:
            return _err("banned" if u else "user_not_found")
        if float(u.balance) < price:
            return _err("insufficient_balance", balance=float(u.balance), price=price)
        u.balance = float(u.balance) - price
        s.add(WalletTxn(bot_id=ctx.bot_id, user_id=u.id, amount=-price, kind="purchase",
                        note=f"خریدِ VPN از راهِ API: {plan.title[:40]}"
                             + (f" [ref:{ref}]" if ref else "")))
        await s.commit()
        user_db_id, tg_id, tg_username = u.id, u.telegram_id, getattr(u, "username", None)
        p_title, p_gb, p_days, p_limit = plan.title, int(plan.volume_gb or 0), int(plan.days or 0), int(plan.limit_ip or 0)
        panel_key = plan.panel_key
        pf = (panel.base_url, panel.username, panel.password, panel.sub_base or "",
              panel.inbound_ids, panel.api_key)
        bot_row = await s.get(BotRow, ctx.bot_id)
        bot_username = bot_row.username if bot_row else None

    # ۲) ساختِ کانفیگ روی پنل (از همان استخرِ لاگین‌شدهٔ مسیرِ تلگرام)
    from tenant.bots.vpn import vpn_user as V
    from tenant.shared.panel_clients import panel_pool as _pool
    from tenant.shared.panel_clients.vpn_panel import gen_uuid, gen_sub_id
    pkw = dict(sub_base=pf[3], inbound_ids_str=pf[4], api_key=pf[5])
    acc = None
    try:
        email = await V._make_config_email(ctx.bot_id, panel_key, bot_username, tg_id, tg_username)
        client = await _pool.get_client(panel_key, pf[0], pf[1], pf[2], **pkw)
        poh = await V.purchase_on_hold_for(ctx.bot_id, panel_key)
        lim = p_limit or await V.panel_default_limit_ip(ctx.bot_id, panel_key)
        acc = await asyncio.wait_for(client.create_account(
            email, gb=p_gb, days=p_days, limit_ip=lim,
            uuid_str=gen_uuid(), sub_id=gen_sub_id(), on_hold=poh), timeout=45)
    except Exception as e:  # noqa: BLE001
        logger.warning("API: ساختِ کانفیگ ناموفق (bot=%s plan=%s): %s", ctx.bot_id, plan_id, e)
        async with Session() as s:
            u2 = await s.scalar(select(BotUser).where(BotUser.id == user_db_id).with_for_update())
            if u2:
                u2.balance = float(u2.balance) + price
                s.add(WalletTxn(bot_id=ctx.bot_id, user_id=u2.id, amount=price, kind="refund",
                                note="عودت: خطای ساختِ کانفیگ (API)"))
            await s.commit()
        try:
            await _pool.drop(panel_key, pf[0], pf[1], pf[2], **pkw)
        except Exception:
            logger.debug("drop pool client failed (بی‌اهمیت)")
        return _err("panel_error", message=str(e)[:200], refunded=True)

    # ۳) ذخیرهٔ سرویس + سفارش (برایِ آمارِ ادمین)
    async with Session() as s:
        new_acc = PanelAccount(
            bot_id=ctx.bot_id, user_telegram_id=tg_id, email=email, panel_key=panel_key,
            uuid=acc.get("uuid"), sub_id=acc.get("sub_id"), sub_link=acc.get("sub_link") or "",
            volume_gb=p_gb, days=p_days, plan_title=p_title, status="active",
            paid_toman=int(price or 0) or None,
            svc_code=await V.new_svc_code(s, ctx.bot_id),
            expires_at=V._expires_at_from_ms(acc.get("expiry_ms")), first_conn_notified=False)
        s.add(new_acc)
        s.add(Order(bot_id=ctx.bot_id, user_id=user_db_id, volume_gb=p_gb, days=p_days, count=1,
                    amount=price, pay_method="wallet", status="paid", panel_key=panel_key,
                    plan_id=plan_id))
        await s.flush()
        code = new_acc.svc_code
        # ref را به همان تراکنشِ کیف‌پول وصل کن تا تکرارِ درخواست دوباره پول نگیرد
        if ref:
            tx = await s.scalar(select(WalletTxn).where(
                WalletTxn.bot_id == ctx.bot_id, WalletTxn.user_id == user_db_id,
                WalletTxn.note.like(f"%[ref:{ref}]%")).order_by(WalletTxn.id.desc()))
            if tx and "svc:" not in (tx.note or ""):
                tx.note = f"{tx.note} svc:{code}"
        await s.commit()
        await s.refresh(new_acc)
        payload = _svc_payload(new_acc)
        bal = await s.scalar(select(BotUser.balance).where(BotUser.id == user_db_id))
    # اطلاعِ خرید به ادمین‌ها/کانالِ اعتمادسازی (مثلِ بقیهٔ مسیرهایِ فروش) — بی‌صدا و غیرمسدودکننده
    asyncio.ensure_future(_notify_sale(ctx.bot_id, user_db_id, p_title, price, code))
    return JSONResponse({"ok": True, "charged": price, "balance": float(bal or 0), "service": payload},
                        status_code=200)


async def _notify_sale(bot_id: int, user_db_id: int, title: str, price, code) -> None:
    try:
        from aiogram import Bot as _AiogramBot
        from tenant import _purchase_alert
        async with Session() as s:
            _u = await s.get(BotUser, user_db_id)
            _row = await s.get(BotRow, bot_id)
        if not (_row and _u):
            return
        _tb = _AiogramBot(token=_row.token)
        try:
            await _purchase_alert(_tb, _row, buyer=_u, section="API (سرویس VPN)",
                                  detail=str(title or "سرویس VPN"), amount=float(price or 0),
                                  tracking_code=str(code) if code else None)
        finally:
            await _tb.session.close()
    except Exception:
        logger.exception("vpn_api: اطلاعِ خرید به مالک ناموفق بود (نادیده گرفته شد)")


async def dispatch(ctx: AuthCtx, action: str, p: dict) -> JSONResponse:
    if not _rate_ok(f"any:{ctx.bot_id}:{ctx.user.id if ctx.user else 0}", _ANY_PER_MIN):
        return _err("rate_limited")

    if action == "plans":
        user = await _target_user(ctx, p) if (not ctx.full_access or p.get("telegram_id")) else None
        items = await _plans(ctx, user)
        return JSONResponse({"ok": True, "count": len(items), "items": items})

    user = await _target_user(ctx, p)
    if action == "balance":
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        return JSONResponse({"ok": True, "balance": float(user.balance), "currency": "TOMAN"})

    if action == "buy":
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        if not _rate_ok(f"buy:{ctx.bot_id}:{user.id}", _BUY_PER_MIN):
            return _err("rate_limited")
        return await _do_buy(ctx, user, p)

    if action == "services":
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        async with Session() as s:
            accs = (await s.scalars(select(PanelAccount).where(
                PanelAccount.bot_id == ctx.bot_id, PanelAccount.user_telegram_id == user.telegram_id,
                PanelAccount.status != "deleted").order_by(PanelAccount.id.desc()).limit(200))).all()
        return JSONResponse({"ok": True, "count": len(accs), "items": [_svc_payload(a) for a in accs]})

    if action == "status":
        acc = await _find_account(ctx, user, p.get("service") or p.get("email") or p.get("code") or "")
        if not acc:
            return _err("service_not_found")
        from tenant.bots.vpn import vpn_user as V
        async with Session() as s:
            panel = await V._panel_for_account(s, ctx.bot_id, acc)
        usage = None
        if panel:
            try:
                client = V._client_from_panel(panel)
                try:
                    usage = await asyncio.wait_for(client.get_usage(acc.email), timeout=15)
                finally:
                    await client.close()
            except Exception as e:  # noqa: BLE001
                logger.info("API status: خواندنِ مصرف ناموفق (%s): %s", acc.email, e)
        out = _svc_payload(acc)
        if usage:
            used = int(usage.get("up", 0) or 0) + int(usage.get("down", 0) or 0)
            total = int(usage.get("total", 0) or 0)
            exp_ms = int(usage.get("expiryTime", 0) or 0)
            out.update({
                "used_bytes": used,
                "total_bytes": total,
                "remaining_bytes": max(total - used, 0) if total else None,
                "expiry_ms": exp_ms or None,
                "enabled": bool(usage.get("enable", True)),
                "online_at_ms": usage.get("last_online_ms"),
                "live": True,
            })
        else:
            out["live"] = False
        return JSONResponse({"ok": True, "service": out})

    return _err("incorrect_action")


async def _run(request: Request, action: str | None, header_key: str | None = None) -> JSONResponse:
    p = await _v2_params(request)
    key = (p.get("key") or header_key or "").strip()
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    try:
        ctx = await _authed(key)
    except HTTPException as e:
        return _err("invalid_api_key" if e.status_code == 401 else str(e.detail))
    act = (action or p.get("action") or "").strip().lower()
    # فقط رباتِ فروشِ VPN
    async with Session() as s:
        b = await s.get(BotRow, ctx.bot_id)
    if not b or b.field != "vpn_sales":
        return _err("not_a_vpn_bot")
    try:
        return await dispatch(ctx, act, p)
    except Exception:
        logger.exception("VPN API: خطای پیش‌بینی‌نشده (action=%s)", act)
        return _err("internal_error")


@public_api_router.api_route("/api/vpn", methods=["GET", "POST"])
async def vpn_api(request: Request):
    return await _run(request, None)


@public_api_router.api_route("/api/v1/vpn/{action}", methods=["GET", "POST"])
async def vpn_api_v1(action: str, request: Request):
    return await _run(request, action, request.headers.get("x-api-key"))
