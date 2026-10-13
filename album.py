# 小象相册 — 本地照片/视频管理器
# 核心能力：多目录订阅、缩略图缓存、Web UI、快速清理、回收站删除
# 运行: 双击 启动.bat / 小象相册.exe / python album.py
# 依赖: Pillow, pillow-heif  (视频缩略图可选 ffmpeg)
import http.server, json, os, re, shutil, subprocess, sys, threading, time, urllib.parse, urllib.request, webbrowser
from pathlib import Path

# HEIC/HEIF 支持（iPhone 照片）
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

from PIL import Image, ImageOps
Image.MAX_IMAGE_PIXELS = 2**31  # 允许超高分辨率全景图，防炸弹限制不全关

# ── 路径配置 ──────────────────────────────────────────────
# 数据目录 = exe/脚本所在目录，thumbs/ trash/ config.json 都放这里
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent
DATA = BASE
THUMBS = DATA / "thumbs"       # 缩略图缓存
TRASH = DATA / "trash"         # 回收站（删除的文件移到这里）
CONFIG = DATA / "config.json"  # 订阅目录配置

# ffmpeg 路径：优先本机，找不到走 PATH
FFMPEG = r"D:/whisper/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe"
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"

# ── 常量 ──────────────────────────────────────────────────
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff"}
VID_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
THUMB_SIZE = (280, 210)  # 缩略图目标尺寸
THUMB_QUALITY = 55       # JPEG 质量，平衡体积和速度

MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/mp4",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska", ".webm": "video/webm",
}

# ── 全局状态 ──────────────────────────────────────────────
media_list = []                              # 扫描到的媒体文件索引
media_lock = threading.Lock()                # media_list 读写锁
thumb_sem = threading.Semaphore(8)           # 缩略图并发生成限流
config = {}                                  # 当前配置
PORT = 18899


# ── 配置读写 ──────────────────────────────────────────────
def load_config():
    """读 config.json，不存在或损坏时返回默认"""
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"dirs": [], "port": 18899}


def save_config(cfg):
    """保存配置到 config.json"""
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


config = load_config()
if config.get("port"):
    PORT = int(config["port"])


# ── 路径解析 ──────────────────────────────────────────────
def resolve_rel(rel):
    """rel 格式 'sid/相对路径' → (根目录Path, 根内路径str)，无效返回 (None, None)

    sid 是订阅目录在 config.dirs 中的索引，例如 '0/iPhone/photo.jpg'
    表示 config.dirs[0] 下的 iPhone/photo.jpg
    """
    sid_str, _, local = rel.partition("/")
    try:
        sid = int(sid_str)
    except ValueError:
        return None, None
    dirs = config.get("dirs", [])
    if sid < 0 or sid >= len(dirs) or not local:
        return None, None
    return Path(dirs[sid]), local


# ── 日期解析 ──────────────────────────────────────────────
# 从文件名提取拍摄日期，兼容常见手机命名：
#   VID_20231218_222504.mp4  →  2023-12
#   2023-07-08 221750.mov    →  2023-07
#   Screenshot_2022-01-02-xx →  2022-01
_DATE_PAT = re.compile(r"(\d{4})(\d{2})(\d{2})[_\s-]?\d{0,6}|(\d{4})-(\d{2})-(\d{2})")


def parse_date(name, mtime):
    """返回 (year, month)：优先文件名，兜底 mtime"""
    m = _DATE_PAT.search(name)
    if m:
        y = int(m.group(1) or m.group(4))
        mo = int(m.group(2) or m.group(5))
        if 2000 <= y <= 2099 and 1 <= mo <= 12:
            return y, mo
    t = time.localtime(mtime)
    return t.tm_year, t.tm_mon


