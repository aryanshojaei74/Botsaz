"""
vpn_panel_factory.py — ساخت کلاینت مناسب هر پنل بر اساس panel_key.

به‌جای ساختن PanelClient (مخصوص سنایی/3X-UI) به‌صورت مستقیم در هر جای کد،
همه‌جا از make_panel_client(panel_row) استفاده کن تا با اضافه‌شدن پنل‌های
بعدی فقط همین فایل اضافه/تغییر کند.

panel_key → کلاینت:
  sanai            → SanaiPanelClient       (3X-UI، فایل مستقل)
  alireza          → AlirezaPanelClient     (3X-UI، فایل مستقل)
  marzban          → MarzbanPanelClient      (مرزبان)
  marzneshin       → MarzneshinPanelClient   (مرزنشین — جانشینِ مرزبان، Serviceمحور)
  rebecca          → RebeccaPanelClient      (ربکا — فورک مستقیم مرزبان)
  pasargad         → PasargadPanelClient    (پاسارگاد قدیمی، مبتنی بر group_ids)
  pasarguard       → PasarGuardOfficialClient (PasarGuard رسمی — github.com/PasarGuard/panel)
  upanel_mizban    → UPanelMizbanClient     (یوپنل مرزبان)
  rickpanel        → RickPanelClient        (RickPanel — مرزبان‌سازگار، بدونِ انتخابِ inbound)
  solidlayer       → SolidLayerPanelClient  (SolidLayer — Management API؛ سرویس‌ها به‌جایِ inbound)
"""
from __future__ import annotations

from tenant.shared.panel_clients.vpn_panel import PanelClient
from tenant.shared.panel_clients.sanai_panel import SanaiPanelClient
from tenant.shared.panel_clients.alireza_panel import AlirezaPanelClient
from tenant.shared.panel_clients.marzban_panel import MarzbanPanelClient
from tenant.shared.panel_clients.marzneshin_panel import MarzneshinPanelClient
from tenant.shared.panel_clients.hiddify_panel import HiddifyPanelClient
from tenant.shared.panel_clients.rebecca_panel import RebeccaPanelClient
from tenant.shared.panel_clients.pasargad_panel import PasarGuardPanelClient as PasargadPanelClient
from tenant.shared.panel_clients.pasarguard_panel import PasarGuardPanelClient as PasarGuardOfficialClient
from tenant.shared.panel_clients.upanel_mizban import UPanelMizbanClient
from tenant.shared.panel_clients.nodex_panel import NodexPanelClient  # === PATCH_NODEX_PANEL_V1 ===
from tenant.shared.panel_clients.shahan_panel import ShahanPanelClient
from tenant.shared.panel_clients.degeris_panel import DegerisPanelClient  # === PATCH_DEGERIS_PANEL_V1 ===
from tenant.shared.panel_clients.eylan_panel import EylanPanelClient  # === PATCH_EYLAN_PANEL_V1 ===
from tenant.shared.panel_clients.heimdall_panel import HeimdallPanelClient  # === PATCH_HEIMDALL_PANEL_V1 ===
from tenant.shared.panel_clients.rickpanel_panel import RickPanelClient  # === PATCH_RICKPANEL_PANEL_V1 ===
from tenant.shared.panel_clients.solidlayer_panel import SolidLayerPanelClient  # === PATCH_SOLIDLAYER_PANEL_V1 ===

# پنل‌هایی که inbound_ids آن‌ها به‌صورت رشتهٔ تگ هستند (نه عدد)
# نکته: پاسارگاد/PasarGuard واقعاً «گروه» عددی دارند نه تگ، اما چون فرم ورودی
# UI برای این دو پنل هم آزاد (رشته‌ای، با کاما) است، همان مسیر تگ‌محور استفاده
# می‌شود و خودِ کلاینت مقادیر عددی را از رشته‌ها استخراج می‌کند (نگاه کن به
# tenant/pasargad_panel.py و tenant/pasarguard_panel.py). ربکا هم مثل مرزبان
# واقعاً تگ‌محور است (نه فقط برای سازگاری فرم).
_TAG_BASED_PANELS = frozenset({"marzban", "rebecca", "pasargad", "pasarguard", "upanel_mizban",
                               "eylan"})   # ایلان: پروتکل/تگ‌هایِ رشته‌ای

