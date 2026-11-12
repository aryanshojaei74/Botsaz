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
    custom    قیمت/بازهٔ حجمِ دلخواه (+ quote با gb و days)           [buy هم با gb+days(+panel) کار می‌کند]
    quote     قیمتِ نهایی با discount_code بدونِ خرید (plan_id یا gb+days)
    delete    حذفِ سرویس از پنل و ربات (service)
    charge    فاکتورِ شارژِ کیف‌پول با آپی (amount) ← pay_url؛  charge_status (order_id)
    trial     دریافتِ تستِ رایگان (طبقِ تنظیماتِ سراسریِ تست؛ هر کاربر یک‌بار)
    withdraw_info / withdraw (amount, destination) / withdraws   برداشتِ وجه از کیف‌پول (اگر مالک روشن کرده باشد)
    transfer_info / transfer (to, amount)   انتقالِ موجودی به کاربرِ دیگرِ همین ربات (اگر مالک روشن کرده باشد)
    ticket    ثبتِ تیکت (text[, subject])؛  tickets فهرستِ تیکت‌ها (+ پاسخ‌ها)؛  ticket_reply (ticket_id, text)

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
from sqlalchemy import func, or_, select

from config import Session
from models import (
    Bot as BotRow, BotUser, Order, PanelAccount, ReferralJoin, VpnPanel, VpnPlan, WalletTxn,
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



def _int_or_none(v) -> int | None:
    v = str(v if v is not None else "").strip()
    return int(v) if v.lstrip("-").isdigit() else None


async def _custom_panels(ctx: AuthCtx) -> list:
    """پنل‌هایِ فعال که «فروشِ حجمِ دلخواه» رویشان روشن است."""
    from tenant.bots.vpn.vpn_user import _vc
    async with Session() as s:
        panels = (await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == ctx.bot_id, VpnPanel.enabled == True).order_by(VpnPanel.id))).all()  # noqa: E712
    return [pn for pn in panels if (await _vc(ctx.bot_id, "vc_enabled", pn.panel_key)) == "1"]


async def _custom_info(ctx: AuthCtx, user: BotUser | None, p: dict) -> JSONResponse:
    """action=custom: قیمت و بازهٔ حجم/مدتِ فروشِ دلخواه (زنده از تنظیماتِ ربات). با gb/days قیمتِ دقیقِ همان ترکیب هم می‌آید."""
    from tenant.bots.vpn.vpn_user import _vc_all_for_user, _vc_gb_cost
    gb_q, days_q = _int_or_none(p.get("gb") or p.get("volume_gb")), _int_or_none(p.get("days"))
    want = (p.get("panel") or "").strip()
    items = []
    for pn in await _custom_panels(ctx):
        if want and pn.panel_key != want:
            continue
        cfg = await _vc_all_for_user(ctx.bot_id, user.telegram_id if user else 0, pn.panel_key,
                                     days=days_q, gb=gb_q)
        item = {
            "panel": pn.panel_key,
            "price_per_gb": _final_price(ctx, float(cfg["vc_price_gb"])),
            "price_per_day": _final_price(ctx, float(cfg["vc_price_day"])),
            "min_gb": int(cfg["vc_min_gb"]), "max_gb": int(cfg["vc_max_gb"]),
            "min_days": int(cfg["vc_min_days"]), "max_days": int(cfg["vc_max_days"]),
        }
        if gb_q is not None and days_q is not None:
            item["quote"] = _final_price(ctx, float(_vc_gb_cost(cfg, gb_q) + days_q * int(cfg["vc_price_day"])))
        items.append(item)
    return JSONResponse({"ok": True, "count": len(items), "items": items})


