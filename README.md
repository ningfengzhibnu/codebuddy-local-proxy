# CodeBuddy Local Proxy

Windows 本地的 CodeBuddy CN API 代理 + Web 管理后台。

**解决的问题**：CodeBuddy CN 的 Agent Loop 原本跑在远程 Linux 服务器上，无法访问本地 Windows 文件系统。本代理将 Agent Loop 留在 Windows 本地执行，只通过 HTTP 代理调用 CodeBuddy CN 的纯 LLM API。

## 架构

```
WorkBuddy (Windows)
  |  models.json -> http://127.0.0.1:19090
  v
LocalCodeBuddyProxy (Windows)
  |  proxy_guard.py (外部守护)
  |  CK Key -> Bearer Token
  v
copilot.tencent.com/v2/chat/completions
```

- **端口**: `127.0.0.1:19090`
- **API**: `http://127.0.0.1:19090/v1/chat/completions`
- **管理后台**: `http://127.0.0.1:19090/admin/dashboard`

## 功能

| 功能 | 说明 |
|------|------|
| 多账号管理 | 增删改/启停，每个账号独立 CK Key |
| 模型选择 | 按账号勾选，一键同步到 WorkBuddy |
| 统计持久化 | 调用次数/Token 存 proxy_stats.json，重启不丢 |
| 流式中继 | 流式 SSE 实时转发，非流式自动聚合 |
| 多线程 | ThreadingHTTPServer 并发处理 |
| 自动重启 | 崩溃 3 秒自动复活 |
| 休眠恢复 | 检测系统休眠，自动重置网络 |
| 外部守护 | proxy_guard.py 每 30 秒检查端口，独立监护 |
| 原子写入 | 配置先写 .tmp 再 os.replace |

## 快速开始

### 1. 获取 CK Key

打开 codebuddy.cn -> F12 -> Application -> Cookies -> 复制 `ck_token`

### 2. 配置

编辑 `local_codebuddy_proxy.py` 中的 `DEFAULT_ACCOUNTS`，填入你的 CK Key。

或通过管理后台 `http://127.0.0.1:19090/admin/dashboard` 添加。

### 3. 启动

```bash
python proxy_guard.py     # 守护 + 代理（推荐）
python local_codebuddy_proxy.py  # 仅代理
```

### 4. 配置 WorkBuddy

打开管理后台 -> 点击「同步全部到WB」。

## 管理后台 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /admin/accounts | 账号列表+统计 |
| POST | /admin/accounts | 添加账号 |
| PUT | /admin/accounts | 更新账号 |
| DELETE | /admin/accounts | 删除账号 |
| GET | /admin/workbuddy-config?all=1 | 同步到 WB |
| GET | /admin/stats | 统计 |
| GET | /admin/dashboard | 管理页面 |

## 文件结构

```
codebuddy-local-proxy/
  local_codebuddy_proxy.py   # 主程序 v2.1
  proxy_guard.py             # 守护进程
  admin_dashboard.html       # 管理后台 UI
  proxy_config.example.json  # 配置示例
  start_proxy.bat / .sh      # 启动脚本
  README.md
```

运行时生成: proxy_config.json, proxy_stats.json, proxy.log, proxy_guard.log

## License

MIT
