#!/usr/bin/env python3
"""闲鱼 AI 回复引擎 — 调用 OpenClaw Agent 生成回复"""

import json
import os
import subprocess

from config import OPENCLAW_CMD
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def load_prompt_template(template_name):
    """加载 prompt 模板文件，找不到则回退到 default.md。"""
    path = os.path.join(PROMPTS_DIR, template_name)
    if not os.path.exists(path):
        path = os.path.join(PROMPTS_DIR, "default.md")
    with open(path) as f:
        return f.read()


def build_system_prompt(item_config):
    """用模板 + 商品配置渲染最终 system prompt。"""
    template_name = item_config.get("prompt_template", "default.md")
    template = load_prompt_template(template_name)

    # 计算价格展示
    listed_price = item_config.get("listed_price")
    min_ratio = item_config.get("min_price_ratio", 0.8)
    floor_price = item_config.get("floor_price")

    if listed_price:
        # floor_price 优先，否则用 min_price_ratio 计算
        computed_floor = floor_price if floor_price else int(listed_price * min_ratio)
        min_price_display = f"标价{listed_price}元，最低{computed_floor}元"
        floor_price_display = f"{computed_floor}元（绝对底价，不可再低）"
    else:
        min_price_display = f"不低于标价的{int(min_ratio * 100)}%"
        floor_price_display = f"标价的{int(min_ratio * 100)}%"

    extra = item_config.get("extra_info", "")
    extra_section = f"- 其他：{extra}" if extra else ""

    fmt_vars = {
        "tone": item_config.get("tone", "友好随和的二手卖家"),
        "product_name": item_config.get("name", "未知商品"),
        "condition_note": item_config.get("condition_note", "未提供"),
        "shipping_note": item_config.get("shipping_note", "未提供"),
        "min_price_display": min_price_display,
        "floor_price_display": floor_price_display,
        "extra_info_section": extra_section,
    }
    # format_map 允许模板中存在未使用的占位符（跳过而非报错）
    class _SafeDict(dict):
        def __missing__(self, key):
            return f"{{{key}}}"
    return template.format_map(_SafeDict(fmt_vars))


def generate_reply(user_message, item_config, history=None):
    """调用 OpenClaw Agent 生成回复。

    Args:
        user_message: 用户发来的消息
        item_config: 商品配置 dict（含 prompt_template, condition_note 等）
        history: 对话历史 list[dict]，每个 dict 有 role 和 content

    Returns:
        dict: {"reply": "回复内容", "needs_human": bool}
    """
    system = build_system_prompt(item_config)

    # 构建对话上下文
    context_parts = [f"系统指令：\n{system}\n"]
    if history:
        context_parts.append("对话历史：")
        for msg in history[-10:]:
            role = "买家" if msg.get("role") == "buyer" else "我"
            context_parts.append(f"{role}：{msg['content']}")

    context_parts.append(f"\n买家最新消息：{user_message}\n\n请回复买家：")
    prompt = "\n".join(context_parts)

    try:
        result = subprocess.run(
            [*OPENCLAW_CMD, "agent", "-m", prompt, "--json", "--agent", "main"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return {"reply": None, "needs_human": True, "error": result.stderr.strip()}

        # 解析 OpenClaw JSON 输出
        output = result.stdout.strip()
        reply_text = _extract_reply(output)

        needs_human = "[需人工]" in reply_text
        if needs_human:
            reply_text = reply_text.replace("[需人工]", "").strip()

        return {"reply": reply_text, "needs_human": needs_human}

    except subprocess.TimeoutExpired:
        return {"reply": None, "needs_human": True, "error": "AI 回复超时"}
    except Exception as e:
        return {"reply": None, "needs_human": True, "error": str(e)}


def _extract_reply(output):
    """从 OpenClaw JSON 输出中提取回复文本。"""
    try:
        data = json.loads(output)
        # OpenClaw --json 输出格式
        if "result" in data:
            payloads = data["result"].get("payloads", [])
            if payloads:
                return payloads[0].get("text", "").strip()
        if "summary" in data:
            return data["summary"].strip()
    except (json.JSONDecodeError, KeyError, IndexError):
        pass

    # 回退：直接用原始输出
    return output.strip()


def should_escalate(message, escalation_keywords):
    """检查消息是否包含需要升级的关键词。"""
    for keyword in escalation_keywords:
        if keyword in message:
            return True
    return False
