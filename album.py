# album.py — 相册管理器：多目录订阅 + 缩略图 + Web UI + 快速清理
# 运行: 双击 启动.bat  或  python album.py
# 依赖: Pillow, pillow-heif  (视频缩略图可选 ffmpeg)
import http.server, json, os, re, shutil, subprocess, sys, threading, time, urllib.parse, webbrowser
from pathlib import Path

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

from PIL import Image, ImageOps

# 数据目录：exe/脚本旁边，thumbs/ trash/ config.json 都放这里
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent
DATA = BASE
THUMBS = DATA / "thumbs"
TRASH = DATA / "trash"
CONFIG = DATA / "config.json"
PORT = 18899

FFMPEG = r"D:/whisper/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe"
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff"}
VID_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
THUMB_SIZE = (400, 300)

MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/mp4",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska", ".webm": "video/webm",
}

media_list = []
media_lock = threading.Lock()
thumb_sem = threading.Semaphore(4)


def load_config():
    """读 config.json，没有就建默认"""
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"dirs": [], "port": 18899}


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


config = load_config()
if config.get("port"):
    PORT = int(config["port"])


def resolve_rel(rel):
    """rel = '0/path/file.jpg' → (根目录Path, 根内相对路径str)，无效返回 (None, None)"""
    sid_str, _, local = rel.partition("/")
    try:
        sid = int(sid_str)
    except ValueError:
        return None, None
    dirs = config.get("dirs", [])
    if sid < 0 or sid >= len(dirs) or not local:
        return None, None
    return Path(dirs[sid]), local


def pregen_thumbs():
    time.sleep(2)
    items = sorted(media_list, key=lambda m: m["size"], reverse=True)
    done = 0
    for m in items:
        thumb = THUMBS / (m["rel"] + ".jpg")
        if thumb.exists():
            continue
        with thumb_sem:
            ensure_thumb(m["rel"], m["is_video"])
        done += 1
        if done % 50 == 0:
            print(f"  预生成缩略图 {done}...")
    print(f"  预生成完成 {done} 个")


_DATE_PAT = re.compile(r"(\d{4})(\d{2})(\d{2})[_\s-]?\d{0,6}|(\d{4})-(\d{2})-(\d{2})")


def parse_date(name, mtime):
    m = _DATE_PAT.search(name)
    if m:
        y = int(m.group(1) or m.group(4))
        mo = int(m.group(2) or m.group(5))
        if 2000 <= y <= 2099 and 1 <= mo <= 12:
            return y, mo
    t = time.localtime(mtime)
    return t.tm_year, t.tm_mon


