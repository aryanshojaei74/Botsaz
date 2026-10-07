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
