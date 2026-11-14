# ثبتِ مینی‌اپِ رباتِ میزبانی‌شده (PATCH_HOSTED_MINIAPP_V1)

مهدی (و هر کاربرِ سرورِ فعال‌سازی) کدِ رباتش را اجرا می‌کند و می‌پرسد «لینکِ مینی‌اپ را کجا ثبت کنم؟».

* هستهٔ کار: `hosted_miniapp.py` — `set_for_app(envs, url, text)`، `set_menu_button`، `clear_menu_button`، `guide_text()`.
* دکمه باید در مینی‌اپِ `/vip` (سرویسِ `/opt/botsazvip`، خارج از این مخزن) گذاشته شود؛ مثلِ `HOSTED_AI_FIX.md` دو endpoint بساز
  (مالکِ اپ را مثلِ بقیه احراز کن):

```python
import sys; sys.path.insert(0, "/opt/botsaz")
import hosted_miniapp as M

@app.get("/api/hosting/{app_id}/miniapp-guide")
async def miniapp_guide(app_id: int, user=Depends(owner_of(app_id))):
    return {"html": M.guide_text()}

@app.post("/api/hosting/{app_id}/miniapp")
async def set_miniapp(app_id: int, body: dict, user=Depends(owner_of(app_id))):
    envs = await load_app_envs(app_id)            # همان جدولِ HostedAppEnv (کلید/مقدار)
    r = await M.set_for_app(envs, body.get("url", ""), body.get("text", "🌐 باز کردن"))
    return {"ok": r.ok, "message": r.message, "bot": r.bot_username}

@app.delete("/api/hosting/{app_id}/miniapp")
async def clear_miniapp(app_id: int, user=Depends(owner_of(app_id))):
    tok = M.find_bot_token(await load_app_envs(app_id))
    r = await M.clear_menu_button(tok) if tok else M.Result(False, "توکن پیدا نشد")
    return {"ok": r.ok, "message": r.message}
```

در صفحهٔ اپ (کنارِ «بازکردنِ ربات»): دکمهٔ «🌐 ثبت مینی‌اپ» ← فیلدِ آدرس (https) + نامِ دکمه ← POST بالا؛ و لینکِ «راهنما» ← GET راهنما.

## راهِ دستی (جوابِ آماده)
BotFather ← `/mybots` ← رباتت ← Bot Settings ← Menu Button ← Configure menu button ← آدرسِ https ← نامِ دکمه.
