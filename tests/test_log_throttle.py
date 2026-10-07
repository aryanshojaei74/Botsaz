"""PATCH_LOG_THROTTLE_PERSIST_V1 — محدودیتِ تکرارِ لاگ بعد از ری‌استارت هم می‌ماند."""
import importlib


def test_should_log_persists_across_reload(tmp_path, monkeypatch):
    import log_throttle
    monkeypatch.setattr(log_throttle, "_PATH", str(tmp_path / ".log_throttle.json"))
    monkeypatch.setattr(log_throttle, "_cache", None)
    assert log_throttle.should_log("k1", 3600) is True
    assert log_throttle.should_log("k1", 3600) is False
    assert log_throttle.should_log("k2", 3600) is True
    # «ری‌استارت»: حافظه پاک می‌شود ولی فایل می‌ماند
    monkeypatch.setattr(log_throttle, "_cache", None)
    monkeypatch.setattr(log_throttle, "_cache_mtime", 0.0)
    assert log_throttle.should_log("k1", 3600) is False
    assert log_throttle.should_log("k1", 0) is True


def test_unwritable_path_never_raises(monkeypatch):
    import log_throttle
    monkeypatch.setattr(log_throttle, "_PATH", "/nonexistent-dir/x/.log_throttle.json")
    monkeypatch.setattr(log_throttle, "_cache", None)
    assert log_throttle.should_log("z", 60) is True
    assert log_throttle.should_log("z", 60) is False


def test_tg_sessions_share_one_ssl_context():
    """PATCH_SHARED_TG_SSL_CONTEXT_V1 — هر Bot() دیگر SSLContextِ تازه (پرهزینه) نمی‌سازد."""
    import config  # noqa: F401  (پچ را نصب می‌کند)
    from aiogram import Bot
    a, b = Bot("123:ABC"), Bot("124:ABD")
    assert a.session._connector_init["ssl"] is b.session._connector_init["ssl"]


def test_large_panel_json_parsed_off_loop(monkeypatch):
    import asyncio
    import json
    from tenant.shared.panel_clients import vpn_panel
    calls = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        calls.append(fn)
        return await real(fn, *a, **k)
    monkeypatch.setattr(vpn_panel.asyncio, "to_thread", spy)
    big = json.dumps({"obj": ["x" * 100] * 3000})
    assert asyncio.run(vpn_panel.loads_json_async(big))["obj"][0] == "x" * 100
    assert asyncio.run(vpn_panel.loads_json_async('{"a":1}')) == {"a": 1}
    assert len(calls) == 1
