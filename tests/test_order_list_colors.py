from aiogram.enums import ButtonStyle

from tenant.bots.virtual_service.smm import _vs_row_style as st


def test_colors_follow_status():
    for s_ in ("completed", "done"):
        assert st(s_) == ButtonStyle.SUCCESS
    assert st("delivered", "n") == ButtonStyle.SUCCESS and st("sent", "g") == ButtonStyle.SUCCESS
    assert st("partial") == ButtonStyle.PRIMARY
    for s_ in ("failed", "canceled", "refunded", "rejected", "expired", "returned"):
        assert st(s_) == ButtonStyle.DANGER
    for s_ in ("sent", "awaiting_admin", "pending", "sending", "auto_processing", "awaiting_stars", "paid"):
        assert st(s_) is None                    # در حالِ انجام → بدونِ رنگ
