#!/usr/bin/env python3
"""deploy/miniapp_files_patch.py — مدیریتِ فایل‌هایِ مینی‌اپ در /vip (PATCH_MINIAPP_FILES_V1).

بعد از miniapp_vip_patch.py و miniapp_ui_patch.py اجرا شود (idempotent؛ از هر فایل پشتیبان می‌گیرد):
    python3 /opt/botsaz/deploy/miniapp_files_patch.py && systemctl restart botsazvip

کارتِ «📁 فایل‌هایِ مینی‌اپ» زیرِ کارتِ مینی‌اپ: فهرستِ فایل‌ها، ✏️ ویرایشِ فایل‌هایِ متنی، ⬆️ جایگزینیِ هر فایل، ➕ افزودنِ فایل، 🗑 حذف.
منطقِ امنِ فایل (ضدِ path-traversal، سقفِ حجم، بدونِ فایلِ مخفی) در hosted_miniapp_domain.py است.
"""
import ast
import shutil
import sys
from pathlib import Path

HOSTING = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/botsazvip/hosting")
UI = HOSTING / "ui.py"
API = HOSTING / "api.py"
MARK_API = "PATCH_MINIAPP_FILES_V1"
MARK_UI = "PATCH_MINIAPP_FILES_UI_V1"

API_BLOCK = '''

# === PATCH_MINIAPP_FILES_V1 === مدیریتِ فایل‌هایِ مینی‌اپ (فهرست / خواندن / نوشتن / آپلودِ تک‌فایل / حذف)
async def _miniapp_files_app(app_id, x_init_data, x_kb_token):
    u = await _auth(x_init_data, x_kb_token)
    app = await _owned_app(u, app_id)
    if not MD.is_active(app):
        raise HTTPException(status_code=400, detail="اول مینی‌اپ را فعال کن.")
    return app


@hosting_router.get("/api/hosting/apps/{app_id}/miniapp/files")
async def api_miniapp_files(app_id: int, x_init_data: str | None = Header(default=None),
                            x_kb_token: str | None = Header(default=None)):
    app = await _miniapp_files_app(app_id, x_init_data, x_kb_token)
    return JSONResponse({"ok": True, "files": MD.list_static(app.name), "limit": MD.MAX_FILE_BYTES})


@hosting_router.get("/api/hosting/apps/{app_id}/miniapp/file")
async def api_miniapp_file_read(app_id: int, path: str, x_init_data: str | None = Header(default=None),
                                x_kb_token: str | None = Header(default=None)):
    app = await _miniapp_files_app(app_id, x_init_data, x_kb_token)
    r = MD.read_static_text(app.name, path)
    if not r.ok:
        raise HTTPException(status_code=400, detail=r.message)
    return JSONResponse({"ok": True, "path": path, "content": r.message})


@hosting_router.put("/api/hosting/apps/{app_id}/miniapp/file")
async def api_miniapp_file_write(app_id: int, body: dict, x_init_data: str | None = Header(default=None),
                                 x_kb_token: str | None = Header(default=None)):
    app = await _miniapp_files_app(app_id, x_init_data, x_kb_token)
    content = (body or {}).get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="متنِ فایل ارسال نشد.")
    r = MD.write_static(app.name, str((body or {}).get("path") or ""), content.encode("utf-8"), must_exist=True)
    if not r.ok:
        raise HTTPException(status_code=400, detail=r.message)
    return JSONResponse({"ok": True, "message": r.message})


@hosting_router.post("/api/hosting/apps/{app_id}/miniapp/file-upload")
async def api_miniapp_file_upload(app_id: int, file: UploadFile = File(...), path: str = "",
                                  x_init_data: str | None = Header(default=None),
                                  x_kb_token: str | None = Header(default=None)):
    app = await _miniapp_files_app(app_id, x_init_data, x_kb_token)
    data = await file.read(MD.MAX_FILE_BYTES + 1)
    r = MD.write_static(app.name, (path or file.filename or "").strip(), data)
    if not r.ok:
        raise HTTPException(status_code=400, detail=r.message)
    return JSONResponse({"ok": True, "message": r.message})


@hosting_router.delete("/api/hosting/apps/{app_id}/miniapp/file")
async def api_miniapp_file_delete(app_id: int, path: str, x_init_data: str | None = Header(default=None),
                                  x_kb_token: str | None = Header(default=None)):
    app = await _miniapp_files_app(app_id, x_init_data, x_kb_token)
    r = MD.delete_static_file(app.name, path)
    if not r.ok:
        raise HTTPException(status_code=400, detail=r.message)
    return JSONResponse({"ok": True, "message": r.message})
'''

