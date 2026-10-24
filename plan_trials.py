"""درخواست تست پلن‌های رشد و حرفه‌ای.

این ماژول مستقل است و فقط از Base/Session موجود استفاده می‌کند تا نیازی به
دست‌کاری models.py نباشد. جدول مربوطه در اولین استفاده (یا هنگام init_db)
به‌صورت خودکار ساخته می‌شود.

چرخهٔ یک درخواست:
    pending  → کاربر درخواست داده، منتظر تایید ادمین
    approved → ادمین برای N روز تایید کرده (تایمر از لحظهٔ تایید شروع می‌شود)
    used     → کاربر با همین تست یک ربات ساخته است (به bot_id وصل شده)
    rejected → ادمین رد کرده
    expired  → مهلت تست تمام شده (و در صورت وجود، ربات به پلن پایه برگشته)
"""
import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Integer, String, select,
)

from config import Session, engine
from models import Base
from plans import Plan

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

# پلن‌هایی که قابل درخواست تست هستند
TRIALABLE_PLANS = (Plan.GROWTH.value, Plan.PRO.value)


class PlanTrialRequest(Base):
    __tablename__ = "plan_trial_requests"

    id = Column(Integer, primary_key=True)
    user_telegram_id = Column(BigInteger, index=True, nullable=False)
    user_name = Column(String(120), default="")
    plan = Column(String(20), nullable=False)            # مقدار پلن درخواستی
    status = Column(String(20), default="pending")        # pending/approved/used/rejected/expired
    days = Column(Integer, default=0)                     # مدت تایید‌شده (روز)
    note = Column(String(400), default="")                # توضیح اختیاری کاربر
    bot_id = Column(Integer, nullable=True)               # ربات ساخته‌شده با این تست
    created_at = Column(DateTime, default=datetime.datetime.now)
    approved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    expiry_notified = Column(Boolean, default=False)
    revoked_at = Column(DateTime, nullable=True)           # زمان لغوِ دستیِ ادمین
    revoke_note = Column(String(400), default="")          # دلیلِ لغو (اختیاری)


_table_ready = False


async def ensure_trials_table() -> None:
    """ساخت جدول در صورت نبودن (idempotent)."""
    global _table_ready
    if _table_ready:
        return
    async with engine.begin() as conn:
        await conn.run_sync(PlanTrialRequest.__table__.create, checkfirst=True)
    _table_ready = True


