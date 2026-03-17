#!/usr/bin/env python3
"""闲鱼自动客服机器人 — WebSocket 连接 + 消息监听 + 智能分流

用法：
    python3 goofish/bot.py start              # 启动服务
    python3 goofish/bot.py login              # 扫码登录获取 cookie（Playwright）
    python3 goofish/bot.py login --from-chrome # 从 Chrome 提取已有 cookie
    python3 goofish/bot.py status             # 查看运行状态
    python3 goofish/bot.py install            # 安装 launchd 自启服务
    python3 goofish/bot.py uninstall          # 卸载 launchd 服务
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import signal
import sys
import re
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

# 从拆分模块导入（保持 bot.py 的公共 API 不变）
from message import decode_message, extract_chat_message, MessagePackDecoder, decrypt_msgpack
from config import load_config, load_cookies, get_item_config, PYTHON_CMD, OPENCLAW_CMD

# --- 配置 ---

WEBSOCKET_URL = "wss://wss-goofish.dingtalk.com/"
APP_KEY = "34839810"  # 用于 HTTP API 签名
IM_APP_KEY = "444e9908a51d1cb236a27862abc769c9"  # 用于 WebSocket /reg 注册
TOKEN_API_URL = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
HEARTBEAT_INTERVAL = 15
TOKEN_REFRESH_INTERVAL = 3600  # 1 小时刷新一次 token
RECONNECT_BASE_DELAY = 5
RECONNECT_MAX_DELAY = 60

CONFIG_DIR = os.path.expanduser("~/.openclaw/goofish")
DEFAULT_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_COOKIES_PATH = os.path.join(CONFIG_DIR, "cookies.json")
STATUS_FILE = os.path.join(CONFIG_DIR, "status.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "chat_history.json")
TOKEN_CACHE_FILE = os.path.join(CONFIG_DIR, "token_cache.json")
DEVICE_ID_FILE = os.path.join(CONFIG_DIR, "device_id.txt")
CONVERSATIONS_DIR = os.path.join(CONFIG_DIR, "conversations")
REPLY_COOLDOWN = 5  # 同一会话最短回复间隔（秒）
DEDUP_MAX_SIZE = 1000  # 去重集合最大容量

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("goofish-bot")


# --- 工具函数 ---

def parse_cookies(cookies_str):
    """将 cookie 字符串解析为 dict。"""
    cookies = {}
    for item in cookies_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def generate_mid():
    """生成消息 ID（参考 goofish-auto-delivery）。"""
    random_part = int(1000 * random.random())
    timestamp = int(time.time() * 1000)
    return f"{random_part}{timestamp} 0"


def generate_uuid():
    """生成 UUID for 消息发送。"""
    timestamp = int(time.time() * 1000)
    return f"-{timestamp}1"


def generate_device_id(user_id):
    """生成设备 ID — UUID v4 格式 + 用户 ID 后缀。"""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    result = []
    for i in range(36):
        if i in [8, 13, 18, 23]:
            result.append("-")
        elif i == 14:
            result.append("4")
        elif i == 19:
            rand_val = int(16 * random.random())
            result.append(chars[(rand_val & 0x3) | 0x8])
        else:
            rand_val = int(16 * random.random())
            result.append(chars[rand_val])
    return "".join(result) + "-" + str(user_id)


def generate_sign(token, t, data):
    """生成 HTTP API 签名：MD5(token&t&appKey&data)。"""
    msg = f"{token}&{t}&{APP_KEY}&{data}"
    return hashlib.md5(msg.encode("utf-8")).hexdigest()


# --- Bot 核心 ---

class GoofishBot:
    def __init__(self, config, cookies_str):
        self.config = config
        self.cookies_str = cookies_str
        self.cookies = parse_cookies(cookies_str)

        if "unb" not in self.cookies:
            log.error("Cookie 中缺少 'unb' 字段，请重新登录")
            sys.exit(1)

        self.my_id = self.cookies["unb"]
        self.cookie_token = self.cookies.get("_m_h5_tk", "").split("_")[0]  # 用于 HTTP API 签名
        self.access_token = None  # WebSocket 注册用的 accessToken，通过 API 获取
        self.last_token_refresh = 0
        self.device_id = self._load_or_create_device_id()
        self.ws = None
        self.running = False
        self.stats = {
            "started_at": None,
            "messages_received": 0,
            "replies_sent": 0,
            "escalated": 0,
            "quick_replies": 0,
            "image_messages": 0,
            "ws_reconnects": 0,
            "last_message_at": None,
            "ws_connected_at": None,
            "errors": [],  # 最近 10 条错误
        }
        self.chat_history = self._load_history()  # cid -> list of messages
        self.manual_takeover = {}  # cid -> timestamp，人工接管的会话
        self.manual_takeover_duration = config.get("ai", {}).get("manual_takeover_minutes", 30) * 60
        self._session = None  # aiohttp session
        self._seen_msg_ids = set()  # 消息去重
        self._last_reply_time = {}  # cid -> timestamp，限流
        self._reply_locks = {}  # cid -> asyncio.Lock，防止同一会话并发 AI 回复

        # 延迟导入 reply 模块
        self._reply_mod = None

    def _load_or_create_device_id(self):
        """加载或创建持久化的 device_id（避免每次重启变化导致 reg 401）。"""
        if os.path.exists(DEVICE_ID_FILE):
            try:
                with open(DEVICE_ID_FILE) as f:
                    did = f.read().strip()
                if did:
                    log.info(f"使用持久化 device_id: {did[:20]}...")
                    return did
            except Exception:
                pass
        did = generate_device_id(self.my_id)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(DEVICE_ID_FILE, "w") as f:
            f.write(did)
        log.info(f"创建新 device_id: {did[:20]}...")
        return did

    def _load_history(self):
        """从文件加载对话历史。"""
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE) as f:
                    data = json.load(f)
                log.info(f"加载对话历史: {len(data)} 个会话")
                return data
            except Exception as e:
                log.warning(f"加载对话历史失败: {e}")
        return {}

    def _save_history(self):
        """保存对话历史到文件。"""
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(HISTORY_FILE, "w") as f:
                json.dump(self.chat_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning(f"保存对话历史失败: {e}")

    def _log_conversation_event(self, cid, event_dict):
        """记录富对话事件到 per-conversation JSONL 日志（用于后续分析优化）。"""
        event_dict.setdefault("ts", int(time.time()))
        os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
        safe_cid = re.sub(r'[^a-zA-Z0-9_-]', '_', str(cid))[:200]
        path = os.path.join(CONVERSATIONS_DIR, f"{safe_cid}.jsonl")
        try:
            with open(path, "a") as f:
                f.write(json.dumps(event_dict, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning(f"写入对话日志失败: {e}")

    @property
    def reply_engine(self):
        if self._reply_mod is None:
            sys.path.insert(0, SCRIPT_DIR)
            from reply import generate_reply, should_escalate
            self._reply_mod = {"generate_reply": generate_reply, "should_escalate": should_escalate}
        return self._reply_mod

    def _load_cached_token(self):
        """尝试从缓存加载 token，有效则返回 True。"""
        if not os.path.exists(TOKEN_CACHE_FILE):
            return False
        try:
            with open(TOKEN_CACHE_FILE) as f:
                cache = json.load(f)
            if time.time() < cache.get("expires_at", 0):
                self.access_token = cache["access_token"]
                self.last_token_refresh = cache["obtained_at"]
                log.info("从缓存加载 accessToken")
                return True
        except Exception:
            pass
        return False

    def _save_token_cache(self):
        """保存 token 到缓存文件。"""
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(TOKEN_CACHE_FILE, "w") as f:
                json.dump({
                    "access_token": self.access_token,
                    "obtained_at": self.last_token_refresh,
                    "expires_at": self.last_token_refresh + TOKEN_REFRESH_INTERVAL,
                }, f)
            os.chmod(TOKEN_CACHE_FILE, 0o600)
        except Exception as e:
            log.warning(f"保存 token 缓存失败: {e}")

    async def refresh_token(self):
        """通过 HTTP API 获取 WebSocket accessToken（优先读缓存）。"""
        if self._load_cached_token():
            return True
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession()

        t = str(int(time.time() * 1000))
        data_val = json.dumps({
            "appKey": IM_APP_KEY,
            "deviceId": self.device_id,
        }, separators=(",", ":"))

        sign = generate_sign(self.cookie_token, t, data_val)

        params = {
            "jsv": "2.7.2",
            "appKey": APP_KEY,
            "t": t,
            "sign": sign,
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.taobao.idlemessage.pc.login.token",
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.im.0.0",
        }
        data = {"data": data_val}

        headers = {
            "Cookie": self.cookies_str,
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Referer": "https://www.goofish.com/",
            "Origin": "https://www.goofish.com",
        }

        try:
            async with self._session.post(TOKEN_API_URL, params=params, data=data, headers=headers) as resp:
                result = await resp.json()
                log.debug(f"Token API 响应: {json.dumps(result, ensure_ascii=False)[:500]}")

                if "data" in result and "accessToken" in result["data"]:
                    self.access_token = result["data"]["accessToken"]
                    self.last_token_refresh = time.time()
                    self._save_token_cache()
                    log.info(f"获取 accessToken 成功: {self.access_token[:20]}...")
                    return True
                else:
                    log.error(f"获取 accessToken 失败: {json.dumps(result, ensure_ascii=False)[:300]}")
                    # 尝试从 Set-Cookie 更新 cookies
                    set_cookies = resp.headers.getall("Set-Cookie", [])
                    updated = False
                    for sc in set_cookies:
                        if "_m_h5_tk=" in sc:
                            new_tk = sc.split("_m_h5_tk=")[1].split(";")[0]
                            self.cookie_token = new_tk.split("_")[0]
                            # 同时更新 cookie 字符串
                            self.cookies["_m_h5_tk"] = new_tk
                            self.cookies_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
                            log.info(f"从响应更新 _m_h5_tk: {new_tk[:20]}...")
                            updated = True
                        elif "_m_h5_tk_enc=" in sc:
                            new_enc = sc.split("_m_h5_tk_enc=")[1].split(";")[0]
                            self.cookies["_m_h5_tk_enc"] = new_enc
                            self.cookies_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
                    if updated:
                        # 保存更新后的 cookies
                        with open(DEFAULT_COOKIES_PATH, "w") as f:
                            json.dump({"cookie_string": self.cookies_str}, f, ensure_ascii=False)
                    return False
        except Exception as e:
            log.error(f"Token API 请求失败: {e}")
            return False

    async def connect(self):
        """建立 WebSocket 连接。"""
        import websockets

        # 先获取 accessToken
        if not self.access_token or (time.time() - self.last_token_refresh) >= TOKEN_REFRESH_INTERVAL:
            success = await self.refresh_token()
            if not success:
                # 重试一次（可能 _m_h5_tk 已更新）
                success = await self.refresh_token()
            if not success:
                raise Exception("无法获取 accessToken，请检查 cookie 是否过期")

        headers = {
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cache-Control": "no-cache",
            "Connection": "Upgrade",
            "Cookie": self.cookies_str,
            "Host": "wss-goofish.dingtalk.com",
            "Origin": "https://www.goofish.com",
            "Pragma": "no-cache",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        }

        try:
            self.ws = await websockets.connect(
                WEBSOCKET_URL,
                additional_headers=headers,
                ping_interval=None,
                ping_timeout=None,
            )
        except TypeError:
            self.ws = await websockets.connect(
                WEBSOCKET_URL,
                extra_headers=headers,
                ping_interval=None,
                ping_timeout=None,
            )

        log.info("WebSocket 已连接")
        self.stats["ws_connected_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        # 注册 — 使用 IM_APP_KEY 和 accessToken
        await self._send({
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": IM_APP_KEY,
                "token": self.access_token,
                "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(macOS/10.15.7) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid(),
            },
        })

        await asyncio.sleep(1)

        # 同步状态
        now_ms = int(time.time() * 1000)
        await self._send({
            "lwp": "/r/SyncStatus/ackDiff",
            "headers": {"mid": generate_mid()},
            "body": [{
                "pipeline": "sync",
                "tooLong2Tag": "PNM,1",
                "channel": "sync",
                "topic": "sync",
                "highPts": 0,
                "pts": now_ms * 1000,
                "seq": 0,
                "timestamp": now_ms,
            }],
        })

        log.info(f"已注册，用户 ID: {self.my_id}")

    async def _send(self, msg):
        """发送 WebSocket 消息。"""
        await self.ws.send(json.dumps(msg))

    async def send_reply(self, cid, to_id, text):
        """发送回复消息。"""
        content = {"contentType": 1, "text": {"text": text}}
        content_b64 = base64.b64encode(json.dumps(content).encode("utf-8")).decode("utf-8")

        # 确保 ID 带 @goofish 后缀（避免重复添加）
        cid_full = cid if "@goofish" in str(cid) else f"{cid}@goofish"
        to_full = to_id if "@goofish" in str(to_id) else f"{to_id}@goofish"
        my_full = self.my_id if "@goofish" in str(self.my_id) else f"{self.my_id}@goofish"

        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {"mid": generate_mid()},
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": cid_full,
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {"type": 1, "data": content_b64},
                    },
                    "extension": {"extJson": "{}"},
                    "ctx": {"appVersion": "1.0", "platform": "web"},
                    "msgReadStatusSetting": 1,
                },
                {
                    "actualReceivers": [to_full, my_full],
                },
            ],
        }

        await self._send(msg)
        log.info(f"已回复 [{cid}]: {text[:50]}...")

    async def heartbeat_loop(self):
        """心跳保活循环。"""
        while self.running:
            try:
                await self._send({"lwp": "/!", "headers": {"mid": generate_mid()}})
            except Exception as e:
                log.warning(f"心跳发送失败: {e}")
                break
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def handle_message(self, chat_msg):
        """处理一条聊天消息。"""
        # 系统消息（交易通知等）→ 检测 + 通知
        if chat_msg.get("msg_type") == "system":
            await self._handle_system_message(chat_msg)
            return

        # 图片消息 → 友好回复
        if chat_msg.get("msg_type") == "image":
            sender = chat_msg.get("sender_id", "")
            if sender and sender != self.my_id:
                cid = chat_msg.get("cid", "")
                if cid:
                    self.stats["image_messages"] += 1
                    item_id = chat_msg.get("item_id", "")
                    item_cfg = get_item_config(self.config, item_id)
                    if item_cfg.get("strategy") == "ebook":
                        reply = "图片我这边看不太清，你直接打字发下书名吧，我马上帮你搜~"
                    else:
                        reply = "收到图片，我看一下~"
                    await self.send_reply(cid, sender, reply)
                    self.stats["replies_sent"] += 1
                    self._log_conversation_event(cid, {
                        "type": "msg_seller_image_ack",
                        "content": reply,
                    })
                    log.info(f"图片消息回复 [{cid}]")
            return

        sender = chat_msg["sender_id"]
        content = chat_msg["content"]
        cid = chat_msg["cid"]
        item_id = chat_msg["item_id"]

        # 消息去重
        dedup_key = f"{cid}:{sender}:{content[:50]}:{chat_msg.get('msg_time', '')}"
        if dedup_key in self._seen_msg_ids:
            log.debug(f"重复消息，跳过: {dedup_key[:80]}")
            return
        self._seen_msg_ids.add(dedup_key)
        if len(self._seen_msg_ids) > DEDUP_MAX_SIZE:
            self._seen_msg_ids.clear()

        # 自己手动发的消息 → 记录到历史 + 标记人工接管
        if sender == self.my_id:
            if cid and content and content.strip():
                if cid not in self.chat_history:
                    self.chat_history[cid] = []
                self.chat_history[cid].append({"role": "seller", "content": content})
                max_history = self.config.get("ai", {}).get("max_history_messages", 10)
                self.chat_history[cid] = self.chat_history[cid][-max_history:]
                self.manual_takeover[cid] = time.time()
                self._save_history()
                self._log_conversation_event(cid, {
                    "type": "msg_seller_manual",
                    "content": content,
                })
                log.info(f"人工回复 [{cid}]: {content[:80]}（暂停自动回复 {self.manual_takeover_duration // 60} 分钟）")
            return

        # 忽略空消息
        if not content or not content.strip():
            return

        self.stats["messages_received"] += 1
        self.stats["last_message_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        log.info(f"收到消息 [{cid}] 来自 {sender}: {content[:80]}")

        # 记录对话历史
        if cid not in self.chat_history:
            self.chat_history[cid] = []
        self.chat_history[cid].append({"role": "buyer", "content": content})
        # 保持历史长度
        max_history = self.config.get("ai", {}).get("max_history_messages", 10)
        self.chat_history[cid] = self.chat_history[cid][-max_history:]
        self._save_history()
        self._log_conversation_event(cid, {
            "type": "msg_buyer",
            "content": content,
            "item_id": item_id,
            "sender_id": sender,
        })

        # 从普通消息中检测交易事件（闲鱼交易卡片消息有时作为文本接收）
        trade_event = self._detect_trade_event(None, content)
        if trade_event:
            await self._handle_trade_event(trade_event, cid, item_id, chat_msg)
            # 付款后告知买家正在处理
            if trade_event == "paid":
                clean_cid = cid.replace("@goofish", "")
                clean_sender = sender.replace("@goofish", "")
                if clean_sender:
                    await self.send_reply(
                        clean_cid, clean_sender,
                        "收到付款，正在帮你找书，找到后会第一时间发到你邮箱，稍等一下哈",
                    )
            return  # 交易事件消息不走 AI 回复

        # 过滤系统推送垃圾消息（蚂蚁森林等）
        spam_keywords = ["蚂蚁森林", "能量可领", "芭芭农场", "淘金币"]
        if any(kw in content for kw in spam_keywords):
            log.info(f"过滤系统推送消息: {content[:50]}")
            return

        # 检查是否人工接管中
        if cid in self.manual_takeover:
            elapsed = time.time() - self.manual_takeover[cid]
            if elapsed < self.manual_takeover_duration:
                remaining = int((self.manual_takeover_duration - elapsed) / 60)
                log.info(f"会话 [{cid}] 人工接管中（剩余 {remaining} 分钟），跳过自动回复")
                return
            else:
                del self.manual_takeover[cid]
                log.info(f"会话 [{cid}] 人工接管已过期，恢复自动回复")

        # per-cid 锁：防止同一会话并发 AI 回复（买家连发多条消息时）
        lock = self._reply_locks.setdefault(cid, asyncio.Lock())
        search_match = None
        search_query = None

        async with lock:
            # 限流：同一会话冷却中不重复回复
            now = time.time()
            last_reply = self._last_reply_time.get(cid, 0)
            if now - last_reply < REPLY_COOLDOWN:
                log.info(f"会话 [{cid}] 冷却中（{REPLY_COOLDOWN}秒内已回复），跳过")
                return

            # 获取商品配置
            item_cfg = get_item_config(self.config, item_id)

            if not item_cfg.get("auto_reply", True):
                log.info(f"商品 {item_id} 未启用自动回复，跳过")
                return

            # 快速回复：关键词匹配，跳过 AI
            quick_reply = self._match_quick_reply(content)
            if quick_reply:
                await asyncio.sleep(random.uniform(1.5, 3.5))
                await self.send_reply(cid, sender, quick_reply)
                self.stats["replies_sent"] += 1
                self.stats["quick_replies"] += 1
                self._last_reply_time[cid] = time.time()
                self.chat_history[cid].append({"role": "seller", "content": quick_reply})
                self._save_history()
                self._log_conversation_event(cid, {
                    "type": "msg_seller_quick",
                    "content": quick_reply,
                    "keyword_matched": content.strip(),
                    "item_id": item_id,
                })
                log.info(f"快速回复 [{cid}]: {quick_reply[:50]}")
                return

            # 检查升级关键词
            escalation_keywords = self.config.get("ai", {}).get("escalation_keywords", [])
            if self.reply_engine["should_escalate"](content, escalation_keywords):
                self.stats["escalated"] += 1
                self._log_conversation_event(cid, {
                    "type": "escalation",
                    "content": content,
                    "reason": "keyword",
                    "item_id": item_id,
                })
                log.info(f"消息触发升级: {content[:50]}")
                await self._notify_discord(sender, content, cid, item_id, reason="升级关键词")
                return

            # AI 回复
            result = self.reply_engine["generate_reply"](
                content, item_cfg, self.chat_history.get(cid)
            )

            if result.get("needs_human") or not result.get("reply"):
                self.stats["escalated"] += 1
                self._log_conversation_event(cid, {
                    "type": "escalation",
                    "content": content,
                    "reason": "ai_uncertain",
                    "error": result.get("error", ""),
                    "item_id": item_id,
                })
                log.info(f"AI 建议人工介入: {result.get('error', '不确定如何回复')}")
                await self._notify_discord(sender, content, cid, item_id, reason="AI 不确定")
                return

            reply_text = result["reply"]

            # 提取搜索标记 [搜索:xxx]，不发给买家
            search_match = re.search(r'\[搜索[:：](.+?)\]\s*$', reply_text)
            if search_match:
                search_query = search_match.group(1).strip()
                reply_text = re.sub(r'\n?\[搜索[:：].+?\]\s*$', '', reply_text).strip()
            elif item_cfg.get("strategy") == "ebook":
                log.warning(f"Ebook 回复未包含搜索标记 [{cid}]: {reply_text[:80]}")

            # 回复延迟（模拟打字）
            delay = item_cfg.get("reply_delay_seconds", 3)
            if delay > 0:
                await asyncio.sleep(delay)

            await self.send_reply(cid, sender, reply_text)
            self.stats["replies_sent"] += 1
            self._last_reply_time[cid] = time.time()

            # 记录自己的回复
            self.chat_history[cid].append({"role": "seller", "content": reply_text})
            self._save_history()
            self._log_conversation_event(cid, {
                "type": "msg_seller_ai",
                "content": reply_text,
                "prompt_template": item_cfg.get("prompt_template", "default.md"),
                "strategy": item_cfg.get("strategy", "default"),
                "item_id": item_id,
            })

        # AI 触发搜索（锁外执行，不阻塞后续消息处理）
        if search_match and search_query:
            log.info(f"AI 触发搜索: {search_query}")
            asyncio.create_task(self._zlib_search_and_reply(cid, sender, search_query, item_id))

    async def _zlib_search_and_reply(self, cid, sender, query, item_id):
        """后台搜索 Z-Library 并将结果发给买家确认。"""
        clean_cid = cid.replace("@goofish", "")
        clean_sender = sender.replace("@goofish", "")
        try:
            import subprocess
            cmd = [
                PYTHON_CMD,
                os.path.join(SCRIPT_DIR, "zlibrary.py"),
                "search", query, "--json",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and result.stdout.strip():
                results = json.loads(result.stdout)
                if results:
                    buyer_msg = self._format_zlib_results_for_buyer(query, results)
                    if clean_sender:
                        await self.send_reply(clean_cid, clean_sender, buyer_msg)
                        # 记录到对话历史，让 AI 知道搜索结果已发
                        self.chat_history.setdefault(cid, []).append(
                            {"role": "seller", "content": buyer_msg}
                        )
                        self._save_history()
                        self._log_event(cid, "msg_seller_ai", buyer_msg, item_id=item_id)
                    # 通知卖家
                    discord_msg = self._format_zlib_results(cid, query, results)
                    await self._notify_trade_event("zlib_search", discord_msg, cid, item_id)
                    log.info(f"Z-Library 搜索完成并发送给买家: {query}")
                else:
                    no_result_msg = f"抱歉，暂时没有搜索到《{query}》，我再帮你找找"
                    if clean_sender:
                        await self.send_reply(clean_cid, clean_sender, no_result_msg)
                        self.chat_history.setdefault(cid, []).append(
                            {"role": "seller", "content": no_result_msg}
                        )
                        self._save_history()
                        self._log_event(cid, "msg_seller_ai", no_result_msg, item_id=item_id)
            else:
                log.warning(f"Z-Library 搜索失败: {result.stderr[:200]}")
                fail_msg = f"抱歉，《{query}》暂时没搜到，我再想办法帮你找找看"
                if clean_sender:
                    await self.send_reply(clean_cid, clean_sender, fail_msg)
                    self.chat_history.setdefault(cid, []).append(
                        {"role": "seller", "content": fail_msg}
                    )
                    self._save_history()
                    self._log_event(cid, "msg_seller_ai", fail_msg, item_id=item_id)
        except subprocess.TimeoutExpired:
            log.warning(f"Z-Library 搜索超时: {query}")
            timeout_msg = f"搜索《{query}》花的时间比较长，我继续帮你找，有结果会告诉你"
            if clean_sender:
                await self.send_reply(clean_cid, clean_sender, timeout_msg)
                self.chat_history.setdefault(cid, []).append(
                    {"role": "seller", "content": timeout_msg}
                )
                self._save_history()
                self._log_event(cid, "msg_seller_ai", timeout_msg, item_id=item_id)
        except Exception as e:
            log.warning(f"Z-Library 搜索异常: {e}")
            err_msg = f"抱歉，《{query}》暂时没搜到，我再想办法帮你找找看"
            if clean_sender:
                await self.send_reply(clean_cid, clean_sender, err_msg)
                self.chat_history.setdefault(cid, []).append(
                    {"role": "seller", "content": err_msg}
                )
                self._save_history()
                self._log_event(cid, "msg_seller_ai", err_msg, item_id=item_id)

    # --- 交易事件处理 ---

    async def _handle_system_message(self, chat_msg):
        """处理系统消息 — 交易通知等。"""
        ct = chat_msg.get("content_type")
        raw = chat_msg.get("raw_content", {})
        cid = chat_msg.get("cid", "")
        item_id = chat_msg.get("item_id", "")

        log.info(f"系统消息 ct={ct} cid={cid} item={item_id}: "
                 f"{json.dumps(raw, ensure_ascii=False)[:300]}")

        event = self._detect_trade_event(ct, raw)
        if event:
            await self._handle_trade_event(event, cid, item_id, chat_msg)

    def _record_error(self, error_msg):
        """记录错误到 stats.errors（保留最近 10 条）。"""
        entry = f"{time.strftime('%H:%M:%S')} {error_msg[:100]}"
        self.stats["errors"].append(entry)
        self.stats["errors"] = self.stats["errors"][-10:]

    def _match_quick_reply(self, content):
        """检查消息是否匹配快速回复关键词。返回回复文本或 None。"""
        quick_replies = self.config.get("quick_replies", {})
        content_stripped = content.strip()
        for keyword, reply in quick_replies.items():
            if keyword in content_stripped:
                return reply
        return None

    def _detect_trade_event(self, content_type, raw_content):
        """从系统消息中识别交易事件类型。

        Returns: "placed_order" | "paid" | "shipped" | "confirmed" | None
        """
        text = str(raw_content)
        if any(kw in text for kw in ["拍下", "已下单", "下单", "已拍"]):
            return "placed_order"
        if any(kw in text for kw in ["已付款", "付款成功", "支付成功"]):
            return "paid"
        if any(kw in text for kw in ["已发货", "发货"]):
            return "shipped"
        if any(kw in text for kw in ["确认收货", "交易成功", "交易完成"]):
            return "confirmed"
        return None

    async def _handle_trade_event(self, event, cid, item_id, chat_msg):
        """处理交易事件：通知 + 自动化。"""
        event_names = {
            "placed_order": "买家已拍下",
            "paid": "买家已付款",
            "shipped": "已发货",
            "confirmed": "交易完成",
        }
        event_name = event_names.get(event, event)
        log.info(f"交易事件: {event_name} cid={cid} item={item_id}")
        self._log_conversation_event(cid, {
            "type": "trade_event",
            "event": event,
            "item_id": item_id,
        })

        # Discord 通知
        await self._notify_trade_event(event, event_name, cid, item_id)

        # 检查自动化任务
        product_cfg = get_item_config(self.config, item_id)
        automation = product_cfg.get("automation", {})
        trigger = automation.get(f"on_{event}")
        if trigger:
            log.info(f"交易事件 [{event}] 触发自动化: {trigger}")
            await self._execute_automation(trigger, cid, item_id, chat_msg)

    async def _notify_trade_event(self, event, event_name, cid, item_id):
        """发送交易事件 Discord 通知。"""
        notification = self.config.get("notification", {})
        channel = notification.get("discord_channel", "")
        if not channel:
            return

        emoji = {
            "placed_order": "🛒", "paid": "💰",
            "shipped": "📦", "confirmed": "✅",
        }.get(event, "📌")
        msg = f"{emoji} {event_name}\n商品: {item_id}\n会话: {cid}"

        try:
            import subprocess
            cmd = [
                *OPENCLAW_CMD,
                "message", "send",
                "--channel", "discord",
                "--target", channel,
                "--message", msg,
            ]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.warning(f"交易通知发送失败: {e}")

    async def _execute_automation(self, trigger, cid, item_id, chat_msg):
        """执行自动化任务。"""
        action = trigger.get("action", "notify_only")

        if action == "send_message":
            text = trigger["message"].format(cid=cid, item_id=item_id)
            sender = chat_msg.get("sender_id", "")
            clean_cid = cid.replace("@goofish", "")
            clean_sender = sender.replace("@goofish", "")
            if clean_sender:
                await self.send_reply(clean_cid, clean_sender, text)
                log.info(f"自动化发送消息: {text[:80]}")

        elif action == "openclaw_agent":
            agent = trigger.get("agent", "main")
            msg = trigger.get("message", "").format(cid=cid, item_id=item_id)
            try:
                import subprocess
                cmd = [
                    *OPENCLAW_CMD,
                    "agent", "-m", msg, "--agent", agent, "--json",
                ]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log.info(f"自动化触发 Agent [{agent}]: {msg[:80]}")
            except Exception as e:
                log.warning(f"自动化执行失败: {e}")

        elif action == "zlib_search":
            # 下单时搜索，发给买家确认，引导付款
            book_name = self._extract_book_name(cid)
            sender = chat_msg.get("sender_id", "")
            clean_cid = cid.replace("@goofish", "")
            clean_sender = sender.replace("@goofish", "")
            if book_name:
                try:
                    import subprocess
                    cmd = [
                        PYTHON_CMD,
                        os.path.join(SCRIPT_DIR, "zlibrary.py"),
                        "search", book_name, "--json",
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if result.returncode == 0 and result.stdout.strip():
                        try:
                            results = json.loads(result.stdout)
                            # 发给买家确认 + 引导付款
                            buyer_msg = self._format_zlib_results_for_buyer(book_name, results)
                            if clean_sender:
                                await self.send_reply(clean_cid, clean_sender, buyer_msg)
                                log.info(f"已发送搜索结果给买家确认: {book_name}")
                            # 通知卖家（Discord）
                            discord_msg = self._format_zlib_results(cid, book_name, results)
                            await self._notify_trade_event("automation", discord_msg, cid, item_id)
                        except json.JSONDecodeError:
                            await self._notify_trade_event(
                                "automation",
                                f"📚 Z-Library 搜索完成但解析失败。书名: {book_name}\n会话: {cid}",
                                cid, item_id,
                            )
                    else:
                        error = result.stderr[:200] if result.stderr else "无输出"
                        if clean_sender:
                            await self.send_reply(
                                clean_cid, clean_sender,
                                f"抱歉，暂时没有搜索到《{book_name}》，我再帮你找找，稍等哈",
                            )
                        await self._notify_trade_event(
                            "automation",
                            f"📚 Z-Library 搜索失败: {error}\n书名: {book_name}\n会话: {cid}",
                            cid, item_id,
                        )
                except subprocess.TimeoutExpired:
                    await self._notify_trade_event(
                        "automation",
                        f"📚 Z-Library 搜索超时\n书名: {book_name}\n会话: {cid}",
                        cid, item_id,
                    )
                except Exception as e:
                    log.warning(f"Z-Library 搜索自动化失败: {e}")
            else:
                await self._notify_trade_event(
                    "automation",
                    f"📚 买家已下单，但未能从对话中提取书名。请手动确认。\n会话: {cid}",
                    cid, item_id,
                )

        elif action == "zlib_deliver":
            # 付款后自动下载 + 邮件发送
            book_name = self._extract_book_name(cid)
            buyer_email = self._extract_buyer_email(cid)
            sender = chat_msg.get("sender_id", "")
            clean_cid = cid.replace("@goofish", "")
            clean_sender = sender.replace("@goofish", "")

            if not book_name:
                await self._notify_trade_event(
                    "automation",
                    f"📚 买家已付款，但未能提取书名，请手动处理。\n会话: {cid}",
                    cid, item_id,
                )
            elif not buyer_email:
                # 没有邮箱，通知卖家手动 deliver
                await self._notify_trade_event(
                    "automation",
                    f"📚 买家已付款，书名: {book_name}\n⚠️ 未检测到买家邮箱，请手动交付\n"
                    f"下载: python3 goofish/zlibrary.py download 1\n会话: {cid}",
                    cid, item_id,
                )
                if clean_sender:
                    await self.send_reply(
                        clean_cid, clean_sender,
                        "收到付款！麻烦留一下你的邮箱地址，我马上把书发给你",
                    )
            else:
                # 有书名+邮箱，自动交付
                try:
                    import subprocess
                    # 先搜索（确保缓存最新）
                    search_cmd = [
                        PYTHON_CMD,
                        os.path.join(SCRIPT_DIR, "zlibrary.py"),
                        "search", book_name, "--json",
                    ]
                    subprocess.run(search_cmd, capture_output=True, text=True, timeout=120)

                    # 下载 + 发送
                    deliver_cmd = [
                        PYTHON_CMD,
                        os.path.join(SCRIPT_DIR, "zlibrary.py"),
                        "deliver", "1", "--to", buyer_email,
                    ]
                    result = subprocess.run(deliver_cmd, capture_output=True, text=True, timeout=180)

                    if result.returncode == 0 and "邮件发送成功" in result.stdout:
                        log.info(f"电子书自动交付成功: {book_name} → {buyer_email}")
                        if clean_sender:
                            await self.send_reply(
                                clean_cid, clean_sender,
                                f"《{book_name}》已发送到你的邮箱 {buyer_email}，请查收！如有问题随时联系我",
                            )
                        await self._notify_trade_event(
                            "automation",
                            f"✅ 电子书自动交付成功\n书名: {book_name}\n邮箱: {buyer_email}\n会话: {cid}",
                            cid, item_id,
                        )
                    else:
                        output = (result.stdout + result.stderr)[-300:]
                        await self._notify_trade_event(
                            "automation",
                            f"📚 电子书自动交付失败\n书名: {book_name}\n邮箱: {buyer_email}\n"
                            f"输出: {output}\n会话: {cid}\n"
                            f"手动: python3 goofish/zlibrary.py deliver 1 --to {buyer_email}",
                            cid, item_id,
                        )
                except subprocess.TimeoutExpired:
                    await self._notify_trade_event(
                        "automation",
                        f"📚 电子书交付超时\n书名: {book_name}\n邮箱: {buyer_email}\n会话: {cid}\n"
                        f"手动: python3 goofish/zlibrary.py deliver 1 --to {buyer_email}",
                        cid, item_id,
                    )
                except Exception as e:
                    log.warning(f"电子书自动交付异常: {e}")

        # notify_only 或带 notify 标记的
        if trigger.get("notify") or action == "notify_only":
            msg = trigger.get("message", "自动化已触发").format(cid=cid, item_id=item_id)
            await self._notify_trade_event("automation", msg, cid, item_id)

    def _extract_book_name(self, cid):
        """从对话历史中提取买家要找的书名。"""
        history = self.chat_history.get(cid, [])
        skip_phrases = {"在吗", "在吗？", "你好", "hi", "hello", "还在吗", "？", "?"}
        prefixes = ("我想找", "想找", "有没有", "找一下", "帮我找", "请问有", "有", "找")

        for msg in history:
            if msg.get("role") != "buyer":
                continue
            content = msg["content"].strip()
            if len(content) <= 2 or content in skip_phrases:
                continue
            # 去除常见前缀
            for prefix in prefixes:
                if content.startswith(prefix):
                    rest = content[len(prefix):].strip()
                    if rest:
                        content = rest
                    break
            # 去除尾部标点
            content = content.rstrip("？?吗呢的")
            if content:
                return content
        return None

    def _extract_buyer_email(self, cid):
        """从对话历史中提取买家的邮箱地址。"""
        history = self.chat_history.get(cid, [])
        for msg in history:
            if msg.get("role") != "buyer":
                continue
            content = msg["content"].strip()
            # 匹配邮箱格式
            match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', content)
            if match:
                return match.group(0)
        return None

    def _format_zlib_results_for_buyer(self, query, results):
        """格式化搜索结果发给买家确认。优先展示 PDF/EPUB。"""
        if not results:
            return f"抱歉，暂时没有搜索到《{query}》相关的电子书，我再帮你找找"

        # 优先选 PDF/EPUB 版本
        preferred = ["PDF", "EPUB"]
        top = results[0]
        for r in results:
            if r.get("extension", "").upper() in preferred:
                top = r
                break

        title = top.get("title", "")
        author = top.get("author", "")
        ext = top.get("extension", "")
        year = top.get("year", "")

        parts = []
        if author:
            parts.append(f"作者: {author}")
        if year:
            parts.append(f"出版: {year}")
        if ext:
            parts.append(f"格式: {ext}")
        info = "，".join(parts)

        msg = f"帮你找到了《{title}》"
        if info:
            msg += f"（{info}）"

        # 列出可用格式
        formats = sorted(set(r.get("extension", "").upper() for r in results if r.get("extension")))
        formats = [f for f in formats if f in ("PDF", "EPUB", "MOBI")]
        if len(formats) > 1:
            msg += f"\n可用格式: {'/'.join(formats)}"

        msg += "\n确认是这本的话，下单付款后我马上发到你邮箱"
        return msg

    def _format_zlib_results(self, cid, query, results):
        """格式化 Z-Library 搜索结果为 Discord 通知消息。"""
        buyer_email = self._extract_buyer_email(cid)
        lines = [f'📚 Z-Library 搜索: "{query}"', f"会话: {cid}"]
        if buyer_email:
            lines.append(f"买家邮箱: {buyer_email}")
        lines.append("")
        for i, book in enumerate(results[:5], 1):
            title = book.get("title", "未知")
            author = book.get("author", "")
            ext = book.get("extension", "")
            size = book.get("size", "")
            info_parts = [p for p in [author, ext, size] if p]
            lines.append(f"{i}. {title}")
            if info_parts:
                lines.append(f"   {' | '.join(info_parts)}")
        lines.append("")
        if buyer_email:
            lines.append(f"交付: python3 goofish/zlibrary.py deliver <序号> --to {buyer_email}")
        else:
            lines.append("下载: python3 goofish/zlibrary.py download <序号>")
            lines.append("⚠️ 未检测到买家邮箱，请手动确认后发送")
        return "\n".join(lines)

    async def _notify_discord(self, sender, content, cid, item_id, reason=""):
        """通过 OpenClaw 发送通知到 Discord #goofish 频道。"""
        notification = self.config.get("notification", {})
        if not notification.get("notify_on_escalation", True):
            return

        channel = notification.get("discord_channel", "")
        msg = f"闲鱼消息需要处理：\n买家: {sender}\n商品: {item_id}\n内容: {content}\n原因: {reason}\n会话: {cid}"
        try:
            import subprocess
            cmd = [
                *OPENCLAW_CMD,
                "message", "send",
                "--channel", "discord",
                "--target", channel,
                "--message", msg,
            ]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            log.warning(f"Discord 通知失败: {e}")

    async def _send_ack(self, message_data):
        """发送 ACK 确认收到消息。"""
        try:
            headers = message_data.get("headers", {})
            mid = headers.get("mid")
            sid = headers.get("sid")
            if mid and sid:
                ack = {
                    "code": 200,
                    "headers": {"mid": mid, "sid": sid},
                }
                await self._send(ack)
        except Exception as e:
            log.debug(f"发送 ACK 失败: {e}")

    async def token_refresh_loop(self):
        """定期刷新 accessToken。"""
        while self.running:
            await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
            try:
                await self.refresh_token()
            except Exception as e:
                log.warning(f"Token 刷新失败: {e}")
                self._record_error(f"Token 刷新失败: {e}")

    async def status_report_loop(self):
        """定期向 Discord 发送状态摘要。"""
        interval_hours = self.config.get("notification", {}).get("status_report_interval_hours", 0)
        if not interval_hours or interval_hours <= 0:
            return  # 未配置，不启动
        interval_seconds = interval_hours * 3600
        while self.running:
            await asyncio.sleep(interval_seconds)
            if not self.running:
                break
            try:
                self._send_status_report()
            except Exception as e:
                log.warning(f"状态上报失败: {e}")

    def _send_status_report(self):
        """发送状态摘要到 Discord。"""
        notification = self.config.get("notification", {})
        channel = notification.get("discord_channel", "")
        if not channel:
            return

        s = self.stats
        uptime = ""
        if s.get("started_at"):
            try:
                from datetime import datetime
                start = datetime.strptime(s["started_at"], "%Y-%m-%d %H:%M:%S")
                delta = datetime.now() - start
                hours = int(delta.total_seconds()) // 3600
                uptime = f"运行 {hours}h"
            except Exception:
                pass

        msg = (
            f"闲鱼机器人状态报告\n"
            f"{uptime} | 收到 {s['messages_received']} 条消息 | "
            f"回复 {s['replies_sent']} 条 | 升级 {s['escalated']} 条 | "
            f"重连 {s['ws_reconnects']} 次"
        )
        try:
            import subprocess
            cmd = [
                *OPENCLAW_CMD,
                "message", "send",
                "--channel", "discord",
                "--target", channel,
                "--message", msg,
            ]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info(f"状态上报已发送: {msg[:100]}")
        except Exception as e:
            log.warning(f"状态上报发送失败: {e}")

    async def message_loop(self):
        """消息接收循环。"""
        while self.running:
            try:
                raw = await self.ws.recv()

                # 解析 JSON
                try:
                    raw_json = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.debug(f"非 JSON 消息: {str(raw)[:200]}")
                    continue

                # 发送 ACK
                await self._send_ack(raw_json)

                decoded = decode_message(raw)

                if not decoded:
                    try:
                        raw_preview = raw[:500] if isinstance(raw, str) else raw[:500].decode("utf-8", errors="replace")
                    except Exception:
                        raw_preview = str(raw)[:500]
                    log.debug(f"未解析消息: {raw_preview}")
                    continue

                if decoded["type"] == "heartbeat_ack":
                    continue

                if decoded["type"] == "reg_ack":
                    log.info(f"注册响应: {json.dumps(decoded['data'], ensure_ascii=False)[:200]}")
                    continue

                if decoded["type"] == "push":
                    log.info(f"收到推送 lwp={decoded['lwp']}: {json.dumps(decoded['data'], ensure_ascii=False)[:300]}")
                    continue

                if decoded["type"] == "unknown_list":
                    log.info(f"收到未知 list 消息: {json.dumps(decoded['data'], ensure_ascii=False)[:300]}")
                    continue

                if decoded["type"] == "sync":
                    log.debug(f"收到 sync 包，含 {len(decoded['messages'])} 条消息")
                    for msg in decoded["messages"]:
                        log.debug(f"sync 消息原始: {json.dumps(msg, ensure_ascii=False, default=str)[:500]}")
                        chat_msg = extract_chat_message(msg)
                        if chat_msg:
                            log.info(f"收到消息: sender={chat_msg['sender_id']} content={chat_msg['content'][:80]}")
                            asyncio.create_task(self.handle_message(chat_msg))
                        else:
                            log.debug(f"消息未解析为 chat_msg, keys={list(msg.keys()) if isinstance(msg, dict) else type(msg)}")

            except Exception as e:
                if self.running:
                    log.error(f"消息接收错误: {e}", exc_info=True)
                break

    def save_status(self):
        """保存运行状态到文件。"""
        active_sessions = len([cid for cid, msgs in self.chat_history.items() if msgs])
        status = {
            "running": self.running,
            "user_id": self.my_id,
            "pid": os.getpid(),
            "active_sessions": active_sessions,
            **self.stats,
        }
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)

    async def run(self):
        """主运行循环（含断线重连）。"""
        self.running = True
        self.stats["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        delay = RECONNECT_BASE_DELAY
        consecutive_failures = 0
        ALERT_THRESHOLD = 5  # 连续失败 N 次发告警

        while self.running:
            try:
                await self.connect()
                self.save_status()
                delay = RECONNECT_BASE_DELAY  # 重置退避
                consecutive_failures = 0  # 连接成功，重置计数

                # 并行运行心跳、Token 刷新、消息循环和状态上报
                await asyncio.gather(
                    self.heartbeat_loop(),
                    self.token_refresh_loop(),
                    self.message_loop(),
                    self.status_report_loop(),
                )

            except Exception as e:
                if not self.running:
                    break
                consecutive_failures += 1
                self.stats["ws_reconnects"] += 1
                self._record_error(f"连接断开: {e}")
                log.error(f"连接断开: {e}")

                # 连续失败告警 + 自动刷新 cookie
                if consecutive_failures == ALERT_THRESHOLD:
                    self._send_failure_alert(consecutive_failures, str(e))
                    # cookie 过期时自动从 Chrome 提取
                    if await self._try_refresh_cookies_from_chrome(str(e)):
                        delay = RECONNECT_BASE_DELAY  # 重置退避，尽快重试

                log.info(f"{delay} 秒后重连...（连续失败 {consecutive_failures} 次）")
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

        log.info("Bot 已停止")

    def _send_failure_alert(self, count, error):
        """连续失败时发 Discord 告警。"""
        notification = self.config.get("notification", {})
        channel = notification.get("discord_channel", "")
        if not channel:
            return
        msg = f"⚠️ 闲鱼机器人连续 {count} 次重连失败！\n最后错误: {error[:200]}\n请检查 cookie 或网络状态"
        try:
            import subprocess
            cmd = [
                *OPENCLAW_CMD,
                "message", "send",
                "--channel", "discord",
                "--target", channel,
                "--message", msg,
            ]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.warning(f"已发送连续失败告警: {count} 次")
        except Exception as e:
            log.warning(f"告警发送失败: {e}")

    async def _try_refresh_cookies_from_chrome(self, error_msg):
        """Cookie 过期时自动刷新：通过 CDP 连接已运行的 OpenClaw Chrome，刷新闲鱼页面恢复登录态，再用 browser_cookie3 提取 cookie。"""
        cookie_keywords = ["cookie", "accessToken", "token", "unb", "401", "过期"]
        if not any(kw in error_msg for kw in cookie_keywords):
            log.info("错误不像 cookie 过期，跳过自动刷新")
            return False

        log.info("检测到 cookie 可能过期，尝试通过 CDP 刷新 Chrome 登录态...")
        try:
            from playwright.async_api import async_playwright

            # Step 1: 通过 CDP 连接已运行的 OpenClaw Chrome（端口 18800）
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp("http://127.0.0.1:18800")
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
                await page.goto("https://www.goofish.com", wait_until="domcontentloaded")
                log.info("已在 Chrome 中打开闲鱼页面，等待 20 秒恢复登录态...")
                await page.wait_for_timeout(20000)
                await page.close()
                await browser.close()

            # Step 2: 用 browser_cookie3 从 Chrome cookie 数据库提取
            import browser_cookie3
            chrome_user_data = os.path.expanduser("~/.openclaw/browser/openclaw/user-data")
            cj = browser_cookie3.chrome(
                domain_name=".goofish.com",
                cookie_file=os.path.join(chrome_user_data, "Default", "Cookies"),
            )
            cookies = {c.name: c.value for c in cj}

            if "unb" not in cookies:
                log.warning("刷新后仍未获取到 unb cookie，Chrome 可能未登录闲鱼")
                return False

            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            _save_cookie_string(cookie_str)

            # 重新加载到当前实例
            self.cookies_str = cookie_str
            self.cookies = parse_cookies(cookie_str)
            self.cookie_token = self.cookies.get("_m_h5_tk", "").split("_")[0]
            self._token_cache = None

            log.info(f"Cookie 自动刷新成功，用户 ID: {self.cookies.get('unb', '?')}")
            self._send_failure_alert(0, "✅ Cookie 已从 Chrome 自动刷新，正在重连...")
            return True

        except ImportError as e:
            log.warning(f"依赖未安装，无法自动刷新 cookie: {e}")
            return False
        except Exception as e:
            log.warning(f"自动刷新 cookie 失败: {e}")
            return False

    def stop(self):
        """停止 bot。"""
        self.running = False
        if self.ws:
            asyncio.create_task(self.ws.close())
        if self._session:
            asyncio.create_task(self._session.close())
        self.save_status()


# --- CLI ---

def cmd_start():
    """启动 bot 服务。"""
    if not OPENCLAW_CMD:
        log.error("OpenClaw 未找到。请设置 OPENCLAW_PATH 环境变量或将 openclaw 加入 PATH。")
        sys.exit(1)
    config = load_config()
    cookies_str = load_cookies()

    bot = GoofishBot(config, cookies_str)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal(sig, frame):
        log.info("收到停止信号，正在关闭...")
        bot.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    log.info("闲鱼客服机器人启动中...")
    loop.run_until_complete(bot.run())


def _save_cookie_string(cookie_str):
    """保存 cookie 字符串到文件，同时清除 token 缓存。"""
    cookies = parse_cookies(cookie_str)
    if "unb" not in cookies:
        print("警告: cookie 中没有 'unb' 字段，可能无法正常使用")

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(DEFAULT_COOKIES_PATH, "w") as f:
        json.dump({"cookie_string": cookie_str}, f, ensure_ascii=False)
    os.chmod(DEFAULT_COOKIES_PATH, 0o600)

    # 清除旧 token 缓存（新 cookie 需要重新获取 token，但保留 device_id）
    if os.path.exists(TOKEN_CACHE_FILE):
        os.remove(TOKEN_CACHE_FILE)

    print(f"Cookie 已保存到 {DEFAULT_COOKIES_PATH}")
    print(f"用户 ID: {cookies.get('unb', '未知')}")
    print("Token 缓存已清除，下次启动会自动刷新")


def cmd_login():
    """扫码登录获取 cookie — Playwright 自动化。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright 未安装，回退到手动模式")
        print("安装: pip3 install playwright && python3 -m playwright install chromium")
        _cmd_login_manual()
        return

    print("正在打开浏览器，请用闲鱼 App 扫码登录...")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.goofish.com")

        print("等待登录中... （扫码后自动检测）")

        # 轮询等待 unb cookie 出现（最长 120 秒）
        for _ in range(120):
            cookies = context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            if "unb" in cookie_dict:
                break
            page.wait_for_timeout(1000)
        else:
            print("登录超时（120秒），请重试")
            browser.close()
            return

        # 提取 goofish 相关 cookie
        goofish_cookies = [
            c for c in cookies
            if any(d in c.get("domain", "") for d in [".goofish.com", "goofish.com", ".taobao.com"])
        ]
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in goofish_cookies)

        browser.close()

    print()
    print("登录成功！")
    _save_cookie_string(cookie_str)


def cmd_login_from_chrome():
    """从已登录的 Chrome 浏览器提取 cookie。"""
    try:
        import browser_cookie3
    except ImportError:
        print("browser_cookie3 未安装")
        print("安装: pip3 install browser_cookie3")
        return

    print("正在从 Chrome 提取 goofish.com cookie...")
    print("（如果弹出 Keychain 权限提示，请点击允许）")
    print()

    try:
        cj = browser_cookie3.chrome(domain_name=".goofish.com")
        cookies = {c.name: c.value for c in cj}
    except Exception as e:
        print(f"提取失败: {e}")
        print("请确保 Chrome 已关闭，或检查 Keychain 权限")
        return

    if "unb" not in cookies:
        print("Chrome 中没有 goofish.com 的登录 cookie")
        print("请先在 Chrome 中登录 https://www.goofish.com")
        return

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    print("提取成功！")
    _save_cookie_string(cookie_str)


def _cmd_login_manual():
    """手动粘贴 cookie（备用方式）。"""
    print("闲鱼手动登录获取 Cookie：")
    print()
    print("1. 打开浏览器访问 https://www.goofish.com")
    print("2. 用闲鱼 App 扫码登录")
    print("3. 在 Console 中执行: document.cookie")
    print("4. 复制输出的完整 cookie 字符串")
    print()

    cookie_str = input("请粘贴完整 cookie 字符串: ").strip()
    if not cookie_str:
        print("未输入 cookie，退出")
        return

    _save_cookie_string(cookie_str)


def cmd_status():
    """查看 bot 运行状态。"""
    if not os.path.exists(STATUS_FILE):
        print("Bot 未启动过（无状态文件）")
        return

    with open(STATUS_FILE) as f:
        status = json.load(f)

    pid = status.get("pid")
    is_running = False
    if pid:
        try:
            os.kill(pid, 0)
            is_running = True
        except OSError:
            pass

    # 计算运行时长
    started_at = status.get("started_at", "")
    uptime_str = ""
    if started_at and is_running:
        try:
            from datetime import datetime
            start = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
            delta = datetime.now() - start
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes = remainder // 60
            uptime_str = f"{hours}h{minutes}m"
        except Exception:
            pass

    print(f"{'='*40}")
    print(f"  闲鱼客服机器人状态")
    print(f"{'='*40}")
    print(f"  状态:     {'🟢 运行中' if is_running else '🔴 已停止'}" + (f" ({uptime_str})" if uptime_str else ""))
    print(f"  用户 ID:  {status.get('user_id', '未知')}")
    print(f"  PID:      {pid or '—'}")
    print(f"  启动时间: {started_at or '—'}")
    print(f"{'─'*40}")
    print(f"  消息统计")
    print(f"{'─'*40}")
    print(f"  收到消息: {status.get('messages_received', 0)}")
    print(f"  自动回复: {status.get('replies_sent', 0)}")
    print(f"  快速回复: {status.get('quick_replies', 0)}")
    print(f"  图片消息: {status.get('image_messages', 0)}")
    print(f"  人工升级: {status.get('escalated', 0)}")
    print(f"  活跃会话: {status.get('active_sessions', 0)}")
    print(f"{'─'*40}")
    print(f"  连接信息")
    print(f"{'─'*40}")
    print(f"  WS 连接:  {status.get('ws_connected_at', '—')}")
    print(f"  重连次数: {status.get('ws_reconnects', 0)}")
    print(f"  最后消息: {status.get('last_message_at', '—')}")

    errors = status.get("errors", [])
    if errors:
        print(f"{'─'*40}")
        print(f"  最近错误")
        print(f"{'─'*40}")
        for err in errors[-5:]:
            print(f"  {err}")
    print(f"{'='*40}")


PLIST_DST = os.path.expanduser("~/Library/LaunchAgents/ai.openclaw.goofish.plist")


def cmd_install():
    """安装 launchd 服务（动态生成 plist）。"""
    import subprocess as sp

    home = os.path.expanduser("~")
    log_dir = os.path.join(home, ".openclaw", "logs")
    os.makedirs(log_dir, exist_ok=True)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.openclaw.goofish</string>
    <key>Comment</key>
    <string>Goofish Auto-Reply Bot</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{os.path.abspath(__file__)}</string>
        <string>start</string>
    </array>
    <key>StandardOutPath</key>
    <string>{log_dir}/goofish.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/goofish.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{home}</string>
        <key>PATH</key>
        <string>{os.path.dirname(sys.executable)}:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>"""

    with open(PLIST_DST, "w") as f:
        f.write(plist_content)

    sp.run(["launchctl", "load", PLIST_DST], check=False)
    print(f"已安装并启动 launchd 服务")
    print(f"  plist: {PLIST_DST}")
    print(f"  日志: {log_dir}/goofish.log")


def cmd_uninstall():
    """卸载 launchd 服务。"""
    import subprocess as sp
    sp.run(["launchctl", "unload", PLIST_DST], check=False)
    if os.path.exists(PLIST_DST):
        os.remove(PLIST_DST)
    print("已卸载 launchd 服务")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "start":
        cmd_start()
    elif cmd == "login":
        if "--from-chrome" in sys.argv:
            cmd_login_from_chrome()
        else:
            cmd_login()
    elif cmd == "status":
        cmd_status()
    elif cmd == "install":
        cmd_install()
    elif cmd == "uninstall":
        cmd_uninstall()
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