JS = """/* === PATCH_MINIAPP_FILES_UI_V1 === کارتِ فایل‌هایِ مینی‌اپ: فهرست، ویرایش، جایگزینی، افزودن، حذف */
var MINI_FILES = [];
var _miniRender0 = miniRender;
miniRender = function(el, id, d){
  _miniRender0(el, id, d);
  el.insertAdjacentHTML('beforeend', '<div id="minifiles-card"></div>');
  miniFilesCard(id, d);
};
function miniFilesCard(id, d){
  var el = document.getElementById('minifiles-card');
  if (!el) return;
  if (!d.active || !d.static){ el.innerHTML = ''; return; }
  el.innerHTML = '<div class="card" style="margin-top:12px"><div class="name">📁 فایل‌هایِ مینی‌اپ</div>'
    + '<div class="meta">هر فایل را جدا جایگزین یا ویرایش کن؛ لازم نیست دوباره کلِ zip را آپلود کنی.</div>'
    + '<div id="minifiles-list" class="meta" style="margin-top:8px">در حالِ بارگذاری…</div></div>';
  miniFilesLoad(id);
}
function miniFilesLoad(id){
  var el = document.getElementById('minifiles-list');
  if (!el) return;
  api('/apps/'+id+'/miniapp/files').then(function(d){
    MINI_FILES = d.files || [];
    var h = '';
    for (var i = 0; i < MINI_FILES.length; i++){
      var f = MINI_FILES[i];
      h += '<div class="kv" style="margin-top:6px"><span class="mono">'+H(f.path)+'</span><span>'+fa(Math.max(1, Math.round(f.size/1024)))+' KB</span></div>'
        + '<div class="grid2" style="margin-top:4px">'
        + (f.text ? '<button onclick="miniFileEdit('+id+','+i+')">✏️ ویرایش</button>' : '<button disabled>—</button>')
        + '<button onclick="miniFileReplace('+id+','+i+')">⬆️ جایگزینی</button></div>'
        + (f.path === 'index.html' ? '' : '<button class="b-off" style="margin-top:4px;width:100%" onclick="miniFileDel('+id+','+i+')">🗑 حذف</button>');
    }
    h += '<button style="margin-top:10px;width:100%" onclick="miniFileAdd('+id+')">➕ افزودنِ فایل</button>';
    el.innerHTML = h;
  }).catch(function(e){ el.innerHTML = H(e.message); });
}
function miniFileEdit(id, i){
  var f = MINI_FILES[i];
  var el = document.getElementById('minifiles-list');
  if (!f || !el) return;
  api('/apps/'+id+'/miniapp/file?path='+encodeURIComponent(f.path)).then(function(d){
    el.innerHTML = '<div class="mono">'+H(f.path)+'</div>'
      + '<textarea id="minifile-text" style="width:100%;min-height:260px;margin-top:8px;direction:ltr;font-family:monospace;font-size:12px"></textarea>'
      + '<div class="grid2" style="margin-top:8px"><button class="b-key" onclick="miniFileSave('+id+','+i+')">💾 ذخیره</button>'
      + '<button onclick="miniFilesLoad('+id+')">انصراف</button></div>';
    document.getElementById('minifile-text').value = d.content;
  }).catch(function(e){ buzz('error'); msg('err', e.message); });
}
function miniFileSave(id, i){
  var f = MINI_FILES[i];
  var t = document.getElementById('minifile-text');
  if (!f || !t) return;
  api('/apps/'+id+'/miniapp/file', {method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: f.path, content: t.value})}).then(function(d){
    buzz('success'); msg('ok', d.message); miniFilesLoad(id);
  }).catch(function(e){ buzz('error'); msg('err', e.message); });
}
function miniFilePick(id, path){
  var i = document.getElementById('fpmf');
  if (!i){
    i = document.createElement('input');
    i.type = 'file'; i.id = 'fpmf'; i.style.display = 'none';
    document.body.appendChild(i);
  }
  i.onchange = function(){
    var f = i.files && i.files[0];
    i.value = '';
    if (!f){ msg('err', 'فایلی انتخاب نشد.'); return; }
    var p = path || prompt('مسیرِ فایل داخلِ مینی‌اپ (مثلاً img/logo.png)', f.name);
    if (!p) return;
    msg('ok', 'در حالِ آپلود…');
    var fd = new FormData(); fd.append('file', f);
    api('/apps/'+id+'/miniapp/file-upload?path='+encodeURIComponent(p), {method:'POST', body:fd}).then(function(d){
      buzz('success'); msg('ok', d.message); miniFilesLoad(id);
    }).catch(function(e){ buzz('error'); msg('err', e.message); });
  };
  i.click();
}
function miniFileReplace(id, i){
  var f = MINI_FILES[i];
  if (f) miniFilePick(id, f.path);
}
function miniFileAdd(id){ miniFilePick(id, ''); }
function miniFileDel(id, i){
  var f = MINI_FILES[i];
  if (!f || !confirm('فایلِ '+f.path+' حذف شود؟')) return;
  api('/apps/'+id+'/miniapp/file?path='+encodeURIComponent(f.path), {method:'DELETE'}).then(function(d){
    msg('ok', d.message); miniFilesLoad(id);
  }).catch(function(e){ msg('err', e.message); });
}

"""

