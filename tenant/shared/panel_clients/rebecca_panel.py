"""rebecca_panel.py — کلاینت مستقل پنل ربکا (Rebecca).

منبع: https://github.com/rebeccapanel/Rebecca
طبق ری‌پازیتوری رسمی، ربکا مستقیماً «Forked from Marzban» است (نه از خانوادهٔ
PasarGuard/گروه‌محور)؛ یعنی برخلاف پاسارگاد/PasarGuard که به‌جای inbound از
group_ids استفاده می‌کنند، ربکا دقیقاً همان قرارداد API مرزبان را دارد:

  لاگین:            POST  /api/admin/token   (form: username, password)
  گرفتن inboundها:   GET   /api/inbounds       → {protocol: [{tag,...}], ...}
  ساخت/ویرایش/حذف کاربر: POST/PUT/DELETE /api/user...  با inbounds به‌صورت
    دیکشنری پروتکل→[tag,...] (رشته‌ای، نه عددی) — عیناً مثل مرزبان.

=== PATCH_REBECCA_SETSERVICE_V1 ===
این فرض (که ربکا همیشه دقیقاً مثل مرزبانِ پایه رفتار می‌کند) دیگر کاملاً
درست نیست: از نسخهٔ ۰.۲.۰ به بعد، پنلِ ربکا مفهومِ «سرویس» را جایگزینِ
انتخابِ دستیِ inbound کرده و POST /api/user دیگر تگِ inboundِ واقعی قبول
نمی‌کند؛ به‌جایش باید یک تگِ ویژهٔ سازگاری («setservice-<id>») فرستاده شود.
چون این تگِ ویژه در لیستِ inboundهای واقعیِ پنل (GET /api/inbounds) وجود
ندارد، منطقِ فیلترکردنِ MarzbanPanelClient (که تگ‌ها را با inboundهای واقعی
مقایسه می‌کند) آن را حذف می‌کرد و درخواست با inbounds خالی می‌رفت. اینجا این
مسیر override شده تا اگر ادمین تگِ `setservice-<id>` را در تنظیماتِ اینباند
وارد کرده باشد، این کراس‌رفرنس رد شود و تگ مستقیماً فرستاده شود.

⚠️ نکته: چون مستندِ رسمیِ دقیقِ این API جدید (v0.2.0+) در دسترس نبود، این
پیاده‌سازی صرفاً بر اساسِ متنِ خودِ پیامِ خطا نوشته شده و **حتماً باید روی
پنلِ واقعی تست شود**؛ اگر بازم خطا داد، متنِ خطای جدید را بفرستید تا دقیق‌تر
تنظیم شود.

=== PATCH_REBECCA_EXPIRE_ISO_V1 ===
از لاگ خطا (error.log) این مورد پیدا شد که علتِ واقعیِ ارورِ «ساخت کانفیگ»
و کرش‌های مکررِ get_usage است:

    File "tenant/marzban_panel.py", line 416, in get_usage
        expiry_ms = int(expire) * 1000 if expire else 0
    ValueError: invalid literal for int() with base 10: '2026-08-04T12:36:29Z'

یعنی نسخهٔ فعلیِ پنلِ ربکا (بک‌اندِ Go) دیگر فیلدِ expire را به‌صورتِ عددِ
Unix timestamp برنمی‌گرداند/نمی‌پذیرد، بلکه رشتهٔ تاریخِ ISO-8601 با پسوندِ
Z (مثلاً "2026-08-04T12:36:29Z") است — دقیقاً مثلِ همان تغییرِ نوعِ
inbound→setservice، این هم یک شکستِ سازگاریِ دیگر با مرزبانِ پایه است.
چون MarzbanPanelClient در همه‌جا `int(expire)` و ارسالِ expire به‌صورتِ عددِ
خام انجام می‌دهد، هم موقعِ خواندنِ مصرف (ValueError بالا) و هم موقعِ
POST /api/user برای ساختِ کاربر (که احتمالاً با همان قالبِ عددی رد می‌شود و
همان چیزی است که کاربر «ارور موقع ساخت کانفیگ» می‌بیند) شکست می‌خورد.

راه‌حل: بدونِ دست‌زدن به marzban_panel.py (که باید برایِ پنل‌هایِ واقعیِ
مرزبان/پاسارگاد/PasarGuard دست‌نخورده بماند)، اینجا expire به هر دو جهت
(خواندن: عدد یا ISO ← epoch ثانیه، نوشتن: epoch ثانیه ← ISO) تبدیل می‌شود.
اگر بازم خطا داد (مثلاً فرمتِ ISO دقیقاً همین نیست، یا میلی‌ثانیه می‌خواهد
نه ثانیه)، متنِ خطایِ جدید را بفرستید.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient, PanelError, _DEFAULT_PROXIES, GB, _safe_username
from tenant.shared.panel_clients.marzban_panel import gen_uuid, gen_sub_id

from logging_setup import get_logger as _get_logger
logger = _get_logger(__name__)

__all__ = ["RebeccaPanelClient"]


def _expire_to_epoch(expire) -> int:
    """expire را (چه عددِ Unix timestamp قدیمی، چه رشتهٔ ISO-8601 جدیدِ
    ربکا مثلِ "2026-08-04T12:36:29Z") به epoch ثانیه تبدیل می‌کند.
    ورودیِ خالی/صفر/نامعتبر → 0 (بی‌انقضا/نامشخص)."""
    if expire in (None, 0, "0", ""):
        return 0
    if isinstance(expire, (int, float)):
        return int(expire)
    s = str(expire).strip()
    if not s:
        return 0
    if s.lstrip("-").replace(".", "", 1).isdigit():
        return int(float(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _epoch_to_iso(epoch) -> str | None:
    """epoch ثانیه را به قالبِ ISO-8601 با پسوندِ Z تبدیل می‌کند (همان چیزی
    که GET /api/user از ربکا برمی‌گرداند)؛ برایِ بی‌انقضا None برمی‌گرداند
    تا فیلدِ expire اصلاً در بدنهٔ درخواست فرستاده نشود."""
    epoch = int(epoch or 0)
    if not epoch:
        return None
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class RebeccaPanelClient(MarzbanPanelClient):
    """کلاینتِ اختصاصیِ پنل ربکا — برای اکثرِ نسخه‌ها دقیقاً رفتار مرزبان پایه
    را دارد، به‌جز حالتِ setservice و قالبِ expire (نسخهٔ ۰.۲.۰+، پایینِ
    همین فایل)."""

    async def _inbounds_by_protocol(self, tags: list[str]) -> dict:
        # === PATCH_REBECCA_SETSERVICE_V1 ===
        setservice_tags = [t for t in (tags or []) if str(t).startswith("setservice-")]
        if setservice_tags:
            # این تگ‌ها در inboundهای واقعیِ پنل وجود ندارند (مفهومِ جدیدِ
            # «سرویس» است)؛ نباید فیلتر بشن — مستقیم زیرِ همهٔ پروتکل‌های
            # پیش‌فرض قرار می‌گیرن تا هرکدوم که سرور واقعاً بهش نیاز داره،
            # پیدا بشه.
            return {proto: list(setservice_tags) for proto in _DEFAULT_PROXIES}
        return await super()._inbounds_by_protocol(tags)

    # === PATCH_REBECCA_V2_EXPIRE_INT_V1 ===
    async def _modify(self, email, **fields):
        """هر PUT به /api/user/{username} از این متد رد می‌شود (renew,
        set_enabled و ...)؛ اگر expire در fields باشد (epoch ثانیه، طبق
        قراردادِ داخلیِ کلاسِ پایه)، اینجا به عددِ صحیحِ Unix timestamp
        (همون فرمتی که POST /api/v2/users هم تأیید شد که واقعاً قبول
        می‌شود) نرمال می‌شود — نه ISO-8601 (که قبلاً به‌اشتباه فرض شده
        بود و باعثِ خطای «invalid expire» می‌شد)."""
        if "expire" in fields:
            fields = dict(fields)
            fields["expire"] = int(fields["expire"]) if fields["expire"] else None
        return await super()._modify(email, **fields)

    # === PATCH_REBECCA_V2_SERVICE_API_V1 ===
    # از یه HAR واقعیِ گرفته‌شده از پنلِ خودِ کاربر (panel.lornanet.ir) این
    # مشخص شد: ساختِ کاربر دیگه از /api/user (v1، با proxies/inbounds)
    # استفاده نمی‌کنه؛ یه Endpoint کاملاً جدید داره:
    #     POST /api/v2/users
    #     بدنه: {username, service_id, note, telegram_id, contact_number,
    #            status, expire, data_limit, ip_limit,
    #            data_limit_reset_strategy, on_hold_expire_duration, flow}
    # یعنی مفهومِ inbound/proxy کاملاً با یه service_id (عددِ ساده، از
    # GET /api/v2/services) جایگزین شده — نه یه تگِ ویژهٔ سازگاری روی
    # inbounds مثلِ چیزی که قبلاً حدس زده بودیم. خوندن (GET /api/user/{u}،
    # GET /api/users) طبقِ همون HAR هنوز v1 و بدونِ تغییره، پس دست‌نخورده
    # می‌مونن؛ فقط ساختِ کاربر به v2 منتقل شد.
    def _parse_service_id(self, tags) -> int:
        """service_id رو از تگِ inbound که ادمین وارد کرده استخراج می‌کنه.
        فرمت‌های پذیرفته‌شده: «setservice-1»، «service-1»، یا فقط «1»."""
        for t in (tags or []):
            s = str(t).strip()
            for prefix in ("setservice-", "service-"):
                if s.startswith(prefix):
                    tail = s[len(prefix):]
                    if tail.isdigit():
                        return int(tail)
            if s.isdigit():
                return int(s)
        return 1  # پیش‌فرض: اولین/تنها سرویسِ رایج (طبقِ نمونهٔ واقعی: id=1)

    # === PATCH_REBECCA_V2_NO_INBOUNDS_V1 ===
    async def _default_service_id(self) -> int:
        """اگه ادمین تگی تنظیم نکرده باشه، اولین سرویسِ پنل رو از
        GET /api/v2/services می‌گیره (شکلِ پاسخ، تأییدشده از HAR واقعی:
        {"services":[{"id":1,...}], "total":1}). اگه در دسترس نبود، 1."""
        try:
            res = await self._request("GET", "/api/v2/services",
                                       params={"limit": 1000, "offset": 0}) or {}
            services = res.get("services") if isinstance(res, dict) else None
            if services:
                return int(services[0].get("id") or 1)
        except Exception:
            logger.exception("خواندنِ فهرستِ سرویس‌هایِ پنل ناموفق بود")
        return 1

    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                              limit_ip=0, inbound_ids=None, mb=None, hours=0,
                              on_hold=False):
        username = _safe_username(email)
        # === PATCH_REBECCA_V2_NO_INBOUNDS_V1 ===
        # قبلاً وقتی تگی تنظیم نشده بود، active_inbound_ids() صدا زده می‌شد
        # که GET /api/inbounds (مسیرِ v1 مرزبان) رو می‌زد — این نسخهٔ ربکا
        # اصلاً اون endpoint رو نداره (404 → None → «هیچ inbound فعالی روی
        # پنل مرزبان پیدا نشد») و ساختِ کانفیگ همیشه شکست می‌خورد.
        # الان: اگه ادمین تگ/سرویس تنظیم کرده از همون، وگرنه اولین سرویس از
        # /api/v2/services (که این پنل واقعاً داره) استفاده می‌شه.
        tags = inbound_ids if inbound_ids is not None else list(self.inbound_tags or [])
        if tags:
            service_id = self._parse_service_id(tags)
        else:
            service_id = await self._default_service_id()
            tags = [f"service-{service_id}"]

        if mb is not None:
            data_limit = int(mb) * 1024 * 1024
        else:
            data_limit = int(gb) * GB if gb else 0

        import time as _time
        if days:
            expire_epoch = int(_time.time()) + int(days) * 86400
        else:
            expire_epoch = 0

        body = {
            "username": username,
            "service_id": service_id,
            "note": "",
            "telegram_id": "",
            "contact_number": "",
            "status": "active",
            # === PATCH_REBECCA_V2_EXPIRE_INT_V1 ===
            # از یه HAR واقعیِ *موفق* (201) این تأیید شد که این Endpoint
            # (POST /api/v2/users) برای expire یه عددِ صحیحِ Unix timestamp
            # ساده می‌خواد (مثلاً 1784709000)، نه رشتهٔ ISO-8601 که قبلاً
            # (بر اساسِ یه فرضِ نادرست) می‌فرستادیم و باعثِ خطای
            # «400 invalid expire» می‌شد.
            "expire": expire_epoch if expire_epoch else None,
            "data_limit": data_limit,
            "ip_limit": int(limit_ip or 0),
            "data_limit_reset_strategy": "no_reset",
            "on_hold_expire_duration": None,
            "flow": None,
        }
        # === PATCH_TRIAL_ON_HOLD_V1 ===
        # شمارشِ مدت از اولین اتصال (سرویسِ تست). این پنل فیلدش را از قبل
        # داشت و فقط همیشه None می‌فرستاد.
        _secs = int(days) * 86400 + int(hours) * 3600
        if on_hold and _secs:
            body["status"] = "on_hold"
            body["on_hold_expire_duration"] = _secs
            body["expire"] = None
        res = await self._request("POST", "/api/v2/users", json=body)
        if res is None:
            raise PanelError("ساخت کانفیگ ناموفق: کاربری با این نام از قبل وجود دارد.")
        sub_url = res.get("key_subscription_url") or res.get("subscription_url") or ""
        return {
            "uuid": uuid_str or gen_uuid(), "sub_id": sub_id or gen_sub_id(),
            "email": username, "gb": gb, "days": days,
            "inbounds": tags, "sub_link": self._abs_sub_link(sub_url),
            "expiry_ms": expire_epoch * 1000 if expire_epoch else 0,
        }

    async def get_usage(self, email):
        """مثلِ get_usage مرزبانِ پایه، با این تفاوت که expire ممکن است
        ISO-8601 باشد (نه فقط عدد) — از _expire_to_epoch استفاده می‌شود تا
        دیگر ValueError نده."""
        cur = await self.get_client(email)
        if not cur:
            return None
        used = int(cur.get("used_traffic", 0) or 0)
        cap = int(cur.get("data_limit", 0) or 0)
        expiry_epoch = _expire_to_epoch(cur.get("expire"))
        result = {
            "up": 0, "down": used,
            "total": cap, "expiryTime": expiry_epoch * 1000 if expiry_epoch else 0,
            "enable": cur.get("status") in ("active", "on_hold"),   # PATCH_ONHOLD_DISPLAY_V1
        }
        if cur.get("status") == "on_hold":   # PATCH_ONHOLD_DISPLAY_V1
            try:
                result["on_hold_seconds"] = int(cur.get("on_hold_expire_duration") or 0)
            except (TypeError, ValueError):
                pass
        # اگه این نصبِ ربکا هنوز این فیلدهای مرزبانی رو داشته باشه (بعضی
        # فورک‌ها حفظشون می‌کنن)، همون‌طور که هستن نشون داده می‌شن؛ اگه
        # نداشته باشه، cur.get() فقط None برمی‌گردونه و چیزی اضافه نمی‌شه —
        # پس نمایشِ نادرست/جعلی امکان نداره.
        online_at = cur.get("online_at")
        if online_at:
            ms = _expire_to_epoch(online_at) * 1000
            if ms:
                result["last_online_ms"] = ms
        ua = cur.get("sub_last_user_agent")
        if ua and str(ua).strip():
            result["client_app"] = str(ua).strip()
        return result

    async def renew(self, email, days, reset_usage=False):
        """مثلِ renew مرزبانِ پایه، فقط با خواندنِ expireِ فعلی از طریقِ
        _expire_to_epoch (به‌جای int خام) تا اگر تاریخِ فعلی ISO باشد کرش
        نکند؛ نوشتنِ expireِ جدید از طریقِ _modify اورراید‌شده بالا خودکار
        به ISO تبدیل می‌شود."""
        cur = await self.get_client(email)
        if not cur:
            raise PanelError("کاربر روی پنل ربکا پیدا نشد.")
        import time as _time
        cur_expire = _expire_to_epoch(cur.get("expire"))
        # === PATCH_RENEW_UNLIMITED_TIME_GUARD_V1 ===
        # مثلِ گاردِ اضافه‌شده به renew مرزبان: اگه از قبل زمان نامحدود
        # بود (expire خالی → cur_expire==0)، تمدید را بی‌خطر رد می‌کنیم.
        if not cur.get("expire") and cur_expire == 0:
            return cur
        now = int(_time.time())
        base = cur_expire if cur_expire > now else now
        new_expire = base + int(days) * 86400
        obj = await self._modify(email, expire=new_expire, status="active")
        if reset_usage:
            await self.reset_usage(email)
        return obj

    async def list_clients(self):
        """مثلِ list_clients مرزبانِ پایه، با خواندنِ expire از طریقِ
        _expire_to_epoch به‌جایِ int خام."""
        res = await self._request("GET", "/api/users", params={"limit": 1000}) or {}
        users = res.get("users", []) if isinstance(res, dict) else []
        out = []
        for u in users:
            used = int(u.get("used_traffic", 0) or 0)
            cap = int(u.get("data_limit", 0) or 0)
            expiry_epoch = _expire_to_epoch(u.get("expire"))
            out.append({
                "email": u.get("username"),
                "traffic": {"up": 0, "down": used, "total": cap},
                "totalGB": cap,
                "expiryTime": expiry_epoch * 1000 if expiry_epoch else 0,
            })
        return out
