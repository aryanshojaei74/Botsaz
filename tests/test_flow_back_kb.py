"""PATCH_FLOW_BACK_KB_V1 — ایلیا: بعد از انتخاب سرویس، «⬅️ بازگشت به منو» داخل منو (کیبورد پایین) بماند."""
import datetime
import itertools
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.types import Chat, Message, ReplyKeyboardMarkup, User
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.vpn import vpn_user as VU

pytestmark = pytest.mark.asyncio

UID = 5
_IDS = itertools.count(101)


def _msg(text="(C) برنزی", chat_type="private", uid=UID):
    m = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=uid, type=chat_type),
                from_user=User(id=uid, is_bot=False, first_name="a"), text=text)
    log = []

    async def delete_message(chat_id, message_id):
        log.append(("delete", message_id))
    fake_bot = SimpleNamespace(delete_message=delete_message)

    async def answer(t, **k):
        log.append(("answer", t, k.get("reply_markup")))
        return SimpleNamespace(message_id=next(_IDS), bot=fake_bot)
    object.__setattr__(m, "answer", answer)
    m.as_(fake_bot)
    return m, log


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(eng, expire_on_commit=False)
    monkeypatch.setattr(tenant, "Session", maker)
    async with maker() as s:
        b = models.Bot(owner_id=1, token="1:A", username="x", field="vpn_sales", plan="pro")
        s.add(b)
        await s.flush()
        sub = models.MainButton(bot_id=b.id, row=0, col=0, label="خرید سرویس جدید", action="submenu")
        s.add(sub)
        await s.flush()
        s.add(models.MainButton(bot_id=b.id, row=0, col=0, label="طلایی (A)", action="buy_vpn", parent_id=sub.id))
        await s.commit()
        ids = (b.id, sub.id)

    async def gbr(_b):
        return SimpleNamespace(id=ids[0], owner_id=1, plan="pro", status="active")

    async def no_disabled(_bid):
        return set()

    async def gs(bid, key, default=""):
        return "reply" if key == "menu_mode" else default

    async def gocu(*a, **k):
        return SimpleNamespace(is_banned=False)
    monkeypatch.setattr(tenant, "get_bot_row", gbr)
    monkeypatch.setattr(tenant, "_disabled_btn_ids", no_disabled)
    monkeypatch.setattr(tenant, "get_setting", gs)
    monkeypatch.setattr(tenant, "get_or_create_user", gocu)
    tenant._FLOW_KB.clear()
    tenant._LAST_MENU.clear()
    tenant._HIDDEN_HOME_BUBBLES.clear()
    yield ids
    tenant._FLOW_KB.clear()
    tenant._LAST_MENU.clear()
    tenant._HIDDEN_HOME_BUBBLES.clear()
    await eng.dispose()


class _State:
    def __init__(self):
        self.cleared, self.data = 0, {}

    async def clear(self):
        self.cleared += 1
        self.data = {}

    async def update_data(self, **k):
        self.data.update(k)

    async def get_data(self):
        return dict(self.data)


async def _swap(msg, bot_id, submenu_id=None):
    tok = tenant._CUR_UID.set(UID)
    try:
        if submenu_id:
            tenant._set_last_menu(bot_id, UID, submenu_id)
        await VU._drop_reply_kb(msg, bot_id)
    finally:
        tenant._CUR_UID.reset(tok)


async def test_swap_remembers_the_submenu_the_user_came_from(env):
    bid, sub_id = env
    m, log = _msg()
    await _swap(m, bid, sub_id)
    assert tenant._flow_kb_peek(bid, UID) == (True, sub_id)
    assert isinstance(log[0][2], ReplyKeyboardMarkup) and log[0][1] == tenant.HOME_MENU_TEXT_HIDDEN_MARK


async def test_next_swap_deletes_only_the_previous_invisible_bubble(env):
    bid, sub_id = env
    m, log = _msg()
    await _swap(m, bid, sub_id)
    first_id = tenant._HIDDEN_HOME_BUBBLES[(bid, UID)]
    m2, log2 = _msg()
    await _swap(m2, bid, sub_id)
    assert ("delete", first_id) in log2                                               # بابل قبلی پاک شد
    assert not any(e[0] == "delete" for e in log)                                     # و بابل اولی تا آن لحظه سر جایش بود
    assert tenant._HIDDEN_HOME_BUBBLES[(bid, UID)] != first_id


async def test_filter_matches_only_our_keyboard_press(env):
    bid, sub_id = env
    bot = SimpleNamespace(token="1:A")
    press, _ = _msg(text=tenant.BTN_BACK_MENU)
    assert not await tenant._is_flow_back_press(press, bot)                           # کیبوردی نگذاشته‌ایم ⇒ روال قبلی
    tenant._flow_kb_mark(bid, UID, sub_id)
    assert await tenant._is_flow_back_press(press, bot)                               # در هر وضعیت FSM (فیلتر به state کاری ندارد)
    other, _ = _msg(text="چیز دیگری")
    assert not await tenant._is_flow_back_press(other, bot)
    group, _ = _msg(text=tenant.BTN_BACK_MENU, chat_type="supergroup")
    assert not await tenant._is_flow_back_press(group, bot)
    tenant._FLOW_KB[(bid, UID)] = (sub_id, tenant._time_bm.monotonic() - tenant._FLOW_KB_TTL - 1)
    assert not await tenant._is_flow_back_press(press, bot)                           # منقضی


