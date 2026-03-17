#!/usr/bin/env python3
"""邮件发送模块 — 用于电子书交付。

用法:
    python3 goofish/mailer.py send --to buyer@example.com --file /path/to/book.pdf
    python3 goofish/mailer.py send --to buyer@example.com --file book.pdf --subject "你要的书"
"""

import argparse
import asyncio
import email.mime.application
import email.mime.multipart
import email.mime.text
import logging
import os
import smtplib
import ssl
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from goofish.config import CONFIG_DIR, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mailer")


def send_email(to_addr, subject, body, attachment_path=None, config=None):
    """发送邮件（支持附件）。

    Args:
        to_addr: 收件人邮箱
        subject: 邮件主题
        body: 邮件正文
        attachment_path: 附件文件路径（可选）
        config: 邮件配置 dict，为 None 则从 config.json 加载

    Returns:
        True 成功，False 失败
    """
    if config is None:
        full_config = load_config()
        config = full_config.get("email", {})

    smtp_host = config.get("smtp_host", "")
    smtp_port = config.get("smtp_port", 465)
    username = config.get("username", "")
    password = config.get("password", "")
    sender = config.get("sender", username)

    if not all([smtp_host, username, password]):
        log.error("邮件配置不完整，请在 config.json 中设置 email.smtp_host/username/password")
        return False

    # 构建邮件
    msg = email.mime.multipart.MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))

    # 添加附件
    if attachment_path:
        if not os.path.exists(attachment_path):
            log.error(f"附件不存在: {attachment_path}")
            return False

        filename = os.path.basename(attachment_path)
        filesize_mb = os.path.getsize(attachment_path) / (1024 * 1024)
        log.info(f"附件: {filename} ({filesize_mb:.1f} MB)")

        if filesize_mb > 50:
            log.error(f"附件过大 ({filesize_mb:.1f} MB)，大部分邮箱限制 50MB")
            return False

        with open(attachment_path, "rb") as f:
            part = email.mime.application.MIMEApplication(f.read(), Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)

    # 发送
    try:
        if smtp_port == 465:
            # SSL
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as server:
                server.login(username, password)
                server.send_message(msg)
        else:
            # STARTTLS
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.starttls()
                server.login(username, password)
                server.send_message(msg)

        log.info(f"邮件已发送: {to_addr}")
        return True

    except smtplib.SMTPAuthenticationError:
        log.error("SMTP 认证失败，请检查用户名和密码（可能需要应用专用密码）")
        return False
    except smtplib.SMTPException as e:
        log.error(f"SMTP 发送失败: {e}")
        return False
    except Exception as e:
        log.error(f"邮件发送异常: {e}")
        return False


def deliver_ebook(to_addr, filepath, book_title=None, config=None):
    """发送电子书到买家邮箱。

    Args:
        to_addr: 买家邮箱
        filepath: 电子书文件路径
        book_title: 书名（用于邮件标题）
        config: 邮件配置

    Returns:
        True 成功，False 失败
    """
    if not book_title:
        book_title = os.path.splitext(os.path.basename(filepath))[0]

    subject = f"你要的电子书: {book_title}"
    body = (
        f"你好！\n\n"
        f"你要的电子书《{book_title}》已找到，请查收附件。\n\n"
        f"文件名: {os.path.basename(filepath)}\n\n"
        f"如果有任何问题，请随时在闲鱼联系我。\n\n"
        f"祝阅读愉快！"
    )

    return send_email(to_addr, subject, body, attachment_path=filepath, config=config)


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="邮件发送工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # send
    p_send = subparsers.add_parser("send", help="发送邮件")
    p_send.add_argument("--to", required=True, help="收件人邮箱")
    p_send.add_argument("--file", dest="filepath", help="附件路径")
    p_send.add_argument("--subject", default=None, help="邮件主题")
    p_send.add_argument("--body", default=None, help="邮件正文")
    p_send.add_argument("--book-title", default=None, help="书名（用于默认标题）")

    # test — 发送测试邮件
    p_test = subparsers.add_parser("test", help="发送测试邮件")
    p_test.add_argument("--to", required=True, help="测试收件人邮箱")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "send":
        if args.filepath:
            # 发送带附件的电子书
            ok = deliver_ebook(
                args.to,
                args.filepath,
                book_title=args.book_title,
                config=None,
            )
        else:
            # 纯文本邮件
            subject = args.subject or "闲鱼消息"
            body = args.body or "（无正文）"
            ok = send_email(args.to, subject, body, config=None)

        sys.exit(0 if ok else 1)

    elif args.command == "test":
        ok = send_email(
            args.to,
            subject="测试邮件 — 闲鱼电子书服务",
            body="这是一封测试邮件，如果你收到了说明邮件配置正确。",
            config=None,
        )
        if ok:
            print("测试邮件发送成功！")
        else:
            print("测试邮件发送失败，请检查配置。")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
