#!/usr/bin/env python3
"""闲鱼 Bot 管理面板 — aiohttp web server

用法：
    python3 goofish/dashboard.py [--port 8420] [--host 127.0.0.1]
"""

import argparse
import json
import os
import sys
import time

from aiohttp import web

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from config import (
    CONFIG_DIR, CONVERSATIONS_DIR, PROMPTS_DIR,
    load_config, save_config,
)

STATIC_DIR = os.path.join(SCRIPT_DIR, "static")
STATUS_FILE = os.path.join(CONFIG_DIR, "status.json")


# --- API handlers ---

async def api_status(request):
    """GET /api/status — bot 运行状态。"""
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            status = json.load(f)
        # 检查进程是否存活
        pid = status.get("pid")
        if pid:
            try:
                os.kill(pid, 0)
                status["alive"] = True
            except (OSError, ProcessLookupError):
                status["alive"] = False
        else:
            status["alive"] = False
    else:
        status = {"running": False, "alive": False}
    return web.json_response(status)


async def api_config(request):
    """GET /api/config — 完整配置（脱敏）。"""
    config = load_config()
    # 隐藏敏感字段
    if "email" in config:
        config["email"] = {k: ("***" if k == "password" else v)
                           for k, v in config["email"].items()}
    return web.json_response(config, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def api_products(request):
    """GET /api/products — 商品列表 + 关联策略。"""
    config = load_config()
    products = config.get("products", {})
    strategies = config.get("strategies", {})
    result = []
    for item_id, product in products.items():
        strategy_name = product.get("strategy", "default")
        strategy = strategies.get(strategy_name, {})
        prompt_file = strategy.get("prompt_template", "default.md")
        prompt_path = os.path.join(PROMPTS_DIR, prompt_file)
        prompt_content = ""
        if os.path.exists(prompt_path):
            with open(prompt_path) as f:
                prompt_content = f.read()
        result.append({
            "item_id": item_id,
            **product,
            "strategy_detail": strategy,
            "prompt_template_content": prompt_content,
        })
    return web.json_response(result, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def api_update_product(request):
    """PUT /api/products/{item_id} — 更新商品配置。"""
    item_id = request.match_info["item_id"]
    data = await request.json()
    config = load_config()
    products = config.setdefault("products", {})
    if item_id in products:
        products[item_id].update(data)
    else:
        products[item_id] = data
    save_config(config)
    return web.json_response({"ok": True})


async def api_strategies(request):
    """GET /api/strategies — 策略列表 + prompt 模板。"""
    config = load_config()
    strategies = config.get("strategies", {})
    result = []
    for name, strategy in strategies.items():
        prompt_file = strategy.get("prompt_template", "default.md")
        prompt_path = os.path.join(PROMPTS_DIR, prompt_file)
        prompt_content = ""
        if os.path.exists(prompt_path):
            with open(prompt_path) as f:
                prompt_content = f.read()
        result.append({
            "name": name,
            **strategy,
            "prompt_content": prompt_content,
        })
    return web.json_response(result, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def api_quick_replies(request):
    """GET /api/quick-replies — 快速回复列表。"""
    config = load_config()
    return web.json_response(
        config.get("quick_replies", {}),
        dumps=lambda o: json.dumps(o, ensure_ascii=False),
    )


async def api_update_quick_replies(request):
    """PUT /api/quick-replies — 更新快速回复。"""
    data = await request.json()
    config = load_config()
    config["quick_replies"] = data
    save_config(config)
    return web.json_response({"ok": True})


async def api_conversations(request):
    """GET /api/conversations — 对话列表。"""
    if not os.path.exists(CONVERSATIONS_DIR):
        return web.json_response([])
    convos = []
    for fname in os.listdir(CONVERSATIONS_DIR):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(CONVERSATIONS_DIR, fname)
        cid = fname.replace("_goofish.jsonl", "").replace(".jsonl", "")
        msg_count = 0
        last_ts = 0
        last_content = ""
        last_type = ""
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    msg_count += 1
                    ts = evt.get("ts", 0)
                    if ts > last_ts:
                        last_ts = ts
                        last_content = evt.get("content", "")[:60]
                        last_type = evt.get("type", "")
                except json.JSONDecodeError:
                    pass
        convos.append({
            "cid": cid,
            "msg_count": msg_count,
            "last_ts": last_ts,
            "last_content": last_content,
            "last_type": last_type,
        })
    convos.sort(key=lambda x: x["last_ts"], reverse=True)
    return web.json_response(convos, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def api_conversation_detail(request):
    """GET /api/conversations/{cid} — 单个对话事件列表。"""
    cid = request.match_info["cid"]
    # 尝试两种文件名格式
    for fname in [f"{cid}_goofish.jsonl", f"{cid}.jsonl"]:
        fpath = os.path.join(CONVERSATIONS_DIR, fname)
        if os.path.exists(fpath):
            break
    else:
        return web.json_response({"error": "not found"}, status=404)

    events = []
    with open(fpath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return web.json_response(events, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def index(request):
    """Serve index.html for all non-API routes."""
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


def create_app():
    app = web.Application()
    # API routes
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/products", api_products)
    app.router.add_put("/api/products/{item_id}", api_update_product)
    app.router.add_get("/api/strategies", api_strategies)
    app.router.add_get("/api/quick-replies", api_quick_replies)
    app.router.add_put("/api/quick-replies", api_update_quick_replies)
    app.router.add_get("/api/conversations", api_conversations)
    app.router.add_get("/api/conversations/{cid}", api_conversation_detail)
    # Static files
    app.router.add_static("/static/", STATIC_DIR)
    app.router.add_get("/", index)
    return app


def main():
    parser = argparse.ArgumentParser(description="闲鱼 Bot 管理面板")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    app = create_app()
    print(f"🐟 闲鱼 Bot 管理面板启动: http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
