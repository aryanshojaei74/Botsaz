"""PATCH_TCP_PRECHECK_DNS_V1: پیش‌بررسی TCP پنل نباید «Future exception was never retrieved» در لاگ بگذارد."""
import asyncio
import gc
import socket

import pytest

from tenant.bots.vpn import vpn_admin as VA


@pytest.mark.asyncio
async def test_slow_failing_dns_after_timeout_leaves_no_unretrieved_future(monkeypatch, caplog):
    loop = asyncio.get_running_loop()

    async def slow_fail(*a, **k):
        await asyncio.sleep(0.2)
        raise socket.gaierror(-5, "No address associated with hostname")
    monkeypatch.setattr(loop, "getaddrinfo", slow_fail)
    errors = []
    loop.set_exception_handler(lambda l, ctx: errors.append(ctx))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await VA.tcp_reach("panel.example.com", 8000, timeout=0.05)
        await asyncio.sleep(0.4)                  # DNS دیرتر شکست می‌خورد
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert not errors


@pytest.mark.asyncio
async def test_precheck_returns_friendly_error_for_bad_host(monkeypatch):
    loop = asyncio.get_running_loop()

    async def fail(*a, **k):
        raise socket.gaierror(-5, "No address")
    monkeypatch.setattr(loop, "getaddrinfo", fail)
    msg = await VA._tcp_precheck("https://panel.example.com:8000", timeout=0.5)
    assert msg and "نمی‌تواند" in msg
