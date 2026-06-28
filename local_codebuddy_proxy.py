#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CodeBuddy CN 纯 LLM API 代理 + Web 管理后台
- LLM 代理: WorkBuddy -> 本代理(Windows) -> copilot.tencent.com/v2/chat/completions
- 管理后台: http://127.0.0.1:19090/admin/dashboard
- Agent Loop 在 Windows 本地执行
"""
import json, os, sys, time, uuid, ssl, threading, copy, re, shutil, traceback, socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.client import IncompleteRead
from urllib.parse import urlparse, parse_qs, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HOST = "127.0.0.1"
PORT = 19090
CN_API = "https://copilot.tencent.com/v2/chat/completions"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "proxy_config.json")
ADMIN_HTML_FILE = os.path.join(SCRIPT_DIR, "admin_dashboard.html")
STATS_FILE = os.path.join(SCRIPT_DIR, "proxy_stats.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "proxy.log")
WB_MODELS_FILE = os.path.expandvars(r"%USERPROFILE%\.workbuddy\models.json")
CB_MODELS_FILE = os.path.expandvars(r"%USERPROFILE%\.codebuddy\models.json")

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as lf:
            lf.write(line + "\n")
    except:
        pass

# ======================== 配置管理 ========================

DEFAULT_ACCOUNTS = [
    {
        "name": "账号1",
        "api_key": "sk-account-1",
        "ck_key": "ck_YOUR_CK_KEY_HERE",
        "enabled": True,
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "kimi-k2.6", "kimi-k2.7", "glm-5.2", "glm-5.1"],
        "default_model": "deepseek-v4-pro",
    },
    {
        "name": "账号2",
        "api_key": "sk-account-2",
        "ck_key": "ck_YOUR_CK_KEY_HERE",
        "enabled": True,
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "kimi-k2.6", "kimi-k2.7", "glm-5.2", "glm-5.1"],
        "default_model": "deepseek-v4-pro",
    },
    {
        "name": "账号3",
        "api_key": "sk-account-3",
        "ck_key": "ck_YOUR_CK_KEY_HERE",
        "enabled": True,
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "kimi-k2.6", "kimi-k2.7", "glm-5.2", "glm-5.1"],
        "default_model": "deepseek-v4-pro",
    },
]

KNOWN_MODELS = [
    "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-thinking",
    "deepseek-v3-2-volc", "deepseek-v3-2", "deepseek-v3", "deepseek-r1",
    "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "glm-5.2", "glm-5.1", "glm-5.0", "glm-5.0-turbo", "glm-5v-turbo", "glm-4.7",
    "minimax-m3-play", "minimax-m2.7", "minimax-m2.5",
    "hy3-preview-agent",
    "claude-4.5", "claude-opus-4-5", "claude-opus-4-8",
    "gpt-5.5", "gpt-5.6", "gpt-5.7",
]

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg
        except:
            pass
    return {"accounts": copy.deepcopy(DEFAULT_ACCOUNTS)}

def save_config(cfg):
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, CONFIG_FILE)  # 原子写入
    except Exception as e:
        log(f"[config] 保存失败: {e}")

config = load_config()
config_lock = threading.Lock()

# ======================== 统计 (持久化) ========================

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_stats_file(s):
    try:
        with open(STATS_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
        os.replace(STATS_FILE + ".tmp", STATS_FILE)
    except Exception as e:
        log(f"[stats] 保存失败: {e}")

stats = load_stats()
stats_lock = threading.Lock()

_last_stats_save = 0

def record_stats(api_key, input_tokens, output_tokens, ok, err=""):
    global _last_stats_save
    need_save = False
    with stats_lock:
        if api_key not in stats:
            stats[api_key] = {
                "call_count": 0, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "success_count": 0, "error_count": 0,
                "last_used": None, "last_error": "",
            }
        s = stats[api_key]
        s["call_count"] += 1
        s["input_tokens"] += input_tokens
        s["output_tokens"] += output_tokens
        s["total_tokens"] += input_tokens + output_tokens
        s["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if ok:
            s["success_count"] += 1
        else:
            s["error_count"] += 1
            s["last_error"] = str(err)[:200]
        now = time.time()
        if now - _last_stats_save > 1:  # 至少间隔1秒防抖动
            need_save = True
            _last_stats_save = now
    # 在锁外保存，避免文件 I/O 阻塞其他线程
    if need_save:
        save_stats_file(stats)

# ======================== LLM 代理 ========================

def do_llm_call(ck_key, body_bytes, stream):
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + ck_key,
        "X-Conversation-ID": str(uuid.uuid4()),
        "X-Request-ID": str(uuid.uuid4()),
        "X-Forwarded-For": "127.0.0.1",
    }
    req = Request(CN_API, data=body_bytes, headers=headers, method="POST")
    try:
        ctx = ssl.create_default_context()
        resp = urlopen(req, timeout=180, context=ctx)
        if stream:
            return "stream", resp
        else:
            return "sync", resp.read().decode("utf-8")
    except HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {err_body[:500]}")
    except URLError as e:
        raise RuntimeError(f"连接失败: {e.reason}")

# ======================== SSE 聚合 ========================

def sse_to_nonstream(sse_text):
    content, reasoning = "", ""
    tool_calls = []
    finish = "stop"
    model = ""
    pt, ct = 0, 0

    for line in sse_text.split('\n'):
        if not line.startswith('data: '):
            continue
        d = line[6:]
        if d.strip() == '[DONE]':
            break
        try:
            chunk = json.loads(d)
            model = chunk.get('model', model)
            u = chunk.get('usage')
            if u:
                pt = u.get('prompt_tokens', pt)
                ct = u.get('completion_tokens', ct)
            for c in chunk.get('choices', []):
                delta = c.get('delta', {})
                if delta.get('content'):
                    content += delta['content']
                if delta.get('reasoning_content'):
                    reasoning += delta['reasoning_content']
                if c.get('finish_reason'):
                    finish = c['finish_reason']
                for tc in delta.get('tool_calls', []):
                    idx = tc.get('index', 0)
                    while len(tool_calls) <= idx:
                        tool_calls.append({'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                    if tc.get('id'):
                        tool_calls[idx]['id'] = tc['id']
                    f = tc.get('function', {})
                    if f.get('name'):
                        tool_calls[idx]['function']['name'] = f['name']
                    if f.get('arguments'):
                        tool_calls[idx]['function']['arguments'] += f['arguments']
        except:
            pass

    msg = {"role": "assistant", "content": content or None}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
        msg["content"] = None
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
    }

# ======================== Admin API ========================

def get_accounts_response():
    """GET /admin/accounts"""
    with config_lock:
        accs = copy.deepcopy(config["accounts"])
    with stats_lock:
        s = copy.deepcopy(stats)
    result = []
    for acc in accs:
        ak = acc["api_key"]
        info = {
            "name": acc["name"],
            "api_key": ak,
            "api_key_masked": mask_key(ak),
            "ck_key_masked": mask_key(acc.get("ck_key", "")),
            "enabled": acc.get("enabled", True),
            "models": acc.get("models", []),
            "default_model": acc.get("default_model", ""),
            "stats": s.get(ak, {}),
        }
        result.append(info)
    return 200, {"accounts": result, "known_models": KNOWN_MODELS}

def put_account(body):
    """PUT /admin/accounts - 更新账号"""
    api_key = body.get("api_key", "")
    with config_lock:
        for acc in config["accounts"]:
            if acc["api_key"] == api_key:
                if "name" in body:
                    acc["name"] = body["name"]
                if "enabled" in body:
                    acc["enabled"] = body["enabled"]
                if "ck_key" in body and body["ck_key"]:
                    acc["ck_key"] = body["ck_key"]
                if "models" in body:
                    acc["models"] = body["models"]
                if "default_model" in body:
                    acc["default_model"] = body["default_model"]
                save_config(config)
                return 200, {"success": True, "name": acc["name"]}
        return 404, {"error": "account not found"}

def post_account(body):
    """POST /admin/accounts - 添加账号"""
    name = body.get("name", "").strip()
    api_key = body.get("api_key", "").strip()
    ck_key = body.get("ck_key", "").strip()
    if not name or not api_key or not ck_key:
        return 400, {"error": "name, api_key, ck_key all required"}
    with config_lock:
        for acc in config["accounts"]:
            if acc["api_key"] == api_key:
                return 409, {"error": "api_key already exists"}
        config["accounts"].append({
            "name": name,
            "api_key": api_key,
            "ck_key": ck_key,
            "enabled": True,
            "models": body.get("models", KNOWN_MODELS[:6]),
            "default_model": body.get("default_model", KNOWN_MODELS[0]),
        })
        save_config(config)
    return 201, {"success": True, "name": name}

def delete_account(body):
    """DELETE /admin/accounts"""
    api_key = body.get("api_key", "")
    with config_lock:
        for i, acc in enumerate(config["accounts"]):
            if acc["api_key"] == api_key:
                name = acc["name"]
                del config["accounts"][i]
                save_config(config)
                return 200, {"success": True, "name": name}
        return 404, {"error": "account not found"}

def workbuddy_config(api_key=None, sync_all=False, selected_models=None):
    """GET /admin/workbuddy-config - 生成 WB 配置并写入 models.json
    selected_models: 逗号分隔的 model ID 列表，只同步这些模型
    """
    with config_lock:
        all_accs = copy.deepcopy(config["accounts"])

    if sync_all:
        target_accs = [a for a in all_accs if a.get("enabled", True)]
    elif api_key:
        target_accs = [a for a in all_accs if a["api_key"] == api_key]
        if not target_accs:
            return 404, {"error": "account not found"}
    else:
        return 400, {"error": "api_key or all=1 required"}

    # 读取现有 models.json
    existing = []
    for mf in [WB_MODELS_FILE, CB_MODELS_FILE]:
        if os.path.exists(mf):
            try:
                with open(mf, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                break
            except:
                pass

    # 构建新模型列表
    new_models = []
    seen = set()
    model_filter = None
    if selected_models:
        model_filter = set(m.strip() for m in selected_models.split(",") if m.strip())

    # 收集本次要同步的账号名
    syncing_account_names = {acc["name"] for acc in target_accs}

    for acc in target_accs:
        for model_id in acc.get("models", []):
            # 如果指定了模型过滤，只处理选中的模型
            if model_filter and model_id not in model_filter:
                continue
            wb_id = f"{acc['name']}/{model_id}"
            if wb_id in seen:
                continue
            seen.add(wb_id)
            new_models.append({
                "id": wb_id,
                "name": f"{acc['name']} {model_id}",
                "vendor": "CodeBuddy-Proxy",
                "apiKey": acc["api_key"],
                "url": f"http://{HOST}:{PORT}/v1/chat/completions",
                "maxInputTokens": 128000,
                "maxOutputTokens": 16384,
                "supportsToolCall": True,
                "supportsImages": True,
                "supportsReasoning": model_id == acc.get("default_model", ""),
                "useCustomProtocol": False,
            })

    # 保留已有的非代理模型 + 未被覆盖的其他账号的代理模型
    for m in existing:
        if m["id"] in seen:
            continue
        # 非代理模型始终保留
        if m.get("vendor") != "CodeBuddy-Proxy":
            seen.add(m["id"])
            new_models.append(m)
            continue
        # 代理模型：如果属于未被本次同步覆盖的账号，保留
        m_account = m["id"].split("/")[0] if "/" in m["id"] else ""
        if m_account and m_account not in syncing_account_names:
            seen.add(m["id"])
            new_models.append(m)

    # 写入文件
    dirs = set(os.path.dirname(f) for f in [WB_MODELS_FILE, CB_MODELS_FILE])
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    json_str = json.dumps(new_models, indent=2, ensure_ascii=False) + "\n"
    for mf in [WB_MODELS_FILE, CB_MODELS_FILE]:
        try:
            d = os.path.dirname(mf)
            os.makedirs(d, exist_ok=True)
            # 备份
            if os.path.exists(mf):
                bak = mf + ".bak"
                shutil.copy2(mf, bak)
            with open(mf, "w", encoding="utf-8") as f:
                f.write(json_str)
        except Exception as e:
            log(f"[sync] 写入 {mf} 失败: {e}")

    # 本次实际新增/更新的模型数（只算目标账号的）
    synced_count = sum(1 for m in new_models if m.get("vendor") == "CodeBuddy-Proxy" and m["id"].split("/")[0] in syncing_account_names)
    total_proxy = sum(1 for m in new_models if m.get("vendor") == "CodeBuddy-Proxy")
    synced_names = [m["id"] for m in new_models if m.get("vendor") == "CodeBuddy-Proxy" and m["id"].split("/")[0] in syncing_account_names]
    log(f"[sync] 本次同步 {synced_count} 个模型，当前共 {total_proxy} 个代理模型")
    return 200, {
        "success": True,
        "synced_count": synced_count,
        "total_proxy": total_proxy,
        "total_models": len(new_models),
        "models": synced_names,
        "files_written": [WB_MODELS_FILE, CB_MODELS_FILE],
    }

def mask_key(k):
    if not k or len(k) <= 8:
        return k
    return k[:8] + "..." + k[-4:]

def _extract_tokens_from_sse(raw_bytes):
    """从 SSE 原始数据中提取 token 用量"""
    pt, ct = 0, 0
    text = raw_bytes.decode("utf-8", errors="replace")
    for line in text.split('\n'):
        if not line.startswith('data: '):
            continue
        d = line[6:].strip()
        if not d or d == '[DONE]':
            continue
        try:
            chunk = json.loads(d)
            u = chunk.get('usage')
            if u:
                pt = u.get('prompt_tokens', pt)
                ct = u.get('completion_tokens', ct)
        except:
            pass
    return pt, ct

# ======================== HTTP Server ========================

class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "LocalCodeBuddyProxy/2.0"

    def log_message(self, format, *args):
        if "/health" not in args[0]:
            log(f"{self.client_address[0]} {args[0]}")

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, IncompleteRead):
            pass

    def _send_html(self, code, html):
        data = html.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, IncompleteRead):
            pass

    def _send_sse(self, sse_text):
        data = sse_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, code, msg):
        self._send_json(code, {"error": {"message": msg, "type": "error", "code": str(code)}})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ---- Admin Routes ----

    def _handle_admin(self, path, method):
        try:
            return self._handle_admin_impl(path, method)
        except Exception as e:
            log(f"[admin] 处理异常: {e}\n{traceback.format_exc()}")
            try:
                self._send_error(500, f"internal error: {e}")
            except:
                pass

    def _handle_admin_impl(self, path, method):
        parsed = urlparse(path)
        qs = parse_qs(parsed.query)

        if path == "/admin/dashboard" or path == "/admin" or path == "/admin/":
            try:
                with open(ADMIN_HTML_FILE, "r", encoding="utf-8") as f:
                    html = f.read()
            except:
                html = "<h1>Admin Dashboard</h1><p>admin_dashboard.html 未找到</p>"
            self._send_html(200, html)
            return

        if path == "/admin/accounts" or path.startswith("/admin/accounts?"):
            if method == "GET":
                code, data = get_accounts_response()
                self._send_json(code, data)
                return
            elif method == "PUT":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                except:
                    self._send_error(400, "invalid JSON")
                    return
                code, data = put_account(body)
                self._send_json(code, data)
                return
            elif method == "POST":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                except:
                    self._send_error(400, "invalid JSON")
                    return
                code, data = post_account(body)
                self._send_json(code, data)
                return
            elif method == "DELETE":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                except:
                    self._send_error(400, "invalid JSON")
                    return
                code, data = delete_account(body)
                self._send_json(code, data)
                return

        if path.startswith("/admin/workbuddy-config"):
            api_key = qs.get("api_key", [None])[0]
            sync_all = qs.get("all", ["0"])[0] == "1"
            models = qs.get("models", [None])[0]
            code, data = workbuddy_config(api_key=api_key, sync_all=sync_all, selected_models=models)
            self._send_json(code, data)
            return

        if path.startswith("/admin/stats"):
            with stats_lock:
                s = copy.deepcopy(stats)
            self._send_json(200, {"stats": s})
            return

        self._send_error(404, "admin endpoint not found")

    # ---- API Routes ----

    def do_GET(self):
        try:
            return self._do_GET_impl()
        except Exception as e:
            log(f"[GET] 未捕获异常: {e}\n{traceback.format_exc()}")

    def _do_GET_impl(self):
        path = urlparse(self.path).path
        if path.startswith("/admin"):
            self._handle_admin(self.path, "GET")
            return
        if path in ("/health", "/v1/health", "/"):
            self._send_json(200, {
                "status": "ok", "backend": "copilot.tencent.com",
                "proxy": "LocalCodeBuddyProxy/2.0", "platform": "Windows"
            })
            return
        self._send_error(404, "not found")

    def do_POST(self):
        try:
            self._do_POST_impl()
        except Exception as e:
            log(f"[POST] 未捕获异常: {e}\n{traceback.format_exc()}")
            try:
                self._send_error(500, f"internal error: {e}")
            except:
                pass

    def _do_POST_impl(self):
        path = urlparse(self.path).path
        if path.startswith("/admin"):
            self._handle_admin(self.path, "POST")
            return
        if path != "/v1/chat/completions":
            self._send_error(404, "not found")
            return
        self._handle_chat()

    def do_PUT(self):
        try:
            return self._do_PUT_impl()
        except Exception as e:
            log(f"[PUT] 未捕获异常: {e}\n{traceback.format_exc()}")

    def _do_PUT_impl(self):
        path = urlparse(self.path).path
        if path.startswith("/admin"):
            self._handle_admin(self.path, "PUT")
            return
        self._send_error(404, "not found")

    def do_DELETE(self):
        try:
            return self._do_DELETE_impl()
        except Exception as e:
            log(f"[DELETE] 未捕获异常: {e}\n{traceback.format_exc()}")

    def _do_DELETE_impl(self):
        path = urlparse(self.path).path
        if path.startswith("/admin"):
            self._handle_admin(self.path, "DELETE")
            return
        self._send_error(404, "not found")

    # ---- Chat Completions ----

    def _handle_chat(self):
        # 读取请求体
        try:
            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length)
            body = json.loads(body_bytes)
        except Exception as e:
            self._send_error(400, f"invalid request: {e}")
            return

        # 根据 apiKey 找账号
        auth = self.headers.get("Authorization", "")
        token = auth.replace("Bearer ", "") if auth.lower().startswith("bearer ") else ""
        acc = None
        with config_lock:
            for a in config["accounts"]:
                if a["api_key"] == token and a.get("enabled", True):
                    acc = copy.deepcopy(a)
                    break

        if not acc:
            self._send_error(401, "invalid or disabled API key")
            return

        # 读取 stream 参数
        stream = body.get("stream", False)
        model = body.get("model", "")

        # 剥离账号名前缀 (妮/deepseek-v4-pro -> deepseek-v4-pro)
        real_model = model.split("/")[-1] if "/" in model else model

        # 替换 body 中的 model
        body["model"] = real_model
        # CN API 只支持流式: 始终发 stream=true, 非流式请求由本代理聚合
        body["stream"] = True
        body_bytes = json.dumps(body).encode("utf-8")

        log(f"[llm] account={acc['name']} model={real_model} stream_client={stream}")

        try:
            _, result = do_llm_call(acc["ck_key"], body_bytes, True)
        except Exception as e:
            record_stats(acc["api_key"], 0, 0, False, str(e))
            self._send_error(502, f"upstream error: {e}")
            return

        try:
            if stream:
                # 流式 → 实时中继 SSE + 追踪 token 用量
                raw_chunks = []
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    while True:
                        chunk = result.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        raw_chunks.append(chunk)
                except IncompleteRead:
                    # 上游中途断开，补发 [DONE] 让客户端正常结束 SSE
                    try:
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except:
                        pass
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                # 从累积的 SSE 数据中提取 token 统计
                pt, ct = _extract_tokens_from_sse(b"".join(raw_chunks))
                if pt > 0 or ct > 0:
                    record_stats(acc["api_key"], pt, ct, True)
                else:
                    record_stats(acc["api_key"], 0, 0, True)
            else:
                # 非流式 → 缓存并聚合
                raw = b""
                try:
                    while True:
                        chunk = result.read(8192)
                        if not chunk:
                            break
                        raw += chunk
                except Exception as e:
                    log(f"[llm] stream read error: {e}")
                sse_text = raw.decode("utf-8", errors="replace")
                aggregated = sse_to_nonstream(sse_text)
                est_tokens = len(sse_text) // 4
                record_stats(acc["api_key"], 0, est_tokens, True)
                resp_data = json.dumps(aggregated, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp_data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(resp_data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, IncompleteRead):
                    pass
        except Exception as e:
            log(f"[llm] 响应异常: {e}\n{traceback.format_exc()}")
            record_stats(acc["api_key"], 0, 0, False, str(e))
            try:
                self._send_error(502, f"response error: {e}")
            except:
                pass


def main():
    _cleanup_port()
    while True:
        try:
            _run_server()
            # _run_server 正常返回 = 自检触发重启
        except KeyboardInterrupt:
            log("[shutdown] 用户中断，退出")
            break
        except Exception as e:
            log(f"[FATAL] {traceback.format_exc()}")
        log("[restart] 3秒后重启...")
        _cleanup_port()
        time.sleep(3)


def _cleanup_port():
    """强制释放端口，处理休眠唤醒后 socket 残留"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.close()
    except:
        pass


