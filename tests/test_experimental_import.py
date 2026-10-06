"""
رگرسیون (PATCH_EXPERIMENT_TEST_IMPORT_FIX_V1): فایلِ tenant_experimental/test_admin_send_delete_experiment.py
با importِ لخت («from admin_send_delete_experiment import ...») فقط وقتی کار می‌کرد که از داخلِ پوشه‌اش
اجرا شود؛ preflight.py آن را به‌صورتِ ماژولِ پکیج import می‌کرد و در *هر* دیپلوی این هشدار را می‌داد:
    ModuleNotFoundError: No module named 'admin_send_delete_experiment'
"""
import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_experiment_test_module_imports_as_a_package_module():
    mod = importlib.import_module("tenant_experimental.test_admin_send_delete_experiment")
    assert callable(mod.main) and callable(mod.test_admin_send_delete_experiment)


def test_experiment_still_passes_when_called_as_a_module():
    mod = importlib.import_module("tenant_experimental.test_admin_send_delete_experiment")
    mod.test_admin_send_delete_experiment()          # همان assertهای main()


def test_script_mode_from_inside_its_folder_still_works():
    """طراحیِ اصلیِ فایل: cd tenant_experimental && python test_admin_send_delete_experiment.py"""
    r = subprocess.run([sys.executable, "test_admin_send_delete_experiment.py"],
                       cwd=ROOT / "tenant_experimental", capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "EXPERIMENT_TESTS=OK" in r.stdout


def test_no_bare_sibling_imports_left_in_tenant_experimental():
    """هر importِ لخت از فایل‌هایِ هم‌پوشه باید یک راهِ جایگزین (پکیجی) هم داشته باشد."""
    import ast
    siblings = {p.stem for p in (ROOT / "tenant_experimental").glob("*.py") if p.stem != "__init__"}
    for p in (ROOT / "tenant_experimental").glob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in siblings:
                # فقط داخلِ بلوکِ except ImportError مجاز است (نسخهٔ جایگزینِ حالتِ اسکریپت)
                parents = [t for t in ast.walk(tree) if isinstance(t, ast.ExceptHandler)
                           and any(n is node for n in ast.walk(t))]
                assert parents, f"{p.name}:{node.lineno} importِ لخت از «{node.module}» بدونِ راهِ جایگزین"