def scan_media():
    """扫描所有订阅目录，rel 统一带 sid 前缀"""
    items = []
    for sid, d in enumerate(config.get("dirs", [])):
        root = Path(d)
        if not root.exists():
            print(f"  跳过不存在的目录: {d}")
            continue
        print(f"  扫描 [{sid}] {d} ...")
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in IMG_EXT | VID_EXT:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            local = str(p.relative_to(root)).replace("\\", "/")
            rel = f"{sid}/{local}"
            year, month = parse_date(p.name, st.st_mtime)
            items.append({
                "rel": rel,
                "sid": sid,
                "name": p.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "is_video": ext in VID_EXT,
                "year": year,
                "month": month,
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def ensure_thumb(rel, is_video):
    root, local = resolve_rel(rel)
    if root is None:
        return None
    src = root / local
    thumb = THUMBS / (rel + ".jpg")
    if thumb.exists():
        return thumb
    thumb.parent.mkdir(parents=True, exist_ok=True)
    try:
        if is_video:
            tmp = thumb.with_name(thumb.name + ".tmp.jpg")
            r = subprocess.run([FFMPEG, "-i", str(src), "-ss", "1", "-vframes", "1", "-y", str(tmp)],
                               capture_output=True, timeout=30)
            if not tmp.exists():
                return None
            img = Image.open(tmp)
            img = ImageOps.exif_transpose(img)
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            img.convert("RGB").save(thumb, "JPEG", quality=85)
            tmp.unlink()
        else:
            img = Image.open(src)
            img = ImageOps.exif_transpose(img)
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            img.convert("RGB").save(thumb, "JPEG", quality=85)
        return thumb
    except Exception as e:
        print(f"  缩略图失败 {rel}: {e}")
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urllib.parse.unquote(self.path.split("?")[0])
        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode("utf-8"))
        elif path == "/api/media":
            self.send_json(media_list)
        elif path == "/api/config":
            self.send_json({"dirs": config.get("dirs", []), "port": PORT})
        elif path.startswith("/thumb/"):
            self.handle_thumb(path[7:])
        elif path.startswith("/media/"):
            self.handle_media(path[7:])
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.unquote(self.path.split("?")[0])
        if path.startswith("/api/delete/"):
            self.handle_delete(path[12:])
        elif path == "/api/batch_delete":
            self.handle_batch_delete()
        elif path == "/api/refresh":
            global media_list
            media_list = scan_media()
            self.send_json({"count": len(media_list)})
        elif path == "/api/config":
            self.handle_config_save()
        else:
            self.send_error(404)

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, mime, cache=False):
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def handle_config_save(self):
        global media_list, config
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            dirs = body.get("dirs", [])
            if not isinstance(dirs, list):
                raise ValueError("dirs 必须是数组")
            dirs = [str(d).strip() for d in dirs if str(d).strip()]
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})
            return
        config = {"dirs": dirs, "port": config.get("port", PORT)}
        save_config(config)
        media_list = scan_media()
        self.send_json({"ok": True, "count": len(media_list)})

    def handle_thumb(self, rel):
        is_video = any(m["rel"] == rel and m["is_video"] for m in media_list)
        with thumb_sem:
            thumb = ensure_thumb(rel, is_video)
        if thumb and thumb.exists():
            self.send_bytes(thumb.read_bytes(), "image/jpeg", cache=True)
        else:
            self.send_error(404)

    def handle_media(self, rel):
        root, local = resolve_rel(rel)
        if root is None:
            self.send_error(404)
            return
        fp = root / local
        if not fp.exists():
            self.send_error(404)
            return
        mime = MIME_MAP.get(fp.suffix.lower(), "application/octet-stream")
        fsize = fp.stat().st_size
        range_hdr = self.headers.get("Range", "")
        if range_hdr.startswith("bytes="):
            spec = range_hdr[6:].split(",")[0]
            if "-" in spec:
                s, e = spec.split("-", 1)
                start = int(s) if s else max(0, fsize - int(e))
                end = min(int(e), fsize - 1) if e else fsize - 1
            else:
                start, end = int(spec), fsize - 1
            start = min(start, fsize - 1)
            if start > end:
                self.send_error(416)
                return
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(fp, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(fsize))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(fp, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    def handle_delete(self, rel):
        root, local = resolve_rel(rel)
        if root is None:
            self.send_json({"ok": False, "error": "无效路径"})
            return
        src = root / local
        if not src.exists():
            self.send_json({"ok": False, "error": "文件不存在"})
            return
        trash_path = TRASH / rel
        trash_path.parent.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            try:
                shutil.move(str(src), str(trash_path))
                break
            except OSError:
                if i < 4:
                    time.sleep(0.5)
                else:
                    self.send_json({"ok": False, "error": "文件被占用,请关闭播放器后重试"})
                    return
        thumb = THUMBS / (rel + ".jpg")
        if thumb.exists():
            thumb.unlink()
        with media_lock:
            media_list[:] = [m for m in media_list if m["rel"] != rel]
        self.send_json({"ok": True})

    def handle_batch_delete(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            rels = body.get("rels", [])
        except Exception:
            self.send_error(400)
            return
        deleted = []
        for rel in rels:
            root, local = resolve_rel(rel)
            if root is None:
                continue
            src = root / local
            if not src.exists():
                continue
            trash_path = TRASH / rel
            trash_path.parent.mkdir(parents=True, exist_ok=True)
            moved = False
            for i in range(3):
                try:
                    shutil.move(str(src), str(trash_path))
                    moved = True
                    break
                except OSError:
                    if i < 2:
                        time.sleep(0.3)
            if not moved:
                continue
            thumb = THUMBS / (rel + ".jpg")
            if thumb.exists():
                thumb.unlink()
            deleted.append(rel)
        with media_lock:
            media_list[:] = [m for m in media_list if m["rel"] not in set(deleted)]
        self.send_json({"ok": True, "deleted": len(deleted)})


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>相册管理器</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Microsoft YaHei',sans-serif;background:#1a1a1a;color:#e0e0e0}
.toolbar{position:sticky;top:0;background:#222;padding:12px 20px;display:flex;align-items:center;gap:8px;z-index:100;border-bottom:1px solid #333;flex-wrap:wrap}
.toolbar button{padding:6px 14px;background:#333;color:#ccc;border:1px solid #444;border-radius:6px;cursor:pointer;font-size:13px}
.toolbar button:hover{background:#444}
.toolbar button.active{background:#0066cc;border-color:#0066cc;color:#fff}
.stats{margin-left:auto;color:#888;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;padding:20px}
.card{background:#2a2a2a;border-radius:10px;overflow:hidden;cursor:pointer;transition:transform .15s,box-shadow .15s;content-visibility:auto;contain-intrinsic-size:auto 200px;position:relative}
.card:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.4)}
.card .thumb{width:100%;height:140px;object-fit:cover;display:block;background:#222}
.card .meta{padding:8px 10px}
.card .size{font-size:12px;color:#f0a030;font-weight:600}
.card .name{font-size:12px;color:#aaa;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;margin-left:6px}
.badge.video{background:#803030;color:#ff8080}
.lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:200;flex-direction:column;align-items:center;justify-content:center}
.lightbox.show{display:flex}
.lightbox img,.lightbox video{max-width:88vw;max-height:72vh;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.6)}
.lightbox .info{margin-top:16px;text-align:center;color:#ccc;font-size:14px;line-height:1.8}
.lightbox .info .path{color:#888;font-size:12px}
.lightbox .actions{margin-top:16px;display:flex;gap:12px;flex-wrap:wrap;justify-content:center}
.lightbox .actions button{padding:10px 28px;border:none;border-radius:8px;font-size:15px;cursor:pointer}
.btn-del{background:#c22;color:#fff}
.btn-del:hover{background:#e33}
.btn-nav{background:#333;color:#ccc}
.btn-nav:hover{background:#444}
.loading{text-align:center;padding:60px;color:#666}
.card .chk{position:absolute;top:8px;left:8px;width:22px;height:22px;border:2px solid #fff;border-radius:4px;background:rgba(0,0,0,.4);cursor:pointer;z-index:2;display:flex;align-items:center;justify-content:center;font-size:14px;color:#fff}
.card .chk.on{background:#0066cc;border-color:#0066cc}
.card .chk:hover{border-color:#0066cc}
.card.sel{outline:2px solid #0066cc}
.selbar{display:none;position:fixed;bottom:0;left:0;right:0;background:#222;border-top:1px solid #444;padding:12px 20px;z-index:150;align-items:center;gap:12px}
.selbar.show{display:flex}
.selbar .cnt{color:#fff;font-size:15px}
.selbar button{padding:8px 20px;border:none;border-radius:6px;font-size:14px;cursor:pointer}
.selbar .del{background:#c22;color:#fff}
.selbar .del:hover{background:#e33}
.selbar .cancel{background:#333;color:#ccc}
.selbar .cancel:hover{background:#444}
.filterbar{background:#222;padding:8px 20px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;border-bottom:1px solid #333}
.filterbar .fbtn{padding:4px 12px;background:#333;color:#aaa;border:1px solid #444;border-radius:14px;cursor:pointer;font-size:12px}
.filterbar .fbtn:hover{background:#444;color:#ccc}
.filterbar .fbtn.active{background:#0066cc;border-color:#0066cc;color:#fff}
.filterbar .sep{color:#555;font-size:12px;margin:0 4px}
.cleanmode{display:none;position:fixed;inset:0;background:#000;z-index:300;flex-direction:column;align-items:center;justify-content:center}
.cleanmode.show{display:flex}
.cleanmode img,.cleanmode video{max-width:96vw;max-height:82vh;border-radius:6px}
.cleanmode .cbar{margin-top:12px;color:#aaa;font-size:14px;display:flex;gap:18px;align-items:center}
.cleanmode .cbar b{color:#fff}
.cleanmode .key{background:#333;padding:2px 10px;border-radius:4px;font-size:12px;color:#ccc}
#selrect{display:none;position:fixed;border:1.5px solid #0066cc;background:rgba(0,102,204,.12);pointer-events:none;z-index:99}
.settings{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:250;align-items:center;justify-content:center}
.settings.show{display:flex}
.settings .panel{background:#2a2a2a;border-radius:12px;padding:24px;width:min(640px,92vw);max-height:86vh;overflow:auto}
.settings h2{font-size:18px;margin-bottom:6px}
.settings .hint{color:#888;font-size:13px;margin-bottom:16px}
.settings .dirrow{display:flex;align-items:center;gap:10px;padding:10px 12px;background:#1f1f1f;border-radius:8px;margin-bottom:8px}
.settings .dirrow .path{flex:1;font-size:13px;color:#ccc;word-break:break-all}
.settings .dirrow .sid{background:#0066cc;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;flex-shrink:0}
.settings .dirrow button{padding:4px 12px;background:#442;color:#c66;border:1px solid #644;border-radius:6px;cursor:pointer;font-size:12px}
.settings .dirrow button:hover{background:#543}
.settings .addrow{display:flex;gap:8px;margin:14px 0}
.settings .addrow input{flex:1;padding:8px 12px;background:#1a1a1a;border:1px solid #444;border-radius:6px;color:#eee;font-size:13px}
.settings .addrow button{padding:8px 18px;background:#0066cc;color:#fff;border:none;border-radius:6px;cursor:pointer}
.settings .foot{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.settings .foot button{padding:8px 22px;border:none;border-radius:6px;cursor:pointer;font-size:14px}
.settings .foot .save{background:#0066cc;color:#fff}
.settings .foot .close{background:#333;color:#ccc}
.empty{padding:80px 20px;text-align:center;color:#666}
.empty h3{color:#aaa;margin-bottom:12px}
</style>
</head>
<body>
<div class="toolbar">
  <button onclick="setSort('date')" id="btn-date">按时间</button>
  <button onclick="setSort('size')" id="btn-size">按大小</button>
  <button onclick="setSort('name')" id="btn-name">按名称</button>
  <button onclick="toggleDir()" id="btn-dir">↓</button>
  <button onclick="doRefresh()">刷新</button>
  <button onclick="enterClean()" style="background:#2a5a2a;border-color:#3a7a3a;color:#8f8">快速清理</button>
  <button onclick="toggleSelectAll()" id="btn-selall">全选</button>
  <button onclick="openSettings()" style="background:#1a3a5a;border-color:#2a5a8a;color:#8cf">设置</button>
  <span class="stats" id="stats">加载中...</span>
</div>
<div class="filterbar" id="filterbar"></div>
<div id="selrect"></div>
<div class="grid" id="grid"><div class="loading">加载中...</div></div>
<div class="selbar" id="selbar">
  <span class="cnt" id="selcnt"></span>
  <button class="del" onclick="deleteSelected()">删除选中</button>
  <button class="cancel" onclick="clearSelect()">取消</button>
</div>
<div class="lightbox" id="lightbox" onclick="if(event.target===this)closeLB()">
  <div id="lb-content"></div>
  <div class="info" id="lb-info"></div>
  <div class="actions">
    <button class="btn-nav" onclick="nav(-1)">← 上一个</button>
    <button class="btn-del" onclick="deleteCurrent()">删除</button>
    <button class="btn-nav" onclick="nav(1)">下一个 →</button>
    <button class="btn-nav" onclick="enterClean(curIdx);closeLB()">快速清理</button>
    <button class="btn-nav" onclick="closeLB()">关闭</button>
  </div>
</div>
<div class="settings" id="settings" onclick="if(event.target===this)closeSettings()">
  <div class="panel">
    <h2>订阅目录设置</h2>
    <div class="hint">像订阅源一样添加多个照片文件夹，保存后自动扫描。路径支持 Windows 盘符或网络路径。</div>
    <div id="dirlist"></div>
    <div class="addrow">
      <input id="dirinput" placeholder="例如 D:\Photos 或 \\\\NAS\\share\\photos" />
      <button onclick="addDir()">添加</button>
    </div>
    <div class="foot">
      <button class="close" onclick="closeSettings()">取消</button>
      <button class="save" onclick="saveSettings()">保存并扫描</button>
    </div>
  </div>
</div>
<div class="cleanmode" id="cleanmode">
  <div id="cm-content"></div>
  <div class="cbar">
    <span class="key">↑ 删除</span><span class="key">↓ 保留</span><span class="key">Esc 退出</span>
    <span id="cm-info"></span>
  </div>
</div>
<script>
let media=[],sortKey='size',sortDir=-1,curIdx=-1,sorted=[],selected=new Set(),shown=0;
let filterYear=0,filterMonth=0,filterType='';
let cfgDirs=[];
const PAGE=200;
async function loadMedia(){
  const r=await fetch('/api/media');
  media=await r.json();
  buildFilter();
  render();
}
function buildFilter(){
  const years=[...new Set(media.map(m=>m.year))].sort();
  let html='<button class="fbtn'+(filterType===''?' active':'')+'" onclick="setType(0)">全部</button>'+
    '<button class="fbtn'+(filterType==='img'?' active':'')+'" onclick="setType(1)">图片</button>'+
    '<button class="fbtn'+(filterType==='vid'?' active':'')+'" onclick="setType(2)">视频</button>'+
    '<span class="sep">|</span>'+
    '<button class="fbtn'+(!filterYear?' active':'')+'" onclick="setFilter(0,0)">全部年月</button>';
  years.forEach(y=>{
    html+='<button class="fbtn'+(filterYear===y&&!filterMonth?' active':'')+'" onclick="setFilter('+y+',0)">'+y+'</button>';
    if(filterYear===y){
      const months=[...new Set(media.filter(m=>m.year===y).map(m=>m.month))].sort((a,b)=>a-b);
      html+='<span class="sep">|</span>';
      months.forEach(m=>{
        html+='<button class="fbtn'+(filterMonth===m?' active':'')+'" onclick="setFilter('+y+','+m+')">'+m+'月</button>';
      });
    }
  });
  document.getElementById('filterbar').innerHTML=html;
}
function setType(t){filterType=t===1?'img':t===2?'vid':'';buildFilter();render();}
function setFilter(y,m){filterYear=y;filterMonth=m;buildFilter();render();}
function setSort(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=-1;}render();}
function toggleDir(){sortDir*=-1;render();}
function render(){
  let list=media;
  if(filterType==='img')list=list.filter(m=>!m.is_video);
  else if(filterType==='vid')list=list.filter(m=>m.is_video);
  if(filterYear)list=list.filter(m=>m.year===filterYear);
  if(filterMonth)list=list.filter(m=>m.month===filterMonth);
  sorted=[...list].sort((a,b)=>{
    let va,vb;
    if(sortKey==='size'){va=a.size;vb=b.size;}
    else if(sortKey==='date'){va=a.year*10000+a.month*100+(a.mtime%86400);vb=b.year*10000+b.month*100+(b.mtime%86400);}
    else{va=a.name.toLowerCase();vb=b.name.toLowerCase();}
    return sortDir*(va<vb?-1:va>vb?1:0);
  });
  ['date','size','name'].forEach(k=>{
    document.getElementById('btn-'+k).classList.toggle('active',sortKey===k);
  });
  document.getElementById('btn-dir').textContent=sortDir===-1?'↓':'↑';
  const total=sorted.reduce((s,m)=>s+m.size,0);
  document.getElementById('stats').textContent=sorted.length+' 个文件 · '+fmt(total);
  document.getElementById('grid').innerHTML='';
  shown=0;
  if(!sorted.length){
    document.getElementById('grid').innerHTML=
      '<div class="empty" style="grid-column:1/-1"><h3>还没有照片</h3>'+
      '<p>点击右上角「设置」添加相册目录</p></div>';
    return;
  }
  appendBatch();
}
function appendBatch(){
  const end=Math.min(shown+PAGE,sorted.length);
  if(shown>=end)return;
  const parts=[];
  for(let i=shown;i<end;i++){
    const m=sorted[i];
    parts.push(
      '<div class="card'+(selected.has(m.rel)?' sel':'')+'" data-idx="'+i+'" onclick="openLB('+i+')">'+
      '<div class="chk'+(selected.has(m.rel)?' on':'')+'" onclick="event.stopPropagation();toggleSelect('+i+')">'+(selected.has(m.rel)?'✓':'')+'</div>'+
      '<img class="thumb" src="/thumb/'+enc(m.rel)+'" loading="lazy" />'+
      '<div class="meta"><div class="size">'+fmt(m.size)+
      (m.is_video?'<span class="badge video">视频</span>':'')+'</div>'+
      '<div class="name" title="'+esc(m.name)+'">'+esc(m.name)+'</div></div></div>'
    );
  }
  document.getElementById('grid').insertAdjacentHTML('beforeend',parts.join(''));
  shown=end;
  updateSelBar();
}
function openLB(i){
  curIdx=i;
  const m=sorted[i];
  const c=document.getElementById('lb-content');
  if(m.is_video){
    c.innerHTML='<video src="/media/'+enc(m.rel)+'" controls autoplay></video>';
  }else{
    c.innerHTML='<img src="/media/'+enc(m.rel)+'" />';
  }
  document.getElementById('lb-info').innerHTML=
    '<div>'+esc(m.name)+'</div>'+
    '<div>'+fmt(m.size)+' · '+new Date(m.mtime*1000).toLocaleString('zh')+'</div>'+
    '<div class="path">'+esc(m.rel)+'</div>';
  document.getElementById('lightbox').classList.add('show');
}
function closeLB(){
  document.getElementById('lightbox').classList.remove('show');
  document.getElementById('lb-content').innerHTML='';
  curIdx=-1;
}
function nav(d){
  const n=curIdx+d;
  if(n>=0&&n<sorted.length)openLB(n);
}
async function deleteCurrent(){
  if(curIdx<0)return;
  const m=sorted[curIdx];
  if(!confirm('删除 '+m.name+'？\n'+fmt(m.size)+'\n\n(移到回收站，可恢复)'))return;
  const v=document.querySelector('#lb-content video');
  if(v){v.pause();v.removeAttribute('src');v.load();}
  await new Promise(r=>setTimeout(r,150));
  const r=await fetch('/api/delete/'+enc(m.rel),{method:'POST'});
  const d=await r.json();
  if(d.ok){
    media=media.filter(x=>x.rel!==m.rel);
    closeLB();render();
  }else alert(d.error||'删除失败');
}
async function doRefresh(){
  document.getElementById('stats').textContent='刷新中...';
  await fetch('/api/refresh',{method:'POST'});
  await loadMedia();
}
function toggleSelect(idx){
  const m=sorted[idx];if(!m)return;
  if(selected.has(m.rel))selected.delete(m.rel);else selected.add(m.rel);
  const card=document.querySelector('[data-idx="'+idx+'"]');
  if(card){
    const on=selected.has(m.rel);
    card.classList.toggle('sel',on);
    const chk=card.querySelector('.chk');
    if(chk){chk.classList.toggle('on',on);chk.textContent=on?'✓':'';}
  }
  updateSelBar();
}
function toggleSelectAll(){
  const allSelected=selected.size===sorted.length&&sorted.length>0;
  if(allSelected)selected.clear();else sorted.forEach(m=>selected.add(m.rel));
  const on=!allSelected;
  document.querySelectorAll('.card').forEach(card=>{
    card.classList.toggle('sel',on);
    const chk=card.querySelector('.chk');
    if(chk){chk.classList.toggle('on',on);chk.textContent=on?'✓':'';}
  });
  updateSelBar();
}
function clearSelect(){
  selected.clear();
  document.querySelectorAll('.card.sel').forEach(card=>{
    card.classList.remove('sel');
    const chk=card.querySelector('.chk');
    if(chk){chk.classList.remove('on');chk.textContent='';}
  });
  updateSelBar();
}
function updateSelBar(){
  const bar=document.getElementById('selbar');
  if(selected.size>0){
    bar.classList.add('show');
    document.getElementById('selcnt').textContent='已选 '+selected.size+' 个文件';
    document.getElementById('btn-selall').textContent='取消全选';
  }else{
    bar.classList.remove('show');
    document.getElementById('btn-selall').textContent='全选';
  }
}
async function deleteSelected(){
  if(selected.size===0)return;
  const total=sorted.filter(m=>selected.has(m.rel)).reduce((s,m)=>s+m.size,0);
  if(!confirm('删除选中的 '+selected.size+' 个文件？\n'+fmt(total)+'\n\n(移到回收站，可恢复)'))return;
  const r=await fetch('/api/batch_delete',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rels:[...selected]})});
  if(r.ok){
    const d=await r.json();
    media=media.filter(x=>!selected.has(x.rel));
    selected.clear();
    render();
  }else alert('删除失败');
}
// 快速清理模式
let cmIdx=-1,cmList=[];
function enterClean(startIdx){
  cmList=[...sorted];cmIdx=startIdx||0;
  if(!cmList.length){alert('没有文件');return;}
  document.getElementById('cleanmode').classList.add('show');
  cmShow();
}
function cmShow(){
  if(cmIdx>=cmList.length){closeClean();return;}
  const m=cmList[cmIdx];
  const c=document.getElementById('cm-content');
  if(m.is_video){
    c.innerHTML='<video src="/media/'+enc(m.rel)+'" controls autoplay></video>';
  }else{
    c.innerHTML='<img src="/media/'+enc(m.rel)+'" />';
  }
  document.getElementById('cm-info').innerHTML=
    '<b>'+(cmIdx+1)+'/'+cmList.length+'</b> '+esc(m.name)+' · '+fmt(m.size);
}
function closeClean(){
  document.getElementById('cleanmode').classList.remove('show');
  document.getElementById('cm-content').innerHTML='';
  cmIdx=-1;render();
}
async function cmDelete(){
  if(cmIdx<0||cmIdx>=cmList.length)return;
  const m=cmList[cmIdx];
  const v=document.querySelector('#cm-content video');
  if(v){v.pause();v.removeAttribute('src');v.load();}
  await new Promise(r=>setTimeout(r,100));
  const r=await fetch('/api/delete/'+enc(m.rel),{method:'POST'});
  const d=await r.json();
  if(!d.ok){alert(d.error||'删除失败');return;}
  media=media.filter(x=>x.rel!==m.rel);
  cmList.splice(cmIdx,1);
  cmShow();
}
// 设置：订阅目录
async function openSettings(){
  try{
    const r=await fetch('/api/config');
    const d=await r.json();
    cfgDirs=d.dirs||[];
  }catch(e){cfgDirs=[];}
  renderDirs();
  document.getElementById('settings').classList.add('show');
}
function closeSettings(){document.getElementById('settings').classList.remove('show');}
function renderDirs(){
  const el=document.getElementById('dirlist');
  if(!cfgDirs.length){
    el.innerHTML='<div style="color:#666;padding:12px">还没有订阅目录</div>';
    return;
  }
  el.innerHTML=cfgDirs.map((d,i)=>
    '<div class="dirrow"><span class="sid">#'+i+'</span>'+
    '<span class="path">'+esc(d)+'</span>'+
    '<button onclick="removeDir('+i+')">移除</button></div>'
  ).join('');
}
function addDir(){
  const inp=document.getElementById('dirinput');
  const v=inp.value.trim();
  if(!v)return;
  if(cfgDirs.includes(v)){alert('已存在');return;}
  cfgDirs.push(v);
  inp.value='';
  renderDirs();
}
function removeDir(i){cfgDirs.splice(i,1);renderDirs();}
async function saveSettings(){
  const r=await fetch('/api/config',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({dirs:cfgDirs})});
  const d=await r.json();
  if(!d.ok){alert(d.error||'保存失败');return;}
  closeSettings();
  await loadMedia();
}
document.addEventListener('keydown',e=>{
  if(document.getElementById('cleanmode').classList.contains('show')){
    if(e.key==='ArrowUp'){e.preventDefault();cmDelete();}
    else if(e.key==='ArrowDown'){e.preventDefault();cmIdx++;cmShow();}
    else if(e.key==='Escape')closeClean();
    return;
  }
  if(document.getElementById('settings').classList.contains('show')){
    if(e.key==='Escape')closeSettings();
    return;
  }
  if(e.key==='Escape')closeLB();
  else if(e.key==='ArrowLeft')nav(-1);
  else if(e.key==='ArrowRight')nav(1);
  else if(e.key==='Delete'){
    if(curIdx>=0)deleteCurrent();
    else if(selected.size>0)deleteSelected();
  }
});
function fmt(b){
  if(b<1024)return b+' B';
  if(b<1048576)return(b/1024).toFixed(1)+' KB';
  if(b<1073741824)return(b/1048576).toFixed(1)+' MB';
  return(b/1073741824).toFixed(2)+' GB';
}
function enc(s){return encodeURIComponent(s);}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
window.addEventListener('scroll',()=>{
  if(shown<sorted.length&&window.innerHeight+window.scrollY>=document.body.offsetHeight-600)appendBatch();
});
let dragging=false,dragX0=0,dragY0=0;
document.getElementById('grid').addEventListener('mousedown',e=>{
  if(e.button!==0)return;
  if(e.target.closest('.card'))return;
  dragging=true;dragX0=e.clientX;dragY0=e.clientY;
  const r=document.getElementById('selrect');
  r.style.display='block';r.style.left=dragX0+'px';r.style.top=dragY0+'px';r.style.width='0';r.style.height='0';
  e.preventDefault();
});
document.addEventListener('mousemove',e=>{
  if(!dragging)return;
  const r=document.getElementById('selrect');
  const x=Math.min(dragX0,e.clientX),y=Math.min(dragY0,e.clientY);
  const w=Math.abs(e.clientX-dragX0),h=Math.abs(e.clientY-dragY0);
  r.style.left=x+'px';r.style.top=y+'px';r.style.width=w+'px';r.style.height=h+'px';
  const rect={left:x,top:y,right:x+w,bottom:y+h};
  document.querySelectorAll('.card').forEach(card=>{
    const cr=card.getBoundingClientRect();
    const hit=!(cr.right<rect.left||cr.left>rect.right||cr.bottom<rect.top||cr.top>rect.bottom);
    if(hit){
      const idx=+card.dataset.idx;const m=sorted[idx];if(!m)return;
      if(!selected.has(m.rel)){
        selected.add(m.rel);
        card.classList.add('sel');
        const chk=card.querySelector('.chk');
        if(chk){chk.classList.add('on');chk.textContent='✓';}
      }
    }
  });
  updateSelBar();
});
document.addEventListener('mouseup',()=>{
  if(!dragging)return;
  dragging=false;
  document.getElementById('selrect').style.display='none';
});
loadMedia();
</script>
</body>
</html>"""


def main():
    global media_list
    THUMBS.mkdir(parents=True, exist_ok=True)
    TRASH.mkdir(parents=True, exist_ok=True)

    pid_file = DATA / "album.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    dirs = config.get("dirs", [])
    if dirs:
        print(f"订阅目录 {len(dirs)} 个:")
        media_list = scan_media()
        total = sum(m["size"] for m in media_list)
        print(f"找到 {len(media_list)} 个文件 ({total / 2**30:.1f} GB)")
        threading.Thread(target=pregen_thumbs, daemon=True).start()
    else:
        print("还没有订阅目录，请在网页「设置」里添加")
        media_list = []

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"相册管理器: {url}")
    print("快捷键: ←→ 翻页 | Delete 删除 | Esc 关闭")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n退出")
    finally:
        pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
