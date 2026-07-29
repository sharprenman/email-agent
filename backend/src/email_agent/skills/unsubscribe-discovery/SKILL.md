---
name: unsubscribe-discovery
description: 只读查找近期订阅、营销和简报邮件，并识别 one-click、mailto、网站或未知退订方式；用户要求查看可退订列表时使用。
allowed-tools: task prepare_skill_workflow merge_subagent_results
---

# 退订候选发现

## 适用条件

- 用户要求查找订阅邮件、营销邮件、新闻简报或可退订发件人。
- 这是只读发现流程，不执行任何退订。

## 输入规则

- `days` 默认 30，最小 1，最大 90。
- 搜索最多 100 条，查询限定订阅、newsletter、marketing、manage preferences 等信号。
- 每个候选必须基于具体邮件 ID 重新读取完整邮件。

## 执行流程

开始前必须调用 `prepare_skill_workflow` 获取受限 `search_criteria`、时间窗口和结果上限。

1. 委派 `mailbox-reader` 调用 `search_skill_emails`，传入
   `skill_name="unsubscribe-discovery"` 和原始参数查找可能的订阅邮件。
2. 对候选邮件调用 `get_email`，获取标准头、正文和 DKIM 证据。
3. 对每封邮件调用 `discover_email_unsubscribe`，确定退订方式与候选指纹。
4. 按发件人或域名归组，保留代表邮件 ID 和可用方式。
5. 使用 `merge_subagent_results` 聚合发现状态。

## 安全边界

- 只能通过 `task` 委派 `mailbox-reader`。
- 不得调用 `execute_unsubscribe`、`send_email`、访问网站或修改邮箱状态。
- one-click 必须由确定性工具确认 HTTPS、DKIM 和头字段覆盖条件。
- 不得把普通网站链接自动认定为已验证 one-click。

## 空结果

- 没有候选时明确返回空列表，不执行退订，也不推荐无关邮件。
- 发现方式为 unknown 时可展示，但必须标记不可自动执行。

## 上游失败

- 搜索失败时返回 `failed`。
- 单封邮件读取或解析失败时返回 `partial`，列出失败邮件 ID。

## 结果要求

- 每个候选包含发件人、代表邮件 ID、方式、来源和安全证据。
- 结尾询问用户选择哪些候选，不能在同一次发现流程中执行。
