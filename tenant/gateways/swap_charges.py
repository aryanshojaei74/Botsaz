"""شارژ کیف پول از طریق «تشخیص واریز» سواپ‌ولت (SwapPay).

این ماژول مستقل است و جدولش در اولین استفاده ساخته می‌شود تا نیازی به
دست‌کاری models.py نباشد.

روش کار: چون SwapPay اندپوینت «ساخت لینک پرداخت برای شخص ثالث» ندارد، شارژ
خودکار از راه «تشخیص واریز» انجام می‌شود:
  ۱) برای هر درخواست شارژ، یک «مقدار کریپتوی یکتا» تولید می‌کنیم (که با درخواست‌های
     در انتظارِ دیگرِ همان ربات تداخل نداشته باشد) تا واریزها قابل‌تشخیص باشند،
  ۲) کاربر دقیقاً همان مقدار را به آدرس واریز حساب سواپ‌ولتِ ادمین می‌فرستد،
  ۳) با خواندن تراکنش‌ها، واریز موفق با همان مقدار را پیدا و کیف را شارژ می‌کنیم.

برای جلوگیری از شارژ دوباره، شناسهٔ تراکنش مصرف‌شده (matched_tx_id) یکتا نگه
داشته می‌شود.
"""
import datetime
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import (
    BigInteger, Column, DateTime, Integer, Numeric, String, UniqueConstraint, select, update,
)

from config import Session, engine
from models import Base

# دقت اعشار مقدار یکتا بر اساس توکن
_DECIMALS = {"USDT": 2, "TRX": 2, "TON": 4, "ETH": 6, "BNB": 5}


class SwapCharge(Base):
    __tablename__ = "swap_charges"
    __table_args__ = (UniqueConstraint("bot_id", "matched_tx_id", name="uq_swap_bot_tx"),)

    id = Column(Integer, primary_key=True)
    bot_id = Column(Integer, index=True, nullable=False)
    user_telegram_id = Column(BigInteger, index=True, nullable=False)
    amount_toman = Column(Numeric(14, 0), default=0)     # مبلغ تقریبی درخواست‌شده (تومان)
    coin = Column(String(10), nullable=False)            # USDT/TON/TRX/...
    network = Column(String(10), default="")             # TRON/TON/BSC
    expected_amount = Column(String(40), nullable=False)  # مقدار کریپتوی یکتا (رشته دقیق)
    status = Column(String(20), default="waiting")        # waiting/paid/canceled/expired
    matched_tx_id = Column(BigInteger, nullable=True)     # شناسهٔ تراکنش سواپ‌ولت که شارژ کرد
    credited_toman = Column(Numeric(14, 0), nullable=True)  # تومانی که واقعاً شارژ شد
    last_seen_id = Column(BigInteger, default=0)          # cursor شروع جستجو
    created_at = Column(DateTime, default=datetime.datetime.now)


_ready = False


async def ensure_table() -> None:
    global _ready
    if _ready:
        return
    async with engine.begin() as conn:
        await conn.run_sync(SwapCharge.__table__.create, checkfirst=True)
    _ready = True


def _quantize(amount: Decimal, coin: str) -> Decimal:
    dec = _DECIMALS.get(coin, 4)
    q = Decimal(1).scaleb(-dec)  # مثلاً 0.01
    return amount.quantize(q, rounding=ROUND_DOWN)


async def make_unique_amount(bot_id: int, coin: str, base_amount: Decimal) -> str:
    """یک مقدار کریپتوی یکتا (رشته) تولید می‌کند که با درخواست‌های در انتظارِ
    همان ربات/توکن تداخل نداشته باشد. با افزودن کوچک‌ترین واحد اعشاری یکتا می‌سازد."""
    await ensure_table()
    dec = _DECIMALS.get(coin, 4)
    step = Decimal(1).scaleb(-dec)
    base = _quantize(base_amount, coin)
    if base <= 0:
        base = step
    async with Session() as s:
        rows = (await s.scalars(
            select(SwapCharge.expected_amount).where(
                SwapCharge.bot_id == bot_id, SwapCharge.coin == coin,
                SwapCharge.status == "waiting",
            )
        )).all()
    taken = set(rows or [])
    cand = base
    for _ in range(500):
        key = format(cand, "f")
        if key not in taken:
            return key
        cand += step
    return format(base, "f")


