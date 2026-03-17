#!/usr/bin/env python3
"""闲鱼消息解码 — MessagePack 解码器 + 消息解析"""

import base64
import json
import logging

log = logging.getLogger("goofish-bot")


# --- MessagePack 解码器（纯 Python） ---

class MessagePackDecoder:
    """轻量级 MessagePack 解码器。"""

    def __init__(self, data):
        self.data = data
        self.offset = 0

    def decode(self):
        if self.offset >= len(self.data):
            return None
        b = self.data[self.offset]

        # positive fixint (0x00 - 0x7f)
        if b <= 0x7F:
            self.offset += 1
            return b
        # fixmap (0x80 - 0x8f)
        if 0x80 <= b <= 0x8F:
            return self._read_map(b & 0x0F)
        # fixarray (0x90 - 0x9f)
        if 0x90 <= b <= 0x9F:
            return self._read_array(b & 0x0F)
        # fixstr (0xa0 - 0xbf)
        if 0xA0 <= b <= 0xBF:
            return self._read_str(b & 0x1F)
        # nil
        if b == 0xC0:
            self.offset += 1
            return None
        # false
        if b == 0xC2:
            self.offset += 1
            return False
        # true
        if b == 0xC3:
            self.offset += 1
            return True
        # bin8
        if b == 0xC4:
            self.offset += 1
            n = self.data[self.offset]
            self.offset += 1
            val = self.data[self.offset : self.offset + n]
            self.offset += n
            return val
        # bin16
        if b == 0xC5:
            self.offset += 1
            n = int.from_bytes(self.data[self.offset : self.offset + 2], "big")
            self.offset += 2
            val = self.data[self.offset : self.offset + n]
            self.offset += n
            return val
        # bin32
        if b == 0xC6:
            self.offset += 1
            n = int.from_bytes(self.data[self.offset : self.offset + 4], "big")
            self.offset += 4
            val = self.data[self.offset : self.offset + n]
            self.offset += n
            return val
        # float32
        if b == 0xCA:
            self.offset += 1
            import struct
            val = struct.unpack(">f", self.data[self.offset : self.offset + 4])[0]
            self.offset += 4
            return val
        # float64
        if b == 0xCB:
            self.offset += 1
            import struct
            val = struct.unpack(">d", self.data[self.offset : self.offset + 8])[0]
            self.offset += 8
            return val
        # uint8
        if b == 0xCC:
            self.offset += 1
            val = self.data[self.offset]
            self.offset += 1
            return val
        # uint16
        if b == 0xCD:
            self.offset += 1
            val = int.from_bytes(self.data[self.offset : self.offset + 2], "big")
            self.offset += 2
            return val
        # uint32
        if b == 0xCE:
            self.offset += 1
            val = int.from_bytes(self.data[self.offset : self.offset + 4], "big")
            self.offset += 4
            return val
        # uint64
        if b == 0xCF:
            self.offset += 1
            val = int.from_bytes(self.data[self.offset : self.offset + 8], "big")
            self.offset += 8
            return val
        # int8
        if b == 0xD0:
            self.offset += 1
            val = int.from_bytes(self.data[self.offset : self.offset + 1], "big", signed=True)
            self.offset += 1
            return val
        # int16
        if b == 0xD1:
            self.offset += 1
            val = int.from_bytes(self.data[self.offset : self.offset + 2], "big", signed=True)
            self.offset += 2
            return val
        # int32
        if b == 0xD2:
            self.offset += 1
            val = int.from_bytes(self.data[self.offset : self.offset + 4], "big", signed=True)
            self.offset += 4
            return val
        # int64
        if b == 0xD3:
            self.offset += 1
            val = int.from_bytes(self.data[self.offset : self.offset + 8], "big", signed=True)
            self.offset += 8
            return val
        # str8
        if b == 0xD9:
            self.offset += 1
            n = self.data[self.offset]
            self.offset += 1
            return self._read_str_bytes(n)
        # str16
        if b == 0xDA:
            self.offset += 1
            n = int.from_bytes(self.data[self.offset : self.offset + 2], "big")
            self.offset += 2
            return self._read_str_bytes(n)
        # str32
        if b == 0xDB:
            self.offset += 1
            n = int.from_bytes(self.data[self.offset : self.offset + 4], "big")
            self.offset += 4
            return self._read_str_bytes(n)
        # array16
        if b == 0xDC:
            self.offset += 1
            n = int.from_bytes(self.data[self.offset : self.offset + 2], "big")
            self.offset += 2
            return self._read_array_items(n)
        # array32
        if b == 0xDD:
            self.offset += 1
            n = int.from_bytes(self.data[self.offset : self.offset + 4], "big")
            self.offset += 4
            return self._read_array_items(n)
        # map16
        if b == 0xDE:
            self.offset += 1
            n = int.from_bytes(self.data[self.offset : self.offset + 2], "big")
            self.offset += 2
            return self._read_map_items(n)
        # map32
        if b == 0xDF:
            self.offset += 1
            n = int.from_bytes(self.data[self.offset : self.offset + 4], "big")
            self.offset += 4
            return self._read_map_items(n)
        # negative fixint (0xe0 - 0xff)
        if b >= 0xE0:
            self.offset += 1
            return b - 256

        self.offset += 1
        return None

    def _read_str_bytes(self, n):
        val = self.data[self.offset : self.offset + n].decode("utf-8", errors="replace")
        self.offset += n
        return val

    def _read_str(self, n):
        self.offset += 1
        return self._read_str_bytes(n)

    def _read_map(self, n):
        self.offset += 1
        return self._read_map_items(n)

    def _read_map_items(self, n):
        result = {}
        for _ in range(n):
            key = self.decode()
            val = self.decode()
            result[key] = val
        return result

    def _read_array(self, n):
        self.offset += 1
        return self._read_array_items(n)

    def _read_array_items(self, n):
        return [self.decode() for _ in range(n)]


