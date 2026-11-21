"""PATCH_PSEL_MANAGE_V1 (Whale) — «مدیریتِ کاملِ پنل» داخلِ رباتِ پنل‌فروشی."""
import time

import pytest

from tenant.bots.panel_seller import manage as MG
from tests.test_panel_seller import _Cb, _Msg, _St, _cbs, env, ui  # noqa: F401

GB = 1024 ** 3
NOW = time.time()


class FC:
    users: dict = {}
    log: list = []

    def __init__(self, *a, **k):
        pass

    async def close(self):
        pass

    async def list_clients(self):
        return [{"email": n, "traffic": {"up": 0, "down": u["used"], "total": u["total"]}, "totalGB": u["total"],
                 "expiryTime": u["exp"], "status": u["status"], "last_online_ms": u.get("online", 0), "created_ms": u["created"]}
                for n, u in FC.users.items()]

    async def get_usage(self, n):
        u = FC.users.get(n)
        if not u:
            return None
        return {"up": 0, "down": u["used"], "total": u["total"], "expiryTime": u["exp"], "enable": u["status"] != "disabled",
                "last_online_ms": u.get("online", 0)}

    async def get_client(self, n):
        return {"status": FC.users[n]["status"], "sub_link": f"https://sub.example/{n}"} if n in FC.users else None

    async def add_traffic(self, n, gb):
        FC.log.append(("gb", n, gb))
        FC.users[n]["total"] += gb * GB

    async def renew(self, n, days):
        FC.log.append(("days", n, days))

    async def reset_usage(self, n):
        FC.log.append(("reset", n))
        FC.users[n]["used"] = 0

    async def reset_sub(self, n):
        FC.log.append(("sub", n))
        return f"https://sub.example/new-{n}"

    async def set_enabled(self, n, on):
        FC.log.append(("enabled", n, on))
        FC.users[n]["status"] = "active" if on else "disabled"

    async def delete_account(self, n):
        FC.log.append(("del", n))
        FC.users.pop(n, None)

    async def _modify(self, n, **fields):
        FC.log.append(("mod", n, fields))

    async def create_account(self, email, gb, days, **kw):
        FC.log.append(("new", email, gb, days))
        return {"sub_link": f"https://sub.example/{email}"}

    async def list_admins(self):
        return [{"username": "sudo1", "is_sudo": True}, {"username": "reseller1", "total_users": 4, "users_usage": 5 * GB}]

    async def _request(self, method, path, **kw):
        FC.log.append(("req", method, path))


def _user(used=0, total=10 * GB, days=10, status="active", i=0, online=0):
    return {"used": used, "total": total, "exp": int((NOW + days * 86400) * 1000) if days else 0, "status": status,
            "created": 1_700_000_000_000 + i, "online": online}


@pytest.fixture
def mg(ui, monkeypatch):
    monkeypatch.setattr(MG, "Session", ui.maker)
    monkeypatch.setattr(MG, "_open", lambda panel: FC())
    FC.log = []
    FC.users = {f"user{i:02d}": _user(used=i * GB, i=i) for i in range(13)}
    FC.users["gone"] = _user(status="disabled", i=99)
    FC.users["old"] = _user(days=-3, i=98)                 # منقضی
    FC.users["unl"] = _user(total=0, days=0, i=97)         # نامحدود
    FC.users["near"] = _user(days=2, i=96, online=int(time.time() * 1000))     # «الان»، نه زمانِ import (در اجرایِ کامل ده‌ها دقیقه فاصله می‌افتاد)
    return ui


@pytest.mark.asyncio
async def test_dashboard_counts_and_single_panel_auto_opens(mg):
    cb = _Cb("psel:mg", uid=500)
    await MG.mg_home(cb, None, _St())
    t = cb.message.out[-1][0]
    assert "مدیریتِ پنل" in t and "کاربران: <b>" in t
    assert f"psel:mgu:3:a:0" in _cbs(cb.message) and "psel:mgn:3" in _cbs(cb.message) and "psel:mga:3" in _cbs(cb.message)
    from tenant.bots.panel_seller.common import fa
    assert fa(17) in t and "⏰ منقضی: " + fa(1) in t and "♾ حجمِ نامحدود: " + fa(1) in t and "آنلاین (۵ دقیقهٔ اخیر): " + fa(1) in t


