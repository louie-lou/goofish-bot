#!/usr/bin/env python3
"""闲鱼机器人配置加载"""

import json
import logging
import os
import shutil
import sys

log = logging.getLogger("goofish-bot")

CONFIG_DIR = os.path.expanduser("~/.openclaw/goofish")
DEFAULT_CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_COOKIES_PATH = os.path.join(CONFIG_DIR, "cookies.json")
CONVERSATIONS_DIR = os.path.join(CONFIG_DIR, "conversations")
REPORTS_DIR = os.path.join(CONFIG_DIR, "reports")
SUGGESTIONS_DIR = os.path.join(CONFIG_DIR, "suggestions")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(PROJECT_DIR, "goofish", "prompts")
SCREENSHOTS_DIR = os.path.join(CONFIG_DIR, "screenshots")
SELECTORS_PATH = os.path.join(CONFIG_DIR, "selectors.json")
DOWNLOADS_DIR = os.path.join(CONFIG_DIR, "downloads")
ZLIB_LOG_PATH = os.path.join(CONFIG_DIR, "zlib_downloads.jsonl")


# --- 外部命令自动发现（支持环境变量覆盖） ---

def _discover_python():
    """发现 Python 可执行文件：环境变量 > which > sys.executable。"""
    env = os.environ.get("GOOFISH_PYTHON")
    if env:
        return env
    return shutil.which("python3") or sys.executable


def _discover_openclaw():
    """发现 OpenClaw 命令：环境变量 > which(openclaw) > node + ~/clawdbot。"""
    env = os.environ.get("OPENCLAW_PATH")
    if env:
        return env.split()
    oc = shutil.which("openclaw")
    if oc:
        return [oc]
    node = shutil.which("node")
    clawdbot = os.path.expanduser("~/clawdbot/dist/index.js")
    if node and os.path.exists(clawdbot):
        return [node, clawdbot]
    return None


PYTHON_CMD = _discover_python()
OPENCLAW_CMD = _discover_openclaw()


def load_config():
    """加载运行配置。"""
    if os.path.exists(DEFAULT_CONFIG_PATH):
        with open(DEFAULT_CONFIG_PATH) as f:
            return json.load(f)

    # 从项目模板复制
    example = os.path.join(PROJECT_DIR, "goofish", "config.example.json")
    if os.path.exists(example):
        with open(example) as f:
            config = json.load(f)
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(DEFAULT_CONFIG_PATH, "w") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        log.info(f"已从模板创建配置: {DEFAULT_CONFIG_PATH}")
        return config

    log.error("找不到配置文件，请先复制 config.example.json 到 ~/.openclaw/goofish/config.json")
    sys.exit(1)


def load_cookies():
    """加载闲鱼 cookie。"""
    if not os.path.exists(DEFAULT_COOKIES_PATH):
        log.error(f"找不到 cookies 文件: {DEFAULT_COOKIES_PATH}")
        log.error("请先运行: python3 goofish/bot.py login")
        sys.exit(1)

    with open(DEFAULT_COOKIES_PATH) as f:
        data = json.load(f)

    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        if "cookie_string" in data:
            return data["cookie_string"]
        return "; ".join(f"{k}={v}" for k, v in data.items())

    log.error("cookies.json 格式不正确")
    sys.exit(1)


def get_playwright_cookies(cookies_str=None):
    """将 cookie 字符串转换为 Playwright 格式的 cookie 列表。

    Args:
        cookies_str: cookie 字符串。如果为 None，从 cookies.json 加载。

    Returns:
        list[dict]: Playwright 格式的 cookie 列表
    """
    if cookies_str is None:
        cookies_str = load_cookies()
    cookies = []
    for item in cookies_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies.append({
                "name": k.strip(),
                "value": v.strip(),
                "domain": ".goofish.com",
                "path": "/",
            })
    return cookies


def get_item_config(config, item_id=None):
    """获取商品配置：products[item_id] + strategies[strategy] + ai 全局配置。

    兼容旧版 items 配置。新版使用 products + strategies 结构。
    """
    ai_config = config.get("ai", {})

    # 新版：products + strategies
    products = config.get("products", {})
    if products:
        product = products.get(item_id, products.get("默认", {})) if item_id else products.get("默认", {})
        default_product = products.get("默认", {})
        merged_product = {**default_product, **product}

        strategy_name = merged_product.get("strategy", "default")
        strategies = config.get("strategies", {})
        strategy = strategies.get(strategy_name, strategies.get("default", {}))

        return {**strategy, **merged_product, "tone": ai_config.get("tone", "")}

    # 旧版兼容：items
    items = config.get("items", {})
    default = items.get("默认", {})
    if item_id and item_id in items:
        return {**default, **items[item_id], "tone": ai_config.get("tone", "")}
    return {**default, "tone": ai_config.get("tone", "")}


def save_config(config):
    """保存配置到文件（自动备份旧配置）。"""
    if os.path.exists(DEFAULT_CONFIG_PATH):
        shutil.copy2(DEFAULT_CONFIG_PATH, DEFAULT_CONFIG_PATH + ".bak")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(DEFAULT_CONFIG_PATH, "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    log.info(f"配置已保存: {DEFAULT_CONFIG_PATH}")
