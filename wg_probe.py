"""
wg_probe.py — فقط خواندنی. نشان می‌دهد «ارسال کانفیگ تکی وایرگارد» برای یک سرویس دقیقاً چه چیزی از پنل می‌گیرد:
۱) لوکیشن‌هایِ بومیِ پنل (/api/wg/user/<u>/configs)  ۲) متنِ هر فایلِ .conf  ۳) وایرگاردِ داخلِ ساب‌لینک
کلیدهایِ PrivateKey/PresharedKey ماسک می‌شوند (فقط ۴ حرفِ اول)؛ می‌توانی خروجی را بفرستی.

اجرا:
    cd /opt/botsaz && source venv/bin/activate
    python3 wg_probe.py <آیدیِ سرویس | کدِ سرویس | بخشی از نامِ کاربریِ پنل>
"""
import asyncio
import re
import sys

from sqlalchemy import or_, select

from config import Session
from models import PanelAccount


def mask_keys(text: str) -> str:
    return re.sub(r"(?im)^(\s*(?:PrivateKey|PresharedKey)\s*=\s*)(\S{4})\S*", r"\1\2…(ماسک)", text or "")


def summarize_conf(text: str) -> dict:
    """فیلدهایِ مهمِ یک فایلِ .conf را برایِ تشخیص بیرون می‌کشد (بدونِ کلیدِ خصوصی)."""
    out = {}
    for key in ("Address", "DNS", "MTU", "Endpoint", "AllowedIPs", "PersistentKeepalive"):
        m = re.search(rf"(?im)^\s*{key}\s*=\s*(.+)$", text or "")
        out[key] = m.group(1).strip() if m else "—"
    out["has_[Interface]"] = "[Interface]" in (text or "")
    out["has_[Peer]"] = "[Peer]" in (text or "")
    out["has_PrivateKey"] = bool(re.search(r"(?im)^\s*PrivateKey\s*=\s*\S+", text or ""))
    out["has_PublicKey"] = bool(re.search(r"(?im)^\s*PublicKey\s*=\s*\S+", text or ""))
    return out


def describe_wg_settings(ps) -> str:
    """proxy_settings.wireguard کاربر را بدونِ لو‌دادنِ کلیدِ خصوصی توصیف می‌کند."""
    if not isinstance(ps, dict):
        return "proxy_settings در پاسخِ پنل نیست (نسخهٔ پنل این فیلد را نمی‌دهد)"
    wg = ps.get("wireguard")
    if not isinstance(wg, dict):
        return "wireguard در proxy_settings نیست ⇒ برایِ این کاربر تنظیماتِ وایرگارد ساخته نشده"
    pk = wg.get("private_key") or ""
    pub = wg.get("public_key") or ""
    ips = wg.get("peer_ips") or []
    return (f"private_key={'دارد (' + pk[:4] + '…)' if pk else 'خالی ❌'} | public_key={'دارد' if pub else 'خالی ❌'} | "
            f"peer_ips={ips if ips else 'خالی ❌'}")


def describe_usage(u) -> str:
    if not isinstance(u, dict):
        return "پاسخی از پنل نیامد"
    used = int(u.get("up") or 0) + int(u.get("down") or 0)
    last = u.get("last_online_ms")
    import datetime as _dt
    last_s = _dt.datetime.utcfromtimestamp(int(last) / 1000).strftime("%Y-%m-%d %H:%M UTC") if last else "هرگز/نامشخص"
    return f"مصرف {used / 1024 / 1024:.1f}MB | آخرین اتصال: {last_s} | کلاینت: {u.get('client_app') or '—'}"


def describe_nodes(res) -> list[str]:
    items = res if isinstance(res, list) else (res.get("nodes") or res.get("items") or []) if isinstance(res, dict) else []
    out = []
    for n in items[:30]:
        if not isinstance(n, dict):
            continue
        st = str(n.get("status") or "?")
        flag = "✅" if st.lower() == "connected" else "❌"
        out.append(f"{flag} {str(n.get('name') or n.get('id'))[:24]:<24} {st:<12} {str(n.get('address') or '')[:30]:<30} "
                   f"{str(n.get('message') or '')[:70]}")
    return out or ["(هیچ نودی در پاسخ نبود)"]


def describe_wg_uri(uri: str) -> str:
    """پارامترهایِ یک لینکِ wireguard:// بدونِ کلیدها: سرور، پورت و نامِ/مقدارِ فیلدهایِ مهم."""
    from urllib.parse import parse_qs, unquote, urlparse
    try:
        u = urlparse((uri or "").strip())
        qs = parse_qs(u.query)
        keys = sorted(qs)
        def g(k, n=60):
            v = unquote(qs.get(k, ["—"])[0])
            return (v[:n] + "…") if len(v) > n else v
        allowed = unquote(qs.get("allowedips", [""])[0])
        return (f"{u.hostname}:{u.port} | address={g('address')} | mtu={g('mtu')} | dns={g('dns')} | "
                f"keepalive={g('keepalive')} | allowedips: {len(allowed.split(',')) if allowed else 0} ردیف | "
                f"پارامترها={keys} | نام={unquote(u.fragment)[:30]!r}")
    except Exception as e:  # noqa: BLE001
        return f"(پارس نشد: {type(e).__name__})"


