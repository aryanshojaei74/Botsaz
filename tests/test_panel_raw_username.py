"""PATCH_PANEL_RAW_USERNAME_V1 — سرویسِ دستی با نامِ اصلیِ پنل (مثلاً zahra-12) پیدا شود."""
import pytest

from tenant.shared.panel_clients import username_resolve as ur
from tenant.shared.panel_clients.upanel_mizban import _safe_username


class _FakePanel:
    def __init__(self, users):
        self.base_url = "https://p"
        self.users = set(users)
        self.calls = []

    async def _request(self, method, path, **kw):
        self.calls.append(path)
        name = path.split("/")[-1]
        return {"username": name} if name in self.users else None


@pytest.fixture(autouse=True)
def _clear():
    ur._RESOLVED.clear()


@pytest.mark.asyncio
async def test_bot_created_names_unchanged_without_extra_calls():
    p = _FakePanel({"botnamh2148crv"})
    assert await ur.resolve_username(p, "BotNamh2148crv", _safe_username) == "botnamh2148crv"
    assert p.calls == []                              # فقط تفاوتِ حروف: هیچ درخواستِ اضافه‌ای


@pytest.mark.asyncio
async def test_bot_created_with_special_chars_keeps_safe_name():
    p = _FakePanel({"ali_x"})                         # ربات این را با نامِ تبدیل‌شده ساخته
    assert await ur.resolve_username(p, "ali.x", _safe_username) == "ali_x"


@pytest.mark.asyncio
async def test_manual_service_uses_raw_name_and_caches():
    p = _FakePanel({"zahra-12"})                      # سرویسِ ازپیش‌موجود روی پنل
    assert await ur.resolve_username(p, "zahra-12", _safe_username) == "zahra-12"
    n = len(p.calls)
    assert await ur.resolve_username(p, "zahra-12", _safe_username) == "zahra-12"
    assert len(p.calls) == n                          # بارِ دوم از کش
