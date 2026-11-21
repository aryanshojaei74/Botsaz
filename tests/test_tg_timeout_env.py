"""PATCH_TG_TIMEOUT_ENV_V1 — تایم‌اوت تلگرام با TG_REQUEST_TIMEOUT قابل تنظیم است (پیش‌فرض ۱۲)."""
import importlib

import pytest


def _timeout(monkeypatch, val):
    from aiogram.client.session.aiohttp import AiohttpSession
    import config
    if val is None:
        monkeypatch.delenv("TG_REQUEST_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("TG_REQUEST_TIMEOUT", val)
    return AiohttpSession().timeout


@pytest.mark.parametrize("val,expect", [(None, 12.0), ("45", 45.0), ("1", 5.0), ("9999", 120.0), ("abc", 12.0)])
def test_timeout_env(monkeypatch, val, expect):
    assert _timeout(monkeypatch, val) == expect
