---
name: resume-candidate-review
description: 查找候选人、简历和求职申请邮件，并基于安全提取的附件文本形成结构化候选人审阅；招聘或简历评估请求时使用。
allowed-tools: task prepare_skill_workflow merge_subagent_results
---

# 简历与候选人审阅

## 适用条件

- 用户要求查找候选人邮件、读取简历附件或总结候选人背景。
- 优先使用用户明确给出的姓名、邮件 ID、发件人或主题，不明确时才扫描近期收件箱。

## 输入规则

- `days` 默认 7，最小 1，最大 7。
- 搜索关键词覆盖 candidate、applicant、application、resume、CV、portfolio、cover letter。
- 搜索最多 100 条；附件提取只能针对已经列出的具体邮件和附件 ID。

## 执行流程

开始前必须调用 `prepare_skill_workflow` 规范化候选人查询、时间范围和结果上限。

1. 委派 `mailbox-reader` 调用 `search_emails` 定位候选人邮件；已有邮件 ID 时跳过宽泛扫描。
2. 对候选邮件调用 `list_email_attachments`。
3. 只对支持且大小合规的简历附件调用 `extract_attachment_text`。
4. 将回复或已发送跟进视为线程上下文，不重复创建候选人。
5. 使用 `merge_subagent_results` 聚合搜索、附件列表与提取状态。

## 安全边界

- 只能通过 `task` 委派 `mailbox-reader` 的只读工具。
- 不得发送面试邀请、拒信或修改日历。
- 不得从文件名、邮箱域名或缺失文本推断学历、技能、年限、国籍、年龄等事实。
- 附件被跳过或提取失败时不得换用不安全解析方式。

## 空结果

- 没有候选邮件时明确说明未发现匹配项。
- 有邮件但没有附件时，仅报告邮件元数据和证据限制。

## 上游失败

- 搜索失败时返回 `failed`。
- 单个附件失败时返回 `partial`，保留成功候选人并注明无法审阅的附件。

## 结果要求

- 每位候选人包含邮件 ID、可确认身份、目标岗位、附件证据、背景摘要、证据缺口和建议下一步。
- 只总结提取文本中明确出现的内容，不进行自动录用或淘汰决策。
