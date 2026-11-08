"""PATCH_HOSTING_DISK_GUARD_V1 — سقفِ فضایِ هر اپِ میزبانی‌شده + پاکسازیِ فایل‌هایِ دانلودی/آپلودیِ قدیمی."""
import os
import time
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import config
import hosting_disk_guard as G
import models

DAY = 86400


def _file(p, size=10, age_days=0):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = time.time() - age_days * DAY
    os.utime(p, (t, t))
    return p


NAMES = {"downloads", "uploads", "tmp", "cache"}


def test_clean_old_only_old_files_in_temp_dirs(tmp_path):
    app = tmp_path / "bot1"
    old_dl = _file(app / "downloads" / "movie.mp4", 100, 10)
    new_dl = _file(app / "downloads" / "fresh.mp4", 100, 1)
    old_up = _file(app / "data" / "uploads" / "a" / "b.zip", 50, 30)
    code = _file(app / "main.py", 20, 400)                                # کدِ پروژه، هرچقدر قدیمی، دست‌نخورده
    env = _file(app / ".env", 5, 400)
    cfgfile = _file(app / "config" / "settings.json", 5, 400)
    venv_tmp = _file(app / ".venv" / "lib" / "tmp" / "x.bin", 7, 400)     # داخلِ venv حتی اگر نامِ tmp باشد
    media_not_in_temp = _file(app / "assets" / "intro.mp4", 9, 400)
    n, freed = G.clean_old(app, 7, NAMES, dry_run=True)
    assert (n, freed) == (2, 150) and old_dl.exists() and old_up.exists()   # dry-run: چیزی پاک نمی‌شود
    n, freed = G.clean_old(app, 7, NAMES)
    assert (n, freed) == (2, 150)
    assert not old_dl.exists() and not old_up.exists()
    for keep in (new_dl, code, env, cfgfile, venv_tmp, media_not_in_temp):
        assert keep.exists(), keep
    assert (app / "downloads").is_dir() and not (app / "data" / "uploads" / "a").exists()      # شاخه‌هایِ خالی پاک، پوشهٔ موقت می‌ماند


def test_clean_old_never_follows_symlinks(tmp_path):
    outside = tmp_path / "outside"
    victim = _file(outside / "important.txt", 10, 400)
    app = tmp_path / "bot2"
    (app / "downloads").mkdir(parents=True)
    os.symlink(outside, app / "downloads" / "link")
    os.symlink(victim, app / "downloads" / "flink.txt")
    G.clean_old(app, 7, NAMES)
    assert victim.exists()                                                # symlink دنبال/پاک نمی‌شود


def test_dir_size(tmp_path):
    app = tmp_path / "b"
    _file(app / "a.bin", 100)
    _file(app / "d" / "b.bin", 50)
    os.symlink(app / "a.bin", app / "ln")
    assert G.dir_size(app) == 150 and G.dir_size(tmp_path / "missing") == 0


def test_fmt_and_report_text():
    assert G.fmt_bytes(5 * 1024 ** 3) == "5.0 GB" and G.fmt_bytes(10) == "10 B"
    assert "هنوز اسکنی" in G.report_text(None)
    rep = {"at": "t", "cap_bytes": 5 * 1024 ** 3, "disk": {"total": 100, "used": 80, "free": 20}, "freed_bytes": 2048, "orphans": ["old-one"],
           "apps": [{"name": "big", "bytes": 6 * 1024 ** 3, "over": True, "stopped": True, "owner_id": 5, "cleaned_bytes": 0},
                    {"name": "mid", "bytes": 4.5 * 1024 ** 3, "over": False, "cleaned_bytes": 100},
                    {"name": "small", "bytes": 1024, "over": False, "cleaned_bytes": 0}]}
    t = G.report_text(rep)
    assert "80٪" in t and "⛔️ <code>big</code>" in t and "🟠 <code>mid</code>" in t and "▫️ <code>small</code>" in t and "old-one" in t