# ── 媒体扫描 ──────────────────────────────────────────────
def scan_media():
    """遍历所有订阅目录，返回媒体文件索引列表（按 mtime 降序）

    每个条目包含 rel（全局唯一路径）、sid（来源目录索引）、
    name/size/mtime/is_video/year/month。
    """
    items = []
    for sid, d in enumerate(config.get("dirs", [])):
        root = Path(d)
        if not root.exists():
            print(f"  跳过不存在的目录: {d}")
            continue
        print(f"  扫描 [{sid}] {d} ...")
        root_s = str(root)
        for dirpath, _, filenames in os.walk(root_s):
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in IMG_EXT | VID_EXT:
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                local = os.path.relpath(fp, root_s).replace("\\", "/")
                rel = f"{sid}/{local}"
                year, month = parse_date(fn, st.st_mtime)
                items.append({
                    "rel": rel,
                    "sid": sid,
                    "name": fn,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "is_video": ext in VID_EXT,
                    "year": year,
                    "month": month,
                })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


# ── 缩略图 ────────────────────────────────────────────────
def ensure_thumb(rel, is_video):
    """生成并缓存缩略图，返回 Path 或 None

    图片直接用 Pillow 缩放；视频用 ffmpeg 抽帧再缩放。
    路径: thumbs/{rel}.jpg  （rel 含 sid 前缀，天然隔离多目录）
    """
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
            subprocess.run(
                [FFMPEG, "-i", str(src), "-ss", "0", "-vframes", "1", "-y", str(tmp)],
                capture_output=True, timeout=30,
            )
            if not tmp.exists():
                return None
            img = Image.open(tmp)
            img = ImageOps.exif_transpose(img)
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            img.convert("RGB").save(thumb, "JPEG", quality=THUMB_QUALITY)
            tmp.unlink()
        else:
            img = Image.open(src)
            img = ImageOps.exif_transpose(img)
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            img.convert("RGB").save(thumb, "JPEG", quality=THUMB_QUALITY)
        return thumb
    except Exception as e:
        print(f"  缩略图失败 {rel}: {e}")
        return None


def pregen_thumbs(limit=400):
    """预热前 N 张缩略图（图片优先，体积大优先），剩余靠懒加载"""
    time.sleep(2)  # 等服务先起来再占 CPU
    # 图片排前面：视频抽帧慢，先出图片用户更早看到效果
    items = sorted(media_list, key=lambda m: (m["is_video"], -m["size"]))
    done = 0
    for m in items[:limit]:
        thumb = THUMBS / (m["rel"] + ".jpg")
        if thumb.exists():
            continue
        with thumb_sem:
            ensure_thumb(m["rel"], m["is_video"])
        done += 1
        if done % 50 == 0:
            print(f"  预生成缩略图 {done}...")
    print(f"  预热完成 {done} 个（其余按需生成）")


# ── 文件操作 ──────────────────────────────────────────────
def do_delete(rel):
    """把一个文件移到回收站，成功返回 True

    重试最多 5 次（应对文件被播放器占用），缩略图一并清理。
    """
    root, local = resolve_rel(rel)
    if root is None:
        return False
    src = root / local
    if not src.exists():
        return False
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
                return False
    thumb = THUMBS / (rel + ".jpg")
    if thumb.exists():
        thumb.unlink()
    return True


