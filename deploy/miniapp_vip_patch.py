#!/usr/bin/env python3
"""deploy/miniapp_vip_patch.py — افزودنِ ساب‌دامینِ مینی‌اپ به سرویسِ botsazvip (PATCH_MINIAPP_DOMAIN_V1).

یک‌بار اجرا شود (idempotent: اگر قبلاً اعمال شده باشد چیزی را دوباره نمی‌نویسد؛ از هر فایل پشتیبان می‌گیرد):
    python3 /opt/botsaz/deploy/miniapp_vip_patch.py
    systemctl restart botsazvip

تغییرها (همه افزودنی‌اند):
  • main.py : حلقهٔ همگام‌سازیِ نگاشت‌ها (هر ۶۰ ثانیه) + middlewareِ سرو کردنِ مینی‌اپ‌هایِ استاتیک.
  • api.py  : endpointهایِ کاربر (وضعیت/خرید/Menu Button/آپلودِ zip استاتیک/حذف).
"""
import ast
import shutil
import sys
from pathlib import Path

HOSTING = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/botsazvip/hosting")

MAIN_LOOP = '''# === PATCH_MINIAPP_DOMAIN_V1 === همگام‌سازیِ نگاشتِ ساب‌دامینِ مینی‌اپ‌ها (هر ۶۰ ثانیه)
async def _miniapp_sync_loop():
    try:
        sys.path.insert(0, "/opt/botsaz")
        import hosted_miniapp_domain as MD
        from hosting import docker_manager as dm
    except Exception:
        logger.exception("miniapp sync: بارگذاری ممکن نشد؛ حلقه اجرا نمی‌شود")
        return
    while True:
        try:
            await MD.sync(dm)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("miniapp sync")
        await asyncio.sleep(60)


'''

MAIN_MW = '''

# === PATCH_MINIAPP_STATIC_V1 === سرو کردنِ مینی‌اپ‌هایِ استاتیکِ *.supraplus.ir (nginx همهٔ آن‌ها را به همین سرویس می‌فرستد)
@app.middleware("http")
async def _miniapp_static_mw(request, call_next):
    try:
        sys.path.insert(0, "/opt/botsaz")
        import hosted_miniapp_domain as _md
        host = (request.headers.get("host") or "")
        if host.lower().split(":")[0].endswith("." + _md.DOMAIN):
            resp = await _md.serve_static(host, request.url.path)
            if resp is not None:
                return resp
    except Exception:
        logger.exception("miniapp static")
    return await call_next(request)
'''

API_BLOCK = '''

# === PATCH_MINIAPP_DOMAIN_V1 === ساب‌دامینِ مینی‌اپ (اشتراکِ ماهانه) + آپلودِ zipِ استاتیک
import sys as _sys_md
_sys_md.path.insert(0, "/opt/botsaz")
import hosted_miniapp_domain as MD  # noqa: E402


@hosting_router.get("/api/hosting/apps/{app_id}/miniapp")
async def api_miniapp_status(app_id: int, x_init_data: str | None = Header(default=None),
                             x_kb_token: str | None = Header(default=None)):
    u = await _auth(x_init_data, x_kb_token)
    app = await _owned_app(u, app_id)
    return JSONResponse({"ok": True, "price": await MD.get_price(), "active": MD.is_active(app),
                         "url": MD.url_of(app.miniapp_sub) if app.miniapp_sub else "",
                         "until": app.miniapp_until.isoformat() if app.miniapp_until else None,
                         "static": MD.has_static(app.name)})


@hosting_router.post("/api/hosting/apps/{app_id}/miniapp/buy")
async def api_miniapp_buy(app_id: int, x_init_data: str | None = Header(default=None),
                          x_kb_token: str | None = Header(default=None)):
    u = await _auth(x_init_data, x_kb_token)
    app = await _owned_app(u, app_id)
    r = await MD.buy(app.id)
    if not r.ok:
        raise HTTPException(status_code=400, detail=r.message)
    try:
        await MD.sync(dm)          # همین الان فعال شود، نه تا ۶۰ ثانیهٔ بعد
    except Exception:
        logger.exception("miniapp sync پس از خرید")
    return JSONResponse({"ok": True, "url": r.url, "until": r.until.isoformat()})


@hosting_router.post("/api/hosting/apps/{app_id}/miniapp/menu-button")
async def api_miniapp_menu(app_id: int, body: dict, x_init_data: str | None = Header(default=None),
                           x_kb_token: str | None = Header(default=None)):
    u = await _auth(x_init_data, x_kb_token)
    app = await _owned_app(u, app_id)
    r = await MD.set_menu_button(app.id, (body or {}).get("text") or "🌐 باز کردن")
    return JSONResponse({"ok": r.ok, "message": r.message})


@hosting_router.post("/api/hosting/apps/{app_id}/miniapp/upload")
async def api_miniapp_upload(app_id: int, file: UploadFile = File(...),
                             x_init_data: str | None = Header(default=None),
                             x_kb_token: str | None = Header(default=None)):
    """zipِ HTML/JS (خروجیِ build) را به‌عنوانِ مینی‌اپِ استاتیک بارگذاری می‌کند (بدونِ کانتینر)."""
    import os as _os
    import tempfile as _tf
    u = await _auth(x_init_data, x_kb_token)
    app = await _owned_app(u, app_id)
    if not MD.is_active(app):
        raise HTTPException(status_code=400, detail="اول مینی‌اپ را فعال کن.")
    fd, tmp = _tf.mkstemp(suffix=".zip")
    size = 0
    try:
        with _os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MD.MAX_ZIP_BYTES:
                    raise HTTPException(status_code=400, detail=f"حجمِ zip بیشتر از {MD.MAX_ZIP_BYTES // (1024 * 1024)} مگابایت است.")
                out.write(chunk)
        r = await asyncio.to_thread(MD.install_static, app.name, tmp)
    finally:
        try:
            _os.unlink(tmp)
        except OSError:
            pass
    if not r.ok:
        raise HTTPException(status_code=400, detail=r.message)
    try:
        await MD.sync(dm)
    except Exception:
        logger.exception("miniapp sync پس از آپلود")
    return JSONResponse({"ok": True, "message": r.message, "url": MD.url_of(app.miniapp_sub)})


@hosting_router.delete("/api/hosting/apps/{app_id}/miniapp/static")
async def api_miniapp_static_delete(app_id: int, x_init_data: str | None = Header(default=None),
                                    x_kb_token: str | None = Header(default=None)):
    u = await _auth(x_init_data, x_kb_token)
    app = await _owned_app(u, app_id)
    existed = MD.remove_static(app.name)
    try:
        await MD.sync(dm)
    except Exception:
        logger.exception("miniapp sync پس از حذفِ استاتیک")
    return JSONResponse({"ok": True, "removed": existed})
'''


