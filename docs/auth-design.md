# 闲鱼机器人 — 认证与持久登录设计

## 概述

闲鱼机器人通过 WebSocket 与闲鱼 IM 服务通信，认证链路涉及三个关键凭证：**Cookie**、**accessToken**、**device_id**。三者需要协调一致才能成功注册并收发消息。

## 认证链路

```
Cookie (浏览器登录态)
   ↓ HTTP API 签名
accessToken (IM 令牌)
   ↓ /reg 注册
WebSocket 会话 (收发消息)
```

### 1. Cookie — 浏览器登录态

- **来源**：用户在浏览器扫码登录 goofish.com 后产生
- **存储**：`~/.openclaw/goofish/cookies.json` (`{"cookie_string": "..."}`)
- **关键字段**：
  - `unb` — 用户 ID，用于标识身份
  - `_m_h5_tk` — API 签名 token（取 `_` 前 32 字符做 MD5 签名）
  - `cookie2` + `sgcookie` — 会话凭证，缺少会导致 `FAIL_SYS_SESSION_EXPIRED`
- **有效期**：数天到数周不等，过期需重新登录
- **获取方式**：见下文「登录方式」

### 2. accessToken — IM 令牌

- **来源**：通过 HTTP API `mtop.taobao.idlemessage.pc.login.token` 获取
- **请求签名**：`MD5("{cookie_token}&{timestamp_ms}&34839810&{data_json}")`
- **请求参数**：`data = {"appKey": IM_APP_KEY, "deviceId": device_id}`
- **存储**：`~/.openclaw/goofish/token_cache.json`
  ```json
  {
    "access_token": "oauth_k1:...",
    "obtained_at": 1773061601.9,
    "expires_at": 1773065201.9
  }
  ```
- **有效期**：1 小时（`TOKEN_REFRESH_INTERVAL = 3600`）
- **刷新策略**：
  - 启动时优先读缓存，未过期则复用
  - 过期后自动调 API 刷新
  - 后台 `token_refresh_loop()` 每小时刷新一次
  - API 响应中的 `Set-Cookie` 会自动更新 `_m_h5_tk`

### 3. device_id — 设备标识

- **格式**：UUID v4 + `-{user_id}` 后缀，如 `A3b7F9c1-D4e2-4f8a-B6c3-E5d1A7f0C2b4-XXXXXXXXXX`
- **存储**：`~/.openclaw/goofish/device_id.txt`（纯文本）
- **生命周期**：**永久持久化**，不随 cookie 更换而改变
- **为什么必须持久化**：
  - accessToken 是用 `(cookie, device_id)` 对申请的
  - WebSocket /reg 注册时需要同一个 device_id
  - 如果 device_id 变了但 token 没变 → `401: device id or appkey is not equal`
  - 如果每次重启随机生成 → 缓存的 token 和新 device_id 不匹配

## 凭证一致性规则

| 操作 | Cookie | Token Cache | Device ID |
|------|--------|-------------|-----------|
| 首次启动 | 必须已有 | 自动获取并缓存 | 自动生成并持久化 |
| 正常重启 | 不变 | 从缓存加载 | 从文件加载 |
| Cookie 过期重新登录 | **更新** | **清除**（需重新获取） | **保留** |
| Token 过期 | 不变 | 自动刷新 | 不变 |
| 手动清除调试 | - | 删除 token_cache.json | 不要删除 |

**核心原则**：device_id 一旦创建就不再改变。换 cookie 只清 token cache。

## 登录方式

### 方式 1：Playwright 扫码登录（推荐）

```bash
python3 goofish/bot.py login
```

1. 启动 Chromium 浏览器，打开 goofish.com
2. 用户用闲鱼 App 扫码
3. 轮询检测 `unb` cookie 出现（最长 120 秒）
4. 提取所有 `.goofish.com` + `.taobao.com` 域的 cookie
5. 保存到 `cookies.json`，清除 token cache

**依赖**：`playwright` + Chromium 浏览器

### 方式 2：Chrome Cookie 提取

```bash
python3 goofish/bot.py login --from-chrome
```

1. 读取 Chrome 本地 Cookies SQLite 数据库
2. 用 macOS Keychain 中的 Chrome Safe Storage 密钥解密
3. 提取 `.goofish.com` 域下所有 cookie
4. 保存到 `cookies.json`，清除 token cache

**依赖**：`browser_cookie3`（自带 `pycryptodomex` + `lz4`）
**前提**：用户已在 Chrome 中登录 goofish.com

### 方式 3：手动粘贴（备用）

当 Playwright 未安装时自动回退。用户从浏览器 Console 执行 `document.cookie` 后粘贴。

## 运行时文件

```
~/.openclaw/goofish/
├── config.json          # 业务配置（商品、策略、快速回复等）
├── cookies.json         # Cookie 登录态（敏感，chmod 600）
├── token_cache.json     # accessToken 缓存（自动管理）
├── device_id.txt        # 设备 ID（永久保留）
├── chat_history.json    # 对话历史
└── status.json          # 运行状态
```

## WebSocket 注册流程

```python
# 1. 连接
ws = websockets.connect("wss://wss-goofish.dingtalk.com/")

# 2. 注册
await ws.send({
    "lwp": "/reg",
    "headers": {
        "app-key": IM_APP_KEY,      # 444e9908a51d1cb236a27862abc769c9
        "token": access_token,       # 从 HTTP API 获取
        "did": device_id,            # 持久化的设备 ID
        "sync": "0,0;0;0;",
        ...
    }
})

# 3. 同步状态
await ws.send({
    "lwp": "/r/SyncStatus/ackDiff",
    "body": [{"pipeline": "sync", "pts": now_ms * 1000, ...}]
})
```

## 消息发送

```python
# cid 可能已带 @goofish 后缀（来自 extract_chat_message）
# send_reply 会自动检测，避免重复添加
cid_full = cid if "@goofish" in cid else f"{cid}@goofish"

await ws.send({
    "lwp": "/r/MessageSend/sendByReceiverScope",
    "body": [
        {
            "cid": cid_full,
            "content": {"contentType": 101, "custom": {"type": 1, "data": base64_text}},
            ...
        },
        {"actualReceivers": [f"{to_id}@goofish", f"{my_id}@goofish"]}
    ]
})
```

## 常见错误

| 错误 | 原因 | 解决 |
|------|------|------|
| `401: device id or appkey is not equal` | device_id 与 token 不匹配 | 删除 token_cache.json（**不要删** device_id.txt），重启 |
| `FAIL_SYS_SESSION_EXPIRED` | Cookie 过期 | 重新登录：`bot.py login` 或 `bot.py login --from-chrome` |
| `RGV587_ERROR` | Token API 被限流 | 等待冷却（数小时），使用 token 缓存减少请求 |
| `conversation not exist` | cid 格式错误（如 `xxx@goofish@goofish`） | 已修复：send_reply 自动检测 @goofish 后缀 |
| `500: conversation not exist` | 注册未成功但 bot 继续运行 | 检查 reg 响应是否有 401 |