async def create_request(tg_id: int, name: str, plan: str, note: str = "") -> tuple[str, int | None]:
    """ثبت درخواست تست جدید.

    خروجی: (وضعیت, شناسهٔ درخواست)
        ok             → ثبت شد
        exists_pending → یک درخواست در انتظار بررسی دارد
        exists_active  → یک تست فعال (تاییدشده یا درحالِ‌استفاده) دارد
        already_used   → قبلاً همین پلن را یک‌بار تست/مصرف کرده — PATCH_TRIAL_ONE_TIME_V1
        invalid_plan   → پلن نامعتبر
    """
    if plan not in TRIALABLE_PLANS:
        return ("invalid_plan", None)
    await ensure_trials_table()
    now = datetime.datetime.now()
    async with Session() as s:
        pend = await s.scalar(
            select(PlanTrialRequest).where(
                PlanTrialRequest.user_telegram_id == tg_id,
                PlanTrialRequest.status == "pending",
            )
        )
        if pend:
            return ("exists_pending", pend.id)
        act = await s.scalar(
            select(PlanTrialRequest).where(
                PlanTrialRequest.user_telegram_id == tg_id,
                PlanTrialRequest.status.in_(("approved", "used")),
                PlanTrialRequest.expires_at > now,
            )
        )
        if act:
            return ("exists_active", act.id)
        # === PATCH_TRIAL_STRICT_ONE_PER_USER_V1 ===
        # قبلاً این چک دو تا سوراخ داشت:
        #   ۱) فقط برایِ *همون پلن* بود (plan == plan) — یعنی یک نفر
        #      می‌تونست هم «رشد» رو تست کنه هم «حرفه‌ای» رو، دو تستِ رایگان.
        #   ۲) فقط وقتی حساب می‌شد که واقعاً باهاش ربات ساخته باشه
        #      (bot_id.isnot(None)) — یعنی اگه تست تایید می‌شد ولی کاربر
        #      ربات نمی‌ساخت و مهلتش تموم می‌شد، دوباره می‌تونست درخواست بده.
        # طبقِ خواستِ ادمین، الان سخت‌گیرانه‌ست: هر کاربر در کلِ عمرِ حسابش
        # فقط و فقط یک‌بار می‌تونه تست بگیره — فارغ از اینکه کدوم پلن بوده
        # و فارغ از اینکه ازش استفاده کرده یا نه.
        used_before = await s.scalar(
            select(PlanTrialRequest).where(
                PlanTrialRequest.user_telegram_id == tg_id,
                PlanTrialRequest.status.in_(("approved", "used", "expired")),
            )
        )
        if used_before:
            return ("already_used", used_before.id)
        # === PATCH_TRIAL_NO_RENEW_V1 ===
        # تستِ ۲ روزه‌ای که کاربر از یک *رباتِ همکار* گرفته هم حساب
        # می‌شود. بدونِ این، کاربر می‌توانست یک تست از همکار بگیرد و یک
        # تستِ دیگر از بات‌سازِ اصلی — دو رباتِ رایگان.
        try:
            from models import PartnerSale
            partner_trial = await s.scalar(
                select(PartnerSale.id).where(
                    PartnerSale.buyer_telegram_id == tg_id,
                    PartnerSale.kind == "trial",
                )
            )
            if partner_trial:
                return ("already_used", None)
        except Exception:
            logger.exception("بررسیِ سابقهٔ تستِ همکار ناموفق بود")
        req = PlanTrialRequest(
            user_telegram_id=tg_id, user_name=(name or "")[:120],
            plan=plan, status="pending", note=(note or "")[:400],
        )
        s.add(req)
        await s.commit()
        return ("ok", req.id)


async def list_pending() -> list[dict]:
    await ensure_trials_table()
    async with Session() as s:
        rows = (await s.scalars(
            select(PlanTrialRequest)
            .where(PlanTrialRequest.status == "pending")
            .order_by(PlanTrialRequest.created_at)
        )).all()
        return [
            {"id": r.id, "tg_id": r.user_telegram_id, "name": r.user_name,
             "plan": r.plan, "note": r.note, "created_at": r.created_at}
            for r in rows
        ]


async def get_request(req_id: int) -> dict | None:
    await ensure_trials_table()
    async with Session() as s:
        r = await s.get(PlanTrialRequest, req_id)
        if not r:
            return None
        return {"id": r.id, "tg_id": r.user_telegram_id, "name": r.user_name,
                "plan": r.plan, "status": r.status, "days": r.days,
                "expires_at": r.expires_at, "note": r.note}


# === PATCH_TRIAL_ADMIN_GIFT_V1 ===
async def grant_direct(tg_id: int, name: str, plan: str, days: int) -> tuple[str, dict | None]:
    """اهدای مستقیمِ تست توسط ادمین (بدونِ نیاز به درخواستِ کاربر).
    اگه کاربر یه درخواستِ pending داشته باشه، همون تایید می‌شه؛ وگرنه یه
    رکوردِ تاییدشدهٔ جدید ساخته می‌شه. محدودیتِ «یک‌بار مصرف» اینجا اعمال
    نمی‌شه چون تصمیمِ آگاهانهٔ خودِ ادمینه.

    خروجی: (وضعیت, دیکشنری)
        ok            → اهدا شد (دیکشنری مثلِ approve_request)
        exists_active → همین الان یه تستِ فعال داره (دیکشنری همون تست)
        invalid_plan  → پلن نامعتبر
    """
    if plan not in TRIALABLE_PLANS:
        return ("invalid_plan", None)
    await ensure_trials_table()
    now = datetime.datetime.now()
    async with Session() as s:
        act = await s.scalar(
            select(PlanTrialRequest).where(
                PlanTrialRequest.user_telegram_id == tg_id,
                PlanTrialRequest.status.in_(("approved", "used")),
                PlanTrialRequest.expires_at > now,
            )
        )
        if act:
            return ("exists_active", {
                "id": act.id, "tg_id": act.user_telegram_id, "plan": act.plan,
                "days": act.days, "expires_at": act.expires_at})
        pend = await s.scalar(
            select(PlanTrialRequest).where(
                PlanTrialRequest.user_telegram_id == tg_id,
                PlanTrialRequest.status == "pending",
            )
        )
        if pend:
            r = pend
            r.plan = plan
        else:
            r = PlanTrialRequest(
                user_telegram_id=tg_id, user_name=(name or "")[:120],
                plan=plan, note="اهدای مستقیم توسط مدیر")
            s.add(r)
        r.status = "approved"
        r.days = int(days)
        r.approved_at = now
        r.expires_at = now + datetime.timedelta(days=int(days))
        r.expiry_notified = False
        await s.commit()
        return ("ok", {"id": r.id, "tg_id": r.user_telegram_id, "plan": r.plan,
                       "days": r.days, "expires_at": r.expires_at})


