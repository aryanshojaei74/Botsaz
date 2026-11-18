"""Nexivo: انتقالِ محصول بینِ «پوشه»ها (نوعِ سرویس) — تکی و گروهی."""
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
from tenant.bots.virtual_service import smm, smm_api

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(smm, "Session", mk)
    import tenant
    monkeypatch.setattr(tenant, "Session", mk)
    getattr(tenant, "_SETTING_CACHE", {}).clear()
    async with mk() as s:
        s.add(models.Bot(id=7, owner_id=1, token="7:T", username="b", field="virtual_service", plan="pro", status="active"))
        s.add(models.Bot(id=8, owner_id=2, token="8:T", username="c", field="virtual_service", plan="pro", status="active"))
        for i, (bid, nm, tk) in enumerate([(7, "Telegram Member 1", "member"), (7, "Telegram Member 2", "member"),
                                           (7, "🇺🇸 آمریکا", "virtual_number"), (8, "x", "member")], start=1):
            s.add(models.SmmService(id=i, bot_id=bid, service_id=("ozv:1:US" if tk == "virtual_number" else str(i)),
                                    name=nm, platform_key="telegram", type_key=tk, is_active=True, panel_rate=1))
        await s.commit()
    yield mk
    await eng.dispose()


async def _types(mk):
    async with mk() as s:
        return {v.id: v.type_key for v in (await s.scalars(select(models.SmmService))).all()}


async def test_move_changes_only_the_type_and_respects_ownership_and_vnum(env):
    moved, skipped = await smm._move_to_folder(7, [1, 2, 3, 4], "channel_member")
    assert (moved, skipped) == (2, 2)                       # شمارهٔ مجازی و محصولِ رباتِ دیگر دست نخورد
    t = await _types(env)
    assert t == {1: "channel_member", 2: "channel_member", 3: "virtual_number", 4: "member"}
    async with env() as s:
        v = await s.get(models.SmmService, 1)
    assert smm._svc_platform_type(v) == ("telegram", "channel_member")      # درختِ مشتری همین را می‌بیند


async def test_invalid_type_is_rejected_and_legacy_empty_platform_is_filled(env):
    assert await smm._move_to_folder(7, [1], "nonsense") == (0, 1)
    assert await smm._move_to_folder(7, [1], "other") == (0, 1)             # «سایر» پوشهٔ قابلِ انتخاب نیست (بازدسته‌بندی برش می‌گرداند)
    async with env() as s:
        v = await s.get(models.SmmService, 2)
        v.platform_key, v.type_key = "", ""
        await s.commit()
    assert await smm._move_to_folder(7, [2], "group_member") == (1, 0)
    async with env() as s:
        v = await s.get(models.SmmService, 2)
    assert v.platform_key and v.type_key == "group_member"
    assert smm._svc_platform_type(v)[1] == "group_member"                   # قبلاً بدونِ platform_key نوعِ ذخیره‌شده نادیده گرفته می‌شد


async def test_moved_type_survives_tree_classification(env):
    await smm._move_to_folder(7, [1, 2], "channel_member")
    tree, _by = await smm._user_active_tree(7)
    assert len(tree["telegram"]["channel_member"]) == 2
    assert "member" not in tree["telegram"] or all(i["pk"] not in (1, 2) for i in tree["telegram"].get("member", []))


def test_folder_picker_lists_real_types_only_and_buttons_exist_in_source():
    keys = [k for k, _l in smm._folder_types()]
    assert "channel_member" in keys and "group_member" in keys and "other" not in keys and "virtual_number" not in keys
    kb = smm._folder_rows("smm:svc:mvset:5:1", "smm:svcview:5:1")
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "smm:svc:mvset:5:1:channel_member" in cbs and cbs[-1] == "smm:svcview:5:1"
    from pathlib import Path
    src = Path("tenant/bots/virtual_service/smm.py").read_text(encoding="utf-8")
    assert 'callback_data=f"smm:svc:mvtyp:{v.id}:{back_page}"' in src and 'callback_data="smm:msel:typ"' in src
