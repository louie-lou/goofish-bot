#!/usr/bin/env python3
"""闲鱼机器人对话分析 + 优化建议生成

用法：
    python3 goofish/analyze.py analyze [--days 7]            # 统计分析
    python3 goofish/analyze.py report [--send] [--days 7]    # 生成优化报告
    python3 goofish/analyze.py apply <report_id> [--dry-run] # 应用建议
    python3 goofish/analyze.py feedback <cid> <good|bad> [comment]  # 手动标记
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from config import (
    CONFIG_DIR, CONVERSATIONS_DIR, REPORTS_DIR, SUGGESTIONS_DIR,
    PROMPTS_DIR, load_config, save_config, OPENCLAW_CMD,
)


# --- 数据加载 ---

def load_conversations(days=7):
    """加载时间窗口内的所有对话事件。"""
    cutoff = time.time() - (days * 86400)
    conversations = {}  # cid -> [events]

    if not os.path.isdir(CONVERSATIONS_DIR):
        return conversations

    for fname in os.listdir(CONVERSATIONS_DIR):
        if not fname.endswith(".jsonl"):
            continue
        cid = fname.replace(".jsonl", "")
        events = []
        path = os.path.join(CONVERSATIONS_DIR, fname)
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("ts", 0) >= cutoff:
                        events.append(event)
        except Exception:
            continue
        if events:
            conversations[cid] = events

    return conversations


# --- 纯 Python 指标计算 ---

def compute_metrics(conversations):
    """从对话事件中计算聚合指标。"""
    total_buyer_msgs = 0
    total_ai_replies = 0
    total_quick_replies = 0
    total_escalations = 0
    total_manual = 0
    total_trades = Counter()
    escalation_reasons = Counter()
    buyer_questions = []
    escalated_questions = []
    bargaining_convos = []
    dropoff_convos = []
    feedback_events = []

    now = time.time()

    for cid, events in conversations.items():
        has_trade = False
        convo_messages = []
        last_bot_reply_ts = None

        for ev in events:
            etype = ev.get("type", "")

            if etype == "msg_buyer":
                total_buyer_msgs += 1
                buyer_questions.append(ev.get("content", ""))
                convo_messages.append(ev)
                last_bot_reply_ts = None  # reset

            elif etype == "msg_seller_ai":
                total_ai_replies += 1
                convo_messages.append(ev)
                last_bot_reply_ts = ev.get("ts")

            elif etype == "msg_seller_quick":
                total_quick_replies += 1
                convo_messages.append(ev)
                last_bot_reply_ts = ev.get("ts")

            elif etype == "msg_seller_manual":
                total_manual += 1
                convo_messages.append(ev)
                last_bot_reply_ts = None

            elif etype == "escalation":
                total_escalations += 1
                reason = ev.get("reason", "unknown")
                escalation_reasons[reason] += 1
                escalated_questions.append(ev.get("content", ""))

            elif etype == "trade_event":
                total_trades[ev.get("event", "")] += 1
                has_trade = True

            elif etype == "feedback":
                feedback_events.append(ev)

        # 检测砍价对话（包含价格相关关键词的对话）
        price_keywords = ["便宜", "优惠", "少", "降", "元", "块", "价", "多少"]
        has_price_talk = any(
            ev.get("type") == "msg_buyer" and
            any(kw in ev.get("content", "") for kw in price_keywords)
            for ev in events
        )
        if has_price_talk and len(convo_messages) >= 3:
            bargaining_convos.append({
                "cid": cid,
                "messages": convo_messages,
                "traded": has_trade,
            })

        # 检测流失（bot 回复后 24h 无买家回复且无成交）
        if last_bot_reply_ts and not has_trade:
            time_since = now - last_bot_reply_ts
            # 看最后一个事件是否是 bot 的回复
            last_relevant = [e for e in events if e.get("type") in
                             ("msg_buyer", "msg_seller_ai", "msg_seller_quick")]
            if last_relevant and last_relevant[-1].get("type") in ("msg_seller_ai", "msg_seller_quick"):
                if time_since > 86400:  # 24h
                    dropoff_convos.append({
                        "cid": cid,
                        "messages": convo_messages[-6:],  # 最后几条
                    })

    # 高频问题聚类（简单去重+计数）
    question_counts = Counter()
    for q in buyer_questions:
        q_clean = q.strip()
        if len(q_clean) >= 2:
            question_counts[q_clean] += 1
    frequent_questions = question_counts.most_common(20)

    return {
        "total_conversations": len(conversations),
        "total_buyer_msgs": total_buyer_msgs,
        "total_ai_replies": total_ai_replies,
        "total_quick_replies": total_quick_replies,
        "total_escalations": total_escalations,
        "total_manual": total_manual,
        "total_trades": dict(total_trades),
        "escalation_reasons": dict(escalation_reasons),
        "frequent_questions": frequent_questions,
        "escalated_questions": escalated_questions,
        "bargaining_convos": bargaining_convos[:5],  # 最多 5 段
        "dropoff_convos": dropoff_convos[:5],
        "feedback_events": feedback_events,
    }


# --- AI 分析 ---

def build_analysis_prompt(metrics, config):
    """构建 AI 分析的 prompt。"""
    template_path = os.path.join(PROMPTS_DIR, "analysis.md")
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"分析模板不存在: {template_path}")
    with open(template_path) as f:
        template = f.read()

    # 格式化各部分
    trades = metrics["total_trades"]
    paid = trades.get("paid", 0)

    escalation_lines = []
    for reason, count in metrics["escalation_reasons"].items():
        escalation_lines.append(f"- {reason}: {count}次")
    escalation_breakdown = "\n".join(escalation_lines) or "无"

    freq_lines = []
    for q, count in metrics["frequent_questions"]:
        freq_lines.append(f"- \"{q}\" ({count}次)")
    frequent_questions = "\n".join(freq_lines[:20]) or "无"

    # 砍价样本
    bargaining_samples = []
    for bc in metrics["bargaining_convos"][:5]:
        lines = [f"### 会话 {bc['cid']} ({'成交' if bc['traded'] else '未成交'})"]
        for msg in bc["messages"][-8:]:
            role = "买家" if msg.get("type") == "msg_buyer" else "卖家"
            lines.append(f"{role}: {msg.get('content', '')[:100]}")
        bargaining_samples.append("\n".join(lines))
    bargaining_text = "\n\n".join(bargaining_samples) or "无"

    # 流失样本
    dropoff_samples = []
    for dc in metrics["dropoff_convos"][:5]:
        lines = [f"### 会话 {dc['cid']}"]
        for msg in dc["messages"]:
            role = "买家" if msg.get("type") == "msg_buyer" else "卖家"
            lines.append(f"{role}: {msg.get('content', '')[:100]}")
        dropoff_samples.append("\n".join(lines))
    dropoff_text = "\n\n".join(dropoff_samples) or "无"

    # 当前快速回复
    qr = config.get("quick_replies", {})
    qr_lines = [f"- \"{k}\" → \"{v}\"" for k, v in qr.items()]
    current_quick_replies = "\n".join(qr_lines) or "无"

    # 当前商品配置
    products = config.get("products", {})
    product_lines = []
    for pid, pcfg in products.items():
        name = pcfg.get("name", pid)
        product_lines.append(f"- {name} (strategy: {pcfg.get('strategy', 'default')})")
    current_products = "\n".join(product_lines) or "无"

    # 升级的问题（未能回答的）
    escalated = metrics.get("escalated_questions", [])
    escalated_lines = [f"- \"{q[:80]}\"" for q in escalated[:10]]
    escalated_text = "\n".join(escalated_lines) or "无"

    class _SafeDict(dict):
        def __missing__(self, key):
            return f"{{{key}}}"

    return template.format_map(_SafeDict({
        "total_conversations": metrics["total_conversations"],
        "total_buyer_msgs": metrics["total_buyer_msgs"],
        "total_ai_replies": metrics["total_ai_replies"],
        "total_quick_replies": metrics["total_quick_replies"],
        "total_escalations": metrics["total_escalations"],
        "total_manual": metrics["total_manual"],
        "total_paid": paid,
        "escalation_breakdown": escalation_breakdown,
        "frequent_questions": frequent_questions,
        "bargaining_samples": bargaining_text,
        "dropoff_samples": dropoff_text,
        "escalated_questions": escalated_text,
        "current_quick_replies": current_quick_replies,
        "current_products": current_products,
    }))


def run_ai_analysis(prompt):
    """调用 OpenClaw 执行 AI 分析。"""
    try:
        result = subprocess.run(
            [*OPENCLAW_CMD, "agent", "-m", prompt, "--json", "--agent", "main"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"AI 分析失败: {result.stderr[:200]}", file=sys.stderr)
            return None

        output = result.stdout.strip()
        try:
            data = json.loads(output)
            if "result" in data:
                payloads = data["result"].get("payloads", [])
                if payloads:
                    text = payloads[0].get("text", "")
                    # 尝试从 AI 输出中提取 JSON
                    return _extract_json_from_text(text)
            if "summary" in data:
                return _extract_json_from_text(data["summary"])
        except json.JSONDecodeError:
            return _extract_json_from_text(output)

    except subprocess.TimeoutExpired:
        print("AI 分析超时", file=sys.stderr)
    except Exception as e:
        print(f"AI 分析异常: {e}", file=sys.stderr)
    return None


def _extract_json_from_text(text):
    """从可能包含 markdown 代码块的文本中提取 JSON。"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # 尝试从 ```json ... ``` 代码块中提取
    import re
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试找到第一个 { 到最后一个 } 的区间
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


