"""سرور روی پایتونِ ۳٫۱۰ است (Ubuntu 22.04)؛ پایتونِ جدید داخلِ f-string بک‌اسلش/نقل‌قولِ تکراری را می‌پذیرد ولی ۳٫۱۰ نه ⇒
بیلدِ ۶۶۲ روی سرور SyntaxError داد و همهٔ ربات‌ها از کار افتادند. این تست همهٔ فایل‌ها را با پایتونِ ۳٫۱۱ (قوانینِ f-stringِ قدیمی) کامپایل می‌کند."""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = shutil.which("python3.11") or shutil.which("python3.10")

_SCRIPT = r"""
import pathlib, sys
bad = []
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    parts = set(p.parts)
    if parts & {"venv", ".venv", "node_modules", "__pycache__", ".git", "tests"}:
        continue
    try:
        compile(p.read_text(encoding="utf-8"), str(p), "exec")
    except SyntaxError as e:
        bad.append(f"{p}:{e.lineno}: {e.msg}")
print("\n".join(bad))
"""


@pytest.mark.skipif(PY is None, reason="python3.10/3.11 در این محیط نیست")
def test_all_modules_compile_with_pre_pep701_python():
    out = subprocess.run([PY, "-I", "-c", _SCRIPT, str(ROOT)], capture_output=True, text=True, timeout=240)
    assert out.stdout.strip() == "", "SyntaxError (پایتونِ سرور):\n" + out.stdout
