import ast
import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import hosted_miniapp_domain as MD

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("miniapp_files_patch", ROOT / "deploy" / "miniapp_files_patch.py")
FP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FP)
spec2 = importlib.util.spec_from_file_location("miniapp_ui_patch", ROOT / "deploy" / "miniapp_ui_patch.py")
UP = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(UP)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(MD, "STATIC_ROOT", tmp_path)
    d = tmp_path / "mygame"
    (d / "img").mkdir(parents=True)
    (d / "index.html").write_text("<html>hi</html>", encoding="utf-8")
    (d / "app.js").write_text("console.log(1)", encoding="utf-8")
    (d / "img" / "a.png").write_bytes(b"\x89PNG" + b"0" * 20)
    return "mygame"


def test_list_and_read(app):
    files = MD.list_static(app)
    assert files[0]["path"] == "index.html"
    assert {f["path"]: f["text"] for f in files} == {"index.html": True, "app.js": True, "img/a.png": False}
    r = MD.read_static_text(app, "app.js")
    assert r.ok and r.message == "console.log(1)"
    assert not MD.read_static_text(app, "img/a.png").ok            # باینری ویرایش نمی‌شود
    assert not MD.read_static_text(app, "nope.js").ok


def test_write_replace_add_delete(app, tmp_path):
    assert MD.write_static(app, "app.js", b"x=2", must_exist=True).ok
    assert (tmp_path / "mygame" / "app.js").read_text() == "x=2"
    r = MD.write_static(app, "css/site.css", b"body{}")
    assert r.ok and "افزوده" in r.message and (tmp_path / "mygame" / "css" / "site.css").is_file()
    assert not MD.write_static(app, "ghost.js", b"1", must_exist=True).ok
    assert MD.delete_static_file(app, "css/site.css").ok
    assert not MD.delete_static_file(app, "index.html").ok         # index.html حذف نمی‌شود
    assert MD.write_static(app, "index.html", b"<html>new</html>").ok


def test_path_traversal_and_hidden_rejected(app, tmp_path):
    for bad in ("../x.txt", "/etc/passwd", "a/../../b", ".env", "img/.hidden", "node_modules/x.js", "a\\b.js", "", "."):
        assert not MD.write_static(app, bad, b"x").ok, bad
        assert not MD.read_static_text(app, bad).ok, bad
        assert not MD.delete_static_file(app, bad).ok, bad
    assert not (tmp_path / "x.txt").exists()


def test_symlink_dir_rejected(app, tmp_path):
    outside = tmp_path.parent / "outside_dir_for_test"
    outside.mkdir(exist_ok=True)
    (tmp_path / "mygame" / "link").symlink_to(outside)
    assert not MD.write_static(app, "link/evil.js", b"x").ok
    assert not (outside / "evil.js").exists()


def test_size_limits(app, monkeypatch):
    monkeypatch.setattr(MD, "MAX_FILE_BYTES", 10)
    assert not MD.write_static(app, "big.bin", b"x" * 11).ok
    monkeypatch.setattr(MD, "MAX_FILE_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(MD, "MAX_UNZIPPED_BYTES", 100)
    assert not MD.write_static(app, "big2.bin", b"x" * 200).ok


def test_requires_static_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(MD, "STATIC_ROOT", tmp_path)
    assert not MD.write_static("emptyapp", "a.js", b"x").ok


API = '''from fastapi import Header, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
hosting_router = None
MD = None
async def _auth(a, b): pass
async def _owned_app(u, i): pass
async def api_miniapp_upload(): pass
'''
UI = '''PAGE_HTML = """<script>
function openApp(id){
  var h = '';
  h += '<div class="card" style="margin-top:12px">'
     + '<div class="kv"><span>وضعیت اشتراک</span><span>'
     + 'x</span></div>';
}
function pick(){
  var x = 1;
}
</script>"""
'''


def test_api_patch_idempotent_and_valid():
    new = FP.patch_api(API)
    assert FP.patch_api(new) == new
    ast.parse(new)
    assert "api_miniapp_file_upload" in new and "api_miniapp_file_delete" in new


def test_api_patch_requires_previous_patch():
    with pytest.raises(SystemExit):
        FP.patch_api("nothing")


def test_ui_patch_requires_card_and_is_idempotent():
    with pytest.raises(SystemExit):
        FP.patch_ui(UI)                      # بدونِ پچِ کارتِ مینی‌اپ
    base = UP.patch(UI)
    new = FP.patch_ui(base)
    assert FP.patch_ui(new) == new
    ast.parse(new)
    assert "function miniFileEdit" in new and "minifiles-card" in new


def test_js_clean():
    assert "\\" not in FP.JS and '"""' not in FP.JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node نیست")
def test_inserted_js_is_valid_javascript():
    ns = {}
    exec(FP.patch_ui(UP.patch(UI)), ns)
    js = ns["PAGE_HTML"].split("<script>")[1].split("</script>")[0]
    p = Path(tempfile.mkdtemp()) / "t.js"
    p.write_text(js)
    r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
