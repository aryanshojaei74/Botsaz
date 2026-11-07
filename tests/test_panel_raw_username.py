"""PATCH_PANEL_RAW_USERNAME_V1/V2 — سرویسِ «افزودنِ دستی» با نامِ دقیقِ پنل پیدا شود
(Zahra8368 رویِ پنلِ حساس به حروف، یا zahra-12)."""
import pytest

from tenant.shared.panel_clients import username_resolve as ur
from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient, _safe_username


class _FakePanel:
    """پنلِ حساس به حروفِ بزرگ/کوچک (مثلِ PasarGuard رویِ PostgreSQL)."""
    def __init__(self, users):
        self.base_url = "https://p"
        self.users = set(users)
        self.calls = []

    async def _request(self, method, path, **kw):
        self.calls.append(path)
        name = path.split("/")[-1]
        return {"username": name, "subscription_url": f"/sub/{name}"} if name in self.users else None


@pytest.fixture(autouse=True)
def _clear():
    ur._RESOLVED.clear()


async def _get(panel, email):
    panel._abs_sub_link = lambda u: "https://p" + u
    return await MarzbanPanelClient.get_client(panel, email)


@pytest.mark.asyncio
async def test_bot_created_service_one_request():
    p = _FakePanel({"botnamh2148crv"})
    assert (await _get(p, "BotNamh2148crv"))["username"] == "botnamh2148crv"
    assert p.calls == ["/api/user/botnamh2148crv"]          # بدونِ درخواستِ اضافه


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Zahra8368", "Misaq", "zahra-12"])
async def test_manual_service_found_by_exact_name_and_cached(name):
    p = _FakePanel({name})
    cur = await _get(p, name)
    assert cur and cur["username"] == name and cur["sub_link"].endswith(name)
    p.calls.clear()
    assert await _get(p, name)
    assert p.calls == [f"/api/user/{name}"]                 # بارِ دوم مستقیم با نامِ درست


@pytest.mark.asyncio
async def test_write_path_resolves_exact_name():
    p = _FakePanel({"Zahra8368"})
    assert await ur.resolve_username(p, "Zahra8368", _safe_username) == "Zahra8368"
    p2 = _FakePanel({"ali_x"})                               # ساختِ ربات با نامِ تبدیل‌شده
    assert await ur.resolve_username(p2, "ali.x", _safe_username) == "ali_x"
