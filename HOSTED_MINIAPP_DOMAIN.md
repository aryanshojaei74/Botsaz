# ساب‌دامینِ مینی‌اپ برایِ اپ‌هایِ میزبانی‌شده (PATCH_MINIAPP_DOMAIN_V1)

هر اپِ «سرور فعال‌سازی» با اشتراکِ ماهانهٔ جدا (پیش‌فرض ۱۰۰٬۰۰۰ تومان؛ از پنلِ مدیر قابلِ تغییر) یک ساب‌دامینِ ثابت می‌گیرد:
`https://m1a2b3c4d.supraplus.ir` → کانتینرِ همان اپ. بعدش Menu Button رباتِ کاربر با یک دکمه به همین آدرس وصل می‌شود.

## زیرساخت (انجام‌شده)
Cloudflare `*` (Proxied) + Origin Cert → nginx `stream` (SNI) → `127.0.0.1:8443` → بلوکِ `*.supraplus.ir` در
`/etc/nginx/conf.d/supraplus-miniapps.conf` که `map $host $miniapp_up` را از فایل می‌خواند.

## ۱) تغییرِ nginx: یک فایل به‌ازایِ هر اپ
```bash
mkdir -p /etc/nginx/miniapp-hosts.d
rm -f /etc/nginx/miniapp-hosts.map.bak && cp /etc/nginx/miniapp-hosts.map /etc/nginx/miniapp-hosts.map.bak 2>/dev/null
sed -i 's#include /etc/nginx/miniapp-hosts.map;#include /etc/nginx/miniapp-hosts.d/*.map;#' /etc/nginx/conf.d/supraplus-miniapps.conf
nginx -t && systemctl reload nginx
```

## ۲) سمتِ ربات (این مخزن — بیلد ۴۸۲)
* `hosted_miniapp_domain.py`: خرید/تمدید (کسر از کیف‌پولِ مالک)، فعال‌سازیِ رایگان، غیرفعال، ثبتِ Menu Button و `sync(dm)`.
* `models.HostedApp`: ستون‌هایِ `miniapp_sub` و `miniapp_until` (خودکار به دیتابیس اضافه می‌شوند).
* پنلِ مدیر: «⚡️ سرور فعال‌سازی ← 🌐 قیمتِ ماهانهٔ مینی‌اپ»، و در صفحهٔ هر اپ: فعال‌سازیِ ۳۰ روزهٔ رایگان / غیرفعال / ثبتِ Menu Button.

## ۳) سمتِ سرویسِ botsazvip (`/opt/botsazvip/hosting/` — خارج از این مخزن)
هر دو تکه به‌صورتِ افزودنی‌اند و کدِ قبلی را عوض نمی‌کنند.

### الف) حلقهٔ همگام‌سازی (`main.py`)
IPِ کانتینر با هر ری‌استارت عوض می‌شود؛ این حلقه هر ۶۰ ثانیه نگاشت‌ها را تازه و اشتراکِ تمام‌شده را برمی‌دارد:
```python
import sys, asyncio, logging
sys.path.insert(0, "/opt/botsaz")
import hosted_miniapp_domain as MD
from hosting import docker_manager as dm        # همان import که api.py برایِ dm دارد

async def _miniapp_sync_loop():
    while True:
        try:
            await MD.sync(dm)
        except Exception:
            logging.getLogger(__name__).exception("miniapp sync")
        await asyncio.sleep(60)

# داخلِ startup (همان‌جا که سایر taskها ساخته می‌شوند):
asyncio.create_task(_miniapp_sync_loop())
```

