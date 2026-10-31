"""
dice_texts.py — PATCH_DICE_CUSTOM_TEXTS_V1

متن‌هایِ بازیِ «بازی کن و جایزه بگیر»، قابلِ تنظیم توسطِ مالکِ ربات.

متغیرهایِ قابلِ استفاده در متن:
  {name}   نامِ بازیکن (فقط در گروه)
  {game}   نامِ بازی
  {emoji}  اموجیِ بازی
  {value}  نتیجهٔ تاس
  {prize}  مبلغِ جایزه (فرمت‌شده)
  {left}   سهمیهٔ باقی‌مانده
  {limit}  کلِ سهمیه
  {period} بازهٔ سهمیه (روزانه/هفتگی/…)

  === PATCH_DICE_QUOTA_COUNTDOWN_V1 === (فقط در متنِ «پایانِ سهمیه»)
  {reset_in}   فاصلهٔ باقی‌مانده تا تجدیدِ سهمیه (مثلاً «۸ ساعت و ۴۱ دقیقه»)
  {reset_at}   ساعتِ تجدیدِ سهمیه (مثلاً «23:03»)
  {reset_line} جملهٔ آمادهٔ هر دو مورد بالا (اگر بازه «همیشگی» باشد، خالی)
"""
from __future__ import annotations

TEXTS: dict[str, tuple[str, str]] = {
    "win_cash": ("🎉 بردِ نقدی",
                 "{emoji} نتیجه: <b>{value}</b>\n\n"
                 "🎉 بردی! <b>{prize}</b> به کیف پولت اضافه شد."),
    "win_plan": ("🎁 بردِ پلن",
                 "{emoji} نتیجه: <b>{value}</b>\n\n🎉 بردی!"),
    "lose":     ("😔 باخت",
                 "{emoji} نتیجه: <b>{value}</b>\n\n"
                 "😔 این بار جایزه‌ای نبود. شانست را دوباره امتحان کن!"),
    # === PATCH_DICE_QUOTA_COUNTDOWN_V1 ===
    "quota":    ("⏳ پایانِ سهمیه",
                 "⏳ سهمیهٔ تو برای «{game}» تمام شده "
                 "({limit} بار {period}).\n{reset_line}"),
}


async def get_text(bot_id: int, key: str, **kw) -> str:
    """متنِ تنظیم‌شده یا پیشفرض، با جای‌گذاریِ متغیرها."""
    _, default = TEXTS.get(key, ("", ""))
    try:
        from tenant import get_setting
        v = (await get_setting(bot_id, f"dice_txt_{key}", "") or "").strip()
    except Exception:
        v = ""
    tpl = v or default
    try:
        # === PATCH_DICE_QUOTA_COUNTDOWN_V1 ===
        # اگر بازه «همیشگی» باشد reset_line خالی است و نباید یک خطِ سفیدِ
        # اضافه ته پیام بماند.
        return tpl.format(**kw).replace("\n\n\n", "\n\n").strip()
    except Exception:
        # اگر ادمین متغیرِ ناشناخته گذاشته باشد، متن خام برگردانده می‌شود
        return tpl
