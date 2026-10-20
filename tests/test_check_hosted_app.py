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
    assert "main.py" in out and "با توکنِ ثبت‌شده فرق دارد" in out      # توکنِ قدیمیِ سخت‌کدشده
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