async def _resolve_spec(ctx: AuthCtx, user: BotUser, p: dict, s) -> tuple[dict | None, JSONResponse | None]:
    """مشخصاتِ خرید از روی plan_id (پلنِ آماده) یا gb+days(+panel) (حجمِ دلخواه). (spec, خطا).
    قیمتِ برگشتی = قیمتِ بعد از تخفیفِ اختصاصیِ کلید و *قبل* از کدِ تخفیف."""
    raw_plan = (p.get("plan_id") or p.get("plan") or "").strip()
    gb_raw = (p.get("gb") or p.get("volume_gb") or "").strip()
    if raw_plan:
        if not raw_plan.isdigit():
            return None, _err("plan_id_required")
        from tenant.bots.vpn.vpn_user import _plan_audience_filter
        aud, _ = await _plan_audience_filter(ctx.bot_id, user.telegram_id)
        plan = await s.get(VpnPlan, int(raw_plan))
        panel = ok_aud = None
        if plan and plan.bot_id == ctx.bot_id:
            panel = await s.scalar(select(VpnPanel).where(
                VpnPanel.bot_id == ctx.bot_id, VpnPanel.panel_key == plan.panel_key))
            ok_aud = await s.scalar(select(VpnPlan.id).where(VpnPlan.id == plan.id, aud))
        if not plan or plan.bot_id != ctx.bot_id or not plan.active or not panel or not panel.enabled or not ok_aud:
            return None, _err("plan_not_found")
        return {"kind": "plan", "plan_id": plan.id, "title": plan.title, "gb": int(plan.volume_gb or 0),
                "days": int(plan.days or 0), "limit_ip": int(plan.limit_ip or 0), "panel_key": plan.panel_key,
                "price": _final_price(ctx, float(plan.price)),
                "pf": (panel.base_url, panel.username, panel.password, panel.sub_base or "",
                       panel.inbound_ids, panel.api_key)}, None
    if not gb_raw:
        return None, _err("plan_id_required")
    gb, days = _int_or_none(gb_raw), _int_or_none(p.get("days"))
    if gb is None or gb <= 0:
        return None, _err("invalid_gb")
    if days is None or days <= 0:
        return None, _err("days_required")
    cands = await _custom_panels(ctx)
    want = (p.get("panel") or "").strip()
    if want:
        cands = [x for x in cands if x.panel_key == want]
    if not cands:
        return None, _err("custom_not_available" if not want else "panel_not_found")
    if len(cands) > 1:
        return None, _err("panel_required", panels=[x.panel_key for x in cands])
    pn = cands[0]
    from tenant.bots.vpn.vpn_user import _vc_all_for_user, _vc_gb_cost
    cfg = await _vc_all_for_user(ctx.bot_id, user.telegram_id, pn.panel_key, days=days, gb=gb)
    lo_g, hi_g, lo_d, hi_d = (int(cfg["vc_min_gb"]), int(cfg["vc_max_gb"]),
                               int(cfg["vc_min_days"]), int(cfg["vc_max_days"]))
    if not (lo_g <= gb <= hi_g):
        return None, _err("gb_out_of_range", min=lo_g, max=hi_g)
    if not (lo_d <= days <= hi_d):
        return None, _err("days_out_of_range", min=lo_d, max=hi_d)
    base = _vc_gb_cost(cfg, gb) + days * int(cfg["vc_price_day"])
    if base <= 0:
        return None, _err("custom_not_available")
    return {"kind": "custom", "plan_id": None, "title": f"حجم دلخواه {gb} گیگ / {days} روز", "gb": gb,
            "days": days, "limit_ip": 0, "panel_key": pn.panel_key, "price": _final_price(ctx, float(base)),
            "pf": (pn.base_url, pn.username, pn.password, pn.sub_base or "", pn.inbound_ids, pn.api_key)}, None


async def _apply_code(ctx: AuthCtx, user: BotUser, spec: dict, code: str, s):
    """(DiscountResult|None, خطا|None). کد خالی ⇒ (None, None)."""
    code = (code or "").strip()
    if not code:
        return None, None
    from tenant.gateways.discount_engine import validate_discount
    res = await validate_discount(bot_id=ctx.bot_id, code=code, telegram_id=user.telegram_id,
                                  price=int(spec["price"]), panel_key=spec["panel_key"],
                                  plan_id=spec["plan_id"], session=s)
    if not res.ok:
        return None, _err("invalid_discount", message=res.reason)
    return res, None


async def _quote(ctx: AuthCtx, user: BotUser, p: dict) -> JSONResponse:
    """action=quote: قیمتِ نهایی (با کدِ تخفیف) بدونِ خرید و بدونِ ثبتِ مصرفِ کد."""
    async with Session() as s:
        spec, err = await _resolve_spec(ctx, user, p, s)
        if err:
            return err
        res, err = await _apply_code(ctx, user, spec, p.get("discount_code") or p.get("code") or "", s)
        if err:
            return err
    final = int(res.final_price) if res else int(spec["price"])
    return JSONResponse({"ok": True, "price": int(spec["price"]), "discount": int(res.discount) if res else 0,
                         "final": final, "discount_code": res.code if res else None,
                         "title": spec["title"], "volume_gb": spec["gb"], "days": spec["days"]})


async def _do_buy(ctx: AuthCtx, user: BotUser, p: dict) -> JSONResponse:
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

    # ۱) اعتبارسنجیِ پلن/حجمِ دلخواه، کدِ تخفیف و کسرِ اتمیکِ موجودی (کد هم در همین تراکنش مصرف می‌شود)
    from tenant.gateways.discount_engine import record_usage, revert_usage
    disc_code_id, disc_amount, disc_code = None, 0, None
    async with Session() as s:
        spec, err = await _resolve_spec(ctx, user, p, s)
        if err:
            return err
        res, err = await _apply_code(ctx, user, spec, p.get("discount_code") or p.get("code") or "", s)
        if err:
            return err
        price = int(res.final_price) if res else int(spec["price"])
        u = await s.scalar(select(BotUser).where(
            BotUser.id == user.id).with_for_update())
        if not u or u.is_banned:
            return _err("banned" if u else "user_not_found")
        if float(u.balance) < price:
            return _err("insufficient_balance", balance=float(u.balance), price=price)
        u.balance = float(u.balance) - price
        if res:
            disc_code_id, disc_amount, disc_code = res.code_id, int(res.discount), res.code
            await record_usage(s, code_id=disc_code_id, bot_id=ctx.bot_id, telegram_id=u.telegram_id,
                               saved_amount=disc_amount)
        s.add(WalletTxn(bot_id=ctx.bot_id, user_id=u.id, amount=-price, kind="purchase",
                        note=f"خریدِ VPN از راهِ API: {spec['title'][:40]}"
                             + (f" [ref:{ref}]" if ref else "")))
        await s.commit()
        user_db_id, tg_id, tg_username = u.id, u.telegram_id, getattr(u, "username", None)
        p_title, p_gb, p_days, p_limit = spec["title"], spec["gb"], spec["days"], spec["limit_ip"]
        panel_key, plan_id, pf = spec["panel_key"], spec["plan_id"], spec["pf"]
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
            if disc_code_id:
                await revert_usage(s, code_id=disc_code_id, telegram_id=tg_id)
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
    out = {"ok": True, "charged": price, "balance": float(bal or 0), "service": payload}
    if disc_code:
        out.update({"discount": disc_amount, "discount_code": disc_code})
    return JSONResponse(out, status_code=200)


