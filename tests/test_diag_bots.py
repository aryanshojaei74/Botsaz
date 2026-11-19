"""PATCH_BOT_DIAG_V1: دسته‌بندیِ «چرا رباتِ دارایِ مهلت کار نمی‌کند»."""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("diag_bots", os.path.join(ROOT, "scripts", "diag_bots.py"))
DB = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DB)

EXP = "https://x.example/webhook/T"
ALL = ["message", "callback_query", "chat_member"]
GOOD = {"url": EXP, "allowed_updates": ALL, "pending_update_count": 0}
NOW = 1_000_000.0


def C(db_status="active", getme="ok", wh=None, expected=EXP):
    return DB.classify(db_status, getme, GOOD if wh is None else wh, expected, NOW, ALL)[0]


def test_classes():
    assert C() == "OK"
    assert C(getme="unauthorized") == "DEAD_TOKEN" and C("paused", "unauthorized") == "DEAD_TOKEN"      # توکنِ باطل همیشه اول
    for st in ("paused", "locked", "admin_paused", "disabled_bad_token"):
        assert C(st) == "STATUS_BLOCKED", st
    assert C(getme="error") == "DELIVERY_ERROR"
    assert C(wh={**GOOD, "url": ""}) == "WRONG_WEBHOOK" and C(wh={**GOOD, "url": "https://old/webhook/T"}) == "WRONG_WEBHOOK"
    assert C(wh={**GOOD, "allowed_updates": []}) == "MISSING_UPDATES"                                    # پیش‌فرضِ تلگرام: بدونِ chat_member
    assert C(wh={**GOOD, "allowed_updates": ["message"]}) == "MISSING_UPDATES"
    assert C(wh={**GOOD, "allowed_updates": ["message", "callback_query"]}) == "MISSING_UPDATES"          # chat_member کم است
    assert C(wh={**GOOD, "pending_update_count": 7}) == "DELIVERY_ERROR"
    assert C(wh={**GOOD, "last_error_date": NOW - 300, "last_error_message": "Read timeout expired"}) == "DELIVERY_ERROR"
    assert C(wh={**GOOD, "last_error_date": NOW - 7200, "last_error_message": "old"}) == "OK"            # خطای قدیمی مهم نیست
    assert C(expected=None) == "OK"                                                                         # WEBHOOK_BASE خالی ⇒ آدرس چک نمی‌شود


@pytest.mark.asyncio
async def test_fix_marks_dead_active_bots_but_leaves_paused_ones(monkeypatch, tmp_path):
    """--fix: توکنِ باطل + status=active ⇒ disabled_bad_token؛ paused (خاموشیِ مالک/شریک) دست نمی‌خورد؛ هیچ تماسِ set_webhook نمی‌رود."""
    import contextlib

    from aiogram.exceptions import TelegramUnauthorizedError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import bot_utils
    import config
    import models

    calls = []

    class FakeTb:
        async def get_me(self):
            raise TelegramUnauthorizedError(method=None, message="Unauthorized")

        async def set_webhook(self, *a, **k):
            calls.append("set_webhook")

    @contextlib.asynccontextmanager
    async def fake_temp(token):
        yield FakeTb()

    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/d.db")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", mk)
    monkeypatch.setattr(bot_utils, "_temp_bot", fake_temp)
    async with mk() as s:
        s.add_all([models.Bot(id=1, owner_id=1, token="1:A", username="a", status="active", plan="pro"),
                   models.Bot(id=2, owner_id=1, token="2:B", username="b", status="paused", plan="partner")])
        await s.commit()
    res = {}
    for bid, st in ((1, "active"), (2, "paused")):
        res[bid] = await DB._inspect({"id": bid, "token": f"{bid}:X", "status": st}, EXP, ALL, fix=True)
    async with mk() as s:
        st = {b.id: b.status for b in (await s.scalars(__import__("sqlalchemy").select(models.Bot))).all()}
    await eng.dispose()

    assert res[1]["cls"] == "DEAD_TOKEN" and "disabled_bad_token" in res[1]["fixed"]
    assert res[2]["cls"] == "DEAD_TOKEN" and res[2]["fixed"] == ""
    assert st == {1: "disabled_bad_token", 2: "paused"} and calls == []
