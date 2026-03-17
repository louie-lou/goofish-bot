# Goofish Auto-Reply Bot (闲鱼自动客服机器人)

> An AI-powered customer service bot for [Goofish (闲鱼)](https://www.goofish.com), China's largest second-hand marketplace. Monitors WebSocket messages in real-time, auto-replies to buyer inquiries, handles price negotiations, and automates e-book delivery via Z-Library.

## 功能

- **WebSocket 实时监听** — 通过闲鱼 IM WebSocket 协议接收买家消息
- **智能分流** — 快速回复（关键词匹配） → AI 生成回复 → 人工升级
- **砍价策略** — 可配置的底价和谈判风格（firm / flexible / default）
- **电子书自动交付** — Z-Library 搜索 + 下载 + 邮件发送，全流程自动化
- **对话分析** — 统计高频问题、流失对话、砍价效果，AI 生成优化建议
- **Discord 通知** — 升级消息、交易事件、状态报告实时推送
- **Cookie 自动刷新** — 过期后通过 CDP 连接 Chrome 自动恢复登录态
- **macOS 自启** — launchd 服务一键安装/卸载

## 前置依赖

- **Python 3.10+**
- **[Playwright](https://playwright.dev/python/)** — 登录扫码、商品管理、Z-Library 下载
- **[OpenClaw](https://github.com/nicepkg/openclaw)** 或兼容 AI CLI — AI 回复引擎（见下方"替换 AI 后端"）
- **SMTP 邮箱**（如 QQ 邮箱）— 电子书邮件交付
- **Chrome 浏览器** — Cookie 自动刷新需要（可选）

## 快速开始

### 1. 安装依赖

```bash
pip3 install websockets playwright aiohttp browser_cookie3 zlibrary
playwright install chromium
```

### 2. 复制配置

```bash
mkdir -p ~/.openclaw/goofish
cp config.example.json ~/.openclaw/goofish/config.json
```

编辑 `~/.openclaw/goofish/config.json`，填写：
- `user_id` — 闲鱼用户 ID
- `products` — 商品配置（名称、价格、策略）
- `notification.discord_channel` — Discord 通知频道
- `email` — SMTP 邮箱配置（电子书交付用）
- `zlib` — Z-Library 账号（电子书搜索用）

### 3. 登录获取 Cookie

```bash
# 方式一：Playwright 扫码登录
python3bot.py login

# 方式二：从已登录的 Chrome 提取
python3bot.py login --from-chrome
```

### 4. 启动服务

```bash
# 前台运行
python3bot.py start

# 安装为 macOS 自启服务
python3bot.py install
```

### 5. 查看状态

```bash
python3bot.py status
```

## 配置说明

参考 [config.example.json](config.example.json)，主要配置：

| 配置项 | 说明 |
|--------|------|
| `products` | 商品配置：名称、策略、价格区间、自动化钩子 |
| `strategies` | 回复策略：对应 prompts/ 下的模板文件 |
| `quick_replies` | 关键词快速回复（不走 AI） |
| `ai` | AI 全局设置：语气、升级关键词、接管时长 |
| `email` | SMTP 邮箱（电子书交付） |
| `zlib` | Z-Library 账号（电子书搜索下载） |
| `notification` | Discord 通知配置 |

**配置继承**：`products[item_id]` → `products[默认]` → `strategies[strategy]` → `ai`

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `GOOFISH_PYTHON` | Python 可执行文件路径 | 自动发现 `python3` |
| `OPENCLAW_PATH` | OpenClaw 命令（空格分隔） | 自动发现 |

## 模块说明

| 文件 | 功能 |
|------|------|
| `bot.py` | 主服务：WebSocket 连接、消息处理、自动化、CLI |
| `reply.py` | AI 回复引擎：调用 OpenClaw Agent 生成回复 |
| `config.py` | 配置加载、路径管理、外部命令发现 |
| `message.py` | 闲鱼 WebSocket MessagePack 消息解码 |
| `product.py` | 商品管理：Playwright 自动化（发布/编辑/上下架） |
| `analyze.py` | 对话分析：统计 + AI 优化报告 + 建议应用 |
| `zlibrary.py` | Z-Library 搜索/下载 + 邮件交付 |
| `mailer.py` | SMTP 邮件发送（附件支持） |
| `prompts/` | AI prompt 模板（default/firm/flexible/service/ebook） |
| `docs/` | 设计文档（认证系统等） |

## 替换 AI 后端

默认使用 OpenClaw 作为 AI 后端。你可以替换为任何符合以下接口的 CLI 工具：

**调用方式**：
```bash
your-tool agent -m "SYSTEM_PROMPT\n\n对话历史..." --json --agent main
```

**期望输出**（JSON）：
```json
{
  "result": {
    "payloads": [{"text": "AI 生成的回复内容"}]
  }
}
```

设置 `OPENCLAW_PATH` 环境变量即可：
```bash
export OPENCLAW_PATH="/path/to/your-tool"
```

## CLI 命令

```bash
python3bot.py start              # 启动服务
python3bot.py login              # Playwright 扫码登录
python3bot.py login --from-chrome # 从 Chrome 提取 cookie
python3bot.py status             # 查看运行状态
python3bot.py install            # 安装 macOS 自启服务
python3bot.py uninstall          # 卸载自启服务

python3zlibrary.py search "书名"           # Z-Library 搜索
python3zlibrary.py deliver 1 --to a@b.com  # 下载 + 邮件发送

python3product.py list                      # 列出在售商品
python3product.py publish --title "..." --price 10 --images a.jpg

python3analyze.py analyze --days 7          # 统计分析
python3analyze.py report --send             # AI 优化报告
```

## 运行时文件

所有运行时数据存储在 `~/.openclaw/goofish/`（不进 git）：

```
~/.openclaw/goofish/
├── config.json           # 实际运行配置
├── cookies.json          # 闲鱼登录凭证
├── token_cache.json      # WebSocket token 缓存
├── device_id.txt         # 设备 ID（永久持久化）
├── chat_history.json     # 对话历史
├── status.json           # 运行状态
├── conversations/        # 对话事件日志（JSONL）
├── downloads/            # Z-Library 下载的电子书
└── reports/              # 分析报告
```

## License

[MIT](LICENSE)