_DEL_PER_MIN = 10
_CHG_PER_MIN = 6


# === PATCH_API_REPORTS_V1 === (Weirdnet) گزارشِ تست/حذفِ سرویس که از طریقِ API انجام می‌شود هم به کانالِ مالک برود
async def _with_tmp_bot(bot_id: int, fn) -> None:
    """fn(bot) را با یک Botِ موقت (از توکنِ ربات) اجرا می‌کند؛ هر خطایی فقط لاگ می‌شود."""
    try:
        from aiogram import Bot as _AiogramBot
        async with Session() as s:
            _row = await s.get(BotRow, bot_id)
        if not _row:
            return
        _tb = _AiogramBot(token=_row.token)
        try:
            await fn(_tb)
        finally:
            await _tb.session.close()
    except Exception:
        logger.exception("vpn_api: ارسالِ گزارش به کانالِ مالک ناموفق بود (نادیده گرفته شد)")


async def _do_delete(ctx: AuthCtx, user: BotUser | None, p: dict) -> JSONResponse:
    """action=delete: حذفِ سرویس از پنل و از ربات (بدونِ برگشتِ پول). فقط وقتی پنل واقعاً حذف کرد (یا حساب از قبل نبود)
    ردیفِ محلی پاک می‌شود؛ خطایِ پنل ⇒ panel_error و سرویس سرجایش می‌ماند تا دوباره تلاش شود."""
    acc = await _find_account(ctx, user, p.get("service") or p.get("email") or p.get("code") or "")
    if not acc:
        return _err("service_not_found")
    async with Session() as s:
        panel = await s.scalar(select(VpnPanel).where(
            VpnPanel.bot_id == ctx.bot_id, VpnPanel.panel_key == (acc.panel_key or "sanai")))
    if panel:
        from tenant.shared.trial_cleanup import _delete_from_panel
        pf = (panel.panel_key, panel.base_url, panel.username, panel.password, panel.sub_base or "",
              panel.inbound_ids, panel.api_key)
        ok, why = await _delete_from_panel(pf, acc.email, ctx.bot_id)
        if not ok:
            return _err("panel_error", message=(why or "")[:200])
    # === PATCH_API_REPORTS_V1 === گزارشِ حذف (قبل از پاک‌شدنِ ردیف، چون از رویِ ردیف ساخته می‌شود)
    try:
        from tenant.bots.vpn.vpn_user import _report_service_deleted
        async with Session() as s:
            _owner_u = await s.scalar(select(BotUser).where(
                BotUser.bot_id == ctx.bot_id, BotUser.telegram_id == acc.user_telegram_id))
        await _with_tmp_bot(ctx.bot_id, lambda b: _report_service_deleted(b, ctx.bot_id, acc.id, _owner_u))
    except Exception:
        logger.exception("vpn_api: گزارشِ حذفِ سرویس ناموفق بود (نادیده گرفته شد)")
    async with Session() as s:
        a2 = await s.get(PanelAccount, acc.id)
        if a2:
            await s.delete(a2)
            await s.commit()
    return JSONResponse({"ok": True, "deleted": True, "service": acc.svc_code or acc.email})


async def _do_charge(ctx: AuthCtx, user: BotUser, p: dict) -> JSONResponse:
    """action=charge: فاکتورِ شارژِ کیف‌پول با درگاهِ آپی (همان مسیرِ مینی‌اپ). بعد از پرداخت موجودیِ ربات خودکار زیاد می‌شود."""
    amount = _int_or_none(p.get("amount"))
    if not amount or amount <= 0:
        return _err("amount_required")
    from tenant.gateways import apay as AP
    async with Session() as s:
        row = await s.get(BotRow, ctx.bot_id)
    if not row:
        return _err("internal_error")
    data, err = await AP.create_charge_core(row, user.telegram_id, amount)
    if not data:
        if (err or "").startswith("open:"):
            return _err("open_invoice", order_id=err.split(":", 1)[1])
        return _err("charge_failed", message=(err or "")[:200])
    return JSONResponse({"ok": True, **data})


async def _charge_status(ctx: AuthCtx, user: BotUser, p: dict) -> JSONResponse:
    oid = (p.get("order_id") or "").strip()
    if not oid:
        return _err("order_id_required")
    from tenant.gateways import apay as AP
    st = await AP.mini_status(ctx.bot_id, user.telegram_id, oid)
    if st is None:
        return _err("order_not_found")
    return JSONResponse({"ok": True, **st})


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


