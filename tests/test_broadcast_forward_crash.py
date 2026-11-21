"""PATCH_BROADCAST_FWD_CRASH_FIX_V1 (شریف): ارسالِ همگانیِ فوروارد نباید هنگِ «در حال ارسال» بدهد."""
import pytest

from tenant.shared import broadcast_runner as br


def test_forward_broadcast_row_builds_without_duplicate_kwarg():
    bc = br.build_broadcast(bot_id=1, text="x", chat_id=5, msg_id=6, total_count=764, fwd=(10, 20))
    assert bc.media_type == "forward" and bc.fwd_chat_id == 10 and bc.fwd_msg_id == 20 and bc.status == "sending"


def test_text_and_media_broadcast_rows_unchanged():
    bc = br.build_broadcast(bot_id=1, text="سلام", chat_id=5, msg_id=6, total_count=3)
    assert bc.media_type is None and bc.media_id is None and bc.cursor_uid == 0
    bc = br.build_broadcast(bot_id=1, text="cap", media_id="F", media_type="video", chat_id=5, msg_id=6)
    assert bc.media_type == "video" and bc.media_id == "F"


@pytest.mark.asyncio
async def test_retry_after_keeps_heartbeat(monkeypatch):
    calls = []

    async def touch(i):
        calls.append(i)

    async def fast_sleep(x):
        return None
    monkeypatch.setattr(br, "_touch", touch)
    monkeypatch.setattr(br.asyncio, "sleep", fast_sleep)
    # همان حلقهٔ انتظارِ اجراکننده: ۲۵ ثانیه ⇒ ۳ ضربان
    wait = 25.0
    while wait > 0:
        step = min(wait, 10.0)
        await br.asyncio.sleep(step)
        wait -= step
        await br._touch(1)
    assert calls == [1, 1, 1]