# ── HTTP 请求处理 ─────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    """纯本地 HTTP 服务，只绑定 127.0.0.1"""

    def log_message(self, fmt, *args):
        pass  # 关掉默认访问日志

    def handle(self):
        """吞掉浏览器关连接时的异常，不刷屏"""
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    # ── 路由 ──────────────────────────────────────────────
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
        elif path == "/api/browse":
            self.handle_browse()
        elif path == "/api/reveal":
            self.handle_reveal()
        elif path == "/api/open":
            self.handle_open()
        else:
            self.send_error(404)

    # ── 响应工具 ──────────────────────────────────────────
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

    def read_body(self):
        """读取 POST body 并解析 JSON"""
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    # ── API 处理 ──────────────────────────────────────────
    def handle_config_save(self):
        """保存订阅目录列表并重新扫描"""
        global media_list, config
        try:
            body = self.read_body()
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
        """返回缩略图，不存在则现场生成"""
        is_video = any(m["rel"] == rel and m["is_video"] for m in media_list)
        with thumb_sem:
            thumb = ensure_thumb(rel, is_video)
        if thumb and thumb.exists():
            self.send_bytes(thumb.read_bytes(), "image/jpeg", cache=True)
        else:
            self.send_error(404)

    def handle_media(self, rel):
        """返回原始媒体文件，支持 HTTP Range（视频拖拽进度条）"""
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
        if not range_hdr.startswith("bytes="):
            # 整文件传输
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
            return

        # Range: bytes=start-end / bytes=start- / bytes=-suffix
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

    def handle_delete(self, rel):
        """删除单个文件（移到回收站）"""
        if not do_delete(rel):
            self.send_json({"ok": False, "error": "文件不存在或被占用"})
            return
        with media_lock:
            media_list[:] = [m for m in media_list if m["rel"] != rel]
        self.send_json({"ok": True})

    def handle_batch_delete(self):
        """批量删除（移到回收站）"""
        try:
            body = self.read_body()
            rels = body.get("rels", [])
        except Exception:
            self.send_error(400)
            return
        deleted = []
        for rel in rels:
            if do_delete(rel):
                deleted.append(rel)
        with media_lock:
            media_list[:] = [m for m in media_list if m["rel"] not in set(deleted)]
        self.send_json({"ok": True, "deleted": len(deleted)})

    def handle_reveal(self):
        """在资源管理器中定位文件"""
        try:
            body = self.read_body()
            root, local = resolve_rel(body.get("rel", ""))
            if root is None:
                self.send_json({"ok": False, "error": "无效路径"})
                return
            subprocess.Popen(["explorer", "/select,", str(root / local)])
            self.send_json({"ok": True})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def handle_open(self):
        """用系统默认播放器打开文件"""
        try:
            body = self.read_body()
            root, local = resolve_rel(body.get("rel", ""))
            if root is None:
                self.send_json({"ok": False, "error": "无效路径"})
                return
            os.startfile(str(root / local))
            self.send_json({"ok": True})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def handle_browse(self):
        """弹出 Windows 原生文件夹选择对话框"""
        try:
            ps = (
                'Add-Type -AssemblyName System.Windows.Forms; '
                '$d = New-Object System.Windows.Forms.FolderBrowserDialog; '
                '$d.Description = "选择相册目录"; '
                'if($d.ShowDialog() -eq "OK"){ $d.SelectedPath }'
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=120,
            )
            self.send_json({"ok": bool(r.stdout.strip()), "path": r.stdout.strip()})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})