# === PATCH_VPN_API_TRIAL_TICKET_V1 === (Weirdnet: «دریافت تست و ثبت تیکت و پاسخ هم اضافه شود»)
async def _api_trial_report(bot_id: int, user: BotUser, link, volume: str, duration: str, panel_key, source: str) -> None:
    """گزارشِ تستِ دریافت‌شده از API در کانال/ادمین‌هایِ مالک (همان گزارشِ تستِ داخلِ ربات، با گیتِ پلنِ همان)."""
    try:
        import tenant as T
        await _with_tmp_bot(bot_id, lambda b: T._trial_report_post(
            b, bot_id, user, getattr(user, "username", None), link or "", volume, duration, panel_key, source))
    except Exception:
        logger.exception("vpn_api: گزارشِ تست ناموفق بود (نادیده گرفته شد)")


async def _do_trial(ctx: AuthCtx, user: BotUser, p: dict) -> JSONResponse:
    """تستِ رایگانِ API — فقط تنظیماتِ سراسریِ تست (trial_enabled/trial_gb/trial_days/trial_hours/trial_source). قانون‌هایِ اختصاصیِ هر دکمهٔ تست اعمال نمی‌شود."""
    import tenant as T
    from models import TrialUsage
    from tenant.shared.panel_clients.vpn_panel import PanelError, gen_uuid, gen_sub_id
    from tenant.bots.vpn.vpn_user import _make_config_email, new_svc_code
    if not _rate_ok(f"trial:{ctx.bot_id}:{user.id}", 3):
        return _err("rate_limited")
    if await T.get_setting(ctx.bot_id, "trial_enabled", "0") != "1":
        return _err("trial_disabled")
    exempt = await T._trial_exempt(ctx.bot_id, user.telegram_id)
    try:
        gb = float(await T.get_setting(ctx.bot_id, "trial_gb", "2"))
    except (ValueError, TypeError):
        gb = 2.0
    try:
        days = int(await T.get_setting(ctx.bot_id, "trial_days", "3"))
    except (ValueError, TypeError):
        days = 3
    try:
        hours = int(await T.get_setting(ctx.bot_id, "trial_hours", "0"))
    except (ValueError, TypeError):
        hours = 0
    source = await T.get_setting(ctx.bot_id, "trial_source", "panel")
    # === PATCH_API_TRIAL_PANEL_V1 === (Weirdnet) انتخابِ پنلِ تست: panel = کلید یا شناسهٔ پنل (action=panels فهرست می‌دهد)؛
    # قبل از ثبتِ مصرف بررسی می‌شود تا پنلِ اشتباه، حقِ تستِ کاربر را نسوزاند. بدونِ panel ⇒ همان رفتارِ قبل (اولین پنلِ فعال).
    want = (p.get("panel") or p.get("panel_key") or p.get("panel_id") or "").strip()
    trial_panel = bot_row = None
    if source != "stock":
        async with Session() as s:
            q = select(VpnPanel).where(VpnPanel.bot_id == ctx.bot_id, VpnPanel.enabled == True)   # noqa: E712
            if want:
                q = q.where(or_(VpnPanel.panel_key == want, VpnPanel.id == int(want)) if want.isdigit()
                            else VpnPanel.panel_key == want)
            trial_panel = await s.scalar(q.order_by(VpnPanel.id).limit(1))
            bot_row = await s.get(BotRow, ctx.bot_id)
        if not trial_panel:
            return _err("panel_not_found" if want else "no_panel")
    # ثبتِ مصرف پیش از تحویل (جلوگیریِ از دوبار گرفتن)؛ شکست ⇒ برگشت
    if not exempt:
        async with Session() as s:
            if await s.scalar(select(TrialUsage).where(TrialUsage.bot_id == ctx.bot_id, TrialUsage.user_telegram_id == user.telegram_id)):
                return _err("trial_already_used")
            s.add(TrialUsage(bot_id=ctx.bot_id, user_telegram_id=user.telegram_id))
            try:
                await s.commit()
            except Exception:      # noqa: BLE001
                await s.rollback()
                return _err("trial_already_used")

    async def _rollback():
        if exempt:
            return
        try:
            async with Session() as s:
                tu = await s.scalar(select(TrialUsage).where(TrialUsage.bot_id == ctx.bot_id, TrialUsage.user_telegram_id == user.telegram_id))
                if tu:
                    await s.delete(tu)
                    await s.commit()
        except Exception:      # noqa: BLE001
            logger.exception("vpn_api trial: برگشتِ ثبتِ مصرف ناموفق")

    if source == "stock":
        from models import Config
        async with Session() as s:
            cfg = await s.scalar(select(Config).where(Config.bot_id == ctx.bot_id, Config.status == "in_stock", Config.price == 0,
                                                      Config.volume_gb >= int(gb)).order_by(Config.id).limit(1).with_for_update())
            if not cfg:
                await _rollback()
                return _err("trial_out_of_stock")
            cfg.status, cfg.owner_user_id = "sold", user.id
            payload, vol, d = cfg.payload, cfg.volume_gb, cfg.days
            await s.commit()
        # === PATCH_API_REPORTS_V1 ===
        await _api_trial_report(ctx.bot_id, user, payload, f"{vol} گیگ" if vol else "نامحدود", f"{d} روز" if d else "بی‌انقضا",
                                None, "stock")
        return JSONResponse({"ok": True, "trial": {"source": "stock", "config": payload, "volume_gb": vol, "days": d}})

    panel = trial_panel
    client = T._trial_panel_client(panel)
    try:
        email = await _make_config_email(ctx.bot_id, panel.panel_key, bot_row.username if bot_row else None,
                                         user.telegram_id, user.full_name or "")
        on_hold = await T.get_setting(ctx.bot_id, "trial_on_hold", "0") == "1"
        if gb < 1:
            acc = await client.create_account(email, gb=0, days=days, limit_ip=0, uuid_str=gen_uuid(), sub_id=gen_sub_id(),
                                              mb=int(round(gb * 1024)), hours=hours, on_hold=on_hold)
        else:
            acc = await client.create_account(email, gb=int(gb), days=days, limit_ip=0, uuid_str=gen_uuid(), sub_id=gen_sub_id(),
                                              hours=hours, on_hold=on_hold)
        sub_link = acc.get("sub_link") or ""
        async with Session() as s:
            new_acc = PanelAccount(bot_id=ctx.bot_id, user_telegram_id=user.telegram_id, email=email, panel_key=panel.panel_key,
                                   uuid=acc.get("uuid"), sub_id=acc.get("sub_id"), sub_link=sub_link,
                                   volume_gb=int(gb) if gb >= 1 else 0, days=days, plan_title="تست رایگان", status="active",
                                   svc_code=await new_svc_code(s, ctx.bot_id), first_conn_notified=False)
            s.add(new_acc)
            await s.commit()
            a = new_acc
        out = _svc_payload(a)
        out["trial"] = True
        # === PATCH_API_REPORTS_V1 ===
        _vol_lbl = (f"{int(round(gb * 1024))} مگابایت" if gb < 1 else f"{int(gb)} گیگ")
        _dur_lbl = (f"{hours} ساعت" if hours and not days else (f"{days} روز" if days else "بی‌انقضا"))
        await _api_trial_report(ctx.bot_id, user, sub_link, _vol_lbl, _dur_lbl, panel.panel_key, "panel")
        return JSONResponse({"ok": True, "service": out})
    except PanelError as e:
        await _rollback()
        logger.warning("vpn_api trial: خطایِ پنل: %s", e)
        return _err("panel_error")
    except Exception:      # noqa: BLE001
        await _rollback()
        logger.exception("vpn_api trial: خطایِ غیرمنتظره")
        return _err("internal_error")
    finally:
        try:
            await client.close()
        except Exception:      # noqa: BLE001
            pass


