---
name: draft-reply-from-email-context
description: 根据指定邮件或待回复邮件的真实上下文生成结构化回复草稿；用户要求起草回复但尚未要求立即发送时使用。
allowed-tools: task prepare_skill_workflow merge_subagent_results
---

# 基于邮件上下文起草回复

## 适用条件

- 用户要求回复某封邮件、最近一封待回复邮件或特定发件人和主题。
- 本 Skill 只生成草稿；即使用户使用“回复”字样，也不能自动发送。

## 输入规则

- 优先使用明确邮件 ID；否则使用用户给出的发件人、主题或查询线索。
- “第 N 封待回复邮件”使用 `get_unanswered_emails` 的当前排序，N 从 1 开始。
- 查询回看默认 30 天，最大 30 天。

## 执行流程

开始前必须调用 `prepare_skill_workflow` 规范化 `search_criteria` 和最大回看窗口。

1. 委派 `mailbox-reader` 调用 `search_skill_emails` 或 `get_unanswered_emails`
   定位唯一目标；Skill 搜索必须传入 `skill_name="draft-reply-from-email-context"`。
2. 对选定邮件 ID 调用 `get_email` 获取完整上下文。
3. 目标不唯一时停止并要求用户选择，不能随机选取。
4. 委派 `mail-writer` 调用 `prepare_email_draft`，设置收件人、主题、正文和 `reply_to_email_id`。
5. 使用 `merge_subagent_results` 聚合读取与草稿状态。

## 安全边界

- 只通过 `task` 委派读取和草稿。
- 本 Skill 不得调用 `send_email`。
- 不得编造原邮件未包含的承诺、日期、价格、附件或已完成动作。
- 草稿结果必须保留 `sent=false`。

## 空结果

- 找不到目标邮件时返回 `failed`，说明查询范围，不生成无上下文回复。
- 待回复排名越界时明确返回实际可用数量。

## 上游失败

- 邮件读取失败时不得继续生成草稿。
- 草稿校验失败时保留目标邮件证据并返回 `partial` 或 `failed`。

## 结果要求

- 返回完整的收件人、主题和正文，并明确“这是草稿，尚未发送”。
- 保留目标邮件 ID 和草稿结构作为证据。