_CLIENT_BY_PANEL_KEY = {
    "sanai":         SanaiPanelClient,
    "alireza":       AlirezaPanelClient,
    "marzban":       MarzbanPanelClient,
    "marzneshin":    MarzneshinPanelClient,  # مرزنشین — github.com/marzneshin/marzneshin
    "hiddify":       HiddifyPanelClient,     # هیدیفای — github.com/hiddify/Hiddify-Manager
    "rebecca":       RebeccaPanelClient,
    "pasargad":      PasargadPanelClient,
    "pasarguard":    PasarGuardOfficialClient,  # PasarGuard رسمی (github.com/PasarGuard/panel)
    "upanel_mizban": UPanelMizbanClient,   # یوپنل (مرزبان)
    "nodex":         NodexPanelClient,     # نودکس — GuardCore API (api.cyberpower.space) — PATCH_NODEX_PANEL_V1
    "degeris":       DegerisPanelClient,   # DEGERIS (ثنایی جدید) — PATCH_DEGERIS_PANEL_V1
    "eylan":         EylanPanelClient,     # ایلان (EylanPanel — X-API-KEY) — PATCH_EYLAN_PANEL_V1
    "heimdall":      HeimdallPanelClient,  # HEIMDALL (نسلِ جدیدِ ۳X-UI، RBAC+CSRF) — PATCH_HEIMDALL_PANEL_V1
    "rickpanel":     RickPanelClient,      # RickPanel (مرزبان‌سازگار، بدونِ inbound) — PATCH_RICKPANEL_PANEL_V1
    "solidlayer":    SolidLayerPanelClient,  # SolidLayer (Management API، /api/subscriptions) — PATCH_SOLIDLAYER_PANEL_V1
    "shahan":        ShahanPanelClient,    # شاهان — endpoint واحد + توکنِ ثابت (shahanpanel.com)
}


# === PATCH_MULTI_PANEL_INSTANCES_V1 ===
# اگر مالکِ ربات چند پنل از یک نوع ثبت کرده باشد، کلیدِ نمونه‌های بعدی
# «<نوع>#<شماره>» است (مثلاً pasargad#2). نوعِ پنل همیشه بخشِ قبلِ # است.
def _base(panel_key: str | None) -> str:
    return (panel_key or "").split("#", 1)[0]


def client_class_for(panel_key: str):
    """کلاس کلاینت مناسب این panel_key را برمی‌گرداند (پیش‌فرض: PanelClient سنایی)."""
    return _CLIENT_BY_PANEL_KEY.get(_base(panel_key), PanelClient)


def _parse_inbounds(panel_key: str, raw: str | None):
    """inbound_ids خام را به فرمت مناسب هر نوع پنل تبدیل می‌کند.
    - پنل‌های مبتنی بر تگ (marzban/upanel_mizban): لیست رشته
    - پنل‌های مبتنی بر عدد (sanai/alireza): لیست int
    """
    if not raw:
        return None
    if _base(panel_key) in _TAG_BASED_PANELS:
        return [t.strip() for t in raw.split(",") if t.strip()]
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


# === PATCH_PANEL_API_KEY_AUTH_V1 ===
# فقط پنل‌هایی که واقعاً از این پارامتر پشتیبانی می‌کنن (خانوادهٔ مرزبان،
# چون MarzbanPanelClient حالا api_key رو می‌شناسه) این کوارگ رو می‌گیرن؛
# وگرنه کلاس‌هایِ دیگه (سنایی/علیرضا/هیدیفای/شاهان) با یه کوارگِ ناشناخته
# خطا می‌دن.
_API_KEY_CAPABLE_PANELS = frozenset({"marzban", "rebecca", "pasargad", "pasarguard", "upanel_mizban", "solidlayer"})


def make_panel_client(panel):
    """panel یک ردیف VpnPanel (یا هر آبجکت با همان فیلدها) است.
    خروجی: نمونهٔ آماده‌ٔ کلاینت مناسب.
    """
    panel_key = getattr(panel, "panel_key", "sanai") or "sanai"
    cls = client_class_for(panel_key)
    ids = _parse_inbounds(panel_key, getattr(panel, "inbound_ids", None))
    kwargs = {}
    # === PATCH_PANEL_API_KEY_AUTH_V1 ===
    api_key = getattr(panel, "api_key", None)
    if api_key and _base(panel_key) in _API_KEY_CAPABLE_PANELS:
        kwargs["api_key"] = api_key
    return cls(
        panel.base_url, panel.username, panel.password,
        sub_base=getattr(panel, "sub_base", "") or "",
        inbound_ids=ids,
        **kwargs,
    )


def make_panel_client_from_fields(panel_key: str, base_url: str, username: str,
                                   password: str, sub_base: str = "",
                                   inbound_ids_str: str | None = None,
                                   api_key: str | None = None):
    """مثل make_panel_client ولی برای جاهایی که فقط فیلدهای تکی دارند."""
    cls = client_class_for(panel_key)
    ids = _parse_inbounds(panel_key, inbound_ids_str)
    kwargs = {}
    if api_key and _base(panel_key) in _API_KEY_CAPABLE_PANELS:  # === PATCH_PANEL_API_KEY_AUTH_V1 ===
        kwargs["api_key"] = api_key
    return cls(base_url, username, password, sub_base=sub_base or "", inbound_ids=ids, **kwargs)
