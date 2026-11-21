"""PATCH_RELINK_RATE_LIMIT_V1 — «بازسازیِ لینک‌ها» با ۴۲۹ی پنل (RickPanel) صبر می‌کند و دوباره تلاش می‌کند."""
import pytest

from tenant.shared.panel_clients import panel_relink as R
from tenant.shared.panel_clients.vpn_panel import PanelError

MSG429 = "GET /api/user/x: پنل موقتاً درخواست‌ها را محدود کرده (HTTP 429 — rate limit). حدوداً 9 ثانیه دیگر امتحان کن."


def test_retry_wait_parsing():
    assert R._retry_wait_from(PanelError(MSG429)) == 10.0             # ۹ + ۱
    assert R._retry_wait_from(PanelError("HTTP 429")) == R._DEFAULT_RETRY_WAIT
    assert R._retry_wait_from(PanelError("حدوداً 500 ثانیه (HTTP 429 rate limit)")) == R._MAX_RETRY_WAIT
    assert R._retry_wait_from(PanelError("Timeout")) is None
    assert R._retry_wait_from(PanelError("HTTP 404")) is None


class FakeClient:
    def __init__(self, errors, result=None):
        self.errors, self.result, self.calls = list(errors), result or {"sub_link": "https://s/new"}, 0

    async def get_client(self, email):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    waits = []

    async def fake_sleep(t):
        waits.append(t)
    monkeypatch.setattr(R.asyncio, "sleep", fake_sleep)
    return waits


@pytest.mark.asyncio
async def test_429_then_success_is_retried_after_retry_after(_no_sleep):
    c = FakeClient([PanelError(MSG429), PanelError(MSG429)])
    res, err = await R._get_client_retry(c, "u")
    assert err is None and res["sub_link"] == "https://s/new" and c.calls == 3
    assert _no_sleep == [10.0, 10.0]


@pytest.mark.asyncio
async def test_persistent_429_gives_error_after_max_retries(_no_sleep):
    c = FakeClient([PanelError(MSG429)] * 20)
    res, err = await R._get_client_retry(c, "u")
    assert res is None and err is not None and c.calls == R._MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_non_rate_limit_error_is_not_retried(_no_sleep):
    c = FakeClient([PanelError("اتصال به پنل برقرار نشد (Timeout)")])
    res, err = await R._get_client_retry(c, "u")
    assert res is None and "Timeout" in str(err) and c.calls == 1 and _no_sleep == []
