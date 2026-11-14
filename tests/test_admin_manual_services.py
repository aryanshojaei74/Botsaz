"""PATCH_ADMIN_MANUAL_SVC_V1 (MidnightSoul) — دکمه‌هایِ مدیریتِ کاربر برایِ کاربرِ دارایِ «سرویس دستی»."""
import pytest

import models
from tests.test_paused_text_e2e import cb_update, env, OWNER  # noqa: F401


async def _mk_user(env, *, manual=True, panel=False):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    import config
    async with config.Session() as s:
        u = models.BotUser(bot_id=env["bid"], telegram_id=800111, full_name="Ali", username="ali", balance=0)
        s.add(u)
        await s.flush()
        if manual:
            s.add(models.Config(bot_id=env["bid"], code=77, volume_gb=0, days=30, payload="vless://x", status="sold",
                                owner_user_id=u.id, custom_label="سرویس علی", category="c1"))
        if panel:
            s.add(models.PanelAccount(bot_id=env["bid"], user_telegram_id=800111, email="e1", panel_key="marzban",
                                      plan_title="پلن", volume_gb=10, days=30, status="active"))
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["days", "gb", "svc"])
async def test_manual_only_user_gets_the_list_and_explanation_not_a_dead_end(env, action):
    if env["field"] != "vpn_sales":
        pytest.skip("فقط رباتِ VPN")
    await _mk_user(env)
    cap = env["cap"]
    await env["dp"].feed_update(env["bot"], cb_update(f"usr:{action}:800111"))
    txt = " ".join(t or "" for t, _b in cap.shown())
    assert "سرویس‌هایِ دستیِ این کاربر" in txt and "#77" in txt and "سرویس علی" in txt
    assert "به پنل وصل نیست" in txt


@pytest.mark.asyncio
async def test_user_without_any_service_still_gets_the_old_alert(env):
    if env["field"] != "vpn_sales":
        pytest.skip("فقط رباتِ VPN")
    await _mk_user(env, manual=False)
    cap = env["cap"]
    await env["dp"].feed_update(env["bot"], cb_update("usr:days:800111"))
    assert not any("سرویس‌هایِ دستیِ" in (t or "") for t, _b in cap.shown())
    assert any(type(c).__name__ == "AnswerCallbackQuery" and "سرویس VPNای ندارد" in (getattr(c, "text", "") or "") for c in cap.calls)


@pytest.mark.asyncio
async def test_panel_user_flow_is_unchanged(env):
    if env["field"] != "vpn_sales":
        pytest.skip("فقط رباتِ VPN")
    await _mk_user(env, manual=True, panel=True)
    cap = env["cap"]
    await env["dp"].feed_update(env["bot"], cb_update("usr:days:800111"))
    assert any("usrdayacc:" in "".join(b for b in btns) for _t, btns in cap.shown())      # لیستِ سرویس‌هایِ پنل‌دار مثل قبل