# === PATCH_API_PROFILE_V1 === (Weirdnet) پنل‌ها، پروفایل، رفرال، کلیدِ API، تراکنش‌ها و تاریخچهٔ پرداخت
async def _panels_info(ctx: AuthCtx) -> JSONResponse:
    """action=panels: پنل‌هایِ فعال (برایِ انتخابِ پنلِ تست/خرید)."""
    from tenant.bots.vpn.vpn_admin import panel_label
    from tenant.bots.vpn.vpn_user import _vc
    import tenant as T
    async with Session() as s:
        panels = (await s.scalars(select(VpnPanel).where(
            VpnPanel.bot_id == ctx.bot_id, VpnPanel.enabled == True).order_by(VpnPanel.id))).all()   # noqa: E712
    items = []
    for pn in panels:
        try:
            name = await panel_label(ctx.bot_id, pn.panel_key)
        except Exception:      # noqa: BLE001
            name = pn.panel_key
        items.append({"id": pn.id, "panel": pn.panel_key, "name": name,
                      "custom_enabled": (await _vc(ctx.bot_id, "vc_enabled", pn.panel_key)) == "1"})
    return JSONResponse({"ok": True, "count": len(items), "items": items,
                         "trial_enabled": await T.get_setting(ctx.bot_id, "trial_enabled", "0") == "1"})


async def _referral_payload(ctx: AuthCtx, user: BotUser) -> dict:
    from tenant.shared.extras import _get_referral_reward, get_or_create_referral
    async with Session() as s:
        brow = await s.get(BotRow, ctx.bot_id)
    uname = (brow.username if brow else "") or ""
    ref = await get_or_create_referral(ctx.bot_id, user, uname, await _get_referral_reward(ctx.bot_id))
    async with Session() as s:
        joins = int(await s.scalar(select(func.count()).select_from(ReferralJoin).where(
            ReferralJoin.bot_id == ctx.bot_id, ReferralJoin.inviter_user_id == user.id)) or 0)
        earned = int(await s.scalar(select(func.coalesce(func.sum(ReferralJoin.reward_paid), 0)).where(
            ReferralJoin.bot_id == ctx.bot_id, ReferralJoin.inviter_user_id == user.id)) or 0)
    out = {"code": ref.code, "link": f"https://t.me/{uname}?start=ref_{ref.code}" if uname else None,
           "clicks": int(ref.click_count or 0), "joins": max(joins, int(ref.join_count or 0)), "reward_earned": earned}
    try:
        from tenant.shared import ref_points as RP
        if await RP.points_enabled(ctx.bot_id):
            out["points"] = int(await RP.get_points(ctx.bot_id, user.id))
    except Exception:      # noqa: BLE001
        logger.debug("vpn_api referral: امتیاز خوانده نشد", exc_info=True)
    return out