# ── 前端页面 ──────────────────────────────────────────────
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小象相册</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🐘</text></svg>">
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
.spin{display:inline-block;width:16px;height:16px;border:2px solid #fff;border-top-color:transparent;border-radius:50%;animation:rot .8s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes rot{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<!-- 工具栏 -->
<div class="toolbar">
  <span style="font-size:16px;font-weight:bold;color:#fff;margin-right:8px">🐘 小象相册</span>
  <a href="https://github.com/dearmouseuncle" target="_blank" style="font-size:12px;color:#4a9eff;margin-right:12px;text-decoration:none">by dearmouseuncle</a>
  <button onclick="setSort('date')" id="btn-date">按时间</button>
  <button onclick="setSort('size')" id="btn-size">按大小</button>
  <button onclick="setSort('name')" id="btn-name">按名称</button>
  <button onclick="toggleDir()" id="btn-dir">倒序</button>
  <button onclick="doRefresh()">刷新</button>
  <button onclick="enterClean()" style="background:#2a5a2a;border-color:#3a7a3a;color:#8f8">快速清理</button>
  <button onclick="toggleSelectAll()" id="btn-selall">全选</button>
  <button onclick="openSettings()" style="background:#1a3a5a;border-color:#2a5a8a;color:#8cf">设置</button>
  <span class="stats" id="stats">加载中...</span>
</div>
<!-- 筛选栏：类型 + 年月 -->
<div class="filterbar" id="filterbar"></div>
<!-- 瀑布流网格 -->
<div class="grid" id="grid"><div class="loading">加载中...</div></div>
<!-- 多选操作栏 -->
<div class="selbar" id="selbar">
  <span class="cnt" id="selcnt"></span>
  <button class="del" onclick="deleteSelected()">删除选中</button>
  <button class="cancel" onclick="clearSelect()">取消</button>
</div>
<!-- 光箱：大图/视频预览 -->
<div class="lightbox" id="lightbox" onclick="if(event.target===this)closeLB()">
  <div id="lb-content"></div>
  <div class="info" id="lb-info"></div>
  <div class="actions">
    <button class="btn-nav" onclick="nav(-1)">← 上一个</button>
    <button class="btn-del" onclick="deleteCurrent()">删除</button>
    <button class="btn-nav" onclick="nav(1)">下一个 →</button>
    <button class="btn-nav" onclick="enterClean(curIdx);closeLB()">快速清理</button>
    <button class="btn-nav" onclick="revealFile()">打开目录</button>
    <button class="btn-nav" onclick="copyName()">复制文件名</button>
    <button class="btn-nav" onclick="externalPlay()" style="background:#5a3a1a;border-color:#7a5a2a;color:#fc8">外部播放</button>
    <button class="btn-nav" onclick="closeLB()">关闭</button>
  </div>
</div>
<!-- 设置：订阅目录管理 -->
<div class="settings" id="settings" onclick="if(event.target===this)closeSettings()">
  <div class="panel">
    <h2>订阅目录设置</h2>
    <div class="hint">像订阅源一样添加多个照片文件夹，保存后自动扫描。路径支持 Windows 盘符或网络路径。</div>
    <div id="dirlist"></div>
    <div class="addrow">
      <input id="dirinput" placeholder="例如 D:\Photos 或 \\\\NAS\\share\\photos" />
      <button onclick="browseDir()">选择文件夹</button>
      <button onclick="addDir()">添加</button>
    </div>
    <div class="foot">
      <button class="close" onclick="closeSettings()">取消</button>
      <button class="save" onclick="saveSettings()">保存并扫描</button>
    </div>
  </div>
</div>
<!-- 快速清理：全屏逐张审阅 -->
<div class="cleanmode" id="cleanmode">
  <div id="cm-content"></div>
  <div class="cbar">
    <span class="key">↑ 删除</span><span class="key">↓ 保留</span><span class="key">Esc 退出</span>
    <span id="cm-info"></span>
  </div>
</div>
<script>
// ── 状态 ──────────────────────────────────────────────────
let media=[],sortKey='size',sortDir=-1,curIdx=-1,sorted=[],selected=new Set(),shown=0;
let filterYear=0,filterMonth=0,filterType='';
let cfgDirs=[];
const PAGE=200;  // 每批渲染数量，配合无限滚动

// ── 数据加载 ──────────────────────────────────────────────
async function loadMedia(){
  const r=await fetch('/api/media');
  media=await r.json();
  buildFilter();
  render();
}

// ── 筛选栏 ────────────────────────────────────────────────
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

// ── 排序 ──────────────────────────────────────────────────
function setSort(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=-1;}render();}
function toggleDir(){sortDir*=-1;render();}

// ── 渲染 ──────────────────────────────────────────────────
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
  document.getElementById('btn-dir').textContent=sortDir===-1?'倒序':'正序';
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

// 分批渲染，配合滚动到底自动加载
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

// ── 光箱 ──────────────────────────────────────────────────
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

// ── 光箱操作 ──────────────────────────────────────────────
async function revealFile(){
  if(curIdx<0)return;
  await fetch('/api/reveal',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rel:sorted[curIdx].rel})});
}
function copyName(){
  if(curIdx<0)return;
  navigator.clipboard.writeText(sorted[curIdx].name).then(()=>{});
}
async function externalPlay(){
  if(curIdx<0)return;
  await fetch('/api/open',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rel:sorted[curIdx].rel})});
}
async function deleteCurrent(){
  if(curIdx<0)return;
  const m=sorted[curIdx];
  if(!confirm('删除 '+m.name+'？\n'+fmt(m.size)+'\n\n(移到回收站，可恢复)'))return;
  // 先停视频释放文件锁，否则 Windows 不让移动
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

// ── 多选 ──────────────────────────────────────────────────
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
    media=media.filter(x=>!selected.has(x.rel));
    selected.clear();
    render();
  }else alert('删除失败');
}

