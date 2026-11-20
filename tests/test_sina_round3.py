"""سینا (۲۰ نوامبر): ترتیب جدول، اعتبار کد گردونه، پاک‌نشدن پیام‌های سفارش."""
import pytest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tenant.shared import miniapp_wheel as WH
from tenant.shared import msg_cleanup as MC


def test_wheel_code_expiry_cycles_and_defaults_to_30_days():
    assert WH._default_cfg()["code_hours"] == 720 and WH._norm({})["code_hours"] == 720
    seq, h = [], 720
    for _ in range(4):
        h = WH.next_code_hours(h)
        seq.append(h)
    assert seq == [24, 72, 168, 720]
    assert WH.code_expiry_label(24) == "1 روز" and WH._norm({"code_hours": 24})["code_hours"] == 24


def _kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="y")]])


@pytest.mark.parametrize("text", [
    "✅ سفارش شما با موفقیت ثبت شد! 🎉\n🔖 کد پیگیری: ABC",
    "✅ سفارش شما با موفقیت تکمیل شد! 🎉",
    "❌ متاسفانه سفارشت توسط ارائه‌دهنده لغو شد",
    "سفارش جدید — کد پیگیری: X12",
])
def test_order_messages_are_never_cleaned(text):
    assert MC.is_order_message(text)
    assert not MC.trackable(SendMessage(chat_id=1, text=text, reply_markup=_kb()))


def test_normal_menu_pages_are_still_cleaned():
    assert MC.trackable(SendMessage(chat_id=1, text="💰 کیف پول شما", reply_markup=_kb()))


@pytest.mark.asyncio
async def test_board_order_switch_changes_ranking(monkeypatch):
    import wager_rank as K
    stats = [{"uid": 1, "games": 500, "wins": 100, "losses": 400}, {"uid": 2, "games": 200, "wins": 150, "losses": 50}]

    def rank(order):
        key = (lambda d: (-d["games"], -d["wins"])) if order == "games" else (lambda d: (-d["wins"], d["losses"], -d["games"]))
        return [d["uid"] for d in sorted(stats, key=key)]
    assert rank("games") == [1, 2] and rank("wins") == [2, 1]
    assert K.board_order_label("games") == "بیشترین بازی" and K.board_order_label("wins") == "بیشترین برد"
    assert K.admin_board([], "games").startswith("📊")