# --- 报告生成 ---

def generate_report(metrics, ai_suggestions, days):
    """生成优化报告（JSON + Markdown）。"""
    report_id = datetime.now().strftime("%Y%m%d-%H%M")
    period_end = datetime.now().strftime("%Y-%m-%d")
    period_start = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d")

    trades = metrics["total_trades"]
    paid = trades.get("paid", 0)

    # Markdown 报告
    md_lines = [
        f"# 闲鱼机器人优化报告 {period_end}",
        "",
        f"## 概览",
        f"分析周期: {period_start} ~ {period_end}",
        f"- 总对话: {metrics['total_conversations']}",
        f"- 买家消息: {metrics['total_buyer_msgs']}",
        f"- AI 回复: {metrics['total_ai_replies']}",
        f"- 快速回复: {metrics['total_quick_replies']}",
        f"- 人工升级: {metrics['total_escalations']}",
        f"- 人工接管: {metrics['total_manual']}",
        f"- 成交（已付款）: {paid}",
        "",
    ]

    # 高频问题
    if metrics["frequent_questions"]:
        md_lines.append("## 高频买家问题")
        md_lines.append("| 问题 | 次数 |")
        md_lines.append("|------|------|")
        for q, count in metrics["frequent_questions"][:10]:
            md_lines.append(f"| {q[:40]} | {count} |")
        md_lines.append("")

    # 升级原因
    if metrics["escalation_reasons"]:
        md_lines.append("## 升级原因分布")
        for reason, count in metrics["escalation_reasons"].items():
            md_lines.append(f"- {reason}: {count}次")
        md_lines.append("")

    # AI 建议
    if ai_suggestions:
        qr_suggestions = ai_suggestions.get("quick_reply_suggestions", [])
        if qr_suggestions:
            md_lines.append("## 建议：新增快速回复")
            md_lines.append("| 关键词 | 建议回复 | 依据 |")
            md_lines.append("|--------|----------|------|")
            for s in qr_suggestions:
                md_lines.append(f"| {s.get('keyword', '')} | {s.get('reply', '')} | {s.get('reason', '')} |")
            md_lines.append("")

        prompt_improvements = ai_suggestions.get("prompt_improvements", [])
        if prompt_improvements:
            md_lines.append("## 建议：Prompt 优化")
            for i, p in enumerate(prompt_improvements, 1):
                md_lines.append(f"{i}. **{p.get('template', '')} - {p.get('section', '')}**")
                md_lines.append(f"   {p.get('suggestion', '')}")
                md_lines.append(f"   理由: {p.get('reason', '')}")
            md_lines.append("")

        product_gaps = ai_suggestions.get("product_info_gaps", [])
        if product_gaps:
            md_lines.append("## 建议：商品信息补充")
            for p in product_gaps:
                md_lines.append(f"- **{p.get('product', '')}**: {p.get('missing_info', '')}")
                md_lines.append(f"  证据: {p.get('evidence', '')}")
            md_lines.append("")

        bargaining = ai_suggestions.get("bargaining_insights", {})
        if bargaining:
            md_lines.append("## 砍价洞察")
            for k, v in bargaining.items():
                md_lines.append(f"- {k}: {v}")
            md_lines.append("")

        summary = ai_suggestions.get("summary", "")
        if summary:
            md_lines.append(f"## 总结")
            md_lines.append(summary)
            md_lines.append("")

    md_lines.append("---")
    md_lines.append(f"报告ID: {report_id}")
    md_lines.append(f"应用: `python3 goofish/analyze.py apply {report_id} --dry-run`")

    md_content = "\n".join(md_lines)

    # 保存
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(SUGGESTIONS_DIR, exist_ok=True)

    md_path = os.path.join(REPORTS_DIR, f"{report_id}-analysis.md")
    with open(md_path, "w") as f:
        f.write(md_content)

    # JSON 报告
    report_data = {
        "report_id": report_id,
        "generated_at": datetime.now().isoformat(),
        "period": {"from": period_start, "to": period_end, "days": days},
        "metrics": {
            "total_conversations": metrics["total_conversations"],
            "total_buyer_msgs": metrics["total_buyer_msgs"],
            "total_ai_replies": metrics["total_ai_replies"],
            "total_quick_replies": metrics["total_quick_replies"],
            "total_escalations": metrics["total_escalations"],
            "total_manual": metrics["total_manual"],
            "total_trades": metrics["total_trades"],
            "frequent_questions": metrics["frequent_questions"],
            "escalation_reasons": metrics["escalation_reasons"],
        },
        "ai_suggestions": ai_suggestions,
    }
    json_path = os.path.join(REPORTS_DIR, f"{report_id}-analysis.json")
    with open(json_path, "w") as f:
        json.dump(report_data, f, ensure_ascii=False, indent=2)

    # 保存待审批建议
    if ai_suggestions:
        suggestions_data = {
            "report_id": report_id,
            "generated_at": datetime.now().isoformat(),
            "period": {"from": period_start, "to": period_end},
            "status": "pending",
            "suggestions": ai_suggestions,
        }
        suggestions_path = os.path.join(SUGGESTIONS_DIR, f"{report_id}.json")
        with open(suggestions_path, "w") as f:
            json.dump(suggestions_data, f, ensure_ascii=False, indent=2)

    return report_id, md_content, md_path