// ── 快速清理模式 ──────────────────────────────────────────
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

// ── 设置面板 ──────────────────────────────────────────────
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
async function browseDir(){
  const r=await fetch('/api/browse',{method:'POST'});
  const d=await r.json();
  if(d.ok&&d.path){
    document.getElementById('dirinput').value=d.path;
    addDir();
  }
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
  const btn=document.querySelector('.settings .save');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span>扫描中...';
  try{
    const r=await fetch('/api/config',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({dirs:cfgDirs})});
    const d=await r.json();
    if(!d.ok){alert(d.error||'保存失败');return;}
    closeSettings();
    await loadMedia();
  }finally{
    btn.disabled=false;btn.textContent='保存并扫描';
  }
}

// ── 键盘快捷键 ────────────────────────────────────────────
document.addEventListener('keydown',e=>{
  // 快速清理模式
  if(document.getElementById('cleanmode').classList.contains('show')){
    if(e.key==='ArrowUp'){e.preventDefault();cmDelete();}
    else if(e.key==='ArrowDown'){e.preventDefault();cmIdx++;cmShow();}
    else if(e.key==='Escape')closeClean();
    return;
  }
  // 设置面板
  if(document.getElementById('settings').classList.contains('show')){
    if(e.key==='Escape')closeSettings();
    return;
  }
  // 光箱
  if(e.key==='Escape')closeLB();
  else if(e.key==='ArrowLeft')nav(-1);
  else if(e.key==='ArrowRight')nav(1);
  else if(e.key==='Delete'){
    if(curIdx>=0)deleteCurrent();
    else if(selected.size>0)deleteSelected();
  }
});

// ── 工具函数 ──────────────────────────────────────────────
function fmt(b){
  if(b<1024)return b+' B';
  if(b<1048576)return(b/1024).toFixed(1)+' KB';
  if(b<1073741824)return(b/1048576).toFixed(1)+' MB';
  return(b/1073741824).toFixed(2)+' GB';
}
function enc(s){return encodeURIComponent(s);}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}

// ── 无限滚动 ──────────────────────────────────────────────
window.addEventListener('scroll',()=>{
  if(shown<sorted.length&&window.innerHeight+window.scrollY>=document.body.offsetHeight-600)appendBatch();
});

// ── 划过选择（按住鼠标拖过即选中）──────────────────────────
let paint=false;
document.getElementById('grid').addEventListener('mousedown',e=>{
  if(e.button!==0)return;
  paint=true;
  e.preventDefault();
});
document.addEventListener('mousemove',e=>{
  if(!paint)return;
  const el=document.elementFromPoint(e.clientX,e.clientY);
  const card=el&&el.closest?el.closest('.card'):null;
  if(card){
    const idx=+card.dataset.idx;const m=sorted[idx];if(!m)return;
    if(!selected.has(m.rel)){
      selected.add(m.rel);
      card.classList.add('sel');
      const chk=card.querySelector('.chk');
      if(chk){chk.classList.add('on');chk.textContent='✓';}
      updateSelBar();
    }
  }
});
document.addEventListener('mouseup',()=>{paint=false;});

// 启动
loadMedia();
</script>
</body>
</html>"""


# ── 启动 ──────────────────────────────────────────────────
def main():
    global media_list
    THUMBS.mkdir(parents=True, exist_ok=True)
    TRASH.mkdir(parents=True, exist_ok=True)

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

    # 启动 HTTP 服务
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"小象相册: {url}")
    print("快捷键: ←→ 翻页 | Delete 删除 | Esc 关闭")
    print("关闭程序: 按 Ctrl+C，或直接关掉本窗口")

    # 等服务就绪再开浏览器，避免白屏
    def open_when_ready():
        for _ in range(50):
            try:
                urllib.request.urlopen(url, timeout=1)
                break
            except Exception:
                time.sleep(0.3)
        webbrowser.open(url)

    threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n退出")


if __name__ == "__main__":
    main()