async def approve_request(req_id: int, days: int) -> dict | None:
    """تایید درخواست برای مدت مشخص. تایمر از همین لحظه شروع می‌شود."""
    await ensure_trials_table()
    now = datetime.datetime.now()
    async with Session() as s:
        r = await s.get(PlanTrialRequest, req_id)
        if not r or r.status != "pending":
            return None
        r.status = "approved"
        r.days = int(days)
        r.approved_at = now
        r.expires_at = now + datetime.timedelta(days=int(days))
        r.expiry_notified = False
        await s.commit()
        return {"id": r.id, "tg_id": r.user_telegram_id, "plan": r.plan,
                "days": r.days, "expires_at": r.expires_at}


async def reject_request(req_id: int) -> dict | None:
    await ensure_trials_table()
    async with Session() as s:
        r = await s.get(PlanTrialRequest, req_id)
        if not r or r.status != "pending":
            return None
        r.status = "rejected"
        await s.commit()
        return {"id": r.id, "tg_id": r.user_telegram_id, "plan": r.plan}


async def active_trial_for(tg_id: int, plan: str) -> dict | None:
    """تست فعال (تاییدشده، مصرف‌نشده، منقضی‌نشده) برای این کاربر و پلن."""
    if plan not in TRIALABLE_PLANS:
        return None
    await ensure_trials_table()
    now = datetime.datetime.now()
    async with Session() as s:
        r = await s.scalar(
            select(PlanTrialRequest).where(
                PlanTrialRequest.user_telegram_id == tg_id,
                PlanTrialRequest.plan == plan,
                PlanTrialRequest.status == "approved",
                PlanTrialRequest.bot_id.is_(None),
                PlanTrialRequest.expires_at > now,
            ).order_by(PlanTrialRequest.expires_at.desc())
        )
        if not r:
            return None
        days_left = max((r.expires_at - now).days, 0)
        return {"id": r.id, "plan": r.plan, "expires_at": r.expires_at,
                "days": r.days, "days_left": days_left}


async def consume_trial(req_id: int, bot_id: int) -> datetime.datetime | None:
    """علامت‌گذاری تست به‌عنوان مصرف‌شده و اتصال آن به رباتِ ساخته‌شده."""
    await ensure_trials_table()
    async with Session() as s:
        r = await s.get(PlanTrialRequest, req_id)
        if not r or r.status != "approved" or r.bot_id is not None:
            return None
        r.status = "used"
        r.bot_id = bot_id
        await s.commit()
        return r.expires_at


async def active_or_used_trials_for_user(tg_id: int) -> list[dict]:
    """تست‌های تاییدشده یا مصرف‌شدهٔ این کاربر که هنوز لغو/منقضی نشده‌اند
    (برای نمایش در پروفایل کاربر + دکمهٔ «لغو تست»)."""
    await ensure_trials_table()
    async with Session() as s:
        rows = (await s.scalars(
            select(PlanTrialRequest).where(
                PlanTrialRequest.user_telegram_id == tg_id,
                PlanTrialRequest.status.in_(("approved", "used")),
            ).order_by(PlanTrialRequest.created_at.desc())
        )).all()
        return [
            {"id": r.id, "plan": r.plan, "status": r.status, "bot_id": r.bot_id,
             "expires_at": r.expires_at}
            for r in rows
        ]


