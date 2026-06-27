# CodeBuddy Local Proxy

在 Windows 本地运行的 CodeBuddy CN API 代理 + Web 管理后台。

**解决的问题**：CodeBuddy CN 的 Agent Loop 原本跑在远程 Linux 服务器上，无法访问本地 Windows 文件系统。本代理将 Agent Loop 留在 Windows 本地执行，只通过 HTTP 代理调用 CodeBuddy CN 的纯 LLM API。

## 架构

```
WorkBuddy (Windows 桌面端)
  │
  │  models.json 指向 http://127.0.0.1:19090
  ▼
LocalCodeBuddyProxy (本代理, Windows 本地)
  │
  │  CK Key 作为 Bearer Token
  ▼
copilot.tencent.com/v2/chat/completions (CodeBuddy CN 纯 LLM API)
```

- **代理端口**: `127.0.0.1:19090`
- **API 端点**: `http://127.0.0.1:19090/v1/chat/completions`
- **管理后台**: `http://127.0.0.1:19090/admin/dashboard`

## 功能

| 功能 | 说明 |
|------|------|
| 多账号管理 | 增删改/启停账号，每个账号绑定独立 CK Key |
| 模型选择 | 按账号勾选模型，一键同步到 WorkBuddy |
| 使用统计 | 调用次数、Token、成功/失败率 |
| SSE 聚合 | CN API 仅支持流式，代理自动聚合为非流式 JSON 响应 |
| 配置热加载 | 管理后台修改即时生效，无需重启 |

## 快速开始

### 1. 获取 CK Key

1. 打开 [codebuddy.cn](https://www.codebuddy.cn)，登录你的账号
2. 按 F12 打开开发者工具 → Application → Cookies → copilot.tencent.com
3. 复制 `ck_token` 对应的值（以 `ck_` 开头）

### 2. 配置账号

编辑 `local_codebuddy_proxy.py` 中的 `DEFAULT_ACCOUNTS`：

```python
DEFAULT_ACCOUNTS = [
    {
        "name": "我的账号",           # 显示名称
        "api_key": "sk-my-key",       # WorkBuddy 用这个识别账号
        "ck_key": "ck_xxxx...",       # 从浏览器 Cookies 获取
        "enabled": True,
        "models": ["deepseek-v4-pro", "kimi-k2.6", "glm-5.2"],
        "default_model": "deepseek-v4-pro",
    },
]
```

也可以首次运行后通过管理后台 `http://127.0.0.1:19090/admin/dashboard` 添加账号。

### 3. 启动代理

**Windows**: 双击 `start_proxy.bat`

**Linux/Mac**: 
```bash
chmod +x start_proxy.sh
./start_proxy.sh
```

**直接运行**:
```bash
python local_codebuddy_proxy.py
```

### 4. 配置 WorkBuddy

启动代理后，打开管理后台 `http://127.0.0.1:19090/admin/dashboard`，点击「同步全部到WB」一键写入 WorkBuddy 配置。

也可以手动在 `%USERPROFILE%\.workbuddy\models.json` 中添加：

```json
{
  "id": "我的账号/deepseek-v4-pro",
  "name": "我的账号 deepseek-v4-pro",
  "vendor": "CodeBuddy-Proxy",
  "apiKey": "sk-my-key",
  "url": "http://127.0.0.1:19090/v1/chat/completions",
  "maxInputTokens": 128000,
  "maxOutputTokens": 16384,
  "supportsToolCall": true,
  "supportsImages": true,
  "supportsReasoning": true,
  "useCustomProtocol": false
}
```

## 管理后台 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/accounts` | 获取所有账号及统计 |
| POST | `/admin/accounts` | 添加账号 |
| PUT | `/admin/accounts` | 更新账号 |
| DELETE | `/admin/accounts` | 删除账号 |
| GET | `/admin/workbuddy-config?api_key=xxx` | 同步指定账号的模型到 WB |
| GET | `/admin/workbuddy-config?api_key=xxx&models=a,b` | 只同步选中的模型 |
| GET | `/admin/workbuddy-config?all=1` | 同步全部启用账号 |
| GET | `/admin/stats` | 查看使用统计 |
| GET | `/admin/dashboard` | 管理后台页面 |

## 文件结构

```
codebuddy-local-proxy/
├── local_codebuddy_proxy.py      # 主程序
├── admin_dashboard.html          # 管理后台前端
├── proxy_config.example.json     # 配置示例
├── start_proxy.bat              # Windows 启动脚本
├── start_proxy.sh               # Linux/Mac 启动脚本
└── README.md
```

首次运行后会自动生成 `proxy_config.json` 持久化账号配置。

## 注意事项

- CN API 只支持流式请求（`stream: true`），非流式请求由代理自动聚合 SSE
- 每个 CK Key 对应一个 CodeBuddy 账号，需从浏览器 Cookies 获取
- WorkBuddy 和 `.codebuddy\models.json` 会自动同步写入
- 仅监听 `127.0.0.1`，不对外暴露，无需担心安全问题

## License

MIT
