# 补抽：只找「需求」和「顾虑」

第一遍抽取已经跑完了。下面给你原文，以及第一遍已经抽到的事实。

你的任务只有一件：**看看有没有漏掉的「需求」和「顾虑」**。
其它一律不归你管——承诺、关键人、竞争态势、时间节点、风险信号都不要出。

| 类型 | 算 | 不算 |
|------|-----|------|
| need 需求 | 客户表达的期望状态、待解决问题、预算与能力要求 | 客户的抱怨、对现状的描述、我方推荐 |
| concern 顾虑 | 客户表达的担忧、异议、阻力、设定的前提条件与否决门槛 | 普通疑问、信息咨询、中性陈述 |

# 三条硬约束

1. **只写原文里明确出现的。** 原文没写的，一个字都不要编。也不要因为它「大概率有」就补上。
2. **不要重复第一遍已经抽到的。** 下面列出了已有的陈述，语义相同的都不许再出。
3. **确实没有就返回 `[]`。** 空数组是正确答案，不是失败。宁可空着，也不要凑一条。

# 定位原文

每条必须带 `source_span`：从原文**原样复制**的最短片段。
不许改写、不许拼接、不许加省略号。定位不到原文的一律不要出——
下游会逐条校验，定位不到的会被拒收，等于白写。

# 需求与顾虑的边界

客户说「我要 A」是 need；客户说「没有 A 我就不谈了」是 concern（severity 填 blocker）。
区别不在内容，在这句话**有没有否决后果**：有没有「否则」「这是前提」「不满足就不」这类说法。
同一件事可以两条都出、共用 source_span。

# 字段

- need：`need_text`、`category`（function / performance / integration / compliance / cost / service / other）、
  `explicitness`（explicit / inferred）、`priority`（must / should / could）
- concern：`concern_text`、`verbatim_quote`（客户原话，同样原样复制）、
  `category`（price / cycle / risk / trust / internal_resistance / fit / compliance / support）、
  `severity`（blocker / high / medium / low）、`is_resolved`（false）

枚举**只能填上面列的英文值**，不要自创、不要填中文、不要用近义词替换。

# 输出

只输出 JSON 数组，不要任何解释文字。
