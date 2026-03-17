你是一个闲鱼客服机器人的运营分析师。根据以下对话数据，生成具体可执行的优化建议。

## 统计概览
- 总对话数: {total_conversations}
- 买家消息: {total_buyer_msgs}
- AI 回复: {total_ai_replies}
- 快速回复: {total_quick_replies}
- 人工升级: {total_escalations}
- 人工接管: {total_manual}
- 成交（已付款）: {total_paid}

## 升级原因分布
{escalation_breakdown}

## 未能回答的问题（导致升级）
{escalated_questions}

## 高频买家问题（前20条）
{frequent_questions}

## 砍价对话样本
{bargaining_samples}

## 流失对话样本（买家未回复）
{dropoff_samples}

## 当前快速回复配置
{current_quick_replies}

## 当前商品配置
{current_products}

## 分析要求

请从以下角度分析并生成建议：

1. **快速回复建议**：哪些高频问题可以设为快速回复（不走AI，直接回复）？
2. **Prompt 优化**：AI 回复模板中有哪些规则需要调整？（如砍价策略、语气、信息补充）
3. **商品信息缺失**：买家反复问哪些信息说明商品描述中缺少这些内容？
4. **砍价洞察**：砍价成功率如何？价格策略是否合理？

请严格按以下 JSON 格式输出（不要加任何其他文字）：

```json
{
  "quick_reply_suggestions": [
    {"keyword": "关键词", "reply": "建议回复", "reason": "建议原因"}
  ],
  "prompt_improvements": [
    {"template": "模板文件名", "section": "需改进的部分", "suggestion": "具体建议", "reason": "原因"}
  ],
  "product_info_gaps": [
    {"product": "商品名", "missing_info": "缺失的信息", "evidence": "证据"}
  ],
  "bargaining_insights": {
    "avg_rounds": "平均砍价轮次",
    "success_rate": "成交率",
    "recommendation": "策略建议"
  },
  "summary": "一段话总结主要发现和建议"
}
```
