"""scripts/check_hosted_app.py — تشخیصِ «توکن درست است ولی Unauthorized»."""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

spec = importlib.util.spec_from_file_location(
    "check_hosted_app", os.path.join(os.path.dirname(__file__), "..", "scripts", "check_hosted_app.py"))
CH = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CH)

GOOD = "123456789:AAEabcdefghijklmnopqrstuvwxyz012345"
OLD = "987654321:AAFzyxwvutsrqponmlkjihgfedcba543210"


def test_diagnose_token_flags_invisible_problems():
    assert CH.diagnose_token(GOOD) == []
    assert any("فاصله" in x for x in CH.diagnose_token(GOOD + "\n"))
    assert any("نقل‌قول" in x for x in CH.diagnose_token('"' + GOOD + '"'))
    assert any("bot" in x for x in CH.diagnose_token("bot" + GOOD))
    assert any("قالب" in x for x in CH.diagnose_token("123:abc"))


def test_mask_never_prints_full_token():
    m = CH.mask(GOOD)
    assert GOOD not in m and m.startswith("1234") and m.endswith(GOOD[-4:])


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/env"):
            body = {"name": "pyhdbkm", "env": [{"key": "BOT_TOKEN", "value": GOOD}], "needed": ["BOT_TOKEN"]}
        elif "/files" in self.path:
            body = {"files": [{"path": "main.py", "size": 100}, {"path": ".env", "size": 50}]}
        elif "/filetext" in self.path:
            body = {"text": f'TOKEN = "{OLD}"\n' if "main.py" in self.path else f"BOT_TOKEN={GOOD}\n"}
        elif "/logs" in self.path:
            body = {"logs": "start\naiogram.exceptions.TelegramUnauthorizedError: Unauthorized\n"}
        else:
            body = {}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)


def test_end_to_end_finds_hardcoded_old_token(monkeypatch, capsys):
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x" if t.strip("'\" ") == GOOD else "❌ تلگرام رد کرد (HTTP 401)")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "5"])
    try:
        rc = CH.main()
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert rc == 1
    assert "main.py" in out and "ثبت‌شده فرق دارد" in out      # توکن قدیمی سخت‌کدشده
    assert OLD not in out and GOOD not in out                             # توکنِ کامل چاپ نمی‌شود
    assert "Unauthorized" in out


def test_req_has_handles_versions_comments_and_name_normalisation():
    req = "# deps\npython_telegram_bot==20.7\nyt-dlp>=2024.1  # video\n-r other.txt\nrequests\n"
    assert CH.req_has(req, "python-telegram-bot") and CH.req_has(req, "yt-dlp") and CH.req_has(req, "Requests")
    assert not CH.req_has(req, "Pillow") and not CH.req_has("", "x")
    assert CH.PIP_NAMES["telegram"] == "python-telegram-bot"


def test_missing_module_is_reported_with_requirements_verdict(monkeypatch, capsys):
    class H2(_H):
        def do_GET(self):
            if "/logs" in self.path:
                body = {"logs": "ERROR - خطا: No module named 'telegram'\n"}
            elif "/filetext" in self.path and "requirements.txt" in self.path:
                body = {"text": "yt-dlp\n"}
            elif "/filetext" in self.path:
                body = {"text": "from telegram.ext import Application\n"}
            elif "/files" in self.path:
                body = {"files": [{"path": "main.py", "size": 10}, {"path": "requirements.txt", "size": 7}]}
            elif self.path.endswith("/env"):
                body = {"name": "x", "env": [{"key": "BOT_TOKEN", "value": GOOD}, {"key": "chat", "value": "1"}]}
            else:
                body = {}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)
    srv = HTTPServer(("127.0.0.1", 0), H2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "5"])
    try:
        rc = CH.main()
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert rc == 1
    assert "python-telegram-bot» در requirements.txt نیست" in out
    assert "Application" in out and "متغیرِ با نامِ کوچک" in out