async def _do_profile(ctx: AuthCtx, user: BotUser) -> JSONResponse:
    from tenant.shared.public_api import user_api_allowed
    async with Session() as s:
        total = int(await s.scalar(select(func.count()).select_from(PanelAccount).where(
            PanelAccount.bot_id == ctx.bot_id, PanelAccount.user_telegram_id == user.telegram_id,
            PanelAccount.status != "deleted")) or 0)
        active = int(await s.scalar(select(func.count()).select_from(PanelAccount).where(
            PanelAccount.bot_id == ctx.bot_id, PanelAccount.user_telegram_id == user.telegram_id,
            PanelAccount.status == "active")) or 0)
    return JSONResponse({"ok": True, "profile": {
        "telegram_id": user.telegram_id, "full_name": user.full_name, "username": user.username,
        "balance": float(user.balance), "currency": "TOMAN", "services_total": total, "services_active": active,
        "referral": await _referral_payload(ctx, user),
        "api": {"has_key": bool(user.api_key), "allowed": await user_api_allowed(user),
                "discount_pct": float(user.api_discount_pct or 0)}}})


async def _do_apikey(ctx: AuthCtx, user: BotUser) -> JSONResponse:
    """action=apikey: کلیدِ APIِ شخصیِ همین کاربر (برایِ نمایش در وب‌سایت/پروفایل)."""
    from tenant.shared.public_api import user_api_allowed
    return JSONResponse({"ok": True, "has_key": bool(user.api_key), "api_key": user.api_key or None,
                         "allowed": await user_api_allowed(user)})


_PAYMENT_KINDS = ("charge", "apay_charge", "swap_charge", "hooshpay_charge", "blupal_charge", "frenzyex_charge")


async def _do_history(ctx: AuthCtx, user: BotUser, action: str, p: dict) -> JSONResponse:
    """action=transactions: همهٔ تراکنش‌هایِ کیف‌پول | action=payments: فقط شارژهایِ موفق (واریز). پارامترها: limit (۱–۱۰۰)، offset، kind."""
    limit = min(max(_int_or_none(p.get("limit")) or 20, 1), 100)
    offset = max(_int_or_none(p.get("offset")) or 0, 0)
    conds = [WalletTxn.bot_id == ctx.bot_id, WalletTxn.user_id == user.id]
    if action == "payments":
        conds += [WalletTxn.amount > 0, WalletTxn.kind.in_(_PAYMENT_KINDS)]
    kind = (p.get("kind") or "").strip()
    if kind:
        conds.append(WalletTxn.kind == kind)
    async with Session() as s:
        total = int(await s.scalar(select(func.count()).select_from(WalletTxn).where(*conds)) or 0)
        rows = (await s.scalars(select(WalletTxn).where(*conds).order_by(WalletTxn.id.desc())
                                .offset(offset).limit(limit))).all()
    return JSONResponse({"ok": True, "total": total, "limit": limit, "offset": offset,
                         "items": [{"id": t.id, "amount": float(t.amount), "kind": t.kind, "note": t.note,
                                    "created_at": _fmt_dt(t.created_at)} for t in rows]})


def _tk_payload(t, replies=None) -> dict:
    d = {"id": t.id, "status": t.status, "subject": t.subject, "text": t.text,
         "created_at": _fmt_dt(t.created_at)}
    if replies is not None:
        d["replies"] = [{"from": r.sender, "text": r.text, "at": _fmt_dt(r.created_at)} for r in replies]
    return d


