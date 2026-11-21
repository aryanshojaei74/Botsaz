"""تستِ منطقِ بکاپ/انتقالِ پنل‌یار (PATCH_PANEL_YAR_BACKUP_MIGRATE_V1) — بدونِ شبکه."""
import asyncio
import json
import time
import types

from tenant.shared.panel_clients import panelyar as py

NOW = int(time.time() * 1000)
GB = 1024 ** 3


def _c(email, used=0, total=0, exp=0, **kw):
    return {"email": email, "traffic": {"up": 0, "down": used, "total": total},
            "totalGB": total, "expiryTime": exp, **kw}


def test_plan_one_cases():
    day = 86_400_000
    # نامحدود در حجم و زمان
    code, kw = py._plan_one(py._norm_client(_c("a")), "r", NOW, "marzban")
    assert code == "ok" and kw["mb"] is None and kw["days"] == 0 and kw["hours"] == 0
    # منقضی هرگز با صفر (=نامحدود) ساخته نمی‌شود
    assert py._plan_one(py._norm_client(_c("b", exp=NOW - day)), "r", NOW, "marzban")[0] == "expired"
    # حجم تمام‌شده در حالتِ «باقی‌مانده» رد می‌شود، در حالتِ «کل» منتقل می‌شود
    n = py._norm_client(_c("c", used=5 * GB, total=5 * GB, exp=NOW + 3 * day))
    assert py._plan_one(n, "r", NOW, "marzban")[0] == "depleted"
    code, kw = py._plan_one(n, "t", NOW, "marzban")
    assert code == "ok" and kw["mb"] == 5 * 1024
    # باقی‌مانده: ۲ گیگ از ۵ گیگ
    n = py._norm_client(_c("d", used=3 * GB, total=5 * GB, exp=NOW + 2 * day + 3_600_000 * 5))
    code, kw = py._plan_one(n, "r", NOW, "marzban")
    assert kw["mb"] == 2048 and kw["days"] == 2 and kw["hours"] in (5, 6)
    # پنلِ بدونِ hours ⇒ روزِ رو‌به‌بالا
    code, kw = py._plan_one(n, "r", NOW, "hiddify")
    assert kw["hours"] == 0 and kw["days"] == 3
    # سنایی: expiryTime منفی = مدتِ کل از اولین اتصال
    code, kw = py._plan_one(py._norm_client(_c("e", exp=-30 * day)), "r", NOW, "marzban")
    assert code == "ok" and kw["days"] == 30
    # بی‌نام
    assert py._plan_one(py._norm_client({"traffic": {}}), "r", NOW, "marzban")[0] == "noname"


def test_make_plan_skips_existing():
    src = [_c("ali"), _c("Reza"), _c("old", exp=NOW - 1000)]
    jobs, st = py._make_plan(src, {"reza"}, "r", "marzban")
    assert [j["email"] for j in jobs] == ["ali"]
    assert st["exists"] == 1 and st["expired"] == 1 and st["ok"] == 1


def test_backup_has_no_password():
    panel = types.SimpleNamespace(panel_key="marzban", base_url="https://p.example",
                                  username="admin", password="SECRET")
    data = json.loads(py._build_backup(panel, [_c("ali", used=1, total=2)]))
    assert data["format"] == "botsaz-panelyar-backup" and data["count"] == 1
    assert "SECRET" not in json.dumps(data) and "admin" not in json.dumps(data["panel"])


def test_run_migration_end_to_end(monkeypatch):
    created, sent = [], []

    class FakeClient:
        def __init__(self, tag): self.tag = tag
        async def list_clients(self):
            return [_c("ali", used=GB, total=3 * GB, exp=NOW + 5 * 86_400_000), _c("bad")] if self.tag == "src" else []
        async def create_account(self, email, gb, days, **kw):
            if email == "bad":
                raise RuntimeError("boom")
            created.append((email, gb, days, kw))
        async def close(self): pass

    monkeypatch.setattr(py, "make_panel_client", lambda p: FakeClient(p.tag))
    monkeypatch.setattr(py, "_MIG_SLEEP", 0)

    class Msg:
        chat = types.SimpleNamespace(id=1)
        async def edit_text(self, *a, **k): sent.append(("edit", a[0][:30]))

    class Bot:
        async def send_document(self, *a, **k): sent.append(("doc", k.get("caption", "")[:20]))

    src = types.SimpleNamespace(id=1, panel_key="marzban", base_url="s", tag="src")
    dst = types.SimpleNamespace(id=2, panel_key="pasargad", base_url="d", tag="dst")
    key = (9, 1, 2)
    py._MIG_RUNNING.add(key)
    asyncio.run(py._run_migration(Bot(), Msg(), types.SimpleNamespace(id=9), src, dst, "r", key))
    assert [c[0] for c in created] == ["ali"]
    assert created[0][3]["mb"] == 2048 and created[0][2] in (4, 5)
    assert key not in py._MIG_RUNNING
    assert any(k == "doc" for k, _ in sent)          # بکاپِ خودکار + فایلِ ناموفق‌ها