def test_fix_token_replaces_rejected_registered_token_with_the_single_valid_file_token(monkeypatch, capsys):
    saved = {}

    class H3(_H):
        def do_GET(self):
            if self.path.endswith("/env"):
                body = {"name": "x", "env": [{"key": "BOT_TOKEN", "value": saved.get("tok", OLD)},
                                             {"key": "ADMIN_ID", "value": "7"}]}
            elif "/files" in self.path:
                body = {"files": [{"path": "main.py", "size": 10}]}
            elif "/filetext" in self.path:
                body = {"text": f'BOT_TOKEN = "{GOOD}"\n'}
            else:
                body = {"logs": ""}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            from urllib.parse import parse_qs
            payload = json.loads(parse_qs(self.rfile.read(n).decode())["payload"][0])
            saved["tok"] = payload["BOT_TOKEN"]
            saved["other"] = payload["ADMIN_ID"]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
    srv = HTTPServer(("127.0.0.1", 0), H3)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x" if t.strip("'\" ") == GOOD else "❌ تلگرام رد کرد (HTTP 401)")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "5", "--fix-token"])
    try:
        CH.main()
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert saved["tok"] == GOOD and saved["other"] == "7"       # فقط توکن عوض شد، بقیهٔ متغیرها دست‌نخورده
    assert "تأیید شد" in out and GOOD not in out and OLD not in out


def test_fix_token_does_nothing_when_registered_token_is_valid(monkeypatch, capsys):
    posted = []

    class H4(_H):
        def do_POST(self):
            posted.append(1)
            self.send_response(200)
            self.end_headers()
    srv = HTTPServer(("127.0.0.1", 0), H4)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "5", "--fix-token"])
    try:
        CH.main()
    finally:
        srv.shutdown()
    assert posted == [] and "نیازی به تغییر نیست" in capsys.readouterr().out


# ═══════════ PATCH_HOSTED_MEDIA_CHECK_V1 — ffmpeg / moviepy ═══════════

def _marks(res):
    return [m for m, _ in res]


def _text(res):
    return "\n".join(t for _, t in res)


def test_req_spec_and_moviepy_version_rules():
    assert CH.req_spec("aiogram==3\nmoviepy==1.0.3\n", "moviepy") == (True, "==1.0.3")
    assert CH.req_spec("MoviePy>=1.0.3  # x\n", "moviepy") == (True, ">=1.0.3")
    assert CH.req_spec("moviepy\n", "moviepy") == (True, "")
    assert CH.req_spec("imageio-ffmpeg\n", "moviepy") == (False, "")
    assert CH.req_spec("imageio_ffmpeg==0.5.1\n", "imageio-ffmpeg") == (True, "==0.5.1")
    for spec, v2 in (("", True), (">=1.0.3", True), ("==2.1.1", True), ("==1.0.3", False), ("<2", False),
                     ("<2.0", False), ("<=1.0.3", False), ("~=1.0.3", False), (">=1.0,<2", False), ("<2.1", True)):
        assert CH.moviepy_is_v2(spec) is v2, spec


EDITOR_CODE = {"bot.py": "from moviepy.editor import VideoFileClip\n\ndef to_note(p):\n    return VideoFileClip(p)\n"}


def test_moviepy_editor_with_unpinned_moviepy_is_the_reported_failure():
    res = CH.diagnose_media_deps(EDITOR_CODE, "aiogram\nmoviepy\n", "FROM python:3.12\nRUN pip install -r r.txt\n", [])
    assert "❌" in _marks(res)
    t = _text(res)
    assert "moviepy.editor" in t and "moviepy==1.0.3" in t and "rebuild" in t
    pinned = CH.diagnose_media_deps(EDITOR_CODE, "moviepy==1.0.3\n", "FROM python:3.12\n", [])
    assert "❌" not in _marks(pinned)                                             # با ۱.x ایمپورت editor درست است
    new_import = CH.diagnose_media_deps({"bot.py": "from moviepy import VideoFileClip\n"}, "moviepy\n",
                                        "FROM python:3.12\n", [])
    assert "❌" not in _marks(new_import)                                         # ایمپورت ۲.x با ۲.x درست است


def test_moviepy_missing_from_requirements_or_requirements_file_missing():
    res = CH.diagnose_media_deps(EDITOR_CODE, "aiogram\n", "FROM python:3.12\n", [])
    assert "❌" in _marks(res) and "نیست" in _text(res)
    res = CH.diagnose_media_deps(EDITOR_CODE, None, "FROM python:3.12\n", [])
    assert "❌" in _marks(res) and "requirements.txt در پروژه نیست" in _text(res)


