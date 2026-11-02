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
