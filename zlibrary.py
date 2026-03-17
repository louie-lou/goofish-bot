#!/usr/bin/env python3
"""Z-Library 电子书搜索下载交付 — Playwright 浏览器自动化

用法:
    python3 goofish/zlib.py search "Deep Learning"          # 搜索书籍
    python3 goofish/zlib.py search "机器学习" --lang chinese  # 按语言过滤
    python3 goofish/zlib.py download 1                       # 下载搜索结果第 N 本
    python3 goofish/zlib.py deliver 1 --to buyer@qq.com      # 下载 + 邮件发送
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.parse

# 项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from goofish.config import CONFIG_DIR, DOWNLOADS_DIR, SCREENSHOTS_DIR, ZLIB_LOG_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("zlib")

# Z-Library URL
ZLIB_BASE = "https://z-library.sk"
SEARCH_CACHE_PATH = os.path.join(CONFIG_DIR, "zlib_search_cache.json")

# 代理 — Z-Library 需要翻墙
PROXY = "http://127.0.0.1:10808"


class ZLibBrowser:
    """Z-Library Playwright 浏览器会话。"""

    def __init__(self, headless=True, proxy=None):
        self.headless = headless
        self.proxy = proxy or PROXY
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start(self):
        """启动浏览器。"""
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        launch_args = {"headless": self.headless}
        if self.proxy:
            launch_args["proxy"] = {"server": self.proxy}

        self.browser = await self.playwright.chromium.launch(**launch_args)
        self.context = await self.browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        self.page = await self.context.new_page()

    async def close(self):
        """清理浏览器资源。"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def screenshot(self, name="debug"):
        """截图保存。"""
        name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)[:50]
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        ts = int(time.time())
        path = os.path.join(SCREENSHOTS_DIR, f"zlib-{name}-{ts}.png")
        await self.page.screenshot(path=path, full_page=True)
        log.info(f"截图: {path}")
        return path

    async def goto(self, url):
        """导航到页面。"""
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self.page.wait_for_timeout(2000)

    async def search(self, query, lang=None, extensions=None):
        """搜索书籍，返回结果列表。"""
        # 构建搜索 URL
        encoded_query = urllib.parse.quote(query)
        url = f"{ZLIB_BASE}/s/{encoded_query}"
        params = []
        if lang:
            params.append(f"languages[]={urllib.parse.quote(lang)}")
        if extensions:
            for ext in extensions:
                params.append(f"extensions[]={urllib.parse.quote(ext)}")
        if params:
            url += "?" + "&".join(params)

        log.info(f"搜索: {url}")
        await self.page.goto(url, wait_until="networkidle", timeout=60000)
        # 模拟人工操作节奏，等待 JS 渲染 Web Components
        await self.page.wait_for_timeout(5000)
        # 滚动触发懒加载
        await self.page.evaluate("window.scrollTo(0, 300)")
        await self.page.wait_for_timeout(2000)
        await self.screenshot("search-results")

        # Z-Library 使用 <z-bookcard> Web Component，信息在 attributes 上
        results = await self.page.evaluate(r"""() => {
            const books = [];
            const cards = document.querySelectorAll('z-bookcard');

            for (const card of cards) {
                const titleEl = card.querySelector('[slot="title"]');
                const authorEl = card.querySelector('[slot="author"]');

                books.push({
                    title: titleEl ? titleEl.textContent.trim() : '',
                    author: authorEl ? authorEl.textContent.trim() : '',
                    year: card.getAttribute('year') || '',
                    language: card.getAttribute('language') || '',
                    extension: (card.getAttribute('extension') || '').toUpperCase(),
                    size: card.getAttribute('filesize') || '',
                    url: card.getAttribute('href') || '',
                    download_path: card.getAttribute('download') || '',
                    book_id: card.getAttribute('id') || '',
                    rating: card.getAttribute('rating') || '',
                });
            }
            return books;
        }""")

        # 补全相对 URL
        for r in results:
            if r.get("url") and not r["url"].startswith("http"):
                r["url"] = ZLIB_BASE + r["url"]
            if r.get("download_path") and not r["download_path"].startswith("http"):
                r["download_url"] = ZLIB_BASE + r["download_path"]

        if not results:
            log.warning("未从页面提取到搜索结果，可能页面结构变化或被 Cloudflare 拦截")
            log.info("已保存截图，请检查页面内容")

        return results

    async def download_book(self, download_url, book_url=None):
        """下载书籍文件。

        Args:
            download_url: 直接下载链接 (如 https://z-library.sk/dl/...)
            book_url: 书籍详情页链接（备选方案）

        Returns:
            下载的文件路径，或 None
        """
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

        # 方案 1：直接访问下载链接
        if download_url:
            log.info(f"访问下载链接: {download_url}")
            try:
                async with self.page.expect_download(timeout=120000) as download_info:
                    await self.page.goto(download_url)

                download = await download_info.value
                suggested = download.suggested_filename or "book.pdf"
                safe_name = re.sub(r'[<>:"/\\|?*]', '_', suggested)[:200]
                filepath = os.path.join(DOWNLOADS_DIR, safe_name)
                await download.save_as(filepath)
                log.info(f"下载完成: {filepath}")
                return filepath
            except Exception as e:
                log.warning(f"直接下载失败: {e}")
                await self.screenshot("download-direct-failed")

        # 方案 2：打开详情页找下载按钮
        if book_url:
            log.info(f"打开书籍详情页: {book_url}")
            await self.page.goto(book_url, wait_until="networkidle", timeout=60000)
            await self.page.wait_for_timeout(3000)
            await self.screenshot("book-detail")

            # 查找下载按钮
            dl_selectors = [
                'a.dlButton',
                'a[class*="download"]',
                'a[href*="/dl/"]',
            ]
            for sel in dl_selectors:
                try:
                    if await self.page.locator(sel).count() > 0:
                        log.info(f"找到下载按钮: {sel}")
                        async with self.page.expect_download(timeout=120000) as download_info:
                            await self.page.locator(sel).first.click()

                        download = await download_info.value
                        suggested = download.suggested_filename or "book.pdf"
                        safe_name = re.sub(r'[<>:"/\\|?*]', '_', suggested)[:200]
                        filepath = os.path.join(DOWNLOADS_DIR, safe_name)
                        await download.save_as(filepath)
                        log.info(f"下载完成（详情页）: {filepath}")
                        return filepath
                except Exception:
                    continue

            log.error("详情页未找到可用的下载按钮")
            await self.screenshot("download-no-button")

        return None


