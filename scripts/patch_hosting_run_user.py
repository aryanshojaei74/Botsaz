#!/usr/bin/env python3
"""patch_hosting_run_user.py — اصلاحِ `docker run` در hosting/docker_manager.py (PATCH_HOSTING_RUN_USER_V1).

مشکل: فایل‌هایِ اپِ مشتری با chown به 10001 داده می‌شوند، ولی اگر Dockerfileِ خودِ مشتری `USER` نداشته باشد، کانتینر با root
اجرا می‌شود و چون `--cap-drop ALL` است، root دیگر توانِ خواندن/نوشتنِ فایل‌هایِ مالِ 10001 را ندارد
(PermissionError: .env ، sqlite unable to open database file) و کانتینر هزاران بار ری‌استارت می‌شود.

راه‌حل: اگر ایمیج کاربرِ مشخصی ندارد (Config.User خالی)، با `--user 10001:10001` اجرا شود. ایمیج‌هایی که خودشان USER دارند دست‌نخورده‌اند.

اجرا (روی سرور):
    python3 patch_hosting_run_user.py /opt/botsazvip/hosting/docker_manager.py
(پشتیبان می‌گیرد، idempotent است، و اگر فایلِ نتیجه کامپایل نشد پشتیبان را برمی‌گرداند.)
"""
import py_compile
import shutil
import sys
from pathlib import Path

MARK = "PATCH_HOSTING_RUN_USER_V1"

HELPER = '''    # === PATCH_HOSTING_RUN_USER_V1 ===
    # ایمیجی که USER ندارد با root اجرا می‌شود؛ با --cap-drop ALL، root فایل‌هایِ مالِ APP_UID را نمی‌خواند/نمی‌نویسد.
    _user_args: list[str] = []
    _ucode, _uout, _uerr = await _run(
        "docker", "image", "inspect", "-f", "{{.Config.User}}", image_name(app_name))
    if _ucode == 0 and not (_uout or "").strip():
        _user_args = ["--user", f"{APP_UID}:{APP_GID}"]

'''
RUN_ANCHOR = '    code, _out, err = await _run(\n        "docker", "run", "-d",'
SEC_ANCHOR = '        "--security-opt", "no-new-privileges",\n'


def patch(text: str) -> str:
    if MARK in text:
        return text
    if text.count(RUN_ANCHOR) != 1 or text.count(SEC_ANCHOR) != 1:
        raise SystemExit("الگوهای مورد انتظار در فایل پیدا نشد؛ فایل تغییر نکرد.")
    text = text.replace(RUN_ANCHOR, HELPER + RUN_ANCHOR, 1)
    text = text.replace(SEC_ANCHOR, SEC_ANCHOR + "        *_user_args,\n", 1)
    return text


def main(path: str) -> None:
    p = Path(path)
    src = p.read_text(encoding="utf-8")
    new = patch(src)
    if new == src:
        print("قبلاً اعمال شده؛ تغییری نشد.")
        return
    bak = p.with_suffix(".py.bak_run_user")
    shutil.copy2(p, bak)
    p.write_text(new, encoding="utf-8")
    try:
        py_compile.compile(str(p), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(bak, p)
        raise SystemExit(f"کامپایل ناموفق؛ پشتیبان برگردانده شد: {e}")
    print(f"✅ اعمال شد. پشتیبان: {bak}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_hosting_run_user.py /opt/botsazvip/hosting/docker_manager.py")
    main(sys.argv[1])
