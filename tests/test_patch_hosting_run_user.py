import importlib.util
import py_compile
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("phru", Path(__file__).resolve().parent.parent / "scripts" / "patch_hosting_run_user.py")
phru = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(phru)

SNIPPET = '''
APP_UID = 10001
APP_GID = 10001


async def _run(*a):
    return 0, "", ""


def image_name(n):
    return n


async def start(app_name, path):
    code, _out, err = await _run(
        "docker", "run", "-d",
        "--name", app_name,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        image_name(app_name))
    return code
'''


def test_patch_adds_user_args_once_and_compiles(tmp_path):
    f = tmp_path / "docker_manager.py"
    f.write_text(SNIPPET)
    phru.main(str(f))
    out = f.read_text()
    assert "*_user_args," in out and "--user" in out and "{{.Config.User}}" in out
    py_compile.compile(str(f), doraise=True)
    phru.main(str(f))                                     # دومین بار: بدونِ تغییر
    assert f.read_text() == out
    assert (tmp_path / "docker_manager.py.bak_run_user").read_text() == SNIPPET


def test_patch_refuses_unknown_layout(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("print('hi')\n")
    try:
        phru.main(str(f))
    except SystemExit:
        pass
    else:
        raise AssertionError("باید رد می‌کرد")
    assert f.read_text() == "print('hi')\n"