async def test_back_press_returns_to_the_origin_submenu_and_drops_the_half_done_step(env, monkeypatch):
    bid, sub_id = env
    rendered, home = [], []

    async def fake_render(msg, bot_id, btn, state):
        rendered.append((bot_id, btn.id, msg.from_user.id))

    async def fake_home(*a, **k):
        home.append(1)
    monkeypatch.setattr(tenant, "_render_reply_submenu", fake_render)
    monkeypatch.setattr(tenant, "send_home_menu", fake_home)
    tenant._flow_kb_mark(bid, UID, sub_id)
    st = _State()
    press, _ = _msg(text=tenant.BTN_BACK_MENU)
    await tenant.flow_back_pressed(press, SimpleNamespace(token="1:A"), st)
    assert rendered == [(bid, sub_id, UID)] and not home and st.cleared == 1
    assert tenant._flow_kb_peek(bid, UID) == (False, None)                            # بعد از برگشت، روال عادی زیرمنو


async def test_back_press_goes_home_without_submenu_or_when_it_is_gone(env, monkeypatch):
    bid, sub_id = env
    home = []

    async def fake_home(msg, bot_id, kb=None, **k):
        home.append(kb)

    async def fake_kb(*a, **k):
        return "MAIN-KB"
    monkeypatch.setattr(tenant, "send_home_menu", fake_home)
    monkeypatch.setattr(tenant, "main_kb_or_remove", fake_kb)
    press, _ = _msg(text=tenant.BTN_BACK_MENU)
    tenant._flow_kb_mark(bid, UID, None)                                              # از منوی اصلی آمده
    await tenant.flow_back_pressed(press, SimpleNamespace(token="1:A"), _State())
    assert home == ["MAIN-KB"]

    async def disabled(_bid):
        return {sub_id}
    monkeypatch.setattr(tenant, "_disabled_btn_ids", disabled)                       # زیرمنو غیرفعال شده
    tenant._flow_kb_mark(bid, UID, sub_id)
    await tenant.flow_back_pressed(press, SimpleNamespace(token="1:A"), _State())
    assert home == ["MAIN-KB", "MAIN-KB"]


async def test_submenu_render_clears_flag_and_removes_the_stale_invisible_bubble(env):
    bid, sub_id = env
    m, log = _msg()
    await _swap(m, bid, sub_id)
    assert tenant._flow_kb_peek(bid, UID)[0]
    stale_id = tenant._HIDDEN_HOME_BUBBLES[(bid, UID)]
    press, log2 = _msg(text=tenant.BTN_BACK_MENU)
    async with tenant.Session() as s:
        btn = await s.get(models.MainButton, sub_id)
    await tenant._render_reply_submenu(press, bid, btn, _State())
    assert tenant._flow_kb_peek(bid, UID) == (False, None)
    sent = [e for e in log2 if e[0] == "answer"]
    assert sent and isinstance(sent[0][2], ReplyKeyboardMarkup)
    texts = [b.text for row in sent[0][2].keyboard for b in row]
    assert "طلایی (A)" in texts and tenant.BTN_BACK_MENU in texts                      # کیبورد کامل زیرمنو برگشت
    assert ("delete", stale_id) in log2                                               # بابل نامرئی تک‌دکمه‌ای پاک شد
    assert (bid, UID) not in tenant._HIDDEN_HOME_BUBBLES


async def test_home_menu_clears_flag(env, monkeypatch):
    bid, sub_id = env
    tenant._flow_kb_mark(bid, UID, sub_id)

    async def no_sticker(*a, **k):
        return None
    monkeypatch.setattr(tenant, "_drop_btn_sticker", no_sticker)
    m, _ = _msg()
    await tenant.send_home_menu(m, bid, ReplyKeyboardMarkup(keyboard=[[tenant.KeyboardButton(text="x")]]))
    assert tenant._flow_kb_peek(bid, UID) == (False, None)


async def test_back_handler_is_registered_before_every_fsm_text_handler():
    """وسط مرحله‌ای متنی (کد تخفیف، حجم دلخواه، …) نباید «⬅️ بازگشت به منو» به‌عنوان ورودی خورده شود."""
    names = [h.callback.__name__ for h in tenant.tenant_router.message.handlers]
    i = names.index("flow_back_pressed")
    assert i < names.index("tenant_buttons_stuck_state_rescue") and i < names.index("tenant_buttons")
    assert all(n not in names[:i] or n in ("home_kb_pressed",) or "fsm" not in n.lower() for n in names[:i])
