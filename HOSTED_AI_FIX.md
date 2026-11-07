# بررسیِ پروژه و اصلاح با هوش مصنوعی (رباتِ میزبانی‌شده)

## مشکلی که حل می‌کند
کاربر کدی می‌گذارد که روی پایتونِ جدید نوشته شده ولی ایمیجِ هاست پایتونِ قدیمی‌تر دارد، مثلاً f-string با نقل‌قولِ هم‌نوعِ تودرتو
(`f"{e('<x a="1">')}"` فقط از ۳٫۱۲ به بعد مجاز است) ⇒ `SyntaxError: f-string: unterminated string` و کانتینر مدام ری‌استارت می‌شود.

## چه چیزی اضافه شد
* `hosted_ai_fix.py` — هسته: `diagnose()` (گزارش)، `plan_fix()` (طرحِ اصلاح)، `apply_fix()` (اعمال).
* دکمه‌هایِ «🔍 بررسیِ هوشمند» و «🛠 اصلاح با هوش مصنوعی» در صفحهٔ هر اپِ میزبانی‌شده در **پنلِ ادمینِ رباتِ اصلی**.
* `scripts/hosted_ai_fix.py` — همان کار از خطِ فرمان.

## پیکربندی (`/opt/botsaz/.env`)
```
HOSTED_FIX_AI_ROUTES=xai=xai-XXXXXXXX        # کلیدِ Grok (xAI). می‌شود: xai:grok-code-fast-1=xai-XXXX
HOSTED_FIX_MODEL=grok-code-fast-1            # اختیاری؛ اگر مدل نبود خودکار از فهرستِ xAI انتخاب می‌شود
VIP_ADMIN_SECRET=...                         # همان که ابزارهایِ ادمینِ میزبانی دارند
VIP_API_BASE=http://127.0.0.1:8002           # پیش‌فرض
```
بعد از تغییرِ `.env` سرویسِ `botsaz-platform` ری‌استارت شود.

## چطور کار می‌کند
1. همهٔ فایل‌هایِ `.py` با **پایتونِ خودِ ایمیجِ اپ** کامپایل می‌شوند (`docker run --rm --network none --entrypoint python IMAGE`)،
   پس همهٔ خطاهایِ نوشتاری یک‌جا دیده می‌شود (نه فقط اولین خطایِ لاگ). لاگ هم برایِ ماژولِ گم‌شده/خطایِ زمانِ اجرا خوانده می‌شود.
2. ماژولِ گم‌شده (فقط نام‌هایِ شناخته‌شده) بدونِ هوش مصنوعی به `requirements.txt` اضافه می‌شود.
3. برایِ خطایِ کد، فقط ±۳۰ خطِ اطرافِ خطا به Grok می‌رود؛ توکن/کلید/رمز با `«SECRET_n»` عوض می‌شود و `.env` هرگز نمی‌رود.
4. جوابِ مدل «ویرایشِ خط‌به‌خط» است؛ فقط داخلِ همان پنجره، حداکثر ۴۰ خط، بدونِ کدِ پرخطرِ تازه (`os.system`، `subprocess`، `eval`، `socket`، …)،
   و باید با پایتونِ ایمیج کامپایل شود (تا ۳ دور). هر نتیجه‌ای که کامپایل نشود نوشته نمی‌شود.
5. پیش‌نمایشِ diff نشان داده می‌شود؛ با «اعمال و ری‌استارت»: پشتیبان در `hosted_backups/<اپ>/<زمان>/` ← نوشتنِ تأییدشده (در صورتِ ناهمخوانی
   برگشتِ همهٔ فایل‌ها) ← ری‌استارت ← خواندنِ لاگ تا معلوم شود بالا آمد.
6. سقف: ۶ اصلاحِ هوش مصنوعی در روز برایِ هر اپ.

## مینی‌اپِ کاربر («بررسی پروژه و سرویس»)
مینی‌اپِ `/vip` در سرویسِ میزبانی (`/opt/botsazvip`) است، نه در این مخزن. برایِ اینکه همان صفحه هم دکمهٔ «اصلاح با هوش مصنوعی» داشته باشد، دو endpoint
در آن سرویس بساز که این‌ها را صدا بزنند (مالکِ اپ را مثلِ بقیهٔ endpointها احراز کن، نه ادمین):

```python
import sys; sys.path.insert(0, "/opt/botsaz")
import hosted_ai_fix as H

client = H.VipClient(base="http://127.0.0.1:8002")      # VIP_ADMIN_SECRET از محیط

@app.post("/api/hosting/{app_id}/diagnose")
async def diagnose(app_id: int, user=Depends(owner_of(app_id))):
    rep, _ = await H.diagnose(client, app_id)
    return {"ok": rep.ok, "fixable": rep.fixable, "text": rep.text(),
            "issues": [i.__dict__ for i in rep.issues]}

@app.post("/api/hosting/{app_id}/ai-fix")
async def ai_fix(app_id: int, user=Depends(owner_of(app_id))):
    rep, texts = await H.diagnose(client, app_id)
    plan = await H.plan_fix(client, app_id, report=rep, texts=texts)
    ok, msgs = await H.apply_fix(client, app_id, plan, rep.name) if plan.patches else (False, plan.notes)
    return {"ok": ok, "messages": msgs, "diff": [p.diff() for p in plan.patches]}
```
یادآوری: با زدنِ دکمه، بخشِ خرابِ کدِ کاربر (بدونِ توکن/کلید) برایِ xAI فرستاده می‌شود؛ در متنِ دکمه/تأیید این را بنویس.

## راهِ سریع برایِ همین الان (روی سرور)
```
cd /opt/botsaz
venv/bin/python scripts/hosted_ai_fix.py --owner <TELEGRAM_ID>            # گزارشِ مشکل
venv/bin/python scripts/hosted_ai_fix.py --owner <TELEGRAM_ID> --fix       # + طرحِ اصلاح (بدونِ نوشتن)
venv/bin/python scripts/hosted_ai_fix.py --owner <TELEGRAM_ID> --fix --apply
```