def _backup(p: Path) -> None:
    b = p.with_name(p.name + ".bak_miniapp")
    if not b.exists():
        shutil.copy2(p, b)


def patch_main(s: str) -> str:
    if "_miniapp_sync_loop" in s and "_miniapp_static_mw" in s:
        return s
    a1 = "@asynccontextmanager\nasync def lifespan(app: FastAPI):"
    a2 = "    task = asyncio.create_task(enforcer_loop(bot))\n    try:\n        yield\n    finally:\n        task.cancel()\n"
    a3 = 'app = FastAPI(title="BotSaz VIP Hosting", lifespan=lifespan)\n'
    if "_miniapp_sync_loop" not in s:
        if s.count(a1) != 1 or s.count(a2) != 1:
            raise SystemExit("main.py: ساختارِ lifespan انتظار را برآورده نکرد؛ چیزی تغییر نکرد")
        s = s.replace(a1, MAIN_LOOP + a1, 1)
        s = s.replace(a2, "    task = asyncio.create_task(enforcer_loop(bot))\\n    mini_task = asyncio.create_task(_miniapp_sync_loop())   # PATCH_MINIAPP_DOMAIN_V1\\n    try:\\n        yield\\n    finally:\\n        task.cancel()\\n        mini_task.cancel()\\n".replace("\\n", "\n"), 1)
    if "_miniapp_static_mw" not in s:
        if s.count(a3) != 1:
            raise SystemExit("main.py: خطِ ساختِ app پیدا نشد؛ چیزی تغییر نکرد")
        s = s.replace(a3, a3 + MAIN_MW, 1)
    return s


def patch_api(s: str) -> str:
    if "api_miniapp_upload" in s:
        return s
    for need in ("hosting_router", "_auth", "_owned_app", "JSONResponse", "HTTPException", "Header", "UploadFile", "File", "asyncio", "dm"):
        if need not in s:
            raise SystemExit(f"api.py: «{need}» پیدا نشد؛ چیزی تغییر نکرد")
    if "api_miniapp_status" in s:        # نسخهٔ قبلیِ پچ (بدونِ آپلود) — همان سه endpoint را برداشته و نسخهٔ کامل را می‌گذاریم
        i = s.index("# === PATCH_MINIAPP_DOMAIN_V1 ===")
        s = s[:i].rstrip("\n") + "\n"
    return s.rstrip("\n") + "\n" + API_BLOCK


def main() -> None:
    for name, fn in (("main.py", patch_main), ("api.py", patch_api)):
        p = HOSTING / name
        s = p.read_text()
        new = fn(s)
        ast.parse(new)                       # قبل از نوشتن مطمئن می‌شویم syntax سالم است
        if new != s:
            _backup(p)
            p.write_text(new)
            print(f"{name}: پچ شد")
        else:
            print(f"{name}: قبلاً پچ شده بود")
    print("OK — حالا: systemctl restart botsazvip")


if __name__ == "__main__":
    main()