async def _do_ticket(ctx: AuthCtx, user: BotUser, action: str, p: dict) -> JSONResponse:
    from models import TenantTicket, TenantTicketReply
    if action == "ticket":
        text = str(p.get("text") or "").strip()
        if not text or len(text) > 3000:
            return _err("invalid_text")
        if not _rate_ok(f"tk:{ctx.bot_id}:{user.id}", 6):
            return _err("rate_limited")
        subject = (str(p.get("subject") or "").strip()[:100]) or None
        async with Session() as s:
            t = TenantTicket(bot_id=ctx.bot_id, user_id=user.id, text=text, subject=subject)
            s.add(t)
            await s.flush()
            tid = t.id
            await s.commit()
        await _notify_ticket(ctx.bot_id, user, tid, text, subject, reply=False)
        return JSONResponse({"ok": True, "ticket": {"id": tid, "status": "open"}})
    if action == "tickets":
        tid = _int_or_none(p.get("ticket_id") or p.get("id"))
        async with Session() as s:
            if tid:
                t = await s.scalar(select(TenantTicket).where(TenantTicket.id == tid, TenantTicket.bot_id == ctx.bot_id, TenantTicket.user_id == user.id))
                if not t:
                    return _err("ticket_not_found")
                reps = (await s.scalars(select(TenantTicketReply).where(TenantTicketReply.ticket_id == t.id).order_by(TenantTicketReply.id))).all()
                return JSONResponse({"ok": True, "ticket": _tk_payload(t, reps)})
            items = (await s.scalars(select(TenantTicket).where(TenantTicket.bot_id == ctx.bot_id, TenantTicket.user_id == user.id)
                                     .order_by(TenantTicket.id.desc()).limit(50))).all()
        return JSONResponse({"ok": True, "count": len(items), "items": [_tk_payload(t) for t in items]})
    # ticket_reply
    tid = _int_or_none(p.get("ticket_id") or p.get("id"))
    text = str(p.get("text") or "").strip()
    if not tid:
        return _err("ticket_id_required")
    if not text or len(text) > 3000:
        return _err("invalid_text")
    if not _rate_ok(f"tkr:{ctx.bot_id}:{user.id}", 12):
        return _err("rate_limited")
    async with Session() as s:
        t = await s.scalar(select(TenantTicket).where(TenantTicket.id == tid, TenantTicket.bot_id == ctx.bot_id, TenantTicket.user_id == user.id).with_for_update())
        if not t:
            return _err("ticket_not_found")
        if t.status == "closed":
            return _err("ticket_closed")
        s.add(TenantTicketReply(ticket_id=t.id, sender="user", text=text))
        t.status = "open"
        subj = t.subject
        await s.commit()
    await _notify_ticket(ctx.bot_id, user, tid, text, subj, reply=True)
    return JSONResponse({"ok": True, "ticket": {"id": tid, "status": "open"}})


async def _notify_ticket(bot_id: int, user: BotUser, tid: int, text: str, subject, *, reply: bool) -> None:
    """اطلاعِ تیکتِ API به ادمین‌هایِ ربات (با دکمهٔ پاسخ)؛ شکست‌خوردنش نباید پاسخِ API را خراب کند."""
    try:
        from aiogram import Bot as _AiogramBot
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        from tenant import _notify_admins
        async with Session() as s:
            _row = await s.get(BotRow, bot_id)
        if not _row:
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 پاسخ", callback_data=f"tkt:reply:{tid}:{user.telegram_id}")]])
        head = (f"💬 پاسخِ جدید به تیکت #{tid}" if reply else f"🎫 تیکت جدید #{tid} (از طریقِ API)") + f"\nکاربر: <code>{user.telegram_id}</code>"
        if subject:
            head += f"\nموضوع: <b>{_html_escape(subject)}</b>"
        _tb = _AiogramBot(token=_row.token)
        try:
            await _notify_admins(_tb, _row, f"{head}\n\n{_html_escape(text)}", kb)
        finally:
            await _tb.session.close()
    except Exception:      # noqa: BLE001
        logger.exception("vpn_api: اطلاعِ تیکت به ادمین ناموفق بود")


def _html_escape(t) -> str:
    import html as _h
    return _h.escape(str(t or ""))


async def dispatch(ctx: AuthCtx, action: str, p: dict) -> JSONResponse:
    if not _rate_ok(f"any:{ctx.bot_id}:{ctx.user.id if ctx.user else 0}", _ANY_PER_MIN):
        return _err("rate_limited")

    if action == "plans":
        user = await _target_user(ctx, p) if (not ctx.full_access or p.get("telegram_id")) else None
        items = await _plans(ctx, user)
        return JSONResponse({"ok": True, "count": len(items), "items": items})

    if action == "panels":         # PATCH_API_TRIAL_PANEL_V1
        return await _panels_info(ctx)

    user = await _target_user(ctx, p)
    if action in ("profile", "referral", "apikey", "transactions", "payments"):   # PATCH_API_PROFILE_V1
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        if action == "profile":
            return await _do_profile(ctx, user)
        if action == "referral":
            return JSONResponse({"ok": True, "referral": await _referral_payload(ctx, user)})
        if action == "apikey":
            return await _do_apikey(ctx, user)
        return await _do_history(ctx, user, action, p)
    if action == "balance":
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        return JSONResponse({"ok": True, "balance": float(user.balance), "currency": "TOMAN"})

    if action == "custom":
        u2 = user if (not ctx.full_access or p.get("telegram_id")) else None
        return await _custom_info(ctx, u2, p)

    if action == "quote":
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        return await _quote(ctx, user, p)

    if action == "delete":
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        if not _rate_ok(f"del:{ctx.bot_id}:{user.id}", _DEL_PER_MIN):
            return _err("rate_limited")
        return await _do_delete(ctx, user, p)

    if action in ("charge", "charge_status"):
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        if not _rate_ok(f"chg:{ctx.bot_id}:{user.id}", _CHG_PER_MIN):
            return _err("rate_limited")
        return await (_do_charge(ctx, user, p) if action == "charge" else _charge_status(ctx, user, p))

    if action == "buy":
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        if not _rate_ok(f"buy:{ctx.bot_id}:{user.id}", _BUY_PER_MIN):
            return _err("rate_limited")
        return await _do_buy(ctx, user, p)

    if action == "trial":          # PATCH_VPN_API_TRIAL_TICKET_V1
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        return await _do_trial(ctx, user, p)

    if action in ("withdraw_info", "withdraw", "withdraws"):      # PATCH_API_WITHDRAW_V1 (Weirdnet)
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        from tenant.shared import withdraw as W
        if action == "withdraw_info":
            return JSONResponse({"ok": True, **await W.api_info(ctx.bot_id, user)})
        if action == "withdraws":
            items = await W.api_list(ctx.bot_id, user.id)
            return JSONResponse({"ok": True, "count": len(items), "items": items})
        if not _rate_ok(f"wd:{ctx.bot_id}:{user.id}", 5):
            return _err("rate_limited")
        amt_raw = str(p.get("amount") or "").replace(",", "").strip()
        amt = int(amt_raw) if amt_raw.isdigit() else None
        code, w = await W.api_request(ctx.bot_id, user, amt, str(p.get("destination") or p.get("card") or p.get("dest") or ""))
        if code != "ok":
            return _err(code)

        async def _tell(b):
            async with Session() as s2:
                _row = await s2.get(BotRow, ctx.bot_id)
            await W.notify_admins_new(b, _row, user, w)
        await _with_tmp_bot(ctx.bot_id, _tell)
        return JSONResponse({"ok": True, "request": {"id": w.id, "amount": int(w.amount), "status": "pending"}})

    if action in ("transfer_info", "transfer"):      # PATCH_API_TRANSFER_V1 (Weirdnet: «انتقال موجودی هم به API اضافه شود»)
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        return await _do_transfer(ctx, user, action, p)

    if action in ("ticket", "tickets", "ticket_reply"):
        if user is None:
            return _err("telegram_id_required" if ctx.full_access else "user_not_found")
        return await _do_ticket(ctx, user, action, p)

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