def test_ffmpeg_binary_needs_dockerfile_or_imageio_ffmpeg():
    code = {"bot.py": "import subprocess\nsubprocess.run(['ffmpeg', '-i', 'a.mp4', 'b.mp4'])\n"}
    bad = CH.diagnose_media_deps(code, "aiogram\n", "FROM python:3.12-slim\nRUN pip install aiogram\n", [])
    assert "❌" in _marks(bad) and "apt-get install -y ffmpeg" in _text(bad)
    ok_docker = CH.diagnose_media_deps(code, "aiogram\n", "FROM python:3.12-slim\nRUN apt-get update && apt-get install -y ffmpeg\n", [])
    assert "❌" not in _marks(ok_docker)
    ok_pip = CH.diagnose_media_deps(code, "aiogram\nimageio-ffmpeg\n", "FROM python:3.12-slim\n", [])
    assert "❌" not in _marks(ok_pip)
    no_docker = CH.diagnose_media_deps(code, "aiogram\n", None, [])
    assert "docker exec" in _text(no_docker)                                       # بدون Dockerfile: دستور چک داخل کانتینر


def test_logs_alone_trigger_the_section_and_unrelated_projects_stay_silent():
    res = CH.diagnose_media_deps({"bot.py": "print('hi')\n"}, "aiogram\n", None,
                                 ["x", "ModuleNotFoundError: No module named 'moviepy.editor'"])
    assert any("moviepy.editor" in t for _, t in res)
    assert CH.diagnose_media_deps({"bot.py": "print('hi')\n"}, "aiogram\n", "FROM python:3.12\n", ["ok"]) == []


def _owner_env(monkeypatch, apps):
    import asyncio
    import config
    import models
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def setup():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as c:
            await c.run_sync(models.Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            for owner, name in apps:
                s.add(models.HostedApp(owner_id=owner, name=name))
            await s.commit()
        return maker
    maker = asyncio.run(setup())
    monkeypatch.setattr(config, "Session", maker)
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")


def test_owner_lookup_without_apps_and_with_several_apps(monkeypatch, capsys):
    _owner_env(monkeypatch, [(111, "klexshop"), (111, "klexshop2"), (222, "other")])
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "--owner", "333"])
    assert CH.main() == 2
    assert "اپی ثبت نشده" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "--owner", "111"])
    assert CH.main() == 2
    out = capsys.readouterr().out
    assert "2 اپ دارد" in out and "klexshop" in out and "klexshop2" in out and "other" not in out


def test_owner_lookup_single_app_runs_the_checks(monkeypatch, capsys):
    _owner_env(monkeypatch, [(8757601463, "klexshop")])
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "--owner", "8757601463"])
    try:
        CH.main()
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert "اپ این کاربر" in out and "klexshop" in out and "══ اپ" in out


def test_resize_with_old_moviepy_warns_about_the_antialias_shim_and_shows_code_lines():
    code = {"bot.py": "from moviepy.editor import VideoFileClip\n"
                      "clip = VideoFileClip(p).crop(width=480, height=480).resize((640, 640))\n"}
    res = CH.diagnose_media_deps(code, "moviepy\n", "FROM python:3.12\n", [])
    t = _text(res)
    assert "ANTIALIAS" in t and "LANCZOS" in t and "bot.py:2" in t and ".resize((640, 640))" in t
    shimmed = {"bot.py": code["bot.py"] + "import PIL.Image\nPIL.Image.ANTIALIAS = PIL.Image.LANCZOS\n"}
    assert "ANTIALIAS" not in _text(CH.diagnose_media_deps(shimmed, "moviepy==1.0.3\n", "FROM python:3.12\n", [])).split("کد bot.py")[0]
    new_style = {"bot.py": "from moviepy import VideoFileClip\nclip = VideoFileClip(p).resized((640, 640))\n"}
    assert "ANTIALIAS" not in _text(CH.diagnose_media_deps(new_style, "moviepy\n", "FROM python:3.12\n", []))


# ═══════════ PATCH_HOSTED_MEDIA_FIX_V1 — --fix-media / --apply / --restart / --verify ═══════════
import urllib.parse as _up

REQ_OLD = "aiogram==3.4\nmoviepy\nqrcode[pil]\n"
BOT_OLD = ("import os\ntry:\n    from moviepy.editor import VideoFileClip\n    OK = True\nexcept ImportError:\n    OK = False\n\n"
           "def f(c):\n    return c.resize(newsize=(384, 384))\n")