async def revoked_trials_for_user(tg_id: int) -> list[dict]:
    """سابقهٔ تست‌های لغوشدهٔ این کاربر (برای نمایش در پروفایل کاربر)."""
    await ensure_trials_table()
    async with Session() as s:
        rows = (await s.scalars(
            select(PlanTrialRequest).where(
                PlanTrialRequest.user_telegram_id == tg_id,
                PlanTrialRequest.status == "revoked",
            ).order_by(PlanTrialRequest.revoked_at.desc())
        )).all()
        return [
            {"id": r.id, "plan": r.plan, "revoked_at": r.revoked_at,
             "revoke_note": r.revoke_note}
            for r in rows
        ]


async def revoke_request(req_id: int, note: str = "") -> dict | None:
    """لغوِ دستیِ یک تستِ تاییدشده/مصرف‌شده توسط ادمین.

    اگر با این تست ربات ساخته شده باشد (status == used)، پلنِ آن ربات فوراً
    به پلنِ پایه برمی‌گردد؛ در غیر این صورت فقط وضعیتِ درخواست «revoked»
    می‌شود تا کاربر دیگر نتواند از آن استفاده کند.
    """
    from models import Bot as BotRow

    await ensure_trials_table()
    now = datetime.datetime.now()
    async with Session() as s:
        r = await s.get(PlanTrialRequest, req_id)
        if not r or r.status not in ("approved", "used"):
            return None
        reverted_bot = None
        if r.status == "used" and r.bot_id:
            bot = await s.get(BotRow, r.bot_id)
            # === PATCH_TRIAL_EXPIRY_IGNORES_REAL_SUBSCRIPTION_V1 ===
            # همان محافظِ بالا: اگر کاربر بعداً واقعاً همین پلن را خریده
            # (سررسیدِ آینده‌دار دارد)، لغوِ این تستِ قدیمی نباید پلنِ
            # واقعی‌اش را به رایگان برگرداند.
            has_valid_subscription = bool(
                bot and bot.plan_expires_at and bot.plan_expires_at > now)
            if (bot and not getattr(bot, "archived_at", None)
                    and bot.plan == r.plan and not has_valid_subscription):
                bot.plan = Plan.FREE.value
                bot.plan_expires_at = now
                bot.last_expiry_notice = None
                reverted_bot = {"username": bot.username, "token": bot.token}
        r.status = "revoked"
        r.revoked_at = now
        r.revoke_note = (note or "")[:400]
        await s.commit()
        return {"id": r.id, "tg_id": r.user_telegram_id, "plan": r.plan,
                "reverted_bot": reverted_bot}


async def used_trial_for_bot(bot_id: int) -> dict | None:
    """تستِ فعال (مصرف‌شده، متصل به این ربات) — برای پیشنهادِ «درخواستِ تمدید» به کاربر."""
    await ensure_trials_table()
    async with Session() as s:
        r = await s.scalar(
            select(PlanTrialRequest).where(
                PlanTrialRequest.bot_id == bot_id,
                PlanTrialRequest.status == "used",
            ).order_by(PlanTrialRequest.id.desc())
        )
        if not r:
            return None
        now = datetime.datetime.now()
        return {"id": r.id, "tg_id": r.user_telegram_id, "plan": r.plan,
                "expires_at": r.expires_at,
                "expired": bool(r.expires_at and r.expires_at <= now)}