@pytest.mark.asyncio
async def test_user_list_paginates_filters_and_searches(mg):
    cb = _Cb("psel:mgu:3:a:0", uid=500)
    await MG.mg_users(cb, None, _St())
    c = _cbs(cb.message)
    assert len([x for x in c if x.startswith("psel:mgv:3:")]) == 10 and "psel:mgu:3:a:1" in c
    cb = _Cb("psel:mgu:3:a:1", uid=500)
    await MG.mg_users(cb, None, _St())
    assert len([x for x in _cbs(cb.message) if x.startswith("psel:mgv:3:")]) == 7
    cb = _Cb("psel:mgu:3:d:0", uid=500)
    await MG.mg_users(cb, None, _St())
    assert [x for x in _cbs(cb.message) if x.startswith("psel:mgv:")] == ["psel:mgv:3:gone"]
    cb = _Cb("psel:mgu:3:x:0", uid=500)
    await MG.mg_users(cb, None, _St())
    xs = [x for x in _cbs(cb.message) if x.startswith("psel:mgv:")]
    assert "psel:mgv:3:old" in xs and "psel:mgv:3:user12" in xs and "psel:mgv:3:user00" not in xs      # منقضی + تمام‌حجم
    st = _St()
    await MG.mg_search_ask(_Cb("psel:mgs:3", uid=500), None, st)
    m = _Msg(500, "user0")
    await MG.mg_search_run(m, None, st)
    assert len([x for x in _cbs(m) if x.startswith("psel:mgv:")]) == 10
    m = _Msg(500, "near")
    st2 = _St(); st2.d = {"pid": 3}
    await MG.mg_search_run(m, None, st2)
    assert [x for x in _cbs(m) if x.startswith("psel:mgv:")] == ["psel:mgv:3:near"]


@pytest.mark.asyncio
async def test_user_card_and_single_step_actions(mg):
    cb = _Cb("psel:mgv:3:user05", uid=500)
    await MG.mg_user(cb, None, _St())
    t = cb.message.out[-1][0]
    assert "user05" in t and "https://sub.example/user05" in t and "▓" in t
    c = _cbs(cb.message)
    for k in ("psel:mgg:3:user05:p", "psel:mgg:3:user05:m", "psel:mgd:3:user05", "psel:mgi:3:user05:g", "psel:mgi:3:user05:d",
              "psel:mgt:3:user05", "psel:mgr:3:user05", "psel:mgl:3:user05", "psel:mgx:3:user05"):
        assert k in c, k
    await MG.mg_toggle(_Cb("psel:mgt:3:user05", uid=500), None)
    assert ("enabled", "user05", False) in FC.log
    await MG.mg_reset_usage(_Cb("psel:mgr:3:user05", uid=500), None)
    assert ("reset", "user05") in FC.log
    cb = _Cb("psel:mgl:3:user05", uid=500)
    await MG.mg_reset_sub(cb, None)
    assert "new-user05" in cb.message.out[-1][0]
    await MG.mg_unlimited(_Cb("psel:mgi:3:user05:g", uid=500), None)
    await MG.mg_unlimited(_Cb("psel:mgi:3:user05:d", uid=500), None)
    assert ("mod", "user05", {"data_limit": 0}) in FC.log and ("mod", "user05", {"expire": 0}) in FC.log


@pytest.mark.asyncio
async def test_delete_needs_confirmation(mg):
    cb = _Cb("psel:mgx:3:user03", uid=500)
    await MG.mg_delete(cb, None)
    assert "user03" in FC.users and "psel:mgxok:3:user03" in _cbs(cb.message)
    await MG.mg_delete(_Cb("psel:mgxok:3:user03", uid=500), None)
    assert "user03" not in FC.users and ("del", "user03") in FC.log


