#!/usr/bin/env python3
"""闲鱼商品管理 — Playwright 浏览器自动化

用法:
    python3 goofish/product.py discover [--page publish|seller]
    python3 goofish/product.py publish --title "..." --desc "..." --price 100 --images img1.jpg [img2.jpg ...]
    python3 goofish/product.py edit <item_id> [--title "..."] [--price 100] [--desc "..."]
    python3 goofish/product.py list
    python3 goofish/product.py manage <item_id> --action <上架|下架|删除>
    python3 goofish/product.py screenshot <url>
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time

# 项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from goofish.config import (
    CONFIG_DIR,
    SCREENSHOTS_DIR,
    SELECTORS_PATH,
    get_playwright_cookies,
    load_config,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("goofish-product")

# 闲鱼页面 URL
PUBLISH_URL = "https://www.goofish.com/sell"
SELLER_URL = "https://seller.goofish.com/"


class GoofishBrowser:
    """闲鱼网页端浏览器会话管理。"""

    def __init__(self, headless=False):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start(self):
        """启动浏览器，注入 cookies。"""
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=self.headless)
        self.context = await self.browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        # 注入 cookies
        cookies = get_playwright_cookies()
        if cookies:
            await self.context.add_cookies(cookies)
            log.info(f"已注入 {len(cookies)} 个 cookies")

        self.page = await self.context.new_page()

    async def close(self):
        """清理浏览器资源。"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def screenshot(self, name="debug"):
        """截图保存到 screenshots 目录。"""
        import re
        name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)[:50]
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        ts = int(time.time())
        path = os.path.join(SCREENSHOTS_DIR, f"{name}-{ts}.png")
        await self.page.screenshot(path=path, full_page=True)
        log.info(f"截图已保存: {path}")
        return path

    async def goto(self, url, wait_until="domcontentloaded"):
        """导航到页面。"""
        await self.page.goto(url, wait_until=wait_until)
        # SPA 额外等待
        await self.page.wait_for_timeout(2000)

    async def check_login(self):
        """检查是否已登录。"""
        await self.goto("https://www.goofish.com")
        # 检查是否有登录状态（检查 cookie 或页面元素）
        cookies = await self.context.cookies()
        cookie_names = {c["name"] for c in cookies}
        if "unb" not in cookie_names:
            log.error("未检测到登录状态，请先运行: python3 goofish/bot.py login")
            return False
        log.info("登录状态正常")
        return True


# --- 选择器管理 ---

def load_selectors():
    """加载缓存的选择器。"""
    if os.path.exists(SELECTORS_PATH):
        with open(SELECTORS_PATH) as f:
            return json.load(f)
    return {}


def save_selectors(selectors):
    """保存选择器缓存。"""
    os.makedirs(os.path.dirname(SELECTORS_PATH), exist_ok=True)
    with open(SELECTORS_PATH, "w") as f:
        json.dump(selectors, f, ensure_ascii=False, indent=2)
    log.info(f"选择器已缓存: {SELECTORS_PATH}")


# --- 子命令实现 ---

async def cmd_screenshot(url):
    """截图指定 URL。"""
    gb = GoofishBrowser(headless=True)
    try:
        await gb.start()
        await gb.goto(url)
        path = await gb.screenshot("manual")
        print(f"截图已保存: {path}")
    finally:
        await gb.close()