### ب) endpointهایِ کاربر (`api.py`، کنارِ بقیهٔ routeهایِ `hosting_router`)
```python
import hosted_miniapp_domain as MD

@hosting_router.get("/api/hosting/apps/{app_id}/miniapp")
async def api_miniapp_status(app_id: int, x_init_data: str | None = Header(default=None),
                             x_kb_token: str | None = Header(default=None)):
    u = await _auth(x_init_data, x_kb_token)
    app = await _owned_app(u, app_id)
    return JSONResponse({"ok": True, "price": await MD.get_price(), "active": MD.is_active(app),
                         "url": MD.url_of(app.miniapp_sub) if app.miniapp_sub else "",
                         "until": app.miniapp_until.isoformat() if app.miniapp_until else None})

@hosting_router.post("/api/hosting/apps/{app_id}/miniapp/buy")
async def api_miniapp_buy(app_id: int, x_init_data: str | None = Header(default=None),
                          x_kb_token: str | None = Header(default=None)):
    u = await _auth(x_init_data, x_kb_token)
    app = await _owned_app(u, app_id)
    r = await MD.buy(app.id)
    if not r.ok:
        raise HTTPException(status_code=400, detail=r.message)
    await MD.sync(dm)                      # همین الان فعال شود، نه تا ۶۰ ثانیهٔ بعد
    return JSONResponse({"ok": True, "url": r.url, "until": r.until.isoformat()})

@hosting_router.post("/api/hosting/apps/{app_id}/miniapp/menu-button")
async def api_miniapp_menu(app_id: int, body: dict, x_init_data: str | None = Header(default=None),
                           x_kb_token: str | None = Header(default=None)):
    u = await _auth(x_init_data, x_kb_token)
    app = await _owned_app(u, app_id)
    r = await MD.set_menu_button(app.id, (body or {}).get("text") or "🌐 باز کردن")
    return JSONResponse({"ok": r.ok, "message": r.message})
```
دکمهٔ «🌐 فعال‌سازی مینی‌اپ (۱۰۰٬۰۰۰ تومان/ماه)» و «🔘 ثبت در Menu Button» در صفحهٔ اپِ مینی‌اپِ `/vip` (ui.py) به این سه endpoint وصل می‌شود.

## نکته‌هایِ عملیاتی
* ساب‌دامین تصادفی است (نامِ دلخواه نیست) تا اسم‌هایِ فریبنده مثلِ `bank-login` ممکن نباشد.
* اشتراکِ تمام‌شده ⇒ حداکثر ~۶۰ ثانیه بعد نگاشت برداشته و آدرس ۴۰۴ می‌شود؛ ساب‌دامین برایِ تمدیدِ بعدی رزرو می‌ماند.
* پورتِ برنامه خودکار تشخیص داده می‌شود (مثلِ مسیرِ `/u/<name>/`)؛ کاربر لازم نیست `PORT` بگیرد.
* `MINIAPP_URL` به متغیرهایِ اپ اضافه می‌شود (بعد از ری‌استارتِ اپ در کدِ کاربر در دسترس است).
* سوءاستفاده: مدیر با «⛔️ غیرفعال» در صفحهٔ اپ همان لحظه آدرس را می‌بندد.


## ۴) نصبِ خودکارِ سمتِ سرویسِ botsazvip (بیلد ۴۸۶)
به‌جایِ ویرایشِ دستیِ `main.py`/`api.py`، فقط این را اجرا کن (پشتیبان `*.bak_miniapp` می‌سازد و idempotent است):
```bash
python3 /opt/botsaz/deploy/miniapp_vip_patch.py && systemctl restart botsazvip
```
افزوده‌ها: حلقهٔ همگام‌سازی، middlewareِ سرو کردنِ مینی‌اپ‌هایِ استاتیک، و endpointهایِ
`GET /miniapp` · `POST /miniapp/buy` · `POST /miniapp/menu-button` · `POST /miniapp/upload` (zip) · `DELETE /miniapp/static`.

### دو حالتِ مینی‌اپ
* **داخلِ پروژه:** وب‌سرورِ خودِ کاربر در کانتینرش؛ پورت خودکار تشخیص داده می‌شود.
* **استاتیک:** zipِ HTML/JS (`index.html` در ریشه یا داخلِ یک پوشهٔ ریشه). بدونِ کانتینر از `/opt/botsazvip/miniapps/<اپ>/` سرو می‌شود؛
  سقفِ zip ۲۰MB، بازشده ۶۰MB، ۲۰۰۰ فایل؛ فایل‌هایِ مخفی (`.env`, `.git`)، `node_modules`، مسیرِ `..` و symlink رد می‌شوند.
  مسیرهایِ بدونِ پسوند به `index.html` می‌روند (SPA).