async def create_charge(bot_id: int, tg_id: int, amount_toman: int, coin: str,
                        network: str, expected_amount: str, last_seen_id: int) -> int:
    await ensure_table()
    async with Session() as s:
        c = SwapCharge(bot_id=bot_id, user_telegram_id=tg_id, amount_toman=amount_toman,
                       coin=coin, network=network, expected_amount=expected_amount,
                       status="waiting", last_seen_id=last_seen_id or 0)
        s.add(c)
        await s.commit()
        return c.id


async def get_charge(charge_id: int) -> SwapCharge | None:
    await ensure_table()
    async with Session() as s:
        return await s.get(SwapCharge, charge_id)


async def list_waiting(max_age_minutes: int | None = None) -> list[dict]:
    """درخواست‌های در انتظار را به‌صورت dict ساده برمی‌گرداند (برای poller)."""
    await ensure_table()
    async with Session() as s:
        rows = (await s.scalars(
            select(SwapCharge).where(SwapCharge.status == "waiting")
            .order_by(SwapCharge.id)
        )).all()
    out = []
    now = datetime.datetime.now()
    for r in rows:
        if max_age_minutes is not None and r.created_at:
            if (now - r.created_at).total_seconds() > max_age_minutes * 60:
                continue
        out.append({
            "id": r.id, "bot_id": r.bot_id, "user_telegram_id": r.user_telegram_id,
            "amount_toman": int(r.amount_toman or 0), "coin": r.coin,
            "expected_amount": r.expected_amount, "last_seen_id": int(r.last_seen_id or 0),
        })
    return out


async def expire_older_than(minutes: int) -> int:
    """درخواست‌های در انتظارِ قدیمی‌تر از N دقیقه را منقضی می‌کند؛ تعداد را برمی‌گرداند."""
    await ensure_table()
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=minutes)
    n = 0
    async with Session() as s:
        rows = (await s.scalars(
            select(SwapCharge).where(SwapCharge.status == "waiting",
                                     SwapCharge.created_at < cutoff)
        )).all()
        for r in rows:
            r.status = "expired"
            n += 1
        await s.commit()
    return n


async def is_tx_used(bot_id: int, tx_id: int) -> bool:
    await ensure_table()
    async with Session() as s:
        r = await s.scalar(select(SwapCharge.id).where(
            SwapCharge.bot_id == bot_id, SwapCharge.matched_tx_id == tx_id))
        return r is not None


async def mark_paid(charge_id: int, tx_id: int, credited_toman: int) -> bool:
    """درخواست را پرداخت‌شده علامت می‌زند. به‌خاطر یکتا بودن matched_tx_id، اگر
    همان تراکنش قبلاً مصرف شده باشد، False برمی‌گرداند (جلوگیری از شارژ دوباره).

    از UPDATE اتمیکِ «WHERE status != paid» استفاده می‌شود (نه read-then-write)
    تا اگر دو فراخوانیِ هم‌زمان برای همان charge_id برسند (مثلاً وبهوک و
    چکِ دستیِ کاربر تقریباً هم‌زمان)، فقط یکی از آن‌ها rowcount=1 بگیرد و
    کیف پول را شارژ کند — نه هر دو."""
    await ensure_table()
    async with Session() as s:
        result = await s.execute(
            update(SwapCharge)
            .where(SwapCharge.id == charge_id, SwapCharge.status != "paid")
            .values(status="paid", matched_tx_id=tx_id, credited_toman=credited_toman)
        )
        if result.rowcount == 0:
            # یا رکورد وجود ندارد، یا (شاید همین لحظه توسط یک فراخوانیِ
            # هم‌زمانِ دیگر) قبلاً paid شده — در هر دو حالت دوباره شارژ نمی‌کنیم.
            await s.rollback()
            return False
        try:
            await s.commit()
            return True
        except Exception:
            await s.rollback()
            return False