async def cmd_discover(page_type="publish"):
    """发现页面选择器。"""
    gb = GoofishBrowser(headless=False)
    try:
        await gb.start()
        if not await gb.check_login():
            return

        url = PUBLISH_URL if page_type == "publish" else SELLER_URL
        log.info(f"打开页面: {url}")
        await gb.goto(url)
        await gb.page.wait_for_timeout(3000)

        await gb.screenshot(f"discover-{page_type}")

        # 探测常见选择器
        selectors = load_selectors()
        selectors[page_type] = {"url": url, "discovered_at": int(time.time()), "elements": {}}

        # 发布页面选择器探测
        if page_type == "publish":
            probes = {
                "title_input": [
                    'input[placeholder*="标题"]',
                    'input[placeholder*="宝贝"]',
                    'textarea[placeholder*="标题"]',
                    '#title',
                    '[data-testid="title"]',
                ],
                "desc_input": [
                    'textarea[placeholder*="描述"]',
                    'textarea[placeholder*="详情"]',
                    '#desc',
                    '[data-testid="desc"]',
                ],
                "price_input": [
                    'input[placeholder*="价格"]',
                    'input[type="number"]',
                    '#price',
                    '[data-testid="price"]',
                ],
                "image_upload": [
                    'input[type="file"]',
                    '[class*="upload"]',
                    '[class*="image-picker"]',
                ],
                "submit_button": [
                    'button[type="submit"]',
                    'button:has-text("发布")',
                    'button:has-text("确认")',
                ],
            }
        else:
            # 卖家中心选择器探测
            probes = {
                "product_list": [
                    '[class*="item-list"]',
                    '[class*="product-list"]',
                    'table',
                ],
                "product_item": [
                    '[class*="item-card"]',
                    '[class*="product-item"]',
                    'tr',
                ],
            }

        for name, candidates in probes.items():
            for sel in candidates:
                try:
                    count = await gb.page.locator(sel).count()
                    if count > 0:
                        selectors[page_type]["elements"][name] = {
                            "selector": sel,
                            "count": count,
                        }
                        log.info(f"  {name}: {sel} (找到 {count} 个)")
                        break
                except Exception:
                    continue
            else:
                log.warning(f"  {name}: 未找到匹配的选择器")

        # 输出页面上所有表单元素（辅助调试）
        form_elements = await gb.page.evaluate("""() => {
            const inputs = document.querySelectorAll('input, textarea, select, button[type="submit"]');
            return Array.from(inputs).map(el => ({
                tag: el.tagName,
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                placeholder: el.placeholder || '',
                className: el.className ? el.className.substring(0, 80) : '',
            }));
        }""")

        if form_elements:
            print(f"\n页面表单元素 ({len(form_elements)} 个):")
            for el in form_elements:
                desc = f"  <{el['tag'].lower()}"
                if el['type']:
                    desc += f" type={el['type']}"
                if el['id']:
                    desc += f" id={el['id']}"
                if el['name']:
                    desc += f" name={el['name']}"
                if el['placeholder']:
                    desc += f' placeholder="{el["placeholder"]}"'
                desc += ">"
                print(desc)

        save_selectors(selectors)
        print(f"\n选择器已保存到: {SELECTORS_PATH}")
        print("如需手动查看页面，浏览器将保持 30 秒...")
        await gb.page.wait_for_timeout(30000)

    finally:
        await gb.close()


