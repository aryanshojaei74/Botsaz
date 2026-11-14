"""PATCH_MY_SVCS_HEADER_V1 — متنِ دکمهٔ «سرویس‌های من» در *همان* پیامِ فهرست می‌آید، نه پیامِ جدا."""
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import tenant
from tenant.bots.vpn import vpn_user as VU

HEADER = "📡 <b>سرویس‌های من</b>\n\nدر این بخش می‌توانی سرویس‌هایت را مدیریت کنی.\n\n"


class _Msg:
    def __init__(self):
        self.chat = NS(id=1)
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append((text, kw))
        return NS(message_id=len(self.sent))


class _Bot:
    async def delete_message(self, *a, **k):
        return None


@pytest_asyncio.fixture
async def env(monkeypatch):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)
    mk = async_sessionmaker(eng, expire_on_commit=False)
    for mod in (tenant, VU):
        monkeypatch.setattr(mod, "Session", mk)
    tenant._SETTING_CACHE.clear()
    async with mk() as s:
        s.add(models.Bot(id=1, owner_id=9, token="1:T", username="b", field="vpn_sales", plan="pro", status="active"))
        s.add(models.BotUser(bot_id=1, telegram_id=55, full_name="u", username="u"))
        s.add(models.PanelAccount(bot_id=1, user_telegram_id=55, email="e1", plan_title="حجمی", volume_gb=10, days=30,
                                  status="active"))
        await s.commit()
    return mk


@pytest.mark.asyncio
async def test_header_is_merged_into_the_single_list_message(env):
    m = _Msg()
    await VU.show_my_services(_Bot(), m, 1, 55, header=HEADER)
    assert len(m.sent) == 1                                          # یک پیام، نه دو پیام
    text, kw = m.sent[0]
    assert text.startswith(HEADER) and "برای دیدن جزئیات، انتخاب کن" in text
    cbs = [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r]
    assert any(c and c.startswith("vsvc:") for c in cbs)             # دکمه‌هایِ سرویس‌ها همان‌جاست


@pytest.mark.asyncio
async def test_no_header_keeps_the_old_text(env):
    m = _Msg()
    await VU.show_my_services(_Bot(), m, 1, 55)
    assert not m.sent[0][0].startswith("در این بخش") and "برای دیدن جزئیات، انتخاب کن" in m.sent[0][0]


@pytest.mark.asyncio
async def test_header_also_on_the_empty_state(env):
    m = _Msg()
    await VU.show_my_services(_Bot(), m, 1, 999, header=HEADER)      # کاربری که سرویس ندارد
    assert m.sent[0][0].startswith(HEADER) and "هنوز سرویس" in m.sent[0][0]


def test_main_button_passes_the_button_text_as_header_instead_of_a_separate_message():
    import inspect
    src = inspect.getsource(tenant._run_button_action)
    assert '"my_services",' in src.split("_HEADER_AWARE_ACTIONS = {")[1].split("}")[0]
    assert "show_my_services(bot, msg, bot_id, msg.from_user.id, header=action_header)" in src