@pytest.mark.asyncio
async def test_volume_and_days_amount_flow_with_guards(mg):
    st = _St()
    await MG.mg_amount_ask(_Cb("psel:mgg:3:user05:p", uid=500), None, st)
    assert st.d == {"pid": 3, "name": "user05", "kind": "g", "sign": "p"}
    m = _Msg(500, "۵")
    await MG.mg_amount_run(m, None, st)
    assert ("gb", "user05", 5) in FC.log and "انجام شد" in m.out[-1][0]
    # کم‌کردنِ بیش از سقف ⇒ رد
    st = _St(); st.d = {"pid": 3, "name": "user05", "kind": "g", "sign": "m"}; st.s = MG.PselMgFSM.amount
    m = _Msg(500, "500")
    await MG.mg_amount_run(m, None, st)
    assert not any(x[0] == "gb" and x[2] == -500 for x in FC.log) and "صفر یا منفی" in m.out[-1][0]
    # عددِ نامعتبر: حالت می‌ماند
    st = _St(); st.d = {"pid": 3, "name": "user05", "kind": "d", "sign": "p"}; st.s = MG.PselMgFSM.amount
    m = _Msg(500, "abc")
    await MG.mg_amount_run(m, None, st)
    assert "❌" in m.out[-1][0] and st.s is not None
    # روز
    m = _Msg(500, "30")
    await MG.mg_amount_run(m, None, st)
    assert ("days", "user05", 30) in FC.log
    # روز برایِ کاربرِ بدونِ انقضا بی‌معنی است
    st = _St(); st.d = {"pid": 3, "name": "unl", "kind": "d", "sign": "p"}; st.s = MG.PselMgFSM.amount
    m = _Msg(500, "5")
    await MG.mg_amount_run(m, None, st)
    assert not any(x[0] == "days" and x[1] == "unl" for x in FC.log) and "نامحدود" in m.out[-1][0]


@pytest.mark.asyncio
async def test_create_user_flow_validates_and_creates(mg):
    st = _St()
    await MG.mg_new_ask(_Cb("psel:mgn:3", uid=500), None, st)
    m = _Msg(500, "a b")
    await MG.mg_new_name(m, None, st)
    assert "❌" in m.out[-1][0] and st.s is MG.PselMgFSM.new_name
    await MG.mg_new_name(_Msg(500, "shop_ali"), None, st)
    await MG.mg_new_gb(_Msg(500, "۵۰"), None, st)
    m = _Msg(500, "0")
    await MG.mg_new_days(m, None, st)
    assert ("new", "shop_ali", 50, 0) in FC.log and "https://sub.example/shop_ali" in m.out[-1][0]


@pytest.mark.asyncio
async def test_admins_list_and_bulk_ops_and_non_admin_blocked(mg):
    cb = _Cb("psel:mga:3", uid=500)
    await MG.mg_admins(cb, None)
    assert "reseller1" in cb.message.out[-1][0] and "psel:mgah:3:reseller1" in _cbs(cb.message)
    cb = _Cb("psel:mgao:3:reseller1:0", uid=500)
    await MG.mg_admin_op(cb, None)
    assert ("req", "POST", "/api/admin/reseller1/users/disable") in FC.log
    # غیرِ مدیر: هیچ کاری نمی‌شود
    n0 = len(FC.log)
    cb = _Cb("psel:mgx:3:user01", uid=999)
    await MG.mg_delete(cb, None)
    await MG.mg_delete(_Cb("psel:mgxok:3:user01", uid=999), None)
    assert "user01" in FC.users and len(FC.log) == n0


@pytest.mark.asyncio
async def test_menu_has_manage_button_and_report(mg):
    from tenant.bots.panel_seller import admin as AM
    cb = _Cb("psel:adm", uid=500)
    await AM.adm_menu(cb, None, _St())
    assert "psel:mg" in _cbs(cb.message)
    cb = _Cb("psel:mgk:3", uid=500)
    await MG.mg_report(cb, None)
    t = cb.message.out[-1][0]
    assert "پرمصرف" in t and "رو به انقضا" in t and "near" in t


@pytest.mark.asyncio
async def test_single_panel_home_works_with_frozen_callback(mg):
    """CallbackQuery در aiogram frozen است؛ مسیرِ تک‌پنل نباید cb.data را عوض کند (Whale: دکمه هیچ‌چیز نمی‌داد)."""
    class Frozen(_Cb):
        def __setattr__(self, k, v):
            if k == "data" and "data" in self.__dict__:
                raise AttributeError("frozen")
            super().__setattr__(k, v)

    cb = Frozen("psel:mg", uid=500)
    await MG.mg_home(cb, None, _St())
    assert "مدیریتِ پنل" in cb.message.out[-1][0]