def _run_server():
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), ProxyHandler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    log("=" * 60)
    log("  LocalCodeBuddyProxy v2.1")
    log(f"  监听: http://{HOST}:{PORT}")
    log(f"  管理后台: http://{HOST}:{PORT}/admin/dashboard")
    log(f"  API端点: http://{HOST}:{PORT}/v1/chat/completions")
    log(f"  上游: {CN_API}")
    log(f"  平台: Windows (自检自愈)")
    log("=" * 60)

    if os.path.exists(ADMIN_HTML_FILE):
        log(f"[init] 管理后台 HTML 已加载")
    else:
        log(f"[init] 警告: 管理后台 HTML 未找到 ({ADMIN_HTML_FILE})")

    with config_lock:
        n = len(config["accounts"])
    log(f"[init] 已加载 {n} 个账号")

    # 手动 accept 循环 + 自检 + 休眠唤醒检测
    server.socket.settimeout(1.0)
    last_alive = time.time()
    last_clock = time.time()
    try:
        while True:
            try:
                server.handle_request()
                last_alive = time.time()
                last_clock = last_alive
            except socket.timeout:
                now = time.time()
                # 检测休眠唤醒：系统时钟跳变大 (>30秒)
                if now - last_clock > 30:
                    log(f"[detect] 系统从休眠恢复 (时钟跳变 {now - last_clock:.0f}s)，重启网络...")
                    break
                last_clock = now
                # 每 5 秒无请求时自检端口
                if now - last_alive > 5:
                    try:
                        s = socket.create_connection((HOST, PORT), timeout=2)
                        s.close()
                        last_alive = now
                    except:
                        log("[selfcheck] 端口无响应，触发重启")
                        break
                continue
            except Exception as e:
                log(f"[accept] 异常: {traceback.format_exc()}")
                break
    except KeyboardInterrupt:
        log("[shutdown] 用户中断")
        with stats_lock:
            save_stats_file(stats)
        raise
    finally:
        with stats_lock:
            save_stats_file(stats)
        try:
            server.server_close()
        except:
            pass


if __name__ == "__main__":
    main()