class _Store:
    def __init__(self, files):
        self.files = dict(files)
        self.posts, self.actions = [], []
        self.corrupt = False
        self.size_lie = {}


def _handler(store):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            u = _up.urlparse(self.path)
            q = _up.parse_qs(u.query)
            if u.path.endswith("/env"):
                self._send({"name": "klexshopbot", "env": [{"key": "BOT_TOKEN", "value": GOOD}], "needed": []})
            elif u.path.endswith("/files"):
                self._send({"files": [{"path": p, "size": store.size_lie.get(p, len(t.encode()))}
                                      for p, t in store.files.items()]})
            elif u.path.endswith("/filetext"):
                self._send({"text": store.files.get(q["path"][0], "")})
            else:
                self._send({"logs": "started\n"})

        def do_POST(self):
            form = _up.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            if self.path.endswith("/filetext"):
                path, text = form["path"][0], form["text"][0]
                store.posts.append(path)
                if store.corrupt and ("imageio-ffmpeg" in text or CH.SHIM_MARK in text):
                    text = text[:-5]                                     # سرور خراب: محتوا ناقص ذخیره می‌شود
                store.files[path] = text
            elif self.path.endswith("/action"):
                store.actions.append(form["action"][0])
            self._send({"ok": True})
    return H


def _run_main(monkeypatch, capsys, store, argv, tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), _handler(store))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIP_ADMIN_SECRET", "s")
    monkeypatch.setenv("VIP_API_BASE", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("BOTSAZ_BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(CH, "get_me", lambda t: "✅ معتبر: @x")
    monkeypatch.setattr(sys, "argv", ["check_hosted_app.py", "5", *argv])
    try:
        rc = CH.main()
    finally:
        srv.shutdown()
    return rc, capsys.readouterr().out


def test_plan_requirements_fix_pins_old_style_code_and_leaves_new_style_alone():
    new, notes = CH.plan_requirements_fix(REQ_OLD, True)
    assert new == "aiogram==3.4\nmoviepy==1.0.3\nqrcode[pil]\nimageio-ffmpeg\n" and any("1.0.3" in n for n in notes)
    assert CH.plan_requirements_fix(new, True)[0] is None                         # دومین اجرا: تغییری نیست
    new2, _ = CH.plan_requirements_fix("moviepy>=2.1\n", False)
    assert new2 == "moviepy>=2.1\nimageio-ffmpeg\n"                               # کد سبک ۲.x ⇒ نسخه دست‌نخورده
    new3, _ = CH.plan_requirements_fix("aiogram\n", True)
    assert new3 == "aiogram\nmoviepy==1.0.3\nimageio-ffmpeg\n"                    # اصلا moviepy نبود
    new4, _ = CH.plan_requirements_fix("moviepy==2.0.0  # x\nimageio_ffmpeg==0.5.1\n", True)
    assert new4 == "moviepy==1.0.3\nimageio_ffmpeg==0.5.1\n"                      # imageio_ffmpeg با زیرخط هم شناخته می‌شود
    assert CH.plan_requirements_fix(None, True)[0] is None


def test_plan_shim_patch_keeps_indentation_compiles_and_is_idempotent():
    new = CH.plan_shim_patch(BOT_OLD)
    assert new is not None
    compile(new, "x", "exec")
    lines = new.splitlines()
    i = next(k for k, ln in enumerate(lines) if "moviepy.editor" in ln)
    assert lines[i - 3].startswith("    import PIL.Image as _PILImage") and "ANTIALIAS" in lines[i - 2] + lines[i - 1]
    assert lines[i - 1].startswith("        _PILImage.ANTIALIAS") or lines[i - 1].startswith("    _PILImage.ANTIALIAS")
    assert CH.plan_shim_patch(new) is None                                        # دوباره اضافه نمی‌شود
    assert CH.plan_shim_patch("print('hi')\n") is None
    top = CH.plan_shim_patch("from moviepy.editor import (\n    VideoFileClip,\n)\nclip.resize(1)\n")
    compile(top, "x", "exec")
    assert top.startswith("import PIL.Image as _PILImage")


def test_size_matches_allows_crlf_but_not_truncation():
    assert CH.size_matches("a\nb\n", 4) and CH.size_matches("a\nb\n", 6)           # فایل CRLF
    assert not CH.size_matches("a\nb\n", 5000)


def test_fix_media_dry_run_writes_nothing(monkeypatch, capsys, tmp_path):
    store = _Store({"requirements.txt": REQ_OLD, "bot.py": BOT_OLD})
    rc, out = _run_main(monkeypatch, capsys, store, ["--fix-media"], tmp_path)
    assert store.posts == [] and store.actions == [] and store.files["requirements.txt"] == REQ_OLD
    assert "پیش‌نمایش" in out and "moviepy==1.0.3" in out and "_PILImage.ANTIALIAS" in out and "--apply" in out
    assert not list(tmp_path.iterdir())                                           # پشتیبان هم نساخت


def test_fix_media_apply_writes_backs_up_restarts_and_is_idempotent(monkeypatch, capsys, tmp_path):
    store = _Store({"requirements.txt": REQ_OLD, "bot.py": BOT_OLD})
    waits = []
    monkeypatch.setattr(CH, "verify_in_container", lambda name, wait=0, poll=10: (waits.append((name, wait)) or (True, "[c] OK moviepy 1.0.3")))
    rc, out = _run_main(monkeypatch, capsys, store, ["--fix-media", "--apply", "--restart", "--verify"], tmp_path)
    assert store.files["requirements.txt"] == "aiogram==3.4\nmoviepy==1.0.3\nqrcode[pil]\nimageio-ffmpeg\n"
    assert CH.SHIM_MARK in store.files["bot.py"] and store.files["bot.py"].count("from moviepy.editor") == 1
    compile(store.files["bot.py"], "bot.py", "exec")
    assert store.actions == ["restart"] and "ری‌استارت درخواست شد" in out
    assert waits == [("klexshopbot", 150)]                                        # بعد از ری‌استارت منتظر ساخت ایمیج می‌ماند
    backups = list(tmp_path.rglob("requirements.txt")) + list(tmp_path.rglob("bot.py"))
    assert {b.name: b.read_text(encoding="utf-8") for b in backups} == {"requirements.txt": REQ_OLD, "bot.py": BOT_OLD}
    assert "klexshopbot" in str(backups[0])
    posts_before = len(store.posts)
    rc2, out2 = _run_main(monkeypatch, capsys, store, ["--fix-media", "--apply", "--restart"], tmp_path)
    assert len(store.posts) == posts_before and store.actions == ["restart"] and "چیزی برای نوشتن نیست" in out2


def test_fix_media_restores_the_original_when_the_server_stores_it_wrong(monkeypatch, capsys, tmp_path):
    store = _Store({"requirements.txt": REQ_OLD, "bot.py": BOT_OLD})
    store.corrupt = True
    rc, out = _run_main(monkeypatch, capsys, store, ["--fix-media", "--apply", "--restart"], tmp_path)
    assert store.files == {"requirements.txt": REQ_OLD, "bot.py": BOT_OLD}        # هر دو به حالت اول برگشتند
    assert "نسخهٔ اصلی برگردانده شد" in out and store.actions == [] and rc == 1    # با نوشتن خراب ری‌استارت نمی‌کند


def test_fix_media_refuses_to_patch_code_when_the_text_may_be_truncated(monkeypatch, capsys, tmp_path):
    store = _Store({"requirements.txt": REQ_OLD, "bot.py": BOT_OLD})
    store.size_lie = {"bot.py": 99_999}
    rc, out = _run_main(monkeypatch, capsys, store, ["--fix-media", "--apply"], tmp_path)
    assert store.files["bot.py"] == BOT_OLD and "ناقص باشد" in out
    assert "moviepy==1.0.3" in store.files["requirements.txt"]                    # requirements با این‌حال درست شد


def test_fix_media_does_nothing_for_projects_without_moviepy(monkeypatch, capsys, tmp_path):
    store = _Store({"requirements.txt": "aiogram\n", "bot.py": "print('hi')\n"})
    rc, out = _run_main(monkeypatch, capsys, store, ["--fix-media", "--apply"], tmp_path)
    assert store.posts == [] and "moviepy را ایمپورت نمی‌کند" in out


def test_fix_media_new_style_code_keeps_moviepy_version(monkeypatch, capsys, tmp_path):
    store = _Store({"requirements.txt": "moviepy\n", "bot.py": "from moviepy import VideoFileClip\nclip.resized((1, 1))\n"})
    rc, out = _run_main(monkeypatch, capsys, store, ["--fix-media", "--apply"], tmp_path)
    assert store.files["requirements.txt"] == "moviepy\nimageio-ffmpeg\n"
    assert store.files["bot.py"] == "from moviepy import VideoFileClip\nclip.resized((1, 1))\n"


def _fake_docker(results):
    calls = []

    def fake(args, input_text=None, timeout=90):
        calls.append(args)
        return results.pop(0) if len(results) > 1 else results[0]
    return fake, calls


OK_LINE = "x\nOK moviepy 1.0.3 numpy 2.1.0 pillow 11.0 frame (24, 24, 3) ffmpeg /usr/bin/ffmpeg\n"


def test_verify_in_container_success_failure_and_python_fallback(monkeypatch):
    ps = (0, "botsaz_klexshopbot\nother_app\n", "")
    fake, calls = _fake_docker([ps, (0, OK_LINE, "")])
    monkeypatch.setattr(CH, "_docker", fake)
    ok, msg = CH.verify_in_container("klexshopbot")
    assert ok and "[botsaz_klexshopbot]" in msg and "moviepy 1.0.3" in msg
    assert calls[1][:3] == ["exec", "-i", "botsaz_klexshopbot"] and calls[1][3:] == ["python", "-"]

    fake, calls = _fake_docker([ps, (1, "", "Traceback\nModuleNotFoundError: No module named 'moviepy.editor'\n")])
    monkeypatch.setattr(CH, "_docker", fake)
    ok, msg = CH.verify_in_container("klexshopbot")
    assert not ok and "No module named 'moviepy.editor'" in msg

    fake, calls = _fake_docker([ps, (127, "", "executable file not found in $PATH"), (0, OK_LINE, "")])
    monkeypatch.setattr(CH, "_docker", fake)
    ok, _ = CH.verify_in_container("klexshopbot")
    assert ok and calls[2][3] == "python3"                                        # python نبود ⇒ python3


def test_verify_in_container_container_lookup_and_waiting(monkeypatch):
    monkeypatch.setattr(CH, "_docker", _fake_docker([(0, "other\n", "")])[0])
    ok, msg = CH.verify_in_container("klexshopbot")
    assert not ok and "پیدا نشد" in msg
    monkeypatch.setattr(CH, "_docker", _fake_docker([(0, "app_klexshopbot\napp_klexshopbot2\n", "")])[0])
    ok, msg = CH.verify_in_container("klexshopbot")
    assert not ok and "app_klexshopbot" in msg and "app_klexshopbot2" in msg      # چند نامزد ⇒ حدس نمی‌زند
    monkeypatch.setattr(CH, "_docker", _fake_docker([(1, "", "docker daemon down")])[0])
    ok, msg = CH.verify_in_container("klexshopbot")
    assert not ok and "docker daemon down" in msg
    # ری‌استارت: اول کانتینر هنوز بالا نیامده، بعد بالا می‌آید
    seq = [(0, "", ""), (0, "app_klexshopbot\n", ""), (0, OK_LINE, "")]
    monkeypatch.setattr(CH, "_docker", _fake_docker(seq)[0])
    monkeypatch.setattr(CH.time, "sleep", lambda s: None)
    ok, _ = CH.verify_in_container("klexshopbot", wait=30, poll=1)
    assert ok


def test_fix_media_refuses_to_rewrite_requirements_when_the_text_may_be_truncated(monkeypatch, capsys, tmp_path):
    store = _Store({"requirements.txt": REQ_OLD, "bot.py": BOT_OLD})
    store.size_lie = {"requirements.txt": 99_999}
    rc, out = _run_main(monkeypatch, capsys, store, ["--fix-media", "--apply"], tmp_path)
    assert store.files["requirements.txt"] == REQ_OLD and "ناقص باشد" in out and "moviepy==1.0.3" in out
    rc, out = _run_main(monkeypatch, capsys, _Store({"requirements.txt": REQ_OLD, "bot.py": BOT_OLD}),
                        ["--fix-media", "--restart"], tmp_path)
    assert "--restart بدون --apply اثری ندارد" in out