async def extend_trial(req_id: int, extra_days: int) -> dict | None:
    """تمدیدِ یک تستِ مصرف‌شده (افزودنِ روز به سررسیدِ فعلی؛ اگر منقضی شده بود، از همین لحظه شروع می‌شود)."""
    await ensure_trials_table()
    now = datetime.datetime.now()
    async with Session() as s:
        r = await s.get(PlanTrialRequest, req_id)
        if not r or r.status != "used":
            return None
        base = r.expires_at if r.expires_at and r.expires_at > now else now
        r.expires_at = base + datetime.timedelta(days=int(extra_days))
        r.days = (r.days or 0) + int(extra_days)
        r.expiry_notified = False
        await s.commit()
        return {"id": r.id, "tg_id": r.user_telegram_id, "plan": r.plan,
                "bot_id": r.bot_id, "expires_at": r.expires_at}


async def expire_due_trials() -> list[dict]:
    """تست‌های منقضی‌شده را رسیدگی می‌کند.

    - تست‌های مصرف‌شده (used) که مهلتشان تمام شده و تمدید نشده‌اند: ربات
      مربوطه به‌طور خودکار بایگانی/لغو می‌شود (مگر اینکه کاربر پلن را واقعاً
      خریده باشد که در این صورت r.plan دیگر برابر پلنِ فعلیِ ربات نیست).
    - تست‌های تاییدشدهٔ مصرف‌نشده که مهلتشان گذشته: منقضی می‌شوند.

    خروجی: فهرستی از موارد جهت اطلاع‌رسانی توسط ربات اصلی، هر کدام شامل:
        {kind, owner_id, username, token, plan}
        kind = "archived" (ربات به‌خاطرِ عدمِ تمدیدِ تست لغو/بایگانی شد) یا
               "lapsed" (تست بدون استفاده منقضی شد)
    """
    from models import Bot as BotRow

    await ensure_trials_table()
    now = datetime.datetime.now()
    out: list[dict] = []
    async with Session() as s:
        due = (await s.scalars(
            select(PlanTrialRequest).where(
                PlanTrialRequest.status.in_(("used", "approved")),
                PlanTrialRequest.expiry_notified == False,  # noqa: E712
                PlanTrialRequest.expires_at.isnot(None),
                PlanTrialRequest.expires_at <= now,
            )
        )).all()
        for r in due:
            if r.status == "used" and r.bot_id:
                bot = await s.get(BotRow, r.bot_id)
                # === PATCH_TRIAL_EXPIRY_IGNORES_REAL_SUBSCRIPTION_V1 ===
                # باگِ گزارش‌شده: این شرط فقط اسمِ پلن را مقایسه می‌کرد
                # (bot.plan == r.plan)، نه این‌که آیا سررسیدِ واقعیِ ربات
                # هنوز آینده است یا نه. نتیجه: کاربری که تستِ رایگانِ «رشد»
                # گرفته بود و بعداً مستقل همان پلن را خریده بود (با
                # plan_expires_at هفته‌ها/ماه‌ها جلوتر)، با رسیدنِ سررسیدِ
                # همان تستِ قدیمی (که ربطی به خریدِ واقعی‌اش نداشت)
                # «بی‌دلیل» بایگانی می‌شد — و چون دلیلش «trial_expired» بود
                # (که تا همین الان لیبل هم نداشت)، برایِ ادمین «نامشخص»
                # نشان داده می‌شد. حالا اگر ربات سررسیدِ واقعیِ آینده‌داری
                # داشته باشد (یعنی واقعاً خریده)، این رکوردِ تستِ قدیمی
                # دیگر نباید بایگانیش کند.
                has_valid_subscription = bool(
                    bot and bot.plan_expires_at and bot.plan_expires_at > now)
                if (bot and not getattr(bot, "archived_at", None)
                        and bot.plan == r.plan and not has_valid_subscription):
                    bot.status = "paused"
                    bot.archived_at = now
                    bot.archive_reason = "trial_expired"
                    out.append({"kind": "archived", "owner_id": bot.owner_id,
                                "username": bot.username, "token": bot.token,
                                "plan": r.plan})
                r.status = "expired"
                r.expiry_notified = True
            else:
                # تست تاییدشده ولی استفاده‌نشده
                r.status = "expired"
                r.expiry_notified = True
                out.append({"kind": "lapsed", "owner_id": r.user_telegram_id,
                            "username": "", "token": "", "plan": r.plan})
        await s.commit()
    return out
