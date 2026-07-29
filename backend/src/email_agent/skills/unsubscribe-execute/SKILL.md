---
name: unsubscribe-execute
description: 对用户明确选择的退订候选执行 one-click 或 mailto 请求，并对网站方式返回人工操作；用户确认具体退订目标时使用。
allowed-tools: task prepare_skill_workflow merge_subagent_results
---

# 执行退订

## 适用条件

- 用户明确选择一个或多个发件人、邮件或上一步候选，并要求执行退订。
- 目标不明确、存在多个匹配或只有宽泛描述时，先执行发现流程。

## 输入规则

- 每个目标必须解析为唯一邮件 ID 和唯一 `UnsubscribeCandidate`。
- `method` 默认 auto；只允许 one_click、mailto、website 或 auto。
- 每个候选使用独立幂等键和独立审批，不批量共用凭证。

## 执行流程

开始前必须调用 `prepare_skill_workflow` 获取目标 `search_criteria` 和执行安全说明。

1. 必要时委派 `mailbox-reader` 调用 `search_skill_emails`，传入
   `skill_name="unsubscribe-execute"` 和原始参数定位唯一目标。
2. 调用 `get_email` 和 `discover_email_unsubscribe` 重新获取最新候选，不能信任旧链接文本。
3. website 或 unknown 方式只返回人工说明，不调用副作用工具。
4. one_click 或 mailto 候选委派 `mail-writer` 调用 `execute_unsubscribe`。
5. 等待 interrupt 与服务层审批后恢复；多目标逐项执行并用 `merge_subagent_results` 聚合。

## 安全边界

- 只能通过 `task` 委派读取或受审批执行。
- 不得生成审批凭证、自动打开网站、跟随重定向或对不明确目标执行。
- 不得绕过 `execute_unsubscribe` 直接发送 mailto 邮件。
- 请求被接受不等于发件人已经永久停止发送邮件。

## 空结果

- 没有目标或无法唯一匹配时返回 `failed`，不产生副作用。
- website 候选返回 `manual_required`，这不是失败，也不是已退订。

## 上游失败

- 重新发现失败时不得使用旧候选继续执行。
- 多目标混合成功和失败时返回 `partial`，逐项保留真实状态。
- 不确定网络结果不得自动重试。

## 结果要求

- 分类报告 confirmed、request_sent、manual_required、already_submitted、failed 和 uncertain。
- 仅 confirmed 可表述为服务器已接受 one-click；mailto 只能表述为退订请求邮件已发送。
