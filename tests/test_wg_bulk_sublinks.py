"""PATCH_WG_BULK_SUBLINKS_V1 — ثبتِ دسته‌جمعیِ لینکِ ساب وایرگارد در انبارِ دستی."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import models
import tenant


class _St:
    def __init__(self, d=None):
        self.d, self.s = dict(d or {}), None

    async def update_data(self, **k):
        self.d.update(k)

    async def get_data(self):
        return dict(self.d)

    async def set_state(self, s):
        self.s = s

    async def clear(self):
        self.d, self.s = {}, None


def test_split_sub_links_validates_and_cleans():
    good, bad = tenant._split_sub_links("https://a.io/sub/1\nhttp://b.io/x, junk\n  https://c.io/3;  ")
    assert good == ["https://a.io/sub/1", "http://b.io/x", "https://c.io/3"] and bad == ["junk"]
    assert tenant._split_sub_links("") == ([], [])


def test_bulk_only_in_link_mode_of_file_kinds():
    assert tenant._wg_link_bulk_on({"cfg_kind": "wireguard", "cfg_wg_mode": "link"})
    assert tenant._wg_link_bulk_on({"cfg_kind": "openvpn", "cfg_wg_mode": "link"})
    assert not tenant._wg_link_bulk_on({"cfg_kind": "wireguard", "cfg_wg_mode": "file"})
    assert not tenant._wg_link_bulk_on({"cfg_kind": "sublink", "cfg_wg_mode": "link"})


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    for mod in (config, tenant):
        monkeypatch.setattr(mod, "Session", maker)
    tenant._BOT_ROW_CACHE.clear()
    async with maker() as s:
        s.add(models.Bot(id=9, owner_id=1, token="9:T", username="b", field="vpn_sales", plan="pro", status="active"))
        await s.commit()
    yield maker
    await engine.dispose()


class _Msg:
    def __init__(self, text):
        self.text = text
        self.out = []
        self.bot = SimpleNamespace()

        async def _edit(*a, **k):
            return None
        self.bot.edit_message_text = _edit

    async def delete(self):
        pass

    async def answer(self, text, **k):
        self.out.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=1), message_id=3)


def _form(**kw):
    d = dict(cfg_kind="wireguard", cfg_wg_mode="link", cfg_form_awaiting="payload", cfg_vol=50, cfg_days=30,
             cfg_users=1, cfg_price=1000, cfg_display="glass", cfg_bulk_links=[],
             cfg_form_prompt_chat_id=1, cfg_form_prompt_msg_id=2)
    d.update(kw)
    return _St(d)


async def _submit(st):
    res = []

    class M:
        async def edit_reply_markup(self, **k):
            pass

        async def answer(self, text, **k):
            res.append(text)
    cb = SimpleNamespace(data="cfgf:submit", message=M(), from_user=SimpleNamespace(id=1))

    async def _ans(*a, **k):
        res.append(("alert", a, k))
    cb.answer = _ans
    await tenant.cfg_form_submit(cb, SimpleNamespace(token="9:T", send_message=_ans), st)
    return res


@pytest.mark.asyncio
async def test_multi_links_collected_across_messages_and_saved_as_separate_configs(db):
    st = _form()
    m1 = _Msg("https://s.io/a\nhttps://s.io/b  junk")
    await tenant.cfg_form_field_value(m1, st)
    m2 = _Msg("https://s.io/b https://s.io/c")        # b تکراری
    await tenant.cfg_form_field_value(m2, st)
    assert st.d["cfg_bulk_links"] == ["https://s.io/a", "https://s.io/b", "https://s.io/c"]
    await _submit(st)
    async with db() as s:
        rows = (await s.scalars(select(models.Config).order_by(models.Config.code))).all()
    assert [r.payload for r in rows] == ["https://s.io/a", "https://s.io/b", "https://s.io/c"]
    assert {r.kind for r in rows} == {"wireguard"} and len({r.code for r in rows}) == 3
    assert all(tenant._wg_payload_is_link(r.payload) for r in rows) and rows[0].status == "in_stock"


@pytest.mark.asyncio
async def test_invalid_only_text_is_rejected(db):
    st = _form()
    m = _Msg("abc def")
    await tenant.cfg_form_field_value(m, st)
    assert st.d["cfg_bulk_links"] == [] and m.out and "http" in m.out[0]


@pytest.mark.asyncio
async def test_legacy_single_link_still_saves(db):
    st = _form(cfg_payload_val="https://old.io/1")
    await _submit(st)
    async with db() as s:
        rows = (await s.scalars(select(models.Config))).all()
    assert [r.payload for r in rows] == ["https://old.io/1"]


@pytest.mark.asyncio
async def test_resubmitting_existing_stock_links_skips_duplicates_without_crash(db):
    st = _form(cfg_bulk_links=["https://s.io/a", "https://s.io/b"])
    await _submit(st)
    st2 = _form(cfg_bulk_links=["https://s.io/a", "https://s.io/c"])
    await _submit(st2)
    st3 = _form(cfg_bulk_links=["https://s.io/a"])
    res = await _submit(st3)                       # همه تکراری → بدونِ IndexError
    async with db() as s:
        rows = (await s.scalars(select(models.Config))).all()
    assert sorted(r.payload for r in rows) == ["https://s.io/a", "https://s.io/b", "https://s.io/c"]
    assert any(isinstance(x, tuple) for x in res)


@pytest.mark.asyncio
async def test_submit_requires_links(db):
    st = _form()
    res = await _submit(st)
    async with db() as s:
        assert not (await s.scalars(select(models.Config))).all()
    assert any(isinstance(x, tuple) for x in res)
