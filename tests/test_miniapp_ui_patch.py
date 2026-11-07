"""PATCH_MINIAPP_UI_V1 — پچِ خودکارِ ui.py (کارتِ مینی‌اپ): idempotent، بدونِ بک‌اسلش، و جاوااسکریپتِ معتبر."""
import ast
import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("miniapp_ui_patch", ROOT / "deploy" / "miniapp_ui_patch.py")
UP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(UP)

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


def test_patch_is_idempotent_and_valid_python():
    new = UP.patch(UI)
    assert UP.patch(new) == new
    ast.parse(new)
    assert new.count('id="mini-card"') == 1 and "function miniBuy" in new


def test_patch_refuses_unknown_layout():
    for bad in ("nothing here", UI.replace("function pick(){", "function other(){")):
        with pytest.raises(SystemExit):
            UP.patch(bad)


def test_inserted_js_has_no_backslashes_or_triple_quotes():
    assert "\\" not in UP.JS and '"""' not in UP.JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node نیست")
def test_inserted_js_is_valid_javascript():
    ns = {}
    exec(UP.patch(UI), ns)
    js = ns["PAGE_HTML"].split("<script>")[1].split("</script>")[0]
    p = Path(tempfile.mkdtemp()) / "t.js"
    p.write_text(js)
    r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
