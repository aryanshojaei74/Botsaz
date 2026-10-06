"""pasargad_panel.py — کلاینت مستقل پنل پاسارگاد (PasarGuard).

PasarGuard فورک مرزبان است؛ احراز هویت و مدیریت کاربر تقریباً یکسان‌اند،
بنابراین این کلاینت از MarzbanPanelClient ارث می‌برد و فقط بخش‌هایی را که
واقعاً فرق دارند override می‌کند:

  • ساخت کاربر: PasarGuard به‌جای inbounds/proxies از «group_ids» استفاده می‌کند
    (POST /api/user با group_ids).
  • لیست «گروه‌ها»: GET /api/group → برای تست اتصال و انتخاب گروه‌های پیش‌فرض.

نقاط مشترک با مرزبان (ارثی، بدون تغییر):
  لاگین:   POST /api/admin/token   (form: username, password)
  کاربر:   GET/PUT/DELETE /api/user/{username} ، ریست مصرف، revoke_sub ، لینک ساب.

نکتهٔ تنظیمِ مهم برای فروشنده:
  در پنل پاسارگاد، مقداری که در فیلد «آی‌دی اینباندها» وارد می‌کنی در واقع
  **آی‌دی گروه‌ها**ست (اعداد، مثل "1,2"). اگر خالی بگذاری، همهٔ گروه‌های
  موجود روی پنل استفاده می‌شوند.
"""
from __future__ import annotations

import re
import time

_GROUPS_CACHE: dict = {}   # PATCH_GROUPS_TTL_CACHE_V1
# === PATCH_PASARGUARD_HWID_CAP_V1 ===
# سقفِ hwid_limit که خودِ پنل اعلام کرده (از پیامِ خطا یاد گرفته می‌شود): (base_url, username) → عدد
_HWID_MAX: dict = {}
_HWID_CAP_RE = re.compile(r"HWID limit cannot exceed (\d+)|HWID limit[^\"]*?maximum is (\d+)", re.IGNORECASE)


def _hwid_cap_from_error(msg: str) -> int | None:
    m = _HWID_CAP_RE.search(msg or "")
    if not m:
        return None
    try:
        n = int(m.group(1) or m.group(2))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None
_GROUPS_TTL = 180.0   # PATCH_GROUPS_SIMPLE_FIRST_V1: گروه‌ها به‌ندرت عوض می‌شوند

from tenant.shared.panel_clients.marzban_panel import (
    MarzbanPanelClient, PanelError, _safe_username, gen_sub_id, gen_uuid, GB,
)

__all__ = ["PasarGuardPanelClient"]