async def cmd_publish(title, desc, price, images, category=None, dry_run=False):
    """发布商品。"""
    selectors = load_selectors()
    publish_sel = selectors.get("publish", {}).get("elements", {})
    if not publish_sel:
        print("错误: 未找到缓存的选择器，请先运行: python3 goofish/product.py discover")
        return

    gb = GoofishBrowser(headless=False)
    try:
        await gb.start()
        if not await gb.check_login():
            return

        log.info("打开发布页面...")
        await gb.goto(PUBLISH_URL)
        await gb.page.wait_for_timeout(3000)
        await gb.screenshot("publish-start")

        # 上传图片
        if images:
            img_sel = publish_sel.get("image_upload", {}).get("selector")
            if img_sel:
                log.info(f"上传 {len(images)} 张图片...")
                file_input = gb.page.locator(img_sel)
                if await file_input.count() > 0:
                    abs_paths = [os.path.abspath(p) for p in images]
                    await file_input.set_input_files(abs_paths)
                    await gb.page.wait_for_timeout(3000)
                    log.info("图片上传完成")
                else:
                    log.warning(f"图片上传元素未找到: {img_sel}")
            else:
                log.warning("未缓存图片上传选择器，跳过图片上传")

        # 填写标题
        title_sel = publish_sel.get("title_input", {}).get("selector")
        if title_sel:
            log.info(f"填写标题: {title}")
            await gb.page.locator(title_sel).fill(title)
        else:
            log.warning("未找到标题输入框选择器")

        # 填写描述
        desc_sel = publish_sel.get("desc_input", {}).get("selector")
        if desc_sel:
            log.info(f"填写描述: {desc[:50]}...")
            await gb.page.locator(desc_sel).fill(desc)
        else:
            log.warning("未找到描述输入框选择器")

        # 填写价格
        price_sel = publish_sel.get("price_input", {}).get("selector")
        if price_sel:
            log.info(f"填写价格: {price}")
            await gb.page.locator(price_sel).fill(str(price))
        else:
            log.warning("未找到价格输入框选择器")

        await gb.screenshot("publish-filled")

        if dry_run:
            print("\n[DRY RUN] 表单已填写但不提交。浏览器保持 30 秒供检查...")
            await gb.page.wait_for_timeout(30000)
            return

        # 提交
        submit_sel = publish_sel.get("submit_button", {}).get("selector")
        if submit_sel:
            log.info("提交发布...")
            await gb.page.locator(submit_sel).click()
            await gb.page.wait_for_timeout(5000)
            await gb.screenshot("publish-result")
            print("发布操作已执行，请检查截图确认结果")
        else:
            log.warning("未找到提交按钮选择器，无法自动提交")
            print("请手动点击发布按钮。浏览器保持 30 秒...")
            await gb.page.wait_for_timeout(30000)

    finally:
        await gb.close()


async def cmd_list():
    """列出在售商品。"""
    gb = GoofishBrowser(headless=True)
    try:
        await gb.start()
        if not await gb.check_login():
            return

        log.info("打开卖家中心...")
        await gb.goto(SELLER_URL)
        await gb.page.wait_for_timeout(3000)
        await gb.screenshot("seller-center")

        # 尝试提取商品列表
        items = await gb.page.evaluate("""() => {
            // 尝试多种方式提取商品信息
            const results = [];

            // 方法1：表格行
            const rows = document.querySelectorAll('tr[data-item-id], tr[class*="item"]');
            for (const row of rows) {
                const title = row.querySelector('[class*="title"], td:nth-child(2)')?.textContent?.trim();
                const price = row.querySelector('[class*="price"]')?.textContent?.trim();
                const status = row.querySelector('[class*="status"]')?.textContent?.trim();
                if (title) results.push({title, price, status});
            }

            // 方法2：卡片
            if (results.length === 0) {
                const cards = document.querySelectorAll('[class*="item-card"], [class*="product-card"]');
                for (const card of cards) {
                    const title = card.querySelector('[class*="title"]')?.textContent?.trim();
                    const price = card.querySelector('[class*="price"]')?.textContent?.trim();
                    const status = card.querySelector('[class*="status"]')?.textContent?.trim();
                    if (title) results.push({title, price, status});
                }
            }

            return results;
        }""")

        if items:
            print(f"\n在售商品 ({len(items)} 个):")
            for i, item in enumerate(items, 1):
                status = item.get("status", "未知")
                price = item.get("price", "未知")
                print(f"  {i}. {item['title']}  ¥{price}  [{status}]")
        else:
            print("\n未能自动提取商品列表。")
            print(f"请查看截图: {SCREENSHOTS_DIR}/")
            print("提示: 可以运行 discover --page seller 更新选择器")

    finally:
        await gb.close()