A_FUNC = "function pick(){"


def _backup(p: Path) -> None:
    b = p.with_name(p.name + ".bak_miniapp_files")
    if not b.exists():
        shutil.copy2(p, b)


def patch_api(s: str) -> str:
    if MARK_API in s:
        return s
    for need in ("api_miniapp_upload", "hosting_router", "_auth", "_owned_app", "JSONResponse", "HTTPException",
                 "Header", "UploadFile", "File", "MD"):
        if need not in s:
            raise SystemExit(f"api.py: «{need}» پیدا نشد (اول miniapp_vip_patch.py را اجرا کن)؛ چیزی تغییر نکرد")
    return s.rstrip("\n") + "\n" + API_BLOCK


def patch_ui(s: str) -> str:
    if MARK_UI in s:
        return s
    if "function miniRender(" not in s:
        raise SystemExit("ui.py: کارتِ مینی‌اپ نیست (اول miniapp_ui_patch.py را اجرا کن)؛ چیزی تغییر نکرد")
    if s.count(A_FUNC) != 1:
        raise SystemExit("ui.py: «function pick(){» یک‌بار پیدا نشد؛ چیزی تغییر نکرد")
    if "\\" in JS or '"""' in JS:
        raise SystemExit("خطای داخلی: JS نباید بک‌اسلش یا سه‌تایی‌کوتیشن داشته باشد")
    return s.replace(A_FUNC, JS + A_FUNC, 1)


def main() -> None:
    for p, fn in ((API, patch_api), (UI, patch_ui)):
        s = p.read_text()
        new = fn(s)
        ast.parse(new)
        if new != s:
            _backup(p)
            p.write_text(new)
            print(f"{p.name}: پچ شد")
        else:
            print(f"{p.name}: قبلاً پچ شده بود")
    print("OK — حالا: systemctl restart botsazvip")


if __name__ == "__main__":
    main()