async def main(ref: str):
    from tenant.bots.vpn import vpn_user as V
    async with Session() as s:
        conds = [PanelAccount.email.ilike(f"%{ref}%"), PanelAccount.svc_code == ref]
        if ref.isdigit() and int(ref) < 2147483647:
            conds.append(PanelAccount.id == int(ref))
        accs = list((await s.scalars(select(PanelAccount).where(or_(*conds)).limit(5))).all())
        if len(accs) != 1:
            print("سرویس‌هایِ پیداشده:", [(a.id, a.email, a.panel_key) for a in accs] or "هیچ", "— دقیق‌تر بنویس.")
            return
        a = accs[0]
        panel = await V._panel_for_account(s, a.bot_id, a)
    print(f"سرویس {a.id} | {a.email} | پنل: {getattr(panel, 'panel_key', '?')} | {getattr(panel, 'base_url', '?')}")
    if not panel:
        print("پنلی وصل نیست.")
        return
    client = V._client_from_panel(panel)
    try:
        # === خودِ کاربر روی پنل: آیا تنظیماتِ وایرگارد (کلید/IP همتا) برایش ساخته شده؟ ===
        try:
            cur = await client.get_client(a.email)
        except Exception as e:  # noqa: BLE001
            cur = None
            print(f"✗ خواندنِ کاربر از پنل: {type(e).__name__}: {str(e)[:160]}")
        if cur is None:
            print("⚠️ کاربر روی پنل پیدا نشد (حذف شده؟)")
        else:
            ps = cur.get("proxy_settings") if isinstance(cur, dict) else None
            print(f"\n— وضعیتِ کاربر روی پنل: status={cur.get('status')!r} group_ids={cur.get('group_ids')} "
                  f"proxy_settings={'هست' if isinstance(ps, dict) else 'در پاسخ نیامد'}")
            print("   ", describe_wg_settings(ps))
        # === آیا این کاربر تا حالا واقعاً وصل شده؟ (مصرف/آخرین اتصال) ===
        try:
            us = await client.get_usage(a.email)
            print("— مصرف/آخرین اتصال:", describe_usage(us))
        except Exception as e:  # noqa: BLE001
            print(f"— مصرف: خوانده نشد ({type(e).__name__}: {str(e)[:100]})")
        # === نودهایِ پنل: اگر نودِ وایرگارد قطع/خطا باشد، پیرهایِ کاربران رویش اعمال نمی‌شود ===
        try:
            nodes = await client._request("GET", "/api/nodes")
            print("— نودهایِ پنل:")
            for line in describe_nodes(nodes):
                print("   ", line)
        except Exception as e:  # noqa: BLE001
            print(f"— نودها: خوانده نشد ({type(e).__name__}: {str(e)[:120]}) — احتمالاً ادمینِ متصل دسترسیِ sudo ندارد")
        eps = []
        if hasattr(client, "get_wireguard_endpoints"):
            try:
                eps = await client.get_wireguard_endpoints(a.email)
            except Exception as e:  # noqa: BLE001
                print(f"✗ /configs خطا داد: {type(e).__name__}: {str(e)[:200]}")
        print(f"\n— لوکیشن‌هایِ بومی: {len(eps)}")
        for ep in eps:
            print(f"   id={ep.get('id')} name={ep.get('name')!r} file={ep.get('file')!r} config_url={ep.get('config_url')!r}")
            try:
                txt = await client.fetch_wireguard_config_text(a.email, ep.get("id"), path=ep.get("config_url"))
            except Exception as e:  # noqa: BLE001
                print(f"   ✗ گرفتنِ متن: {type(e).__name__}: {str(e)[:160]}")
                continue
            print("   خلاصه:", summarize_conf(txt))
            print("   ---- متن (ماسک‌شده) ----")
            print("   " + mask_keys(txt)[:900].replace("\n", "\n   "))
        links = []
        try:
            links = await client.get_client_links(a.email)
        except Exception as e:  # noqa: BLE001
            print(f"\n✗ get_client_links خطا: {type(e).__name__}: {str(e)[:160]}")
        wg_links = [l for l in links if (l or "").lstrip().startswith(("wireguard://", "[Interface]"))]
        print(f"\n— لینک‌هایِ ساب/پنل: کل {len(links)} | وایرگارد {len(wg_links)}")
        for n, l in enumerate(wg_links, 1):
            print(f"   #{n}", describe_wg_uri(l))
        if wg_links:
            print("\n— فایلِ .conf ای که ربات از اولین لینک می‌سازد (کلید ماسک):")
            conf = V._parse_wireguard_uri(wg_links[0]) if (wg_links[0] or "").lstrip().startswith("wireguard://") else wg_links[0]
            print("   خلاصه:", summarize_conf(conf or ""))
            print("   " + mask_keys(conf or "(ساخته نشد)")[:700].replace("\n", "\n   "))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