async def cmd_edit(item_id, title=None, price=None, desc=None):
    """编辑商品。"""
    gb = GoofishBrowser(headless=False)
    try:
        await gb.start()
        if not await gb.check_login():
            return

        # 尝试直接访问编辑页面
        edit_url = f"https://www.goofish.com/sell?itemId={item_id}"
        log.info(f"打开编辑页面: {edit_url}")
        await gb.goto(edit_url)
        await gb.page.wait_for_timeout(3000)
        await gb.screenshot("edit-start")

        selectors = load_selectors()
        publish_sel = selectors.get("publish", {}).get("elements", {})

        if title:
            title_sel = publish_sel.get("title_input", {}).get("selector")
            if title_sel:
                log.info(f"更新标题: {title}")
                el = gb.page.locator(title_sel)
                await el.clear()
                await el.fill(title)

        if desc:
            desc_sel = publish_sel.get("desc_input", {}).get("selector")
            if desc_sel:
                log.info(f"更新描述: {desc[:50]}...")
                el = gb.page.locator(desc_sel)
                await el.clear()
                await el.fill(desc)

        if price:
            price_sel = publish_sel.get("price_input", {}).get("selector")
            if price_sel:
                log.info(f"更新价格: {price}")
                el = gb.page.locator(price_sel)
                await el.clear()
                await el.fill(str(price))

        await gb.screenshot("edit-filled")
        print("编辑完成，浏览器保持 30 秒供手动确认提交...")
        await gb.page.wait_for_timeout(30000)

    finally:
        await gb.close()


async def cmd_manage(item_id, action):
    """管理商品：上架/下架/删除。"""
    gb = GoofishBrowser(headless=False)
    try:
        await gb.start()
        if not await gb.check_login():
            return

        log.info("打开卖家中心...")
        await gb.goto(SELLER_URL)
        await gb.page.wait_for_timeout(3000)

        # 操作需要在卖家中心找到对应商品并执行操作
        # 由于 SPA 结构不确定，这里提供页面截图和手动操作引导
        await gb.screenshot("manage-start")
        print(f"\n操作: {action} 商品 {item_id}")
        print("由于闲鱼卖家中心页面结构可能变化，建议:")
        print("  1. 先运行 discover --page seller 更新选择器")
        print("  2. 在打开的浏览器中手动操作")
        print("\n浏览器保持 60 秒...")
        await gb.page.wait_for_timeout(60000)

    finally:
        await gb.close()


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="闲鱼商品管理工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # discover
    p_discover = subparsers.add_parser("discover", help="发现页面选择器")
    p_discover.add_argument("--page", choices=["publish", "seller"], default="publish", help="目标页面")

    # screenshot
    p_screenshot = subparsers.add_parser("screenshot", help="截图指定 URL")
    p_screenshot.add_argument("url", help="目标 URL")

    # publish
    p_publish = subparsers.add_parser("publish", help="发布商品")
    p_publish.add_argument("--title", required=True, help="商品标题")
    p_publish.add_argument("--desc", required=True, help="商品描述")
    p_publish.add_argument("--price", required=True, type=float, help="价格")
    p_publish.add_argument("--images", nargs="+", help="图片路径")
    p_publish.add_argument("--category", help="分类")
    p_publish.add_argument("--dry-run", action="store_true", help="只填写不提交")

    # list
    subparsers.add_parser("list", help="列出在售商品")

    # edit
    p_edit = subparsers.add_parser("edit", help="编辑商品")
    p_edit.add_argument("item_id", help="商品 ID")
    p_edit.add_argument("--title", help="新标题")
    p_edit.add_argument("--price", type=float, help="新价格")
    p_edit.add_argument("--desc", help="新描述")

    # manage
    p_manage = subparsers.add_parser("manage", help="管理商品")
    p_manage.add_argument("item_id", help="商品 ID")
    p_manage.add_argument("--action", required=True, choices=["上架", "下架", "删除"], help="操作")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "screenshot":
        asyncio.run(cmd_screenshot(args.url))
    elif args.command == "discover":
        asyncio.run(cmd_discover(args.page))
    elif args.command == "publish":
        asyncio.run(cmd_publish(
            title=args.title,
            desc=args.desc,
            price=args.price,
            images=args.images or [],
            category=args.category,
            dry_run=args.dry_run,
        ))
    elif args.command == "list":
        asyncio.run(cmd_list())
    elif args.command == "edit":
        asyncio.run(cmd_edit(args.item_id, title=args.title, price=args.price, desc=args.desc))
    elif args.command == "manage":
        asyncio.run(cmd_manage(args.item_id, args.action))


if __name__ == "__main__":
    main()