def decrypt_msgpack(b64_data):
    """解码 base64 + MessagePack 数据。"""
    try:
        raw = base64.b64decode(b64_data)
        decoder = MessagePackDecoder(raw)
        return decoder.decode()
    except Exception as e:
        log.debug(f"MessagePack 解码失败: {e}")
        return None


# --- 消息解码 ---

def decode_message(raw_data):
    """解码闲鱼消息。返回解析后的 dict 或 None。"""
    try:
        data = json.loads(raw_data)
    except (json.JSONDecodeError, TypeError):
        return None

    # 心跳响应 / 普通 ack
    if "code" in data and data.get("code") == 200:
        return {"type": "heartbeat_ack"}

    # 注册响应
    lwp = data.get("lwp", "")
    if lwp == "/r/reg":
        return {"type": "reg_ack", "data": data}

    # 同步推送消息
    body = data.get("body", {})
    if not isinstance(body, dict):
        if isinstance(body, list):
            return {"type": "unknown_list", "data": data}
        return None

    # 格式1：body.syncPushPackage.data[]
    sync_pkg = body.get("syncPushPackage")
    if sync_pkg:
        messages = _decode_sync_package(sync_pkg)
        if messages:
            return {"type": "sync", "messages": messages}

    # 格式2：body 本身就包含消息数据
    if "data" in body:
        messages = _try_decode_data(body["data"])
        if messages:
            return {"type": "sync", "messages": messages}

    # 格式3：lwp 为推送端点
    if lwp and "/r/" in lwp and lwp != "/r/SyncStatus/ackDiff":
        return {"type": "push", "lwp": lwp, "data": data}

    return None


def _decode_sync_package(sync_pkg):
    """解码 syncPushPackage。"""
    messages = []
    for item in sync_pkg.get("data", []):
        raw = item.get("data", "")
        if not raw:
            continue
        decoded = _try_decode_data(raw)
        messages.extend(decoded)
    return messages


def _try_decode_data(raw):
    """尝试多种方式解码数据（JSON → base64 → JSON/MessagePack）。"""
    messages = []
    if isinstance(raw, dict):
        messages.append(raw)
        return messages
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                messages.append(item)
        return messages
    if not isinstance(raw, str):
        return messages

    # 尝试直接 JSON
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            messages.append(parsed)
        elif isinstance(parsed, list):
            messages.extend(p for p in parsed if isinstance(p, dict))
        return messages
    except (json.JSONDecodeError, ValueError):
        pass

    # base64 解码
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded_bytes = base64.b64decode(padded)
    except Exception:
        return messages

    # JSON 或 MessagePack
    if decoded_bytes and decoded_bytes[0] in (0x7B, 0x5B):
        try:
            parsed = json.loads(decoded_bytes.decode("utf-8"))
            if isinstance(parsed, dict):
                messages.append(parsed)
            elif isinstance(parsed, list):
                messages.extend(p for p in parsed if isinstance(p, dict))
            return messages
        except Exception:
            pass

    # MessagePack
    try:
        decoder = MessagePackDecoder(decoded_bytes)
        result = decoder.decode()
        if isinstance(result, dict):
            messages.append(result)
        elif isinstance(result, list):
            messages.extend(d for d in result if isinstance(d, dict))
    except Exception as e:
        log.debug(f"MessagePack 解码失败: {e}")

    return messages


