#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CodeBuddy CN 纯 LLM API 代理 + Web 管理后台

功能：
1. 将 WorkBuddy 的 API 请求转发到 CodeBuddy CN (copilot.tencent.com)
2. 提供 Web 管理后台 (http://127.0.0.1:19090/admin/dashboard)：
   - 多账号管理（增删改/启停）
   - 模型选择（按账号勾选模型）
   - 一键同步模型配置到 WorkBuddy
   - 使用统计

原理：
  WorkBuddy(Windows) → 本代理(localhost:19090) → copilot.tencent.com/v2/chat/completions
  CN API 仅支持流式，非流式请求由本代理聚合 SSE 后返回。
  Agent Loop 在 Windows 本地执行，无需远程 Linux 服务器。

依赖：Python 3.8+ (纯标准库，无第三方依赖)

快速开始：
  1. 修改本文件中的 DEFAULT_ACCOUNTS，填入你的 CK Key
  2. 运行: python local_codebuddy_proxy.py
  3. 打开管理后台: http://127.0.0.1:19090/admin/dashboard
  4. 在 WorkBuddy models.json 中配置指向 http://127.0.0.1:19090
"""

import json
import os
import sys
import time
import uuid
import ssl
import threading
import copy
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ======================== 配置 ========================

HOST = "127.0.0.1"
PORT = 19090
CN_API = "https://copilot.tencent.com/v2/chat/completions"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "proxy_config.json")
ADMIN_HTML_FILE = os.path.join(SCRIPT_DIR, "admin_dashboard.html")
WB_MODELS_FILE = os.path.expandvars(r"%USERPROFILE%\.workbuddy\models.json")
CB_MODELS_FILE = os.path.expandvars(r"%USERPROFILE%\.codebuddy\models.json")

# ======================== 默认账号（请替换为你的真实 CK Key） ========================

DEFAULT_ACCOUNTS = [
    {
        "name": "我的账号",
        "api_key": "sk-my-account-key",
        "ck_key": "ck_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "enabled": True,
        "models": ["deepseek-v4-pro", "deepseek-v4-flash", "kimi-k2.6", "kimi-k2.7", "glm-5.2", "glm-5.1"],
        "default_model": "deepseek-v4-pro",
    },
]

# 已知可用模型列表（可通过管理后台为账号选择）
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

# ======================== 配置管理 ========================


def load_config():
    """加载持久化配置，不存在则使用默认配置"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg
        except Exception:
            pass
    return {"accounts": copy.deepcopy(DEFAULT_ACCOUNTS)}


def save_config(cfg):
    """保存配置到文件"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")


config = load_config()
config_lock = threading.Lock()

# ======================== 使用统计 ========================

stats = {}
stats_lock = threading.Lock()


def record_stats(api_key, input_tokens, output_tokens, ok, err=""):
    """记录 API 调用统计"""
    with stats_lock:
        if api_key not in stats:
            stats[api_key] = {
                "call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "success_count": 0,
                "error_count": 0,
                "last_used": None,
                "last_error": "",
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


# ======================== LLM 代理核心 ========================


def do_llm_call(ck_key, body_bytes):
    """发送请求到 CodeBuddy CN API"""
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
        return resp
    except HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {err_body[:500]}")
    except URLError as e:
        raise RuntimeError(f"连接失败: {e.reason}")


# ======================== SSE 聚合 ========================


def sse_to_nonstream(sse_text):
    """将 SSE 流式响应聚合为 OpenAI 格式 JSON"""
    content, reasoning = "", ""
    tool_calls = []
    finish = "stop"
    model = ""
    pt, ct = 0, 0

    for line in sse_text.split("\n"):
        if not line.startswith("data: "):
            continue
        d = line[6:]
        if d.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(d)
            model = chunk.get("model", model)
            u = chunk.get("usage")
            if u:
                pt = u.get("prompt_tokens", pt)
                ct = u.get("completion_tokens", ct)
            for c in chunk.get("choices", []):
                delta = c.get("delta", {})
                if delta.get("content"):
                    content += delta["content"]
                if delta.get("reasoning_content"):
                    reasoning += delta["reasoning_content"]
                if c.get("finish_reason"):
                    finish = c["finish_reason"]
                for tc in delta.get("tool_calls", []):
                    idx = tc.get("index", 0)
                    while len(tool_calls) <= idx:
                        tool_calls.append({
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                    if tc.get("id"):
                        tool_calls[idx]["id"] = tc["id"]
                    f = tc.get("function", {})
                    if f.get("name"):
                        tool_calls[idx]["function"]["name"] = f["name"]
                    if f.get("arguments"):
                        tool_calls[idx]["function"]["arguments"] += f["arguments"]
        except Exception:
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
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
        },
    }


# ======================== Admin API 实现 ========================


def mask_key(k):
    """脱敏显示 Key"""
    if not k or len(k) <= 8:
        return k
    return k[:8] + "..." + k[-4:]


def get_accounts_response():
    """GET /admin/accounts - 获取所有账号及统计"""
    with config_lock:
        accs = copy.deepcopy(config["accounts"])
    with stats_lock:
        s = copy.deepcopy(stats)
    result = []
    for acc in accs:
        ak = acc["api_key"]
        result.append({
            "name": acc["name"],
            "api_key": ak,
            "api_key_masked": mask_key(ak),
            "ck_key_masked": mask_key(acc.get("ck_key", "")),
            "enabled": acc.get("enabled", True),
            "models": acc.get("models", []),
            "default_model": acc.get("default_model", ""),
            "stats": s.get(ak, {}),
        })
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
    """DELETE /admin/accounts - 删除账号"""
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
    """
    GET /admin/workbuddy-config - 生成 WB 配置并写入 models.json

    参数:
      api_key: 只同步指定账号
      all=1: 同步全部已启用账号
      models: 逗号分隔模型列表，只同步这些模型

    支持增量同步：不会删除其他账号的代理模型或非代理模型。
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

    # 读取现有配置
    existing = []
    for mf in [WB_MODELS_FILE, CB_MODELS_FILE]:
        if os.path.exists(mf):
            try:
                with open(mf, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                break
            except Exception:
                pass

    new_models = []
    seen = set()
    model_filter = None
    if selected_models:
        model_filter = set(m.strip() for m in selected_models.split(",") if m.strip())

    syncing_account_names = {acc["name"] for acc in target_accs}

    # 构建目标账号的模型配置
    for acc in target_accs:
        for model_id in acc.get("models", []):
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

    # 保留已有的非代理模型 和 未被覆盖的其他账号的代理模型
    for m in existing:
        if m["id"] in seen:
            continue
        if m.get("vendor") != "CodeBuddy-Proxy":
            seen.add(m["id"])
            new_models.append(m)
            continue
        m_account = m["id"].split("/")[0] if "/" in m["id"] else ""
        if m_account and m_account not in syncing_account_names:
            seen.add(m["id"])
            new_models.append(m)

    # 写入 WorkBuddy 和 CodeBuddy 的 models.json
    for mf in [WB_MODELS_FILE, CB_MODELS_FILE]:
        try:
            d = os.path.dirname(mf)
            os.makedirs(d, exist_ok=True)
            if os.path.exists(mf):
                bak = mf + ".bak"
                shutil.copy2(mf, bak)
            with open(mf, "w", encoding="utf-8") as f:
                json.dump(new_models, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except Exception as e:
            print(f"[sync] 写入 {mf} 失败: {e}")

    synced_count = sum(
        1
        for m in new_models
        if m.get("vendor") == "CodeBuddy-Proxy"
        and m["id"].split("/")[0] in syncing_account_names
    )
    total_proxy = sum(1 for m in new_models if m.get("vendor") == "CodeBuddy-Proxy")
    synced_names = [
        m["id"]
        for m in new_models
        if m.get("vendor") == "CodeBuddy-Proxy"
        and m["id"].split("/")[0] in syncing_account_names
    ]
    print(f"[sync] 本次同步 {synced_count} 个模型，当前共 {total_proxy} 个代理模型")
    return 200, {
        "success": True,
        "synced_count": synced_count,
        "total_proxy": total_proxy,
        "total_models": len(new_models),
        "models": synced_names,
        "files_written": [WB_MODELS_FILE, CB_MODELS_FILE],
    }


# ======================== HTTP Server ========================


class ProxyHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器：代理 /v1/chat/completions + 管理后台 API"""

    server_version = "LocalCodeBuddyProxy/2.0"

    def log_message(self, format, *args):
        if "/health" not in args[0]:
            print(f"[{time.strftime('%H:%M:%S')}] {self.client_address[0]} {args[0]}")

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, html):
        data = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
        parsed = urlparse(path)
        qs = parse_qs(parsed.query)

        # 管理后台页面
        if path == "/admin/dashboard" or path == "/admin" or path == "/admin/":
            try:
                with open(ADMIN_HTML_FILE, "r", encoding="utf-8") as f:
                    html = f.read()
            except Exception:
                html = "<h1>Admin Dashboard</h1><p>admin_dashboard.html 未找到</p>"
            self._send_html(200, html)
            return

        # 账号管理 API
        if path == "/admin/accounts" or path.startswith("/admin/accounts?"):
            if method == "GET":
                code, data = get_accounts_response()
                self._send_json(code, data)
                return
            elif method in ("PUT", "POST", "DELETE"):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length))
                except Exception:
                    self._send_error(400, "invalid JSON")
                    return
                if method == "PUT":
                    code, data = put_account(body)
                elif method == "POST":
                    code, data = post_account(body)
                else:
                    code, data = delete_account(body)
                self._send_json(code, data)
                return

        # 同步到 WorkBuddy 配置
        if path.startswith("/admin/workbuddy-config"):
            api_key = qs.get("api_key", [None])[0]
            sync_all = qs.get("all", ["0"])[0] == "1"
            models = qs.get("models", [None])[0]
            code, data = workbuddy_config(
                api_key=api_key, sync_all=sync_all, selected_models=models
            )
            self._send_json(code, data)
            return

        # 统计接口
        if path.startswith("/admin/stats"):
            with stats_lock:
                s = copy.deepcopy(stats)
            self._send_json(200, {"stats": s})
            return

        self._send_error(404, "admin endpoint not found")

    # ---- HTTP Methods ----

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/admin"):
            self._handle_admin(self.path, "GET")
            return
        if path in ("/health", "/v1/health", "/"):
            self._send_json(200, {
                "status": "ok",
                "backend": "copilot.tencent.com",
                "proxy": "LocalCodeBuddyProxy/2.0",
                "platform": "Windows",
            })
            return
        self._send_error(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/admin"):
            self._handle_admin(self.path, "POST")
            return
        if path != "/v1/chat/completions":
            self._send_error(404, "not found")
            return
        self._handle_chat()

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/admin"):
            self._handle_admin(self.path, "PUT")
            return
        self._send_error(404, "not found")

    def do_DELETE(self):
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

        # 根据 apiKey 查找对应的 CK Key
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

        stream = body.get("stream", False)
        model = body.get("model", "")

        # 剥离账号名前缀 (用户名/deepseek-v4-pro -> deepseek-v4-pro)
        real_model = model.split("/")[-1] if "/" in model else model
        body["model"] = real_model
        # CN API 仅支持流式，始终发 stream=true
        body["stream"] = True
        body_bytes = json.dumps(body).encode("utf-8")

        print(f"[llm] account={acc['name']} model={real_model} stream_client={stream}")

        try:
            resp = do_llm_call(acc["ck_key"], body_bytes)
        except Exception as e:
            record_stats(acc["api_key"], 0, 0, False, str(e))
            self._send_error(502, f"upstream error: {e}")
            return

        # 读取全部 SSE 响应
        raw = b""
        try:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                raw += chunk
        except Exception as e:
            print(f"[llm] stream read error: {e}")

        sse_text = raw.decode("utf-8", errors="replace")
        est_tokens = len(sse_text) // 4

        if stream:
            record_stats(acc["api_key"], 0, est_tokens, True)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            aggregated = sse_to_nonstream(sse_text)
            record_stats(acc["api_key"], 0, est_tokens, True)
            resp_data = json.dumps(aggregated, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_data)


# ======================== 入口 ========================


def main():
    server = HTTPServer((HOST, PORT), ProxyHandler)
    print(f"{'=' * 60}")
    print(f"  LocalCodeBuddyProxy v2.0")
    print(f"  监听: http://{HOST}:{PORT}")
    print(f"  管理后台: http://{HOST}:{PORT}/admin/dashboard")
    print(f"  API端点: http://{HOST}:{PORT}/v1/chat/completions")
    print(f"  上游: {CN_API}")
    print(f"  平台: Windows (Agent Loop 本地执行)")
    print(f"{'=' * 60}")

    if os.path.exists(ADMIN_HTML_FILE):
        print(f"[init] 管理后台 HTML 已加载")
    else:
        print(f"[init] 警告: 管理后台 HTML 未找到 ({ADMIN_HTML_FILE})")

    with config_lock:
        n = len(config["accounts"])
    print(f"[init] 已加载 {n} 个账号")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[shutdown] 正在关闭...")
        server.shutdown()


if __name__ == "__main__":
    main()