@pytest_asyncio.fixture
async def env(monkeypatch, tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(config, "Session", maker)
    monkeypatch.setattr(G, "APPS_ROOT", tmp_path / "apps")
    (tmp_path / "apps").mkdir()
    async with maker() as s:
        s.add(models.HostedApp(id=1, owner_id=111, name="bigbot", status="running"))
        s.add(models.HostedApp(id=2, owner_id=222, name="smallbot", status="running"))
        s.add(models.HostedApp(id=3, owner_id=333, name="nearbot", status="running"))
        await s.commit()
    await G.set_cfg("hdg_cap_mb", "1")                                    # سقفِ ۱ مگابایت برایِ تست
    await G.set_cfg("hdg_docker", "0")                                    # پاکسازیِ داکر در تست‌هایِ قبلی نقشی ندارد
    await G.set_cfg("hdg_backups", "0")                                   # و پشتیبان‌ها هم (برایِ آن‌ها تستِ جدا هست)
    monkeypatch.setattr(G, "BACKUPS_ROOT", tmp_path / "root")
    monkeypatch.setattr(G, "OPT_ROOT", tmp_path / "opt")
    MB = 1024 * 1024
    _file(tmp_path / "apps" / "bigbot" / "data" / "blob.bin", int(1.5 * MB), 0)           # بالایِ سقف و جدید (پاک نمی‌شود)
    _file(tmp_path / "apps" / "smallbot" / "main.py", 1000, 0)
    _file(tmp_path / "apps" / "smallbot" / "downloads" / "old.mp4", int(2 * MB), 10)      # قدیمی ⇒ پاک؛ بعد زیرِ سقف
    _file(tmp_path / "apps" / "nearbot" / "data" / "x.bin", int(0.9 * MB), 0)             # ۹۰٪ سقف ⇒ هشدارِ زودهنگام
    os.makedirs(tmp_path / "apps" / "orphan-dir")
    calls, msgs = [], []

    async def run(*cmd):
        calls.append(cmd)
        return 0, ""

    class Bot:
        async def send_message(self, chat, text, **kw):
            msgs.append((chat, text))
    yield NS(maker=maker, run=run, calls=calls, msgs=msgs, bot=Bot(), root=tmp_path / "apps")
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_once_cleans_stops_over_cap_apps_and_warns_near_cap(env):
    rep = await G.run_once(bot=env.bot, run=env.run)
    by = {a["name"]: a for a in rep["apps"]}
    assert by["smallbot"]["cleaned_files"] == 1 and by["smallbot"]["bytes"] < 1024 * 1024 and not by["smallbot"]["over"]
    assert not (env.root / "smallbot" / "downloads" / "old.mp4").exists() and (env.root / "smallbot" / "main.py").exists()
    assert by["bigbot"]["over"] and by["bigbot"]["stopped"] is True
    assert env.calls == [("docker", "stop", "bsapp-bigbot")]
    async with env.maker() as s:
        a = await s.get(models.HostedApp, 1)
        assert a.status == "stopped" and "سقفِ" in a.last_error
        assert (await s.get(models.HostedApp, 2)).status == "running"                       # بقیه دست‌نخورده
    to_owner = [m for m in env.msgs if m[0] == 111]
    assert to_owner and "خاموش شد" in to_owner[0][1]
    assert any(m[0] == 333 and "رسیده" in m[1] for m in env.msgs)                                # هشدارِ زودهنگام، بدونِ خاموشی
    assert rep["orphans"] == ["orphan-dir"] and rep["freed_bytes"] > 0
    saved = await G.load_report()
    assert saved and [a["name"] for a in saved["apps"]][0] == "bigbot"                       # مرتب‌شده بر اساسِ حجم


@pytest.mark.asyncio
async def test_warnings_are_not_repeated_within_24h(env):
    await G.run_once(bot=env.bot, run=env.run)
    n = len(env.msgs)
    await G.run_once(bot=env.bot, run=env.run)                                                # فوراً دوباره
    assert len(env.msgs) == n
    # یک بار هم «خاموش‌شده» دوباره خاموش نمی‌شود (status دیگر running نیست)
    assert env.calls.count(("docker", "stop", "bsapp-bigbot")) == 1


@pytest.mark.asyncio
async def test_dry_run_changes_nothing_and_reports_reclaimable(env):
    rep = await G.run_once(dry_run=True, bot=env.bot, run=env.run)
    assert (env.root / "smallbot" / "downloads" / "old.mp4").exists() and env.calls == [] and env.msgs == []
    assert rep["reclaimable_bytes"] >= 2 * 1024 * 1024 and rep["freed_bytes"] == 0
    assert await G.load_report() is None                                                      # گزارشِ dry-run ذخیره نمی‌شود
    async with env.maker() as s:
        assert (await s.get(models.HostedApp, 1)).status == "running"


@pytest.mark.asyncio
async def test_warn_only_action_and_disabled_and_clean_off(env):
    await G.set_cfg("hdg_action", "warn")
    rep = await G.run_once(bot=env.bot, run=env.run)
    assert env.calls == [] and any(m[0] == 111 and "پاک کن" in m[1] for m in env.msgs)
    async with env.maker() as s:
        assert (await s.get(models.HostedApp, 1)).status == "running"
    # پاکسازی خاموش ⇒ فایلِ قدیمی می‌ماند
    await G.set_cfg("hdg_clean", "0")
    _file(env.root / "smallbot" / "downloads" / "again.mp4", 500, 20)
    await G.run_once(bot=env.bot, run=env.run)
    assert (env.root / "smallbot" / "downloads" / "again.mp4").exists()
    # کلِ ماژول خاموش ⇒ نه پاکسازی نه خاموشی
    await G.set_cfg("hdg_enabled", "0")
    await G.set_cfg("hdg_clean", "1")
    env.calls.clear()
    await G.run_once(bot=env.bot, run=env.run)
    assert env.calls == [] and (env.root / "smallbot" / "downloads" / "again.mp4").exists()


@pytest.mark.asyncio
async def test_stop_failure_is_reported_not_fatal(env):
    async def bad(*cmd):
        return 1, "no such container"
    rep = await G.run_once(bot=env.bot, run=bad)
    big = [a for a in rep["apps"] if a["name"] == "bigbot"][0]
    assert big["over"] and big["stopped"] is False and any("ممکن نشد" in m[1] for m in env.msgs if m[0] == 111)
    async with env.maker() as s:
        assert (await s.get(models.HostedApp, 1)).status == "running"


@pytest.mark.asyncio
async def test_cfg_parsing_and_bad_values(env):
    await G.set_cfg("hdg_cap_mb", "abc")
    await G.set_cfg("hdg_days", "-3")
    await G.set_cfg("hdg_dirs", "Downloads, ,Temp")
    c = await G.get_cfg()
    assert c["cap_mb"] == 5120 and c["days"] == 1
    assert c["dirs"] == {"downloads", "temp"}


@pytest.mark.asyncio
async def test_admin_screen_and_menu_entry(env):
    import platform_admin_menu as AM
    import platform_hdg as H
    assert "hdg:menu" in [it[1] for it in AM._SECTION_BY_KEY["system"]["items"]]
    await G.run_once(bot=env.bot, run=env.run)
    txt, kb = await H.hdg_screen()
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert {"hdg:run", "hdg:dry", "hdg:cap", "hdg:days", "hdg:clean", "hdg:action"} <= set(datas) and "bigbot" in txt


OUT_BUILDER = "ID  RECLAIMABLE\nabc  1GB\nTotal:\t25.2GB\nTotal reclaimed space: 25.2GB\n"
OUT_IMAGES = "Total reclaimed space: 611.2MB\n"


@pytest.mark.asyncio
async def test_docker_prune_parses_reclaimed_space_and_errors():
    calls = []

    async def run(*cmd):
        calls.append(cmd)
        return (0, OUT_BUILDER) if cmd[:3] == ("docker", "builder", "prune") else (0, OUT_IMAGES)
    res = await G.docker_prune(run, 48)
    assert res == {"builder": "25.2GB", "images": "611.2MB"}
    assert calls[0] == ("docker", "builder", "prune", "-f", "--filter", "until=48h") and calls[1] == ("docker", "image", "prune", "-f")
    assert not any("volume" in c or "system" in c for c in calls[0] + calls[1])                  # volume/system prune هرگز

    async def bad(*cmd):
        return 1, "Cannot connect to the Docker daemon"

    async def boom(*cmd):
        raise RuntimeError("x")
    assert await G.docker_prune(bad, 24) == {"builder": "خطا", "images": "خطا"}
    assert await G.docker_prune(boom, 24) == {"builder": "خطا", "images": "خطا"}

    async def none_freed(*cmd):
        return 0, "Total reclaimed space: 0B"
    assert (await G.docker_prune(none_freed, 1))["builder"] == "0B"


@pytest.mark.asyncio
async def test_run_once_prunes_docker_by_default_hours_and_not_in_dry_run_or_when_off(env):
    await G.set_cfg("hdg_docker", "1")
    await G.set_cfg("hdg_docker_hours", "24")

    async def run(*cmd):
        env.calls.append(cmd)
        return (0, OUT_BUILDER if cmd[:3] == ("docker", "builder", "prune") else OUT_IMAGES)
    rep = await G.run_once(bot=env.bot, run=run)
    assert ("docker", "builder", "prune", "-f", "--filter", "until=24h") in env.calls
    assert rep["docker"]["builder"] == "25.2GB" and "25.2GB" in G.report_text(rep)
    env.calls.clear()
    await G.run_once(dry_run=True, bot=env.bot, run=run)
    assert not any(c[:2] == ("docker", "builder") for c in env.calls)                           # dry-run: چیزی پاک نمی‌شود
    await G.set_cfg("hdg_docker", "0")
    env.calls.clear()
    rep2 = await G.run_once(bot=env.bot, run=run)
    assert not any(c[:2] == ("docker", "builder") for c in env.calls) and rep2["docker"] is None


@pytest.mark.asyncio
async def test_docker_ui_buttons(env):
    import platform_hdg as H
    txt, kb = await H.hdg_screen()
    datas = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "hdg:docker" in datas and "hdg:dockeron" in datas and "hdg:backups" in datas and "کشِ build داکر" in txt and "botsaz-backup" in txt


def test_prune_backups_keeps_newest_n_and_touches_only_exact_pattern(tmp_path):
    root = tmp_path / "r"
    for n in (9, 10, 11, 100, 488):                                       # ترتیب عددی (۹ < ۱۰ < ۱۰۰)، نه رشته‌ای
        _file(root / f"botsaz-backup-{n}" / "venv" / "big.bin", 1000)
    keep_me = [_file(root / "botsaz-code-backup-20261108.tgz", 10), _file(root / "notes.txt", 5),
               _file(root / "botsaz-backup-x" / "f", 5), _file(root / "botsaz-backup-12-old" / "f", 5), _file(root / "my-botsaz-backup-7" / "f", 5)]
    dry = G.prune_backups(root, 2, dry_run=True)
    assert dry["removed"] == ["botsaz-backup-9", "botsaz-backup-10", "botsaz-backup-11"] and dry["bytes"] == 3000
    assert (root / "botsaz-backup-9").exists()                              # dry-run: چیزی پاک نشد
    res = G.prune_backups(root, 2)
    assert res["removed"] == ["botsaz-backup-9", "botsaz-backup-10", "botsaz-backup-11"] and res["kept"] == ["botsaz-backup-100", "botsaz-backup-488"]
    assert not (root / "botsaz-backup-9").exists() and (root / "botsaz-backup-100").exists() and (root / "botsaz-backup-488").exists()
    for k in keep_me:
        assert k.exists(), k                                                # الگوهایِ نزدیک ولی غیرِ دقیق، و پشتیبانِ tar دست‌نخورده
    assert G.prune_backups(tmp_path / "missing", 2) == {"removed": [], "kept": [], "bytes": 0}
    assert G.prune_backups(root, 5)["removed"] == []                        # کمتر از keep ⇒ هیچ


@pytest.mark.asyncio
async def test_run_once_prunes_backups_when_enabled(env, tmp_path):
    for n in (1, 2, 3, 4):
        _file(tmp_path / "root" / f"botsaz-backup-{n}" / "f", 50)
    await G.set_cfg("hdg_backups", "1")
    rep = await G.run_once(bot=env.bot, run=env.run)
    assert rep["backups"]["removed"] == ["botsaz-backup-1", "botsaz-backup-2"] and "پشتیبان‌هایِ قدیمیِ نصب پاک شد" in G.report_text(rep)
    assert (tmp_path / "root" / "botsaz-backup-4").exists() and not (tmp_path / "root" / "botsaz-backup-1").exists()
    for n in (5, 6):
        _file(tmp_path / "root" / f"botsaz-backup-{n}" / "f", 50)
    dry = await G.run_once(dry_run=True, bot=env.bot, run=env.run)
    assert (tmp_path / "root" / "botsaz-backup-3").exists() and dry["backups"]["removed"] == ["botsaz-backup-3", "botsaz-backup-4"]
    await G.set_cfg("hdg_backups", "0")
    rep2 = await G.run_once(bot=env.bot, run=env.run)
    assert rep2["backups"] is None and (tmp_path / "root" / "botsaz-backup-3").exists()


def test_prune_opt_dated_backups_sorted_by_date_and_exact_pattern(tmp_path):
    opt = tmp_path / "opt"
    names = ["botsaz.bak-2026-11-07-1600", "botsaz.bak-2026-11-07-2102", "botsaz.bak-2026-11-06-2359", "botsaz.bak-2026-11-08-0030"]
    for n in names:
        _file(opt / n / "venv" / "x", 100)
    keep_me = [_file(opt / "botsaz" / "main.py", 5), _file(opt / "botsazvip" / "f", 5), _file(opt / "botsaz.bak-manual" / "f", 5),
               _file(opt / "botsaz.bak-2026-11-07-21" / "f", 5)]
    res = G.prune_backups(opt, 2, opt_style=True)
    assert res["removed"] == ["botsaz.bak-2026-11-06-2359", "botsaz.bak-2026-11-07-1600"]                    # قدیمی‌ترین‌ها بر اساسِ تاریخ
    assert res["kept"] == ["botsaz.bak-2026-11-07-2102", "botsaz.bak-2026-11-08-0030"] and res["bytes"] == 200
    for k in keep_me:
        assert k.exists(), k                                                                                # خودِ /opt/botsaz و botsazvip هرگز


@pytest.mark.asyncio
async def test_run_once_includes_opt_backups(env, tmp_path):
    for n in ("botsaz.bak-2026-11-01-1000", "botsaz.bak-2026-11-02-1000", "botsaz.bak-2026-11-03-1000"):
        _file(tmp_path / "opt" / n / "f", 30)
    await G.set_cfg("hdg_backups", "1")
    rep = await G.run_once(bot=env.bot, run=env.run)
    assert rep["backups"]["removed"] == ["botsaz.bak-2026-11-01-1000"] and (tmp_path / "opt" / "botsaz.bak-2026-11-03-1000").exists()