class PasarGuardPanelClient(MarzbanPanelClient):
    """کلاینت پنل پاسارگاد (PasarGuard) — مبتنی بر گروه (group_ids)."""

    def __init__(self, base_url, username, password, sub_base="",
                 inbound_ids=None, verify_ssl=False, timeout=8, api_key=None):
        # === PATCH_PANEL_TIMEOUT_REDUCE_V1 ===
        # نگاه کن به توضیحِ کاملِ همین پچ در tenant/hiddify_panel.py.
        super().__init__(base_url, username, password, sub_base=sub_base,
                         inbound_ids=inbound_ids, verify_ssl=verify_ssl, timeout=timeout,
                         api_key=api_key)
        # === PATCH_STRIP_DASHBOARD_PATH_V1 ===
        # حذفِ مسیرِ داشبورد از انتهای base_url حالا توی خودِ MarzbanPanelClient
        # (کلاسِ پایه) انجام می‌شود — برای هر چهار پنلِ این خانواده یک‌جا،
        # نه فقط اینجا. دیگر نیازی به تکرارش در این فایل نیست.
        # در پاسارگاد، «اینباندها»ی ذخیره‌شده در واقع آی‌دی گروه‌ها (int) هستند.
        self.group_ids: list[int] = []
        for t in (self.inbound_tags or []):
            s = str(t).strip()
            if s.isdigit():
                self.group_ids.append(int(s))

    # ───────── گروه‌ها ─────────
    async def list_groups(self, *, prefer_full: bool = False) -> list[dict]:
        """فهرست گروه‌های پنل: GET /api/groups → {groups: [...], total}.
        (مسیر جمع «groups»؛ مسیر مفردِ /api/group فقط برای ساختِ گروه است.)"""
        # === PATCH_PASARGUARD_GROUPS_RBAC_V1 ===
        # از نسخهٔ ۵ به بعد پاسارگاد RBAC دارد: ادمینِ «اپراتور» (غیرِ sudo)
        # دیگر به /api/groups دسترسی ندارد و باید از نسخهٔ سبکِ
        # /api/groups/simple استفاده کند (فقط id و name برمی‌گرداند — که برای
        # ما کافی است). قبلاً فقط مسیر اول صدا زده می‌شد و برای فروشنده‌هایی
        # که با ادمینِ اپراتور وصل شده بودند، ۴۰۳ می‌گرفت و ساختِ کانفیگ
        # شکست می‌خورد.
        # === PATCH_PASARGAD_GROUPS_CF403_V1 ===
        # علتِ خالی‌بودنِ لیست را ثبت می‌کنیم (self._groups_diag) تا ربات به‌جایِ
        # «inbound پیدا نشد»ِ گمراه‌کننده، دلیلِ واقعی را نشان دهد.
        # === PATCH_GROUPS_TTL_CACHE_V1 ===
        # لاگ: ۲۳۰ بارِ «GET /api/groups» کند. هر ساختِ کانفیگ دو بار لیست را می‌خواند
        # (یک‌بار برایِ پیدا کردنِ گروه‌ها، یک‌بار برایِ اعتبارسنجی). نتیجهٔ موفق ۶۰ ثانیه
        # نگه داشته می‌شود تا ساخت/تمدیدِ پشت‌سرهم هر بار منتظرِ پنل نماند.
        _ck = (self.base_url, self.username)
        _hit = _GROUPS_CACHE.get(_ck)
        if not prefer_full and _hit and time.monotonic() - _hit[0] < _GROUPS_TTL:
            self._groups_diag = ""
            return list(_hit[1])
        self._groups_diag = ""
        _notes = []
        # === PATCH_GROUPS_SIMPLE_FIRST_V1 ===
        # اندازه‌گیریِ زنده (probe): روی پنلِ ادمینِ «اپراتور» مسیرِ کامل /api/groups همیشه ۴۰۳ می‌داد و
        # _request روی ۴۰۳ توکن را باطل و لاگینِ اجباری می‌زند (با محدودکنندهٔ ۱۲ثانیه‌ایِ لاگین ⇒ تا ۱۲ثانیه
        # معطلی در هر ساخت)؛ روی پنلِ sudo هم /api/groups هر بار ≈۷ثانیه بود، درحالی‌که /api/groups/simple
        # (فقط id و name — همان چیزی که لازم داریم) ≈۰٫۰۵ثانیه. پس اول «simple»، و فقط اگر نبود/خالی بود مسیرِ کامل.
        _paths = ("/api/groups", "/api/groups/simple") if prefer_full else ("/api/groups/simple", "/api/groups")
        for path in _paths:
            try:
                res = await self._request("GET", path)
            except PanelError as e:
                if "HTTP 403" in str(e) or "HTTP 401" in str(e):
                    _notes.append(f"{path}: دسترسی رد شد (ادمین sudo نیست؟)")
                    continue
                raise
            if res is None:
                _notes.append(f"{path}: ۴۰۴ (این مسیر روی این نسخه از پنل نیست)")
                continue
            items = []
            if isinstance(res, dict):
                for _k in ("groups", "items", "data", "results"):
                    if isinstance(res.get(_k), list):
                        items = res[_k]
                        break
            elif isinstance(res, list):
                items = res
            if items:
                _GROUPS_CACHE[_ck] = (time.monotonic(), list(items))
                return items
            _notes.append(f"{path}: پاسخ آمد ولی لیستِ گروه‌ها خالی بود")
        self._groups_diag = " | ".join(_notes)
        return []

    async def active_group_ids(self) -> list[int]:
        """آی‌دی گروه‌های هدف؛ اگر فروشنده گروهی مشخص نکرده باشد، همهٔ گروه‌ها."""
        if self.group_ids:
            return list(self.group_ids)
        groups = await self.list_groups()
        ids = [g.get("id") for g in groups
               if isinstance(g, dict) and g.get("id") is not None]
        if not ids:
            raise PanelError("هیچ گروهی روی پنل پاسارگاد پیدا نشد. اول یک گروه بساز.")
        return ids

    # برای سازگاری با test_connection و کدهای مشترک (که active_inbound_ids صدا می‌زنند)
    async def active_inbound_ids(self):
        return await self.active_group_ids()

    async def list_inbounds(self):
        """نمایش گروه‌ها به‌شکل لیست تخت برای UI تست اتصال."""
        groups = await self.list_groups()
        return [{"id": g.get("id"), "tag": g.get("name"), "remark": g.get("name"),
                 "protocol": "group", "enable": True}
                for g in groups if isinstance(g, dict)]

    async def _post_user(self, path: str, body: dict):
        """POST ساختِ کاربر. اگر پنل بگوید «HWID limit cannot exceed N» (یا «maximum is N»)، یک‌بار با سقفِ خودِ پنل
        دوباره می‌فرستد (کاربر هنوز ساخته نشده، پس تکرار بی‌خطر است) و سقف را برایِ دفعاتِ بعد یادش می‌ماند.
        === PATCH_PASARGUARD_HWID_CAP_V1 ==="""
        ck = (self.base_url, self.username)
        cap = _HWID_MAX.get(ck)
        if cap and int(body.get("hwid_limit") or 0) > cap:
            body["hwid_limit"] = cap
        try:
            return await self._request("POST", path, json=body)
        except PanelError as e:
            mx = _hwid_cap_from_error(str(e))
            if mx and int(body.get("hwid_limit") or 0) > mx:
                _HWID_MAX[ck] = mx
                body["hwid_limit"] = mx
                return await self._request("POST", path, json=body)
            raise

    # ───────── ساخت کاربر ─────────
    async def create_account(self, email, gb, days, *, uuid_str=None, sub_id=None,
                             limit_ip=0, inbound_ids=None, mb=None, hours=0,
                             on_hold=False):
        """ساخت کاربر در پاسارگاد با همان امضای create_account سایر پنل‌ها.

        تفاوت با مرزبان: به‌جای inbounds/proxies، فهرست group_ids ارسال می‌شود.
        days>0 → expire به‌صورت UTC timestamp مطلق (ثانیه)؛ days=0 → بدون انقضا.
        === PATCH_PASARGUARD_HWID_LIMIT_V1 ===
        limit_ip اینجا به‌عنوانِ hwid_limit (محدودیتِ تعدادِ دستگاه) فرستاده
        می‌شود؛ قبلاً نادیده گرفته می‌شد که باعثِ ردِ درخواست توسطِ پنل‌هایی
        می‌شد که hwidِ نامحدود را قبول نمی‌کنند.

        === PATCH_TRIAL_HOURLY_V1 ===
        hours: مدتِ اضافه به ساعت (روی days). expire به‌صورت ثانیه‌ای محاسبه
        می‌شه، پس دقتِ ساعتی/دقیقه‌ای هم کاملاً پشتیبانی می‌شه. مثال:
        days=0, hours=2 → تستِ ۲ ساعته. days=1, hours=12 → ۱ روز و ۱۲ ساعت.
        """
        username = _safe_username(email)
        if inbound_ids is not None:
            group_ids = [int(x) for x in inbound_ids if str(x).strip().isdigit()]
        else:
            group_ids = await self.active_group_ids()

        # === PATCH_PASARGUARD_VALIDATE_GROUP_IDS_V1 ===
        # قبل از POST /api/user، group_ids را در برابر گروه‌های واقعیِ پنل
        # چک کن. اگر یکی از آی‌دی‌ها روی پنل وجود نداشته باشد، پاسارگارد به‌جای
        # خطای روشن، HTTP 404 با body «Group not found» برمی‌گرداند — که چون
        # _request روی هر 404 فقط None برمی‌گرداند (بدون نگه‌داشتنِ body)،
        # قبلاً به‌اشتباه به‌عنوانِ مشکلِ RBAC/WAF تفسیر می‌شد (دیده‌شده در
        # panel.id=391 / bot=@mcteam_vpn_bot: inbound_ids دیتابیس شاملِ گروهِ
        # ۲ بود که روی خودِ پنل حذف شده بود). با این چک، خطا زودتر و با
        # پیامِ دقیق (شاملِ همان آی‌دیِ نامعتبر) نشان داده می‌شود.
        try:
            real_groups = await self.list_groups()
            real_ids = {g.get("id") for g in real_groups if isinstance(g, dict)}
            if group_ids and any(gid not in real_ids for gid in group_ids):
                # PATCH_GROUPS_SIMPLE_FIRST_V1: لیستِ سبک ممکن است صفحه‌بندی/کوتاه باشد؛ قبل از رد کردنِ ساخت،
                # یک‌بار با مسیرِ کامل (بدونِ کش) راستی‌آزمایی کن.
                _full = await self.list_groups(prefer_full=True)
                real_ids = {g.get("id") for g in _full if isinstance(g, dict)} or real_ids
        except PanelError:
            real_ids = None  # اگر خودِ لیستِ گروه‌ها هم شکست خورد، این چک را رد کن

        if real_ids:
            invalid_ids = [gid for gid in group_ids if gid not in real_ids]
            if invalid_ids:
                raise PanelError(
                    "ساخت کانفیگ در پاسارگاد ناموفق بود: گروه(های) "
                    f"{invalid_ids} در تنظیماتِ این پنل ثبت شده‌اند اما روی خودِ "
                    f"پنل وجود ندارند (گروه‌های معتبرِ فعلی: {sorted(real_ids)}). "
                    "لطفاً «آی‌دی اینباندها/گروه‌ها» را در تنظیماتِ این پنل در "
                    "بات‌ساز اصلاح کن.")

        if mb is not None:
            data_limit = int(mb) * 1024 * 1024
        else:
            data_limit = int(gb) * GB if gb else 0
        # محاسبهٔ کلِ ثانیه‌های عمر (روزها + ساعت‌ها) — اگه هر دو صفر باشه، بدون انقضا
        total_seconds = int(days) * 86400 + int(hours) * 3600
        expire = int(time.time()) + total_seconds if total_seconds else None

        # === PATCH_PASARGUARD_CREATE_V2 ===
        # طبقِ اسکیمای رسمیِ PasarGuard (مدلِ UserCreate در کلاینتِ رسمیِ
        # pasarguard 2.1.0) بدنهٔ درست دقیقاً همین است و فیلدِ `expire` هنوز
        # معتبر و از نوعِ «timestamp ثانیه‌ای (int) یا datetime» است.
        # ⚠️ مهم: مدل‌های پاسارگاد با extra="allow" ساخته شده‌اند، یعنی فیلدِ
        # ناشناخته را بی‌صدا نادیده می‌گیرند و خطا نمی‌دهند؛ پس فرستادنِ
        # فیلدهای من‌درآوردی (مثل expire_strategy) باعث می‌شد کانفیگ «بدون
        # انقضا» ساخته شود بدون هیچ هشداری. فقط فیلدهای اسکیما ارسال می‌شوند.
        # === PATCH_PASARGUARD_HWID_LIMIT_V1 ===
        # گزارشِ ادمین: پنلِ پاسارگاد با «HWID limit cannot be unlimited;
        # maximum is 2» رد می‌کرد، درحالی‌که فروشنده صراحتاً «۱ کاربر»
        # (یعنی محدودیتِ دستگاه = ۱) تنظیم کرده بود. علتش این بود که این
        # کلاینت اصلاً فیلدِ hwid_limit رو نمی‌فرستاد (طبقِ داکیومنتِ قدیمیِ
        # بالا که می‌گفت limit_ip در پاسارگاد نادیده گرفته می‌شود) — پس پنل
        # پیش‌فرضِ خودش (نامحدود=۰) رو می‌ذاشت، که بعضی نسخه‌ها/تنظیماتِ
        # پنل (مثلِ همینجا) قبول نمی‌کنن. الان مقدارِ واقعیِ limit_ip فروشنده
        # به‌عنوانِ hwid_limit فرستاده می‌شه؛ اگه صفر/تنظیم‌نشده بود (یعنی
        # قصدِ فروشنده نامحدود بوده)، یه عددِ بزرگِ محدود (۹۹۹) به‌جایِ صفر
        # می‌فرستیم که تقریباً معادلِ نامحدود است ولی هیچ پنلی رَدش نمی‌کنه.
        body = {
            "username": username,
            "group_ids": group_ids,
            "data_limit": data_limit,
            "data_limit_reset_strategy": "no_reset",
            "hwid_limit": int(limit_ip) if limit_ip else 999,
            # هنگام ساخت فقط دو مقدار مجاز است: active یا on_hold
            "status": "active",
            "note": "",
        }
        if expire is not None:
            body["expire"] = expire
        # === PATCH_TRIAL_ON_HOLD_V1 ===
        # شمارشِ مدت از اولین اتصال (مخصوصِ سرویسِ تست). در این حالت expire
        # معنا ندارد و مدت از on_hold_expire_duration خوانده می‌شود.
        _secs = int(days) * 86400 + int(hours) * 3600
        if on_hold and _secs:
            body["status"] = "on_hold"
            body["on_hold_expire_duration"] = _secs
            body.pop("expire", None)

        # === PATCH_PASARGUARD_CREATE_PATH_FALLBACK_V2 ===
        # نسخهٔ قبلی این‌جا روی ۴۰۴ به /api/users (جمع) فالبک می‌کرد، اما آن
        # مسیر توی کل خانوادهٔ مرزبان/پاسارگارد همیشه فقط GET (لیستِ کاربران)
        # بوده و POST رویش هیچ‌وقت جواب نمی‌ده — همون چیزی که با ۴۰۵ Method
        # Not Allowed دیده شد. یعنی اون فالبک از اول اشتباه بود و فقط خطای
        # واقعی (۴۰۴ روی مسیر درستِ /api/user) رو زیرِ یک خطای دومِ گمراه‌کننده
        # قایم می‌کرد.
        #
        # ۴۰۴ روی POST /api/user در حالی که list_groups (GET) از همون
        # base_url جواب می‌گیره، معمولاً یعنی خودِ base_url مشکلی نداره ولی
        # یک لایهٔ میانی (نگینکس/WAF/ریدایرکتِ FastAPI برای اسلشِ انتهایی)
        # دقیقاً همین مسیر POST رو قبل از رسیدن به اپ گم می‌کنه. پس به‌جای
        # نمایشِ فوریِ خطا، چند وریانتِ بی‌خطر و رایج از خودِ آدرس/مسیر امتحان
        # می‌شه؛ فقط اگه همه ۴۰۴ بدن پیامِ تشخیصی نهایی نشون داده می‌شه.
        base = self.base_url
        candidates = [base]
        # وریانت ۱: اگه base_url به‌اشتباه با «/api» تمام شده (فروشنده گاهی
        # آدرسِ کامل API رو به‌جای ریشهٔ دامنه وارد می‌کنه) → «/api» تکراری
        # می‌شه (.../api/api/user) و ۴۰۴ می‌گیریم؛ بدونِ اون هم امتحان کن.
        if re.search(r"/api$", base, flags=re.IGNORECASE):
            stripped = re.sub(r"/api$", "", base, flags=re.IGNORECASE)
            if stripped and stripped not in candidates:
                candidates.append(stripped)

        create_path_variants = ["/api/user", "/api/user/"]  # بدون و با اسلشِ انتهایی

        res = None
        tried = []
        try:
            for cand_base in candidates:
                for path in create_path_variants:
                    self.base_url = cand_base
                    tried.append(f"{cand_base}{path}")
                    try:
                        res = await self._post_user(path, body)
                    except PanelError as e:
                        # این یعنی پنل واقعاً جواب داد (نه ۴۰۴) — خطای واقعی
                        # همینه، دیگه لازم نیست وریانتِ دیگه‌ای امتحان بشه.
                        #
                        # === PATCH_PASARGUARD_POST_401_RBAC_HINT_V1 ===
                        # گزارشِ ادمین: باوجودِ اینکه «تستِ اتصال» و لیستِ
                        # گروه‌ها (GET) موفق بودند (یعنی توکن معتبر بود و
                        # لاگین درست انجام شده)، POST /api/user با ۴۰۱
                        # «Could not validate credentials» رد می‌شد — حتی
                        # بعدِ ریترایِ خودکارِ _request با توکنِ کاملاً تازه
                        # (نگاه کن به PATCH_TOKEN_CACHE_V1 در marzban_panel.py).
                        # چون این خطا بعدِ یک تلاشِ ناموفق با توکنِ تازه هم
                        # تکرار می‌شود، علتش «توکنِ کهنه» نیست. رایج‌ترین علتِ
                        # واقعیِ این الگو (GET کار می‌کند ولی POSTِ ساختِ کاربر
                        # نه) این است که ادمینِ متصل‌شده «اپراتور» است، نه
                        # sudo — همان چیزی که در نسخه‌های دیگرِ RBACِ پاسارگارد
                        # با ۴۰۳/۴۰۴ دیده و قبلاً فیکس شده بود؛ این پنل به‌جایش
                        # ۴۰۱ برمی‌گرداند.
                        if "HTTP 401" in str(e):
                            raise PanelError(
                                f"ساخت کانفیگ در پاسارگاد ناموفق بود: {e}\n\n"
                                "چون خواندنِ گروه‌ها (GET) از همین پنل درست کار می‌کند "
                                "ولی ساختِ کاربر (POST) حتی بعدِ ورودِ دوباره رد می‌شود، "
                                "محتمل‌ترین علت این است که ادمینِ متصل‌شده «اپراتور» "
                                "است و مجوزِ ساختِ کاربر ندارد. با همین یوزرنیم/پسورد "
                                "وارد پنل وب شو و در مدیریتِ ادمین‌ها چک کن is_sudo "
                                "فعال باشد؛ اگر نبود، یک ادمینِ sudo بساز و همان را "
                                "جایِ این تنظیمات بگذار.") from e
                        raise PanelError(f"ساخت کانفیگ در پاسارگاد ناموفق بود: {e}") from e
                    if res is not None:
                        break
                if res is not None:
                    break
        finally:
            self.base_url = base  # همیشه base_url اصلی برگردونده بشه

        if res is None:
            # === PATCH_PASARGAD_STALE_TOKEN_404_RETRY_V1 ===
            # قبل از اعلامِ شکستِ نهایی: شاید توکنِ کش‌شده سمتِ پنل باطل/کهنه
            # شده (که با ۴۰۱/۴۰۳ مواجه نمی‌شیم تا کشِ مشترک خودش پاک بشه،
            # چون این حالت هم دقیقاً به‌شکلِ همون ۴۰۴ ظاهر می‌شه). یک‌بارِ
            # آخر با توکنِ کاملاً تازه (دورزدنِ کش) امتحان کن.
            try:
                await self.login(force=True)
                self.base_url = base
                res = await self._post_user("/api/user", body)
                if res is not None:
                    tried.append("(retry بعدِ force-relogin روی توکنِ کهنهٔ احتمالی) /api/user")
            except PanelError as e:
                raise PanelError(f"ساخت کانفیگ در پاسارگاد ناموفق بود: {e}") from e
            finally:
                self.base_url = base

        if res is None:
            # === PATCH_PASARGUARD_404_RBAC_HINT_V1 ===
            # قبلاً این پیام فقط WAF/ریورس‌پراکسی رو مقصر می‌دونست. اما یه
            # دلیلِ خیلی رایج‌ترِ دیگه هم برای همین الگو (POST همیشه ۴۰۴،
            # درحالی‌که GET /api/groups از همون base_url درست جواب می‌ده)
            # وجود داره: توی نسخه‌های RBAC-دارِ پاسارگارد، وقتی ادمینِ
            # استفاده‌شده «اپراتور» (غیرِ sudo) باشه و مجوزِ ساختِ کاربر رو
            # نداشته باشه، بعضی نسخه‌ها به‌جای ۴۰۳ (Forbidden) با ۴۰۴ جواب
            # می‌دن — یعنی مسیر رو از دیدِ اون ادمین «قایم» می‌کنن. این دقیقاً
            # هم‌خانواده‌ی همون مشکلیه که برای list_groups (RBAC، پچِ
            # PASARGUARD_GROUPS_RBAC_V1) قبلاً دیده و فیکس شده بود. پس این
            # فرضیه رو هم به‌عنوانِ محتمل‌ترین دلیلِ جایگزین به کاربر نشون
            # می‌دیم، نه فقط WAF رو.
            raise PanelError(
                f"ساخت کانفیگ ناموفق: پنل به هیچ‌کدوم از این آدرس‌ها جواب نداد "
                f"(همه ۴۰۴): {', '.join(tried)}. "
                "چون دریافتِ گروه‌ها (GET) از همین base_url درست کار می‌کنه، دو تا "
                "دلیلِ محتمل هست:\n"
                "۱) ادمینِ متصل‌شده به این پنل «sudo» نیست (یه ادمینِ اپراتور/محدوده) "
                "و مجوزِ ساختِ کاربر رو نداره — بعضی نسخه‌های پاسارگارد این حالت رو "
                "به‌جای ۴۰۳ با ۴۰۴ نشون می‌دن (مسیر رو از ادمینِ بدونِ مجوز قایم "
                "می‌کنن). برای بررسی: با همین یوزرنیم/پسورد وارد پنل وب شو و ببین "
                "توی مدیریتِ ادمین‌ها is_sudo فعاله یا نه؛ اگه نیست، یه ادمینِ sudo "
                "بساز و جایگزینِ تنظیماتِ این مستأجر کن.\n"
                "۲) یک نگینکس/پراکسی/WAF جلوی پنل دقیقاً درخواستِ POST به این مسیر "
                "رو قبل از رسیدن به اپ بلاک یا گم می‌کنه — تنظیماتِ ریورس‌پراکسیِ "
                "جلوی این پنل رو هم بررسی کن.")
        sub_url = res.get("subscription_url") or ""
        return {
            "uuid": uuid_str or gen_uuid(), "sub_id": sub_id or gen_sub_id(),
            "email": username, "gb": gb, "days": days,
            "inbounds": group_ids, "sub_link": self._abs_sub_link(sub_url),
            "expiry_ms": (expire * 1000) if expire else 0,
        }