async def _do_transfer(ctx: AuthCtx, user: BotUser, action: str, p: dict) -> JSONResponse:
    """PATCH_API_TRANSFER_V1 — انتقالِ موجودی بینِ کاربرانِ همین ربات، دقیقاً با همان قاعدهٔ داخلِ ربات
    (حالتِ «خاموش/با تأییدِ ادمین/خودکار» که مالک تعیین کرده؛ از «مالی و کیف پول ← 🔁 انتقال موجودی»).
    transfer_info: وضعیت و حداقل مبلغ.  transfer: to (آیدیِ عددی یا @یوزرنیمِ گیرنده) + amount."""
    from tenant.shared import balance_transfer as BT
    mode = await BT.get_mode(ctx.bot_id)
    if action == "transfer_info":
        return JSONResponse({"ok": True, "enabled": mode != "off", "mode": mode, "min_amount": BT.MIN_AMOUNT,
                             "needs_admin_approval": mode == "approve"})
    if mode == "off":
        return _err("transfer_disabled")
    if not _rate_ok(f"btr:{ctx.bot_id}:{user.id}", 10):
        return _err("rate_limited")
    amt = BT.parse_amount(str(p.get("amount") or "")) or 0
    if amt < BT.MIN_AMOUNT:
        return _err("invalid_amount", min_amount=BT.MIN_AMOUNT)
    to = await BT.find_user(ctx.bot_id, str(p.get("to") or p.get("destination") or p.get("recipient") or ""))
    if to is None:
        return _err("recipient_not_found")
    if to.id == user.id:
        return _err("same_user")
    res, t = await BT.do_transfer(ctx.bot_id, user.id, to.id, amt, mode)
    if res == "no_balance":
        return _err("insufficient_balance")
    if res == "invalid" or t is None:
        return _err("invalid_transfer")

    async def _tell(b):
        async with Session() as s2:
            _row = await s2.get(BotRow, ctx.bot_id)
        if res == "done":
            await BT._notify_user(b, to.telegram_id, f"💰 <b>{BT.fmt(amt)}</b> از طرفِ کاربرِ دیگری به کیف پولتان منتقل شد.")
            return
        from tenant import _notify_admins
        import html as _h
        fn_ = _h.escape(user.full_name or "") + (f" (@{user.username})" if user.username else "")
        tn_ = _h.escape(to.full_name or "") + (f" (@{to.username})" if to.username else "")
        await _notify_admins(
            b, _row,
            f"🔁 <b>درخواستِ انتقالِ موجودی #{t.id}</b> (از طریقِ API)\n\n👤 از: {fn_} (<code>{user.telegram_id}</code>)\n"
            f"👤 به: {tn_} (<code>{to.telegram_id}</code>)\n💵 مبلغ: <b>{BT.fmt(amt)}</b>",
            BT._kb([[BT._btn("✅ تأیید", f"btr:ap:{t.id}", BT.ButtonStyle.SUCCESS),
                     BT._btn("❌ رد", f"btr:rj:{t.id}", BT.ButtonStyle.DANGER)]]))
    await _with_tmp_bot(ctx.bot_id, _tell)
    async with Session() as s3:
        me = await s3.get(BotUser, user.id)
    return JSONResponse({"ok": True, "status": "done" if res == "done" else "pending",
                         "transfer": {"id": t.id, "amount": amt, "to": to.telegram_id},
                         "balance": float(me.balance) if me else None})


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