def send_discord_report(md_content):
    """发送报告到 Discord。"""
    config = load_config()
    channel = config.get("notification", {}).get("discord_channel", "")
    if not channel:
        print("未配置 Discord channel，跳过发送")
        return

    # 截断过长的报告
    if len(md_content) > 1800:
        md_content = md_content[:1800] + "\n\n...(报告已截断，完整版请查看文件)"

    try:
        subprocess.run(
            [*OPENCLAW_CMD, "message", "send",
             "--channel", channel, "--text", md_content],
            capture_output=True, text=True, timeout=30,
        )
        print("报告已发送到 Discord")
    except Exception as e:
        print(f"Discord 发送失败: {e}")


# --- 审批应用 ---

def cmd_apply(report_id, dry_run=False):
    """应用优化建议。"""
    suggestions_path = os.path.join(SUGGESTIONS_DIR, f"{report_id}.json")
    if not os.path.exists(suggestions_path):
        print(f"找不到建议文件: {suggestions_path}")
        sys.exit(1)

    with open(suggestions_path) as f:
        data = json.load(f)

    if data["status"] != "pending":
        print(f"报告 {report_id} 状态为 {data['status']}，无法应用")
        return

    suggestions = data.get("suggestions", {})
    config = load_config()
    changes = []

    # 应用快速回复
    qr_suggestions = suggestions.get("quick_reply_suggestions", [])
    if qr_suggestions:
        quick_replies = config.setdefault("quick_replies", {})
        for item in qr_suggestions:
            keyword = item.get("keyword", "")
            reply = item.get("reply", "")
            if keyword and reply and keyword not in quick_replies:
                if dry_run:
                    changes.append(f"  + quick_reply: \"{keyword}\" → \"{reply}\"")
                else:
                    quick_replies[keyword] = reply
                    changes.append(f"  + quick_reply: \"{keyword}\" → \"{reply}\"")

    if dry_run:
        print(f"DRY RUN — 报告 {report_id} 将应用以下变更：")
        if changes:
            for c in changes:
                print(c)
        else:
            print("  (无快速回复变更)")

        # 显示 prompt 和商品建议（仅展示不应用）
        prompt_changes = suggestions.get("prompt_improvements", [])
        if prompt_changes:
            print("\nPrompt 优化建议（需手动确认）：")
            for p in prompt_changes:
                print(f"  ~ {p.get('template', '')}/{p.get('section', '')}: {p.get('suggestion', '')[:80]}")

        product_gaps = suggestions.get("product_info_gaps", [])
        if product_gaps:
            print("\n商品信息补充建议（需手动更新闲鱼）：")
            for p in product_gaps:
                print(f"  {p.get('product', '')}: {p.get('missing_info', '')}")
        return

    # 实际应用
    if changes:
        save_config(config)
        print(f"已应用 {len(changes)} 项快速回复变更：")
        for c in changes:
            print(c)
    else:
        print("无快速回复变更需要应用")

    # 标记为已应用
    data["status"] = "applied"
    data["applied_at"] = datetime.now().isoformat()
    data["applied_changes"] = changes
    with open(suggestions_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Prompt 和商品建议只展示
    prompt_changes = suggestions.get("prompt_improvements", [])
    if prompt_changes:
        print("\nPrompt 优化建议（需手动修改模板文件）：")
        for p in prompt_changes:
            print(f"  文件: goofish/prompts/{p.get('template', '')}")
            print(f"  部分: {p.get('section', '')}")
            print(f"  建议: {p.get('suggestion', '')}")
            print(f"  理由: {p.get('reason', '')}")
            print()

    product_gaps = suggestions.get("product_info_gaps", [])
    if product_gaps:
        print("商品信息补充建议（需到闲鱼平台手动更新）：")
        for p in product_gaps:
            print(f"  {p.get('product', '')}: {p.get('missing_info', '')}")
            print(f"  证据: {p.get('evidence', '')}")
            print()

    print(f"\n重启 bot 生效: launchctl kickstart -k gui/501/ai.openclaw.goofish")


# --- 手动反馈 ---

def cmd_feedback(cid, rating, comment=""):
    """写入人工反馈到对话日志。"""
    os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
    safe_cid = str(cid).replace("@goofish", "").replace("/", "_")
    path = os.path.join(CONVERSATIONS_DIR, f"{safe_cid}.jsonl")

    event = {
        "ts": int(time.time()),
        "type": "feedback",
        "rating": rating,
    }
    if comment:
        event["comment"] = comment

    with open(path, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print(f"已记录反馈: cid={cid} rating={rating}" + (f" comment={comment}" if comment else ""))


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="闲鱼机器人对话分析工具")
    sub = parser.add_subparsers(dest="command")

    # analyze
    p_analyze = sub.add_parser("analyze", help="统计分析对话数据")
    p_analyze.add_argument("--days", type=int, default=7, help="分析最近 N 天（默认 7）")

    # report
    p_report = sub.add_parser("report", help="生成优化报告")
    p_report.add_argument("--days", type=int, default=7, help="分析最近 N 天（默认 7）")
    p_report.add_argument("--send", action="store_true", help="发送到 Discord")

    # apply
    p_apply = sub.add_parser("apply", help="应用优化建议")
    p_apply.add_argument("report_id", help="报告 ID（如 20260310-0900）")
    p_apply.add_argument("--dry-run", action="store_true", help="预览变更不实际应用")

    # feedback
    p_feedback = sub.add_parser("feedback", help="手动标记对话反馈")
    p_feedback.add_argument("cid", help="会话 ID")
    p_feedback.add_argument("rating", choices=["good", "bad"], help="评价")
    p_feedback.add_argument("comment", nargs="?", default="", help="备注")

    args = parser.parse_args()

    if args.command == "analyze":
        conversations = load_conversations(args.days)
        if not conversations:
            print(f"最近 {args.days} 天没有对话数据")
            print(f"对话日志目录: {CONVERSATIONS_DIR}")
            return
        metrics = compute_metrics(conversations)
        print(f"=== 闲鱼机器人对话分析（最近 {args.days} 天） ===\n")
        print(f"总对话数:     {metrics['total_conversations']}")
        print(f"买家消息:     {metrics['total_buyer_msgs']}")
        print(f"AI 回复:      {metrics['total_ai_replies']}")
        print(f"快速回复:     {metrics['total_quick_replies']}")
        print(f"人工升级:     {metrics['total_escalations']}")
        print(f"人工接管:     {metrics['total_manual']}")
        print(f"交易事件:     {json.dumps(metrics['total_trades'], ensure_ascii=False)}")
        print()
        if metrics["frequent_questions"]:
            print("高频问题 Top 10:")
            for q, count in metrics["frequent_questions"][:10]:
                print(f"  [{count}次] {q[:60]}")
        if metrics["escalation_reasons"]:
            print(f"\n升级原因:")
            for reason, count in metrics["escalation_reasons"].items():
                print(f"  {reason}: {count}次")
        if metrics["dropoff_convos"]:
            print(f"\n流失对话: {len(metrics['dropoff_convos'])} 个（bot 回复后 24h 无响应）")
        if metrics["bargaining_convos"]:
            traded = sum(1 for c in metrics["bargaining_convos"] if c["traded"])
            total = len(metrics["bargaining_convos"])
            print(f"\n砍价对话: {total} 个（成交 {traded} 个）")
        if metrics["feedback_events"]:
            good = sum(1 for f in metrics["feedback_events"] if f.get("rating") == "good")
            bad = sum(1 for f in metrics["feedback_events"] if f.get("rating") == "bad")
            print(f"\n人工反馈: good={good} bad={bad}")

    elif args.command == "report":
        conversations = load_conversations(args.days)
        if not conversations:
            print(f"最近 {args.days} 天没有对话数据")
            return
        metrics = compute_metrics(conversations)
        config = load_config()

        print("正在生成 AI 分析...")
        prompt = build_analysis_prompt(metrics, config)
        ai_suggestions = run_ai_analysis(prompt)

        if not ai_suggestions:
            print("AI 分析未返回有效结果，生成无建议报告")
            ai_suggestions = {}

        report_id, md_content, md_path = generate_report(metrics, ai_suggestions, args.days)
        print(f"\n报告已生成:")
        print(f"  Markdown: {md_path}")
        print(f"  报告ID: {report_id}")
        print()
        print(md_content)

        if args.send:
            send_discord_report(md_content)

    elif args.command == "apply":
        cmd_apply(args.report_id, dry_run=args.dry_run)

    elif args.command == "feedback":
        cmd_feedback(args.cid, args.rating, args.comment)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