def extract_chat_message(parsed_msg):
    """从解析后的消息中提取聊天信息。

    支持多种消息格式：
    - 格式A（闲鱼 JSON）：chatType + operation.content
    - 格式B（MessagePack）：message[1][10][reminderContent]
    - 格式C（header/body 结构）

    Returns:
        dict with keys: sender_id, sender_nick, content, cid, item_id, msg_time
        or None if not a chat message.
    """
    if not isinstance(parsed_msg, dict):
        return None

    try:
        # 格式A：闲鱼 JSON 消息 — chatType + operation 结构
        if "chatType" in parsed_msg and "operation" in parsed_msg:
            op = parsed_msg.get("operation", {})
            content_data = op.get("content", {})
            content_type = content_data.get("contentType")
            session_id = str(parsed_msg.get("sessionId", ""))

            # contentType=8 是会话信息/系统消息，跳过
            if content_type == 8:
                return None

            # contentType=1 是文本消息
            if content_type == 1:
                text_data = content_data.get("text", {})
                text = text_data.get("text", "")
            # contentType=2 图片消息
            elif content_type == 2:
                image_data = content_data.get("image", content_data.get("picture", {}))
                image_url = image_data.get("url", image_data.get("picUrl", ""))
                session_info = op.get("sessionInfo", {})
                extensions = session_info.get("extensions", {})
                item_id = extensions.get("itemId", "")
                sender_uid = op.get("senderUid", "")
                return {
                    "sender_id": str(sender_uid) if sender_uid else "",
                    "sender_nick": "",
                    "content": "[图片]",
                    "cid": session_id,
                    "item_id": item_id,
                    "msg_time": extensions.get("arouseTimeStamp", 0),
                    "msg_type": "image",
                    "image_url": image_url,
                }
            # contentType=101 custom 消息
            elif content_type == 101:
                custom = content_data.get("custom", {})
                if custom.get("type") == 1:
                    text_b64 = custom.get("data", "")
                    text_json = json.loads(base64.b64decode(text_b64).decode("utf-8"))
                    text = text_json.get("text", {}).get("text", "")
                else:
                    return None
            else:
                # 其他 contentType — 可能是交易通知等系统消息
                session_info = op.get("sessionInfo", {})
                extensions = session_info.get("extensions", {})
                item_id = extensions.get("itemId", "")
                sender_uid = op.get("senderUid", "")
                return {
                    "sender_id": str(sender_uid) if sender_uid else "",
                    "sender_nick": "",
                    "content": json.dumps(content_data, ensure_ascii=False)[:500],
                    "cid": session_id,
                    "item_id": item_id,
                    "msg_time": extensions.get("arouseTimeStamp", 0),
                    "msg_type": "system",
                    "content_type": content_type,
                    "raw_content": content_data,
                }

            if not text:
                return None

            # 提取发送者和商品信息
            session_info = op.get("sessionInfo", {})
            extensions = session_info.get("extensions", {})
            item_id = extensions.get("itemId", "")

            sender_id = ""
            sender_nick = ""
            receiver_ids = op.get("receiverIds", [])
            sender_uid = op.get("senderUid", "")
            if sender_uid:
                sender_id = str(sender_uid)
            elif receiver_ids:
                for rid in receiver_ids:
                    sender_id = str(rid)

            return {
                "sender_id": sender_id,
                "sender_nick": sender_nick,
                "content": text,
                "cid": session_id,
                "item_id": item_id,
                "msg_time": extensions.get("arouseTimeStamp", 0),
            }

        # 格式B：MessagePack 解码后 — message[1][10] 包含聊天信息
        if isinstance(parsed_msg.get(1), dict):
            msg_data = parsed_msg[1]
            info = msg_data.get(10, {})
            if isinstance(info, dict) and "reminderContent" in info:
                cid = str(msg_data.get(2, ""))
                sender_nick = info.get("senderNick", "")
                sender_id = info.get("senderUserId", "")
                content = info.get("reminderContent", "")
                item_id = ""
                reminder_url = info.get("reminderUrl", "")
                if "itemId=" in reminder_url:
                    item_id = reminder_url.split("itemId=")[1].split("&")[0]
                return {
                    "sender_id": str(sender_id),
                    "sender_nick": sender_nick,
                    "content": content,
                    "cid": cid,
                    "item_id": item_id,
                    "msg_time": msg_data.get(5, 0),
                }

        # 格式C：header/body 结构
        if "header" in parsed_msg and "body" in parsed_msg:
            header = parsed_msg["header"]
            body = parsed_msg["body"]

            sender_id = header.get("senderId", "").replace("@goofish", "")
            cid = header.get("cid", "").replace("@goofish", "")

            content_data = body.get("content", {})
            custom = content_data.get("custom", {})
            if custom.get("type") == 1:
                text_b64 = custom.get("data", "")
                text_json = json.loads(base64.b64decode(text_b64).decode("utf-8"))
                content = text_json.get("text", {}).get("text", "")
            else:
                content = str(content_data)

            return {
                "sender_id": sender_id,
                "sender_nick": "",
                "content": content,
                "cid": cid,
                "item_id": header.get("itemId", ""),
                "msg_time": header.get("msgTime", 0),
            }

    except Exception as e:
        log.debug(f"消息解析失败: {e}")

    return None