# --- 搜索结果缓存 ---

def save_search_cache(query, results):
    """缓存搜索结果供后续 download 使用。"""
    cache = {
        "ts": int(time.time()),
        "query": query,
        "results": results,
    }
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SEARCH_CACHE_PATH, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def load_search_cache():
    """加载搜索缓存。"""
    if not os.path.exists(SEARCH_CACHE_PATH):
        return None
    with open(SEARCH_CACHE_PATH) as f:
        return json.load(f)


def log_download(book_info, filepath):
    """记录下载审计日志。"""
    entry = {
        "ts": int(time.time()),
        "title": book_info.get("title", ""),
        "author": book_info.get("author", ""),
        "extension": book_info.get("extension", ""),
        "filepath": filepath,
    }
    try:
        with open(ZLIB_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --- CLI 子命令 ---

async def cmd_search(query, lang=None, extensions=None, output_json=False, headless=True):
    """搜索书籍。"""
    zb = ZLibBrowser(headless=headless)
    try:
        await zb.start()
        results = await zb.search(query, lang=lang, extensions=extensions)

        if not results:
            print("未找到匹配书籍")
            return

        # 缓存结果
        save_search_cache(query, results)

        if output_json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return

        # 人类可读输出
        print(f'\n搜索 "{query}" — 找到 {len(results)} 本:\n')
        for i, book in enumerate(results, 1):
            title = book.get("title", "未知")
            author = book.get("author", "")
            year = book.get("year", "")
            lang_str = book.get("language", "")
            ext = book.get("extension", "")
            size = book.get("size", "")
            file_info = book.get("file_info", "")

            print(f"  {i}. {title}")
            parts = []
            if author:
                parts.append(f"作者: {author}")
            if year:
                parts.append(f"年份: {year}")
            if lang_str:
                parts.append(f"语言: {lang_str}")
            if ext or size or file_info:
                parts.append(f"文件: {ext} {size}" if ext else f"文件: {file_info}")
            if parts:
                print(f"     {' | '.join(parts)}")
            print()

        print(f"下载: python3 goofish/zlib.py download <序号>")

    finally:
        await zb.close()


async def cmd_download(index, headless=True):
    """下载搜索结果中的第 N 本。返回 (book_info, filepath) 或 (None, None)。"""
    cache = load_search_cache()
    if not cache:
        print("错误: 请先执行 search 命令")
        return None, None

    results = cache.get("results", [])
    cache_ts = cache.get("ts", 0)
    age_min = (int(time.time()) - cache_ts) / 60

    if age_min > 60:
        print(f"警告: 搜索缓存已过期 ({int(age_min)} 分钟前)，建议重新搜索")

    if index < 1 or index > len(results):
        print(f"错误: 序号 {index} 超出范围 (1-{len(results)})")
        return None, None

    book = results[index - 1]
    download_url = book.get("download_url", "")
    book_url = book.get("url", "")

    if not download_url and not book_url:
        print(f"错误: 第 {index} 本书没有链接，请重新搜索")
        return None, None

    print(f'准备下载: {book.get("title", "未知")}')
    print(f'格式: {book.get("extension", "?")}  大小: {book.get("size", "?")}')

    zb = ZLibBrowser(headless=headless)
    try:
        await zb.start()
        filepath = await zb.download_book(download_url, book_url=book_url)

        if filepath:
            log_download(book, filepath)
            print(f"\n下载完成: {filepath}")
            return book, filepath
        else:
            print("\n下载失败，请检查截图或手动下载")
            if book_url:
                print(f"书籍页面: {book_url}")
            return None, None

    finally:
        await zb.close()


async def cmd_deliver(index, to_email, headless=True):
    """下载搜索结果中的第 N 本并邮件发送给买家。"""
    from goofish.mailer import deliver_ebook

    # 先下载
    book, filepath = await cmd_download(index, headless=headless)
    if not filepath:
        print("下载失败，无法发送邮件")
        return

    # 发送邮件
    book_title = book.get("title", "")
    print(f"\n正在发送邮件到: {to_email}")
    ok = deliver_ebook(to_email, filepath, book_title=book_title)
    if ok:
        print(f"邮件发送成功: {to_email}")
    else:
        print(f"邮件发送失败，文件已下载到: {filepath}")
        print("请手动发送或检查邮件配置")


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="Z-Library 电子书搜索下载工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # search
    p_search = subparsers.add_parser("search", help="搜索书籍")
    p_search.add_argument("query", help="书名或关键词")
    p_search.add_argument("--lang", help="语言过滤 (english/chinese/...)")
    p_search.add_argument("--ext", nargs="+", help="格式过滤 (pdf epub mobi)")
    p_search.add_argument("--json", action="store_true", dest="output_json", help="JSON 输出")
    p_search.add_argument("--no-headless", action="store_true", help="显示浏览器窗口")

    # download
    p_download = subparsers.add_parser("download", help="下载书籍")
    p_download.add_argument("index", type=int, help="搜索结果序号")
    p_download.add_argument("--no-headless", action="store_true", help="显示浏览器窗口")

    # deliver — 下载 + 邮件发送
    p_deliver = subparsers.add_parser("deliver", help="下载并邮件发送")
    p_deliver.add_argument("index", type=int, help="搜索结果序号")
    p_deliver.add_argument("--to", required=True, help="收件人邮箱")
    p_deliver.add_argument("--no-headless", action="store_true", help="显示浏览器窗口")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "search":
        asyncio.run(cmd_search(
            args.query,
            lang=args.lang,
            extensions=args.ext,
            output_json=args.output_json,
            headless=not args.no_headless,
        ))
    elif args.command == "download":
        asyncio.run(cmd_download(
            args.index,
            headless=not args.no_headless,
        ))
    elif args.command == "deliver":
        asyncio.run(cmd_deliver(
            args.index,
            args.to,
            headless=not args.no_headless,
        ))


if __name__ == "__main__":
    main()
