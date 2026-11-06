# -*- coding: utf-8 -*-
"""keyserver.py —— 小千问 API 中转 + key 管理 + 用量统计

它坐在 llama-server(8080) 前面，监听 8090：
  朋友用 http://你的IP:8090/v1  +  你发的 key
  你打开 http://你的IP:8090/admin?pw=管理密码 看用量

启动：python keyserver.py
"""

import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "http://127.0.0.1:8080"
PORT = int(os.environ.get("PORT", "8090"))
BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "ai-web", "keys.json")


def load():
    try:
        with open(DB, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    if "admin" not in d:
        d["admin"] = secrets.token_urlsafe(12)
        d.setdefault("keys", {})
        save(d)
        print("管理密码（请记下）:", d["admin"])
    d.setdefault("keys", {})
    return d


def save(d):
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with open(DB, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)



def ensure_local_model():
    """本地模型被空闲卸载时，转发前自动拉起"""
    import time as _t
    try:
        with urllib.request.urlopen(UPSTREAM + "/health", timeout=3) as r:
            if r.status == 200:
                return True
    except Exception:
        pass
    try:
        urllib.request.urlopen("http://127.0.0.1:8081/ensure?kind=big", timeout=300).read()
    except Exception:
        return False
    for _ in range(90):
        _t.sleep(1)
        try:
            with urllib.request.urlopen(UPSTREAM + "/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
    return False



def count_usage(blob, content_type):
    """从 JSON 或 SSE 里提取 total_tokens"""
    txt = blob.decode("utf-8", "replace") if isinstance(blob, (bytes, bytearray)) else str(blob)
    best = 0
    if "text/event-stream" in (content_type or ""):
        for line in txt.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                j = json.loads(payload)
                u = j.get("usage") or {}
                t = int(u.get("total_tokens") or 0)
                if t > best:
                    best = t
            except Exception:
                pass
        return best
    try:
        j = json.loads(txt)
        return int((j.get("usage") or {}).get("total_tokens") or 0)
    except Exception:
        return 0


def bump_key(key, tokens):
    """累计 tokens / 请求数 / 最后时间 / 今日用量"""
    import time as _t
    d = load()
    rec = d["keys"].get(key)
    if not rec:
        return
    now = int(_t.time())
    day = _t.strftime("%Y-%m-%d", _t.localtime(now))
    rec["tokens"] = rec.get("tokens", 0) + int(tokens or 0)
    rec["requests"] = rec.get("requests", 0) + 1
    rec["last"] = now
    daily = rec.get("daily") or {}
    daily[day] = daily.get(day, 0) + int(tokens or 0)
    rec["daily"] = dict(list(daily.items())[-14:])
    save(d)



# ============ 模型下载中心 ============
import threading

DL_DIR = os.path.join("F:" + os.sep, "AI模型", "下载")
_DL = {}


def _dl_dir_ok():
    try:
        os.makedirs(DL_DIR, exist_ok=True)
        return True
    except Exception:
        return False


def _dl_progress(name, proc, path):
    """后台线程：定期记录文件大小"""
    import time as _t
    while True:
        rec = _DL.get(name)
        if rec is None:
            return
        try:
            rec["bytes"] = os.path.getsize(path) if os.path.exists(path) else 0
        except Exception:
            rec["bytes"] = 0
        rc = proc.poll()
        if rc is not None:
            rec["done"] = True
            rec["ok"] = (rc == 0)
            rec["error"] = "" if rc == 0 else ("curl exit %s" % rc)
            try:
                rec["bytes"] = os.path.getsize(path)
            except Exception:
                pass
            return
        _t.sleep(1)


def start_download(url, name):
    if not url.startswith("http"):
        return None, "url 必须以 http 开头"
    if not _dl_dir_ok():
        return None, "无法创建下载目录 %s" % DL_DIR
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "model.gguf"
    path = os.path.join(DL_DIR, safe)
    try:
        proc = subprocess.Popen(["curl.exe", "-L", "-C", "-", "--retry", "3", "-o", path, url],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return None, "启动失败：%s" % exc
    _DL[safe] = {"name": safe, "path": path, "url": url, "bytes": 0, "done": False,
                 "ok": False, "error": "", "started": int(time.time())}
    threading.Thread(target=_dl_progress, args=(safe, proc, path), daemon=True).start()
    return safe, None



# ============ 能力探测 & 代码保护 ============
_PROBE = {"ts": 0, "data": None}


def probe_caps(up=None):
    """探测上游支持什么：模型列表 / 生图 / 语音 / 视频"""
    import time as _t
    if not up and _PROBE["data"] and (_t.time() - _PROBE["ts"] < 60):
        return _PROBE["data"]
    base = (up or UPSTREAM).rstrip("/")
    caps = {"models": [], "image": False, "audio": False, "video": False, "chat": True, "up": base}

    def try_get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=8) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception:
            return 0, ""

    sc, body = try_get("/models")
    if sc == 200:
        try:
            j = json.loads(body)
            caps["models"] = [m.get("id") for m in (j.get("data") or []) if m.get("id")]
        except Exception:
            pass

    def try_post(path, payload):
        try:
            req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return 0

    # 生图：404/405 视为不支持；其它（200/400/401/422）说明端点存在
    s = try_post("/images/generations", {"prompt": "test", "n": 1, "size": "256x256"})
    caps["image"] = s in (200, 201, 400, 401, 422)
    s2 = try_post("/video/generations", {"prompt": "test"})
    caps["video"] = s2 in (200, 201, 400, 401, 422)
    s3 = try_post("/audio/transcriptions", {})
    caps["audio"] = s3 in (200, 400, 401, 422)

    if not up:
        _PROBE["data"] = caps
        _PROBE["ts"] = _t.time()
    return caps


def minify_html(html):
    """去掉注释、多余空白，减少可读性（不是真加密）"""
    import re as _re
    h = _re.sub(r"<!--(?!\[if).*?-->", "", html, flags=_re.S)
    h = _re.sub(r"/\*(?!\*).*?\*/", "", h, flags=_re.S)
    h = _re.sub(r"\n\s*\n+", "\n", h)
    h = _re.sub(r"^\s+", "", h, flags=_re.M)
    return h



# ============ 客户端后端能力 ============
TOOLS_API = "http://127.0.0.1:8081"


def _get_json(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def tools_run(name, args, level="workspace", workspace=""):
    try:
        body = json.dumps({"name": name, "args": args, "level": level, "workspace": workspace},
                          ensure_ascii=False).encode()
        req = urllib.request.Request(TOOLS_API + "/tool", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        return {"error": "工具服务不可用：%s" % exc}


def models_list():
    sc, body = _get_json(TOOLS_API + "/models", 10)
    if sc == 200:
        try:
            return json.loads(body)
        except Exception:
            pass
    return {"models": [], "gguf": []}


def model_add(name, path, ctx=8192, mmproj=None, ngl=99):
    try:
        body = json.dumps({"name": name, "path": path, "ctx": ctx,
                           "mmproj": mmproj}, ensure_ascii=False).encode()
        req = urllib.request.Request(TOOLS_API + "/models", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def hub_search(src, q, limit=20):
    """四个来源的搜索"""
    src = (src or "modelscope").lower()
    q = (q or "").strip()
    out = []

    if src == "ollama":
        # 本地已装
        sc, body = _get_json("http://127.0.0.1:11434/api/tags", 5)
        if sc == 200:
            try:
                for m in (json.loads(body).get("models") or []):
                    out.append({"id": m.get("name"), "name": m.get("name"),
                                "size": m.get("size", 0), "installed": True, "src": "ollama"})
            except Exception:
                pass
        # 在线库（HTML 解析）
        import re as _re
        sc2, html = _get_json("https://ollama.com/search?q=" + urllib.parse.quote(q), 15)
        if sc2 == 200:
            for m in _re.findall(r'href="/library/([a-zA-Z0-9_.\-]+)"', html)[:limit]:
                if not any(x["id"] == m for x in out):
                    out.append({"id": m, "name": m, "size": 0, "installed": False, "src": "ollama"})

    elif src == "lmstudio":
        for port in (1234, 1235):
            sc, body = _get_json("http://127.0.0.1:%d/v1/models" % port, 5)
            if sc == 200:
                try:
                    for m in (json.loads(body).get("data") or []):
                        out.append({"id": m.get("id"), "name": m.get("id"),
                                    "size": 0, "installed": True, "src": "lmstudio"})
                except Exception:
                    pass
                break
        # 本地模型目录扫描
        root = os.path.expanduser(r"~/.lmstudio/models")
        if os.path.isdir(root):
            for dp, _dn, fn in os.walk(root):
                for f in fn:
                    if f.endswith(".gguf"):
                        out.append({"id": os.path.relpath(os.path.join(dp, f), root),
                                    "name": f, "size": os.path.getsize(os.path.join(dp, f)),
                                    "installed": True, "src": "lmstudio"})

    elif src == "github":
        sc, body = _get_json("https://api.github.com/search/repositories?q=" +
                             urllib.parse.quote(q + " gguf") + "&sort=stars&per_page=" + str(limit), 20,
                             {"User-Agent": "xiaoqianwen"})
        if sc == 200:
            try:
                for it in (json.loads(body).get("items") or []):
                    out.append({"id": it["full_name"], "name": it["full_name"],
                                "desc": (it.get("description") or "")[:80],
                                "stars": it.get("stargazers_count", 0), "src": "github",
                                "url": it.get("html_url")})
            except Exception:
                pass

    elif src == "hf":
        sc, body = _get_json("https://hf-mirror.com/api/models?search=" +
                             urllib.parse.quote(q) + "&limit=" + str(limit), 20)
        if sc == 200:
            try:
                for it in json.loads(body):
                    out.append({"id": it.get("id"), "name": it.get("id"),
                                "downloads": it.get("downloads", 0), "src": "hf"})
            except Exception:
                pass

    elif src == "modelscope":
        sc, body = _get_json("https://modelscope.cn/api/v1/dolphin/models?PageSize=%d&PageNumber=1&Search=%s"
                             % (limit, urllib.parse.quote(q)), 20)
        if sc == 200:
            try:
                d = json.loads(body)
                items = ((d.get("Data") or {}).get("Model") or {}).get("Models") or []
                for it in items:
                    out.append({"id": it.get("Path") or it.get("Name"), "name": it.get("Name"),
                                "desc": (it.get("Description") or "")[:80], "src": "modelscope",
                                "downloads": it.get("Downloads", 0)})
            except Exception:
                pass

    return out[:limit]


def hub_files(src, repo):
    """列出仓库里所有可下载文件：gguf 量化版 + safetensors 完整版 + 配置"""
    files = []
    repo = (repo or "").strip().strip("/")
    if not repo:
        return files
    SKIP = (".gitattributes", ".md", ".png", ".jpg", ".jpeg", ".gif", ".webp")
    if src == "modelscope":
        sc, body = _get_json("https://modelscope.cn/api/v1/models/%s/repo/files?Revision=master&Recursive=true"
                             % repo, 25)
        if sc == 200:
            try:
                for f in (json.loads(body).get("Data") or {}).get("Files") or []:
                    p = f.get("Path") or ""
                    if not p or p.lower().endswith(SKIP):
                        continue
                    low = p.lower()
                    files.append({"name": p, "mb": round((f.get("Size") or 0) / 1048576, 1),
                                  "url": "https://modelscope.cn/models/%s/resolve/master/%s" % (repo, p),
                                  "gguf": low.endswith(".gguf"),
                                  "weight": low.endswith((".gguf", ".safetensors", ".bin", ".pt", ".pth"))})
            except Exception:
                pass
    else:
        sc, body = _get_json("https://hf-mirror.com/api/models/%s/tree/main" % repo, 25)
        if sc == 200:
            try:
                for f in json.loads(body):
                    p = f.get("path") or ""
                    if not p or p.lower().endswith(SKIP):
                        continue
                    low = p.lower()
                    size = f.get("size") or 0
                    files.append({"name": p, "mb": round(size / 1048576, 1),
                                  "url": "https://hf-mirror.com/%s/resolve/main/%s" % (repo, p),
                                  "gguf": low.endswith(".gguf"),
                                  "weight": low.endswith((".gguf", ".safetensors", ".bin", ".pt", ".pth"))})
            except Exception:
                pass
    files.sort(key=lambda x: (not x["gguf"], not x["weight"], -x["mb"]))
    return files[:300]



# ============ 插件目录 & 多线程下载 ============
PLUGIN_DIR = os.path.join(BASE, "客户端", "plugins")


def list_plugins():
    """读取 plugins 目录里的 .json 插件"""
    out = []
    try:
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        for fn in sorted(os.listdir(PLUGIN_DIR)):
            if not fn.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(PLUGIN_DIR, fn), "r", encoding="utf-8-sig") as f:
                    d = json.load(f)
                d["_file"] = fn
                out.append(d)
            except Exception as exc:
                out.append({"name": fn, "_file": fn, "error": str(exc)[:60]})
    except Exception:
        pass
    return out


def _seg_download(url, path, workers=4):
    """多线程分段下载；服务器不支持 Range 时退回单线程"""
    import threading
    total = 0
    try:
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=20) as r:
            total = int(r.headers.get("Content-Length") or 0)
            accept = (r.headers.get("Accept-Ranges") or "").lower()
    except Exception:
        return False
    if total <= 0 or "bytes" not in accept:
        return False
    part = total // workers
    pieces = []
    def worker(i, start, end):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Range": "bytes=%d-%d" % (start, end)})
            with urllib.request.urlopen(req, timeout=120) as r:
                with open(path + ".part%d" % i, "wb") as f:
                    while True:
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
        except Exception:
            pass
    threads = []
    for i in range(workers):
        start = i * part
        end = total - 1 if i == workers - 1 else (start + part - 1)
        t = threading.Thread(target=worker, args=(i, start, end), daemon=True)
        t.start(); threads.append(t)
    for t in threads:
        t.join()
    try:
        with open(path, "wb") as out:
            for i in range(workers):
                piece = path + ".part%d" % i
                if os.path.exists(piece):
                    with open(piece, "rb") as pf:
                        while True:
                            buf = pf.read(1048576)
                            if not buf:
                                break
                            out.write(buf)
                    os.remove(piece)
        return True
    except Exception:
        return False



# ============ 训练任务 ============
TRAIN_DIR = os.path.join(BASE, "训练")
_TRAIN = {"proc": None, "log": "", "status": "idle", "started": 0, "cmd": ""}


def _train_reader(proc, logpath):
    try:
        with open(logpath, "a", encoding="utf-8") as lf:
            for line in proc.stdout:
                lf.write(line); lf.flush()
                _TRAIN["log"] = (_TRAIN["log"] + line)[-6000:]
    except Exception:
        pass
    proc.wait()
    _TRAIN["status"] = "done" if proc.returncode == 0 else "failed"
    _TRAIN["returncode"] = proc.returncode


def train_start(cmd):
    import time as _t
    if _TRAIN["proc"] and _TRAIN["proc"].poll() is None:
        return None, "已有训练在跑"
    os.makedirs(TRAIN_DIR, exist_ok=True)
    logpath = os.path.join(TRAIN_DIR, "train.log")
    open(logpath, "w", encoding="utf-8").close()
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=TRAIN_DIR,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return None, str(exc)
    _TRAIN.update({"proc": proc, "log": "", "status": "running",
                   "started": int(_t.time()), "cmd": cmd, "returncode": None})
    import threading as _th
    _th.Thread(target=_train_reader, args=(proc, logpath), daemon=True).start()
    return True, None


def train_stop():
    pr = _TRAIN.get("proc")
    if pr and pr.poll() is None:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pr.pid)],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        _TRAIN["status"] = "stopped"
        return True
    return False


DATASETS = [
    {"n": "Alpaca 中文 5 万", "id": "silk-road/alpaca-data-gpt4-chinese",
     "u": "https://hf-mirror.com/silk-road/alpaca-data-gpt4-chinese/resolve/main/alpaca_gpt4_data_zh.json",
     "f": "alpaca_zh.json", "size": "37 MB", "desc": "中文指令微调，问答对"},
    {"n": "Firefly 中文指令 1.1M", "id": "YeungNLP/firefly-train-1.1M",
     "u": "https://hf-mirror.com/YeungNLP/firefly-train-1.1M/resolve/main/firefly-train-1.1M.jsonl",
     "f": "firefly.jsonl", "size": "1.1 GB", "desc": "大规模中文指令集"},
    {"n": "ShareGPT 中文 90K", "id": "shareAI/ShareGPT-Chinese-English-90k",
     "u": "https://hf-mirror.com/shareAI/ShareGPT-Chinese-English-90k/resolve/main/sharegpt_cn.json",
     "f": "sharegpt_cn.json", "size": "约 200 MB", "desc": "多轮对话，最贴近真实聊天"},
    {"n": "Dolly 中文 15K", "id": "databricks/databricks-dolly-15k",
     "u": "https://hf-mirror.com/databricks/databricks-dolly-15k/resolve/main/databricks-dolly-15k.jsonl",
     "f": "dolly15k.jsonl", "size": "12 MB", "desc": "英文高质量指令，可翻译用"},
    {"n": "数学推理 GSM8K", "id": "meta-math/GSM8K_zh",
     "u": "https://hf-mirror.com/meta-math/GSM8K_zh/resolve/main/train.json",
     "f": "gsm8k_zh.json", "size": "5 MB", "desc": "中文小学数学应用题"},
    {"n": "代码指令 CodeAlpaca", "id": "sahil2801/CodeAlpaca-20k",
     "u": "https://hf-mirror.com/sahil2801/CodeAlpaca-20k/resolve/main/code_alpaca_20k.json",
     "f": "code_alpaca.json", "size": "10 MB", "desc": "代码生成/解释指令"},
    {"n": "COIG 中文开放指令", "id": "BAAI/COIG",
     "u": "https://hf-mirror.com/BAAI/COIG/resolve/main/README.md",
     "f": "coig_readme.md", "size": "—", "desc": "需按子文件挑选（说明）"},
    {"n": "OpenHermes 2.5", "id": "teknium/OpenHermes-2.5",
     "u": "https://hf-mirror.com/teknium/OpenHermes-2.5/resolve/main/README.md",
     "f": "openhermes_readme.md", "size": "—", "desc": "英文通用指令（说明）"},
    {"n": "LIMA 精选 1K", "id": "GAIR/lima",
     "u": "https://hf-mirror.com/GAIR/lima/resolve/main/lima.json",
     "f": "lima.json", "size": "2 MB", "desc": "极少但极精，对齐风格"},
]


# ============ 社区（P2P） ============
PEER_FILE = os.path.join(BASE, "ai-web", "peers.json")
_INBOX = []


def _load_peers():
    try:
        with open(PEER_FILE, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {"me": "", "friends": []}


def _save_peers(d):
    try:
        with open(PEER_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False



# ============ 训练向导 ============
MODES = {
    "pt":     {"name": "预训练 PT", "desc": "从零开始学语言（需要海量文本，个人不推荐）", "data": "纯文本"},
    "cpt":    {"name": "继续预训练 CPT", "desc": "在现有模型上灌领域文本，注入知识", "data": "纯文本"},
    "sft":    {"name": "监督微调 SFT", "desc": "问答对训练，改说话风格和格式（最常用）", "data": "问答对"},
    "dpo":    {"name": "偏好对齐 DPO", "desc": "好答案 vs 坏答案，调语气和倾向", "data": "偏好对"},
}


def check_env():
    """检测训练环境"""
    import shutil as _sh
    env = {"python": sys.version.split()[0] if "sys" in globals() else "",
           "torch": False, "transformers": False, "peft": False,
           "llama_finetune": bool(_sh.which("llama-finetune")),
           "llamafactory": bool(_sh.which("llamafactory-cli")),
           "nvidia": False, "gpu": ""}
    try:
        import torch  # noqa
        env["torch"] = True
        try:
            env["nvidia"] = bool(torch.cuda.is_available())
            if env["nvidia"]:
                env["gpu"] = torch.cuda.get_device_name(0)
        except Exception:
            pass
    except Exception:
        pass
    for mod in ("transformers", "peft"):
        try:
            __import__(mod)
            env[mod] = True
        except Exception:
            pass
    return env


def save_upload(name, b64):
    import base64 as _b64
    os.makedirs(TRAIN_DIR, exist_ok=True)
    safe = "".join(ch for ch in (name or "data.json") if ch not in '\\/:*?"<>|').strip() or "data.json"
    path = os.path.join(TRAIN_DIR, safe)
    try:
        data = _b64.decode("utf-8") if isinstance(b64, str) else bytes(b64)
        with open(path, "wb") as f:
            f.write(_b64.b64decode(data))
        return path, len(data)
    except Exception as exc:
        return None, str(exc)


def make_plan(req):
    """生成 LLaMA-Factory 配置 + 启动脚本"""
    mode = req.get("mode") or "sft"
    model = req.get("model") or ""
    dataset = req.get("dataset") or "data.json"
    stage = {"pt": "pt", "cpt": "pt", "sft": "sft", "dpo": "dpo"}.get(mode, "sft")
    r = int(req.get("lora_rank") or 16)
    ep = float(req.get("epochs") or 3)
    lr = req.get("lr") or "2e-4"
    bs = int(req.get("batch") or 1)
    cut = int(req.get("cutoff") or 1024)
    os.makedirs(TRAIN_DIR, exist_ok=True)

    yaml = []
    yaml.append("### 由小千问自动生成")
    yaml.append("stage: %s" % stage)
    yaml.append("do_train: true")
    yaml.append("model_name_or_path: %s" % (model or "./base"))
    yaml.append("dataset: my_data")
    yaml.append("template: qwen")
    yaml.append("finetuning_type: lora")
    yaml.append("lora_rank: %d" % r)
    yaml.append("lora_target: all")
    yaml.append("output_dir: ./out")
    yaml.append("per_device_train_batch_size: %d" % bs)
    yaml.append("gradient_accumulation_steps: 4")
    yaml.append("learning_rate: %s" % lr)
    yaml.append("num_train_epochs: %g" % ep)
    yaml.append("cutoff_len: %d" % cut)
    yaml.append("logging_steps: 10")
    yaml.append("save_steps: 200")
    yaml.append("fp16: true")
    if stage == "dpo":
        yaml.append("pref_beta: 0.1")
    y1 = os.path.join(TRAIN_DIR, "train.yaml")
    with open(y1, "w", encoding="utf-8") as f:
        f.write("\n".join(yaml) + "\n")

    # 数据集登记（LLaMA-Factory 的 dataset_info）
    dj = {"my_data": {"file_name": dataset, "formatting": "alpaca" if stage != "pt" else "text"}}
    y2 = os.path.join(TRAIN_DIR, "dataset_info.json")
    with open(y2, "w", encoding="utf-8") as f:
        json.dump(dj, f, ensure_ascii=False, indent=2)

    bat = os.path.join(TRAIN_DIR, "开始训练.bat")
    lines = ["@echo off", "chcp 65001 >nul", "cd /d %~dp0",
             "echo 需要先安装 LLaMA-Factory：pip install llamafactory",
             "llamafactory-cli train train.yaml",
             "pause"]
    with open(bat, "w", encoding="utf-8") as f:
        f.write("\r\n".join(lines))

    return {"yaml": y1, "dataset_info": y2, "bat": bat, "stage": stage, "mode_name": MODES.get(mode, {}).get("name", mode)}



# ============ 批量下载（单任务 / 可取消 / 可选目录） ============
_DL2 = {}
_dl_seq = [0]


def _dl2_worker(key):
    import time as _t
    import traceback as _tb
    rec = _DL2.get(key)
    if not rec:
        return
    try:
        _dl2_worker_inner(rec)
    except Exception:
        rec["state"] = "failed"
        rec["error"] = _tb.format_exc()[-300:]


def _dl2_worker_inner(rec):
    import time as _t
    try:
        os.makedirs(rec["dir"], exist_ok=True)
    except Exception:
        rec["dir"] = DL_DIR
        try:
            os.makedirs(DL_DIR, exist_ok=True)
        except Exception:
            rec["state"] = "failed"; rec["error"] = "无法创建下载目录"; return
    for i, f in enumerate(rec["files"]):
        if rec.get("cancel"):
            rec["state"] = "cancelled"
            return
        rec["current"] = f["name"]
        rec["index"] = i + 1
        path = os.path.join(rec["dir"], f["name"])
        try:
            proc = subprocess.Popen(["curl.exe", "-L", "-C", "-", "--retry", "3", "-o", path, f["url"]],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:
            rec["state"] = "failed"; rec["error"] = str(exc)[:80]; return
        rec["proc"] = proc
        while proc.poll() is None:
            if rec.get("cancel"):
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass
                rec["state"] = "cancelled"
                return
            try:
                sz = os.path.getsize(path) if os.path.exists(path) else 0
            except Exception:
                sz = 0
            now = _t.time()
            prev = rec.get("_last_bytes", 0)
            rec["speed"] = max(0, (rec["bytes"] + sz - prev)) / max(0.2, now - rec.get("_last_t", now - 0.2))
            rec["_last_bytes"] = rec["bytes"] + sz
            rec["_last_t"] = now
            rec["file_bytes"] = sz
            _t.sleep(1)
        try:
            rec["bytes"] += os.path.getsize(path)
        except Exception:
            pass
    if rec.get("cancel"):
        rec["state"] = "cancelled"
        rec["speed"] = 0
        return
    rec["state"] = "done"
    rec["speed"] = 0


def _new_job(key, rec):
    _DL2[key] = rec
    import threading as _th
    _th.Thread(target=_dl2_worker, args=(key,), daemon=True).start()



# ============ 账号 / 兑换码 / 社区 / 公告 ============
import hashlib as _hash
import secrets as _secrets

USER_FILE = os.path.join(BASE, "ai-web", "users.json")
CODE_FILE = os.path.join(BASE, "ai-web", "codes.json")
POST_FILE = os.path.join(BASE, "ai-web", "posts.json")
ANNOUNCE_FILE = os.path.join(BASE, "ai-web", "announce.json")
MEDIA_DIR = os.path.join(BASE, "社区", "uploads")
TOKENS = {}
APP_VERSION = "1.8.0"
CHANGELOG = [
    {"v": "1.8.0", "date": "2026-11-06", "items": [
        "社区改版：发帖 / 发图 / 发视频 / 评论 / 点赞",
        "账号系统：邮箱注册登录、兑换码",
        "远程更新与更新公告",
        "下载任务可逐个清除",
        "文件权限三档 + 完全权限二次确认"]},
    {"v": "1.7.0", "date": "2026-11-06", "items": [
        "GGUF 按模型归组，一个模型一个下拉",
        "下载取消状态修复", "数据集下载走批量接口"]},
]


def _jload(path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _jsave(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _pw_hash(pw, salt):
    return _hash.sha256((salt + pw).encode("utf-8")).hexdigest()


def user_register(email, pw, name):
    email = (email or "").strip().lower()
    if "@" not in email or len(pw or "") < 6:
        return None, "邮箱格式不对或密码少于 6 位"
    users = _jload(USER_FILE, {})
    if email in users:
        return None, "这个邮箱已注册"
    salt = _secrets.token_hex(8)
    users[email] = {"email": email, "name": (name or email.split("@")[0]),
                    "salt": salt, "pw": _pw_hash(pw, salt),
                    "created": int(time.time()), "credits": 100, "tier": "free",
                    "avatar": ""}
    _jsave(USER_FILE, users)
    return users[email], None


def user_login(email, pw):
    email = (email or "").strip().lower()
    users = _jload(USER_FILE, {})
    u = users.get(email)
    if not u:
        return None, "邮箱未注册"
    if u["pw"] != _pw_hash(pw, u["salt"]):
        return None, "密码错误"
    tok = _secrets.token_hex(16)
    TOKENS[tok] = email
    return {"token": tok, "user": u}, None


def user_by_token(tok):
    email = TOKENS.get(tok)
    if not email:
        return None
    return _jload(USER_FILE, {}).get(email)


def redeem(email, code):
    code = (code or "").strip().upper()
    codes = _jload(CODE_FILE, {})
    c = codes.get(code)
    if not c:
        return None, "兑换码不存在"
    used_by = c.get("used_by") or []
    if isinstance(used_by, str):
        used_by = [used_by] if used_by else []
    limit = int(c.get("limit") or 1)
    if email in used_by:
        return None, "你已经用过这个兑换码了"
    if len(used_by) >= limit:
        return None, "这个兑换码已用完"
    users = _jload(USER_FILE, {})
    u = users.get(email)
    if not u:
        return None, "用户不存在"
    u["credits"] = u.get("credits", 0) + int(c.get("credits", 0))
    if c.get("tier"):
        u["tier"] = c["tier"]
    users[email] = u
    _jsave(USER_FILE, users)
    used_by.append(email)
    c["used_by"] = used_by
    c["used_at"] = int(time.time())
    codes[code] = c
    _jsave(CODE_FILE, codes)
    return {"credits": c.get("credits", 0), "tier": c.get("tier", u.get("tier"))}, None


def gen_codes(n, credits, tier, note, limit=1):
    codes = _jload(CODE_FILE, {})
    out = []
    for _ in range(int(n)):
        code = "XQW-" + _secrets.token_hex(4).upper()
        codes[code] = {"credits": credits, "tier": tier, "note": note,
                       "created": int(time.time()), "used_by": [], "limit": int(limit)}
        out.append(code)
    _jsave(CODE_FILE, codes)
    return out


def posts_load():
    return _jload(POST_FILE, {"posts": []})


def posts_save(d):
    return _jsave(POST_FILE, d)


def media_save(name, b64):
    import base64 as _b
    os.makedirs(MEDIA_DIR, exist_ok=True)
    safe = "".join(ch for ch in (name or "file.bin") if ch not in '\\/:*?"<>|').strip()
    safe = "%d_%s" % (int(time.time() * 1000), safe or "file.bin")
    with open(os.path.join(MEDIA_DIR, safe), "wb") as f:
        f.write(_b.b64decode(b64))
    return safe



# ============ 邮箱验证码 ============
MAIL_FILE = os.path.join(BASE, "ai-web", "mail.json")
_VCODES = {}


def mail_cfg():
    d = _jload(MAIL_FILE, {})
    return d if isinstance(d, dict) else {}


def send_mail(to, subject, body):
    """配置了 SMTP 就发真邮件，否则返回 None 表示本地模式"""
    cfg = mail_cfg()
    if not cfg.get("host") or not cfg.get("user") or not cfg.get("pass"):
        return None
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg.get("user")
    msg["To"] = to
    try:
        port = int(cfg.get("port") or (465 if cfg.get("ssl") else 587))
        if cfg.get("ssl"):
            s = smtplib.SMTP_SSL(cfg["host"], port, timeout=20)
        else:
            s = smtplib.SMTP(cfg["host"], port, timeout=20)
            s.starttls()
        s.login(cfg["user"], cfg["pass"])
        s.sendmail(cfg["user"], [to], msg.as_string())
        s.quit()
        return True
    except Exception as exc:
        return "ERR:" + str(exc)[:120]


def make_vcode(email):
    code = "%06d" % _secrets.randbelow(1000000)
    _VCODES[email.lower()] = {"code": code, "exp": int(time.time()) + 600}
    return code


def check_vcode(email, code):
    rec = _VCODES.get((email or "").lower())
    if not rec:
        return False, "请先获取验证码"
    if int(time.time()) > rec["exp"]:
        return False, "验证码已过期"
    if str(code).strip() != rec["code"]:
        return False, "验证码不正确"
    _VCODES.pop((email or "").lower(), None)
    return True, None



# ============ 注册模式 & 邀请码 ============
CONF_FILE = os.path.join(BASE, "ai-web", "appconf.json")


def app_conf():
    d = _jload(CONF_FILE, {})
    if not isinstance(d, dict) or not d:
        d = {}
    d.setdefault("register_mode", "email")   # email | invite | open
    return d


def save_conf(d):
    return _jsave(CONF_FILE, d)


def invite_check(code):
    codes = _jload(CODE_FILE, {})
    c = codes.get((code or "").strip().upper())
    if not c or c.get("kind") != "invite":
        return False, "邀请码不存在"
    used = c.get("used_by") or []
    if isinstance(used, str):
        used = [used] if used else []
    if len(used) >= int(c.get("limit") or 1):
        return False, "邀请码已用完"
    return True, None


def invite_consume(code, email):
    codes = _jload(CODE_FILE, {})
    key = (code or "").strip().upper()
    c = codes.get(key)
    if not c:
        return
    used = c.get("used_by") or []
    if isinstance(used, str):
        used = [used] if used else []
    used.append(email)
    c["used_by"] = used
    codes[key] = c
    _jsave(CODE_FILE, codes)




def bootstrap_files():
    """首次运行时自动创建必要的数据文件（部署到云时用）"""
    try:
        d = os.path.join(BASE, "ai-web")
        os.makedirs(d, exist_ok=True)
        kp = os.path.join(d, "keys.json")
        if not os.path.exists(kp):
            with open(kp, "w", encoding="utf-8") as f:
                json.dump({"admin": secrets.token_urlsafe(9),
                           "keys": {"sk-" + secrets.token_hex(16): {"note": "默认", "tokens": 0,
                                                                    "requests": 0, "created": int(time.time())}}},
                          f, ensure_ascii=False, indent=2)
        for name, val in (("users.json", {}), ("codes.json", {}), ("posts.json", {"posts": []}),
                          ("announce.json", []), ("appconf.json", {"register_mode": "email"})):
            fp = os.path.join(d, name)
            if not os.path.exists(fp):
                with open(fp, "w", encoding="utf-8") as f:
                    json.dump(val, f, ensure_ascii=False)
        os.makedirs(os.path.join(BASE, "社区", "uploads"), exist_ok=True)
        os.makedirs(os.path.join(BASE, "训练"), exist_ok=True)
    except Exception:
        pass


bootstrap_files()

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass


    def _gzip_ok(self):
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _send_blob(self, blob, ctype, cache="no-store"):
        import gzip as _gz
        is_text = ("text/" in ctype) or ("json" in ctype) or ("javascript" in ctype) or ("svg" in ctype)
        if is_text and len(blob) > 400 and self._gzip_ok():
            try:
                blob = _gz.compress(blob, 6)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", cache)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(blob)
                return
            except Exception:
                pass
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(blob)

    def _json(self, obj, code=200):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if len(body) > 800 and self._gzip_ok():
                import gzip as _gz2
                body = _gz2.compress(body, 6)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception:
            pass

        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def _key(self):
        a = self.headers.get("Authorization") or ""
        return a[7:].strip() if a.lower().startswith("bearer ") else ""

    def _check(self):
        d = load()
        k = self._key()
        if k not in d["keys"]:
            self._json({"error": {"message": "无效的 API key", "type": "auth"}}, 401)
            return None, None
        rec = d["keys"][k]
        if rec.get("quota") and rec.get("tokens", 0) >= rec["quota"]:
            self._json({"error": {"message": "额度已用完", "type": "quota"}}, 429)
            return None, None
        return d, k

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Upstream")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        d = load()
        # 客户端页面与静态资源（同源托管）
        if u.path in ("/", "/index.html", "/qwen.svg", "/lemon.svg", "/lemon.ico"):
            import mimetypes
            fname = {"": "index.html", "/": "index.html", "/index.html": "index.html",
                     "/qwen.svg": "qwen.svg", "/lemon.svg": "lemon.svg",
                     "/lemon.ico": "lemon.ico"}.get(u.path, "index.html")
            fpath = os.path.join(BASE, "客户端", fname)
            if not os.path.exists(fpath):
                return self._json({"error": "客户端文件缺失：%s" % fpath}, 404)
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()
            # 默认不压缩（压缩会破坏 JS）；需要时加 ?min=1
            if fname == "index.html" and (q.get("min") or [""])[0] == "1":
                text = minify_html(text)
            blob = text.encode("utf-8")
            ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            if ctype.startswith("text") or ctype.endswith("svg+xml"):
                ctype += "; charset=utf-8"
            self._send_blob(blob, ctype, "no-store")
            return

        if u.path.startswith("/admin"):
            pw = (q.get("pw") or [""])[0].strip()
            if pw != d["admin"]:
                page = """<!DOCTYPE html><html lang=zh><head><meta charset=utf-8><title>小千问 Key 管理</title>
<style>body{background:#0e1116;color:#e6edf3;font-family:'Microsoft YaHei UI',sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#131a24;border:1px solid #24303f;border-radius:14px;padding:30px 34px;width:330px}
h1{font-size:17px;margin:0 0 18px}input{width:100%%;box-sizing:border-box;background:#0b1017;color:#cfe0f0;
border:1px solid #24303f;border-radius:8px;padding:11px 13px;font-size:14px;outline:none;margin-bottom:12px}
button{width:100%%;background:#2563eb;color:#fff;border:none;border-radius:8px;padding:11px;font-size:14px;cursor:pointer}
.tip{color:#6b7a8d;font-size:12px;margin-top:14px}</style></head><body><div class=box>
<h1>🐋 小千问 · Key 管理</h1><input id=pw placeholder="输入管理密码" autofocus>
<button onclick="location.search='?pw='+encodeURIComponent(document.getElementById('pw').value.trim())">进入</button>
<div class=tip>密码在 ai-web\keys.json 的 admin 字段</div></div>
<script>document.getElementById('pw').addEventListener('keydown',function(e){if(e.key==='Enter')document.querySelector('button').click()});</script>
</body></html>"""
                b2 = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b2)))
                self.end_headers()
                self.wfile.write(b2)
                return

            import time as _t
            today = _t.strftime("%Y-%m-%d")
            rows = [{"key": k, "note": v.get("note", ""), "tokens": v.get("tokens", 0),
                     "requests": v.get("requests", 0), "quota": v.get("quota", 0),
                     "last": v.get("last", 0), "today": (v.get("daily") or {}).get(today, 0)}
                    for k, v in d["keys"].items()]
            rows.sort(key=lambda x: -x["tokens"])

            if (q.get("fmt") or [""])[0] == "json":
                return self._json({"ok": True, "keys": rows, "today": today,
                                   "total_tokens": sum(r["tokens"] for r in rows),
                                   "today_tokens": sum(r["today"] for r in rows)})

            import html as _h
            def row_html(r):
                last = r["last"]
                last_s = _t.strftime("%H:%M:%S", _t.localtime(last)) if last else "-"
                return ("<tr><td class=c1>" + _h.escape(r["key"]) + "</td><td>" + _h.escape(r["note"] or "-") +
                        "</td><td class=num>" + str(r["today"]) + "</td><td class=num>" + str(r["tokens"]) +
                        "</td><td class=num>" + str(r["requests"]) + "</td><td class=num>" + last_s +
                        "</td><td class=num>" + str(r["quota"] or "∞") + "</td></tr>")
            body_rows = "".join(row_html(r) for r in rows) or "<tr><td colspan=7 class=empty>还没有 key</td></tr>"
            total = sum(r["tokens"] for r in rows)
            today_tok = sum(r["today"] for r in rows)

            page = """<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>
<title>小千问 Key 管理</title><style>
body{background:#0e1116;color:#e6edf3;font-family:'Microsoft YaHei UI',sans-serif;padding:26px}
h1{font-size:19px;margin:0 0 4px}.sub{color:#6b7a8d;font-size:13px;margin-bottom:16px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%%;background:#3ddc84;margin-right:6px;
animation:pulse 1.6s infinite}@keyframes pulse{0%%{opacity:1}50%%{opacity:.25}100%%{opacity:1}}
input,button{background:#131a24;color:#cfe0f0;border:1px solid #24303f;border-radius:8px;padding:9px 12px;
font-size:13px;outline:none}button{background:#2563eb;color:#fff;border:none;cursor:pointer;font-weight:500}
button:hover{background:#1d4ed8}
table{border-collapse:collapse;width:100%%;margin-top:18px;font-size:13px}
th{text-align:left;color:#8b98a8;font-weight:500;border-bottom:1px solid #24303f;padding:10px 8px}
td{padding:9px 8px;border-bottom:1px solid #1a2230}.num{text-align:right;font-variant-numeric:tabular-nums}
.c1{font-family:monospace;font-size:11.5px;color:#3ddc84}.empty{color:#666;text-align:center}
.meta{margin-top:14px;color:#6b7a8d;font-size:12px}
</style></head><body>
<h1>🐋 小千问 · API Key 管理 <span style="font-size:12px;font-weight:400"><span class=dot></span>实时</span></h1>
<div class=sub>累计 <b style=color:#3ddc84 id=total>""" + str(total) + """</b> tokens ·
今日 <b style=color:#e0c060 id=today>""" + str(today_tok) + """</b> tokens ·""" + str(len(rows)) + """ 个 key</div>
<div><input id=note placeholder="备注（给谁用）" style="width:190px">
<input id=quota placeholder="额度(留空=不限)" style="width:140px">
<button onclick=addKey()>+ 新建 key</button></div>
<table><tr><th>Key</th><th>备注</th><th class=num>今日</th><th class=num>累计</th>
<th class=num>请求</th><th class=num>最后使用</th><th class=num>额度</th></tr>
<tbody id=tb>""" + body_rows + """</tbody></table>
<h2 style="font-size:15px;margin:26px 0 8px">👥 注册方式</h2>
<div><select id=regMode onchange=setMode()>
  <option value=email>邮箱验证码（默认）</option>
  <option value=invite>邀请码（不用邮箱）</option>
  <option value=open>开放注册（不验证）</option>
</select>
<button onclick=genInvites()>+ 生成邀请码</button>
<span class=meta id=invOut></span></div>
<script>
async function setMode(){
  const m=document.getElementById('regMode').value;
  const r=await (await fetch('/admin/appconf',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pw:PW,register_mode:m})})).json();
  alert(r.ok?'已切换为：'+m:'失败：'+(r.error||''));
}
async function genInvites(){
  const r=await (await fetch('/admin/invites',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pw:PW,n:5,limit:1,note:'注册邀请'})})).json();
  if(r.ok){ document.getElementById('invOut').textContent=' 新码：'+r.codes.join(' '); }
  else alert('失败：'+(r.error||''));
}
fetch('/appconf').then(r=>r.json()).then(j=>{document.getElementById('regMode').value=j.register_mode||'email';});
</script>
<h2 style="font-size:15px;margin:26px 0 8px">🎟️ 兑换码管理</h2>
<div><input id=cN placeholder="数量 5" style="width:90px">
<input id=cCredits placeholder="积分 500" style="width:110px">
<input id=cTier placeholder="等级 plus" style="width:110px">
<input id=cLimit placeholder="限量 1" style="width:90px">
<input id=cNote placeholder="备注" style="width:140px">
<button onclick=genCodes()>+ 生成</button></div>
<div id=codeList style="margin-top:12px"></div>
<div class=meta>已用完的兑换码自动从列表消失 · 可改可删</div>
<script>
async function genCodes(){
  const body={admin:PW, n:parseInt(document.getElementById('cN').value||'5'),
    credits:parseInt(document.getElementById('cCredits').value||'500'),
    tier:document.getElementById('cTier').value||'plus',
    limit:parseInt(document.getElementById('cLimit').value||'1'),
    note:document.getElementById('cNote').value||''};
  const r=await (await fetch('/auth/codes',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)})).json();
  if(r.ok){ alert('已生成：\n'+r.codes.join('\n')); loadCodes(); } else alert('失败：'+(r.error||''));
}
async function loadCodes(){
  const r=await (await fetch('/admin/codes?pw='+encodeURIComponent(PW),{cache:'no-store'})).json();
  const box=document.getElementById('codeList');
  if(!r.ok){ box.innerHTML='<div style=color:#e08a8a>'+ (r.error||'读取失败') +'</div>'; return; }
  if(!r.items.length){ box.innerHTML='<div style=color:#666>没有可用兑换码</div>'; return; }
  let h='<table><tr><th>兑换码</th><th>积分</th><th>等级</th><th>限量</th><th>已用</th><th>备注</th><th>操作</th></tr>';
  for(const it of r.items){
    h+='<tr><td class=c1>'+it.code+'</td><td class=num>'+it.credits+'</td><td>'+it.tier+'</td>'+
       '<td class=num>'+it.limit+'</td><td class=num>'+it.used+'</td><td>'+(it.note||'-')+'</td>'+
       '<td><button onclick="editCode(\''+it.code+'\','+it.credits+',\''+it.tier+'\','+it.limit+')">改</button> '+
       '<button onclick="delCode(\''+it.code+'\')">删</button></td></tr>';
  }
  box.innerHTML=h+'</table>';
}
async function editCode(code,credits,tier,limit){
  const c2=prompt('积分',credits); if(c2===null) return;
  const t2=prompt('等级',tier); if(t2===null) return;
  const l2=prompt('限量（可被几个人兑换）',limit); if(l2===null) return;
  const r=await (await fetch('/admin/codes/update',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pw:PW,code:code,credits:parseInt(c2)||0,tier:t2,limit:parseInt(l2)||1})})).json();
  alert(r.ok?'已修改':'失败：'+(r.error||'')); loadCodes();
}
async function delCode(code){
  if(!confirm('删除兑换码 '+code+' ？')) return;
  const r=await (await fetch('/admin/codes/delete',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pw:PW,code:code})})).json();
  alert(r.ok?'已删除':'失败：'+(r.error||'')); loadCodes();
}
loadCodes();
</script>
<div class=meta>朋友连接：http://你的IP:8090/v1 &nbsp; api_key 填上面的 key &nbsp;·&nbsp; 每 3 秒自动刷新</div>
<script>
const PW = new URLSearchParams(location.search).get('pw');
async function addKey(){
  const note = document.getElementById('note').value || '未命名';
  const quota = parseInt(document.getElementById('quota').value || '0') || 0;
  const r = await (await fetch('/admin/new?pw=' + encodeURIComponent(PW), {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({note:note, quota:quota})})).json();
  if (r.ok) { alert('已创建：' + r.key); location.reload(); } else alert('失败：' + (r.error || '未知'));
}
function fmt(ts){ if(!ts) return '-'; const d=new Date(ts*1000);
  return d.toTimeString().slice(0,8); }
async function tick(){
  try {
    const j = await (await fetch('/admin?pw=' + encodeURIComponent(PW) + '&fmt=json', {cache:'no-store'})).json();
    if (!j.ok) return;
    document.getElementById('total').textContent = j.total_tokens;
    document.getElementById('today').textContent = j.today_tokens;
    document.getElementById('tb').innerHTML = j.keys.map(k =>
      '<tr><td class=c1>' + k.key + '</td><td>' + (k.note || '-') + '</td><td class=num>' + k.today +
      '</td><td class=num>' + k.tokens + '</td><td class=num>' + k.requests + '</td><td class=num>' +
      fmt(k.last) + '</td><td class=num>' + (k.quota || '∞') + '</td></tr>').join('')
      || '<tr><td colspan=7 class=empty>还没有 key</td></tr>';
  } catch(e) {}
}
setInterval(tick, 3000);
</script></body></html>"""
            b2 = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b2)))
            self.end_headers()
            self.wfile.write(b2)
            return


        if u.path.startswith("/netinfo"):
            import socket as _sk
            ips = []
            try:
                for info in _sk.getaddrinfo(_sk.gethostname(), None, _sk.AF_INET):
                    ip = info[4][0]
                    if ip not in ips and not ip.startswith("127."):
                        ips.append(ip)
            except Exception:
                pass
            try:
                s2 = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
                s2.connect(("8.8.8.8", 80))
                ip2 = s2.getsockname()[0]
                s2.close()
                if ip2 not in ips and not ip2.startswith("127."):
                    ips.append(ip2)
            except Exception:
                pass
            return self._json({"ok": True, "port": PORT, "ips": ips})
        if u.path.startswith("/appconf"):
            d2 = app_conf()
            return self._json({"ok": True, "register_mode": d2.get("register_mode", "email")})
        if u.path.startswith("/version"):
            return self._json({"ok": True, "version": APP_VERSION, "changelog": CHANGELOG})
        if u.path.startswith("/announce"):
            return self._json({"ok": True, "items": _jload(ANNOUNCE_FILE, [])})
        if u.path.startswith("/auth/me"):
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
            u = user_by_token(tok)
            if not u:
                return self._json({"ok": False, "error": "未登录"}, 401)
            u2 = {k: v for k, v in u.items() if k not in ("pw", "salt")}
            return self._json({"ok": True, "user": u2})
        if u.path.startswith("/community/posts"):
            return self._json({"ok": True, "data": posts_load()})
        if u.path.startswith("/community/media/"):
            name = u.path.split("/community/media/")[-1]
            fp = os.path.join(MEDIA_DIR, name)
            if not os.path.exists(fp):
                return self._json({"error": "not found"}, 404)
            import mimetypes as _mt
            with open(fp, "rb") as f:
                blob = f.read()
            ctype = _mt.guess_type(name)[0] or "application/octet-stream"
            self._send_blob(blob, ctype, "public, max-age=604800, immutable")
            return
        if u.path.startswith("/admin/codes"):
            pw2 = (q.get("pw") or [""])[0]
            try:
                admin_pw = load().get("admin", "")
            except Exception:
                admin_pw = ""
            if pw2 != admin_pw:
                return self._json({"ok": False, "error": "密码错误"}, 403)
            codes = _jload(CODE_FILE, {})
            rows = []
            for k, v in codes.items():
                ub = v.get("used_by") or []
                if isinstance(ub, str):
                    ub = [ub] if ub else []
                lim = int(v.get("limit") or 1)
                if len(ub) >= lim:
                    continue  # 已用完的不显示
                rows.append({"code": k, "credits": v.get("credits", 0), "tier": v.get("tier", ""),
                             "note": v.get("note", ""), "limit": lim, "used": len(ub),
                             "used_by": ub, "created": v.get("created", 0)})
            rows.sort(key=lambda x: -x["created"])
            return self._json({"ok": True, "items": rows})
        if u.path.startswith("/train/env"):
            return self._json({"ok": True, "env": check_env(), "modes": MODES, "dir": TRAIN_DIR})
        if u.path.startswith("/train/status"):
            return self._json({"ok": True, "status": _TRAIN.get("status"), "log": _TRAIN.get("log", ""),
                               "cmd": _TRAIN.get("cmd", ""), "returncode": _TRAIN.get("returncode"),
                               "dir": TRAIN_DIR})
        if u.path.startswith("/datasets"):
            return self._json({"ok": True, "items": DATASETS})
        if u.path.startswith("/peer/inbox"):
            return self._json({"ok": True, "messages": _INBOX[-50:]})
        if u.path.startswith("/peer/me"):
            return self._json({"ok": True, "data": _load_peers()})
        if u.path.startswith("/plugins/open"):
            try:
                os.makedirs(PLUGIN_DIR, exist_ok=True)
                subprocess.Popen(["explorer", PLUGIN_DIR])
                return self._json({"ok": True, "dir": PLUGIN_DIR})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)
        if u.path.startswith("/plugins"):
            return self._json({"ok": True, "dir": PLUGIN_DIR, "items": list_plugins()})
        if u.path.startswith("/hub/search"):
            src = (q.get("src") or ["modelscope"])[0]
            kw = (q.get("q") or [""])[0]
            return self._json({"ok": True, "items": hub_search(src, kw)})
        if u.path.startswith("/hub/files"):
            src = (q.get("src") or ["hf"])[0]
            repo = (q.get("repo") or [""])[0]
            return self._json({"ok": True, "files": hub_files(src, repo)})
        if u.path.startswith("/models/list"):
            return self._json({"ok": True, "data": models_list()})
        if u.path.startswith("/api/probe"):
            up = (q.get("up") or [""])[0]
            return self._json({"ok": True, "caps": probe_caps(up or None)})
        if u.path.startswith("/dl/jobs"):
            items = []
            for k, v in _DL2.items():
                items.append({"key": k, "name": v.get("title") or k, "state": v.get("state"),
                              "index": v.get("index", 0), "total": len(v.get("files", [])),
                              "bytes": v.get("bytes", 0) + v.get("file_bytes", 0),
                              "speed": v.get("speed", 0), "current": v.get("current", ""),
                              "dir": v.get("dir", ""), "error": v.get("error", ""),
                              "file_bytes": v.get("file_bytes", 0)})
            items.sort(key=lambda x: x["key"], reverse=True)
            return self._json({"ok": True, "jobs": items, "dir": DL_DIR})
        if u.path.startswith("/dl/cancel"):
            key = (q.get("key") or [""])[0]
            rec = _DL2.get(key)
            if not rec:
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            rec["cancel"] = True
            rec["state"] = "cancelled"
            try:
                pr = rec.get("proc")
                if pr and pr.poll() is None:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pr.pid)],
                                   capture_output=True, timeout=10)
            except Exception:
                pass
            return self._json({"ok": True})
        if u.path.startswith("/dl/open"):
            target = (q.get("dir") or [DL_DIR])[0]
            try:
                os.makedirs(target, exist_ok=True)
                subprocess.Popen(["explorer", target])
                return self._json({"ok": True, "dir": target})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)
        if u.path.startswith("/dl/status"):
            items = list(_DL.values())
            items.sort(key=lambda x: x.get("started", 0), reverse=True)
            return self._json({"ok": True, "dir": DL_DIR, "items": items})
        if u.path.startswith("/v1/models"):
            try:
                with urllib.request.urlopen(UPSTREAM + "/v1/models", timeout=10) as r:
                    return self._json(json.loads(r.read().decode()))
            except Exception as e:
                return self._json({"error": str(e)}, 502)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        # 流式上传必须最先处理（不能先读进内存）
        if self.path.startswith("/community/upload"):
            u0 = urlparse(self.path)
            q0 = parse_qs(u0.query)
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
            usr = user_by_token(tok)
            if not usr:
                return self._json({"ok": False, "error": "请先登录"}, 401)
            try:
                total = int(self.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            if total <= 0:
                return self._json({"ok": False, "error": "空文件"}, 400)
            if total > 500 * 1024 * 1024:
                return self._json({"ok": False, "error": "超过 500MB"}, 413)
            os.makedirs(MEDIA_DIR, exist_ok=True)
            nm0 = (q0.get("name") or ["file.bin"])[0]
            safe = "".join(ch for ch in nm0 if ch not in '\\/:*?"<>|').strip() or "file.bin"
            fn = "%d_%s" % (int(time.time() * 1000), safe)
            path = os.path.join(MEDIA_DIR, fn)
            left = total
            try:
                with open(path, "wb") as f:
                    while left > 0:
                        chunk = self.rfile.read(min(1048576, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)[:80]}, 500)
            return self._json({"ok": True, "name": fn, "url": "/community/media/" + fn,
                               "bytes": total - left})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        u = urlparse(self.path)

        if u.path.startswith("/train/upload"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            path, n = save_upload(req.get("name"), req.get("b64") or "")
            if not path:
                return self._json({"ok": False, "error": n}, 400)
            return self._json({"ok": True, "path": path, "bytes": n})
        if u.path.startswith("/train/plan"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            return self._json({"ok": True, "plan": make_plan(req)})
        if u.path.startswith("/dl/cancel") and u.command == "POST":
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                req = {}
            key = req.get("key") or ""
            rec = _DL2.get(key)
            if not rec:
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            rec["cancel"] = True
            rec["state"] = "cancelled"
            try:
                pr = rec.get("proc")
                if pr and pr.poll() is None:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pr.pid)],
                                   capture_output=True, timeout=10)
            except Exception:
                pass
            return self._json({"ok": True})
        if u.path.startswith("/admin/appconf"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            try:
                admin_pw = load().get("admin", "")
            except Exception:
                admin_pw = ""
            if str(req.get("pw") or "") != str(admin_pw):
                return self._json({"ok": False, "error": "密码错误"}, 403)
            d2 = app_conf()
            if req.get("register_mode") in ("email", "invite", "open"):
                d2["register_mode"] = req["register_mode"]
            save_conf(d2)
            return self._json({"ok": True, "register_mode": d2.get("register_mode")})
        if u.path.startswith("/admin/invites"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            try:
                admin_pw = load().get("admin", "")
            except Exception:
                admin_pw = ""
            if str(req.get("pw") or "") != str(admin_pw):
                return self._json({"ok": False, "error": "密码错误"}, 403)
            codes = _jload(CODE_FILE, {})
            out = []
            for _ in range(int(req.get("n") or 5)):
                code = "INV-" + _secrets.token_hex(3).upper()
                codes[code] = {"kind": "invite", "credits": 0, "tier": "free", "note": req.get("note") or "",
                               "created": int(time.time()), "used_by": [], "limit": int(req.get("limit") or 1)}
                out.append(code)
            _jsave(CODE_FILE, codes)
            return self._json({"ok": True, "codes": out})
        if u.path.startswith("/admin/codes/update"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            try:
                admin_pw = load().get("admin", "")
            except Exception:
                admin_pw = ""
            if str(req.get("pw") or "") != str(admin_pw):
                return self._json({"ok": False, "error": "密码错误"}, 403)
            codes = _jload(CODE_FILE, {})
            key = (req.get("code") or "").strip().upper()
            if key not in codes:
                return self._json({"ok": False, "error": "兑换码不存在"}, 404)
            v = codes[key]
            if req.get("credits") is not None:
                v["credits"] = int(req["credits"])
            if req.get("tier"):
                v["tier"] = req["tier"]
            if req.get("note") is not None:
                v["note"] = req["note"]
            if req.get("limit") is not None:
                v["limit"] = int(req["limit"])
            codes[key] = v
            _jsave(CODE_FILE, codes)
            return self._json({"ok": True})
        if u.path.startswith("/admin/codes/delete"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            try:
                admin_pw = load().get("admin", "")
            except Exception:
                admin_pw = ""
            if str(req.get("pw") or "") != str(admin_pw):
                return self._json({"ok": False, "error": "密码错误"}, 403)
            codes = _jload(CODE_FILE, {})
            key = (req.get("code") or "").strip().upper()
            if key not in codes:
                return self._json({"ok": False, "error": "兑换码不存在"}, 404)
            codes.pop(key)
            _jsave(CODE_FILE, codes)
            return self._json({"ok": True})
        if u.path.startswith("/auth/sendcode"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            email = (req.get("email") or "").strip().lower()
            if "@" not in email or "." not in email.split("@")[-1]:
                return self._json({"ok": False, "error": "邮箱格式不对"}, 400)
            code = make_vcode(email)
            r = send_mail(email, "小千问验证码", "你的验证码是：%s（10 分钟内有效）" % code)
            if isinstance(r, str) and r.startswith("ERR:"):
                return self._json({"ok": False, "error": "邮件发送失败：" + r[4:]}, 500)
            if r is None:
                return self._json({"ok": True, "dev": True, "code": code,
                                   "msg": "未配置 SMTP，验证码直接显示（测试模式）"})
            return self._json({"ok": True, "dev": False, "msg": "验证码已发送到邮箱"})
        if u.path.startswith("/auth/register"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            mode = app_conf().get("register_mode", "email")
            if mode == "invite":
                ok, verr = invite_check(req.get("code"))
                if not ok:
                    return self._json({"ok": False, "error": verr}, 400)
            elif mode == "open":
                pass
            else:
                ok, verr = check_vcode(req.get("email"), req.get("code"))
                if not ok:
                    return self._json({"ok": False, "error": verr}, 400)
            u, err = user_register(req.get("email"), req.get("pw"), req.get("name"))
            if not err and mode == "invite":
                invite_consume(req.get("code"), (req.get("email") or "").lower())
            if err:
                return self._json({"ok": False, "error": err}, 400)
            tok = _secrets.token_hex(16)
            TOKENS[tok] = u["email"]
            u2 = {k: v for k, v in u.items() if k not in ("pw", "salt")}
            return self._json({"ok": True, "token": tok, "user": u2})
        if u.path.startswith("/auth/login"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            mode = app_conf().get("register_mode", "email")
            if mode == "email":
                ok, verr = check_vcode(req.get("email"), req.get("code"))
                if not ok:
                    return self._json({"ok": False, "error": verr}, 400)
            r, err = user_login(req.get("email"), req.get("pw"))
            if err:
                return self._json({"ok": False, "error": err}, 400)
            u2 = {k: v for k, v in r["user"].items() if k not in ("pw", "salt")}
            return self._json({"ok": True, "token": r["token"], "user": u2})
        if u.path.startswith("/auth/avatar"):
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
            u = user_by_token(tok)
            if not u:
                return self._json({"ok": False, "error": "未登录"}, 401)
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            try:
                fn = media_save("avatar_" + req.get("ext", "png"), req.get("b64") or "")
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)[:60]}, 400)
            users = _jload(USER_FILE, {})
            users[u["email"]]["avatar"] = fn
            _jsave(USER_FILE, users)
            return self._json({"ok": True, "avatar": fn, "url": "/community/media/" + fn})
        if u.path.startswith("/auth/redeem"):
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
            u = user_by_token(tok)
            if not u:
                return self._json({"ok": False, "error": "未登录"}, 401)
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            r, err = redeem(u["email"], req.get("code"))
            if err:
                return self._json({"ok": False, "error": err}, 400)
            return self._json({"ok": True, "got": r})
        if u.path.startswith("/auth/codes"):
            pw = (q.get("pw") or [""])[0] if False else ""
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            try:
                admin_pw = load().get("admin", "")
            except Exception:
                admin_pw = ""
            if str(req.get("admin") or "") != str(admin_pw):
                return self._json({"ok": False, "error": "管理密码错误"}, 403)
            try:
                codes = gen_codes(int(req.get("n") or 5), int(req.get("credits") or 100),
                                  req.get("tier") or "plus", req.get("note") or "",
                                  int(req.get("limit") or 1))
                return self._json({"ok": True, "codes": codes})
            except Exception as exc:
                return self._json({"ok": False, "error": "生成失败：%s" % exc}, 500)

        if u.path.startswith("/community/upload"):
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
            u = user_by_token(tok)
            if not u:
                return self._json({"ok": False, "error": "请先登录"}, 401)
            qn = (q.get("name") or ["file.bin"])[0]
            try:
                total = int(self.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            if total <= 0:
                return self._json({"ok": False, "error": "空文件"}, 400)
            if total > 500 * 1024 * 1024:
                return self._json({"ok": False, "error": "超过 500MB"}, 413)
            os.makedirs(MEDIA_DIR, exist_ok=True)
            safe = "".join(ch for ch in qn if ch not in '\\/:*?"<>|').strip() or "file.bin"
            fn = "%d_%s" % (int(time.time() * 1000), safe)
            path = os.path.join(MEDIA_DIR, fn)
            left = total
            try:
                with open(path, "wb") as f:
                    while left > 0:
                        chunk = self.rfile.read(min(1048576, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        left -= len(chunk)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)[:80]}, 500)
            return self._json({"ok": True, "name": fn, "url": "/community/media/" + fn,
                               "bytes": total - left})
        if u.path.startswith("/community/post"):
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
            u = user_by_token(tok)
            if not u:
                return self._json({"ok": False, "error": "请先登录"}, 401)
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            media = []
            for m in (req.get("media") or [])[:4]:
                try:
                    if m.get("done") and m.get("name"):
                        if os.path.exists(os.path.join(MEDIA_DIR, m["name"])):
                            media.append({"name": m["name"], "type": m.get("type") or "image"})
                        continue
                    fn = media_save(m.get("name") or "file", m.get("b64") or "")
                    media.append({"name": fn, "type": m.get("type") or "image"})
                except Exception:
                    pass
            d2 = posts_load()
            d2["posts"].insert(0, {"id": _secrets.token_hex(6), "author": u.get("name") or u["email"],
                                   "email": u["email"], "avatar": u.get("avatar", ""),
                                   "text": req.get("text") or "",
                                   "media": media, "time": int(time.time()),
                                   "likes": [], "comments": []})
            d2["posts"] = d2["posts"][:200]
            posts_save(d2)
            return self._json({"ok": True})
        if u.path.startswith("/community/comment"):
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "").strip()
            u = user_by_token(tok)
            if not u:
                return self._json({"ok": False, "error": "请先登录"}, 401)
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            d2 = posts_load()
            for p2 in d2["posts"]:
                if p2["id"] == req.get("id"):
                    p2["comments"].append({"author": u.get("name") or u["email"],
                                           "avatar": u.get("avatar", ""),
                                           "text": req.get("text") or "",
                                           "time": int(time.time())})
                    break
            posts_save(d2)
            return self._json({"ok": True})
        if u.path.startswith("/community/like"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            d2 = posts_load()
            for p2 in d2["posts"]:
                if p2["id"] == req.get("id"):
                    who = req.get("who") or "someone"
                    if who in p2["likes"]:
                        p2["likes"].remove(who)
                    else:
                        p2["likes"].append(who)
                    break
            posts_save(d2)
            return self._json({"ok": True})
        if u.path.startswith("/dl/remove"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                req = {}
            key = req.get("key") or ""
            rec = _DL2.pop(key, None)
            if rec:
                rec["cancel"] = True
                try:
                    pr = rec.get("proc")
                    if pr and pr.poll() is None:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pr.pid)],
                                       capture_output=True, timeout=10)
                except Exception:
                    pass
            return self._json({"ok": True})
        if u.path.startswith("/train/stop"):
            return self._json({"ok": train_stop()})
        if u.path.startswith("/train/start"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            cmd = (req.get("cmd") or "").strip()
            if not cmd:
                return self._json({"ok": False, "error": "命令为空"}, 400)
            ok, err = train_start(cmd)
            return self._json({"ok": bool(ok), "error": err})
        if u.path.startswith("/peer/friend"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            d = _load_peers()
            d["friends"] = [f for f in d.get("friends", []) if f.get("url") != req.get("url")]
            if req.get("url"):
                d["friends"].append({"name": req.get("name") or req.get("url"),
                                     "url": req.get("url"), "token": req.get("token", "")})
            _save_peers(d)
            return self._json({"ok": True, "friends": d["friends"]})
        if u.path.startswith("/peer/send"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            target = (req.get("url") or "").rstrip("/")
            if not target:
                return self._json({"ok": False, "error": "缺少目标地址"}, 400)
            payload = json.dumps({"from": req.get("from") or "朋友",
                                  "text": req.get("text") or "",
                                  "token": req.get("token") or ""}, ensure_ascii=False).encode()
            try:
                r2 = urllib.request.Request(target + "/peer/recv", data=payload,
                                            headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(r2, timeout=15) as resp:
                    return self._json({"ok": True, "reply": resp.read().decode()[:200]})
            except Exception as exc:
                return self._json({"ok": False, "error": "发送失败：%s" % exc}, 502)
        if u.path.startswith("/peer/recv"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False}, 400)
            _INBOX.append({"from": req.get("from") or "未知", "text": req.get("text") or "",
                           "time": int(time.time())})
            return self._json({"ok": True})
        if u.path.startswith("/plugins/add"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            name = (req.get("name") or "plugin").strip()
            safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "plugin"
            try:
                os.makedirs(PLUGIN_DIR, exist_ok=True)
                with open(os.path.join(PLUGIN_DIR, safe + ".json"), "w", encoding="utf-8") as f:
                    json.dump(req, f, ensure_ascii=False, indent=2)
                return self._json({"ok": True, "file": safe + ".json", "dir": PLUGIN_DIR})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, 500)
        if u.path.startswith("/tools/run"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            res = tools_run(req.get("name"), req.get("args") or {},
                            req.get("level") or "workspace", req.get("workspace") or "")
            return self._json({"ok": True, "result": res})
        if u.path.startswith("/models/add"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            ok = model_add(req.get("name") or "local", req.get("path") or "",
                           int(req.get("ctx") or 8192), req.get("mmproj"), int(req.get("ngl") or 99))
            return self._json({"ok": ok})
        if u.path.startswith("/dl/batch"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            files = req.get("files") or []
            if not files:
                return self._json({"ok": False, "error": "文件列表为空"}, 400)
            folder = (req.get("dir") or DL_DIR).strip() or DL_DIR
            _dl_seq[0] += 1
            key = "job%d" % _dl_seq[0]
            rec = {"title": req.get("title") or (files[0].get("name") or "下载"),
                   "files": [{"url": f.get("url"), "name": f.get("name") or "file.bin"} for f in files],
                   "dir": folder, "state": "running", "bytes": 0, "speed": 0,
                   "file_bytes": 0, "index": 0, "cancel": False}
            _new_job(key, rec)
            return self._json({"ok": True, "key": key, "dir": folder, "count": len(files)})
        if u.path.startswith("/dl"):
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            if req.get("fast"):
                url = req.get("url", "")
                nm_req = req.get("name", "model.bin")
                safe = "".join(ch for ch in nm_req if ch not in '\\/:*?"<>|').strip() or "model.bin"
                if not _dl_dir_ok():
                    return self._json({"ok": False, "error": "无法创建下载目录"}, 400)
                path = os.path.join(DL_DIR, safe)
                _DL[safe] = {"name": safe, "path": path, "url": url, "bytes": 0, "done": False,
                             "ok": False, "error": "", "started": int(time.time()), "fast": True}
                def _run():
                    ok = _seg_download(url, path, int(req.get("workers") or 4))
                    rec = _DL.get(safe)
                    if rec is not None:
                        rec["done"] = True; rec["ok"] = ok and os.path.exists(path)
                        rec["error"] = "" if rec["ok"] else "分段下载失败"
                        try:
                            rec["bytes"] = os.path.getsize(path)
                        except Exception:
                            pass
                threading.Thread(target=_run, daemon=True).start()
                return self._json({"ok": True, "name": safe, "dir": DL_DIR, "fast": True})
            nm, err = start_download(req.get("url", ""), req.get("name", "model.gguf"))
            if err:
                return self._json({"ok": False, "error": err}, 400)
            return self._json({"ok": True, "name": nm, "dir": DL_DIR})
        if u.path.startswith("/admin/new"):
            d = load()
            if (parse_qs(u.query).get("pw") or [""])[0] != d["admin"]:
                return self._json({"error": "密码错误"}, 403)
            try:
                req = json.loads(raw.decode() or "{}")
            except Exception:
                req = {}
            k = "sk-" + secrets.token_hex(16)
            d["keys"][k] = {"note": req.get("note", ""), "tokens": 0, "requests": 0,
                            "quota": int(req.get("quota") or 0), "created": int(time.time())}
            save(d)
            return self._json({"ok": True, "key": k})

        if not u.path.startswith("/v1/"):
            return self._json({"error": "not found"}, 404)

        upstream = (self.headers.get("X-Upstream") or "").strip().rstrip("/")
        if upstream and upstream != UPSTREAM:
            # 直通模式：用调用者自己的 key，转发到外部大模型接口
            try:
                body = json.loads(raw.decode() or "{}")
            except Exception:
                return self._json({"error": "bad json"}, 400)
            want_stream = bool(body.get("stream"))
            body["stream"] = False
            _path = u.path
            if upstream.lower().endswith("/v1") and _path.startswith("/v1"):
                _path = _path[3:]          # 避免 /v1/v1/...
            fwd = urllib.request.Request(upstream + _path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": self.headers.get("Authorization") or ""})
            try:
                with urllib.request.urlopen(fwd, timeout=300) as r:
                    data = r.read()
            except urllib.error.HTTPError as e:
                return self._json({"error": {"message": e.read().decode()[:300]}}, e.code)
            except Exception as e:
                return self._json({"error": {"message": str(e)}}, 502)
            if not want_stream:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
                return
            # 流式客户端：把整段结果包成 SSE 发回去
            try:
                j = json.loads(data.decode())
                msg = (j.get("choices") or [{}])[0].get("message") or {}
                content = msg.get("content") or ""
                reason = msg.get("reasoning_content") or ""
            except Exception:
                content, reason = data.decode("utf-8", "replace"), ""
            chunks = []
            def pack(obj):
                chunks.append("data: " + json.dumps(obj, ensure_ascii=False) + chr(10) * 2)
            if reason:
                pack({"choices": [{"delta": {"reasoning_content": reason}}]})
            for i in range(0, len(content), 40):
                pack({"choices": [{"delta": {"content": content[i:i+40]}}]})
            usage = {}
            try:
                usage = (j.get("usage") or {}) if isinstance(j, dict) else {}
            except Exception:
                usage = {}
            pack({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage})
            chunks.append("data: [DONE]" + chr(10) * 2)
            blob = "".join(chunks).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(blob)
            return

        d, k = self._check()
        if d is None:
            return

        # 本地模型没加载就先拉起来（空闲卸载后朋友的第一次消息也能用）
        ensure_local_model()

        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        body.setdefault("stream_options", {"include_usage": True})

        req = urllib.request.Request(UPSTREAM + u.path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            return self._json({"error": e.read().decode()[:400]}, e.code)
        except Exception as e:
            return self._json({"error": str(e)}, 502)

        # 精确统计（支持流式 SSE）
        used = count_usage(data, r.headers.get("content-type") if hasattr(r, "headers") else "")
        if not used:
            used = len(body.get("messages", [])) * 20
        bump_key(k, used)

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Upstream")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    d = load()
    print("=" * 56)
    print(" 中转站已启动  http://127.0.0.1:%d" % PORT)
    print(" 管理密码: %s" % d["admin"])
    print(" 查用量:  http://127.0.0.1:%d/admin?pw=密码" % PORT)
    print(" 建 key:  POST /admin/new?pw=密码  {\"note\":\"给谁用\"}")
    print("=" * 56)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
