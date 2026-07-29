---
name: urgent-email-triage
description: 查找近期紧急、高优先级、需要立即关注或临近截止的邮件；用户要求紧急邮件分诊时使用。
allowed-tools: task prepare_skill_workflow merge_subagent_results
---

# 紧急邮件分诊

## 适用条件

- 用户要求查找紧急邮件、高优先级请求、告警、截止事项或需要立即回复的邮件。
- 这是只读工作流，不负责自动回复或修改邮件状态。

## 输入规则

- `days` 默认 7，最小 1，最大 7。
- 查询范围限定收件箱，关键词至少覆盖 urgent、critical、deadline、overdue、action required、security alert。
- 首次搜索最多返回 100 条；只有匹配项才读取完整正文。

## 执行流程

开始前必须调用 `prepare_skill_workflow`，后续搜索只能原样使用其返回的
`search_criteria` 和结果上限。

1. 委派 `mailbox-reader` 调用 `search_skill_emails`，传入
   `skill_name="urgent-email-triage"` 和原始参数，由服务端执行限定时间及收件箱查询。
2. 对每个匹配邮件 ID 委派调用 `get_email`，读取真实正文和头信息。
3. 委派调用 `get_unanswered_emails`，补充没有命中关键词但仍等待回复的线程。
4. 使用 `merge_subagent_results` 保留搜索、正文和待回复来源的独立状态。
5. 按生产阻断、安全告警、明确截止、直接请求和一般提醒降序整理。

## 安全边界

- 只能通过 `task` 委派 `mailbox-reader`。
- 不得仅因营销邮件包含“urgent”就提升为高优先级。
- 不得调用 `send_email`、退订执行、日历写入或邮箱状态修改。
- 优先级必须有主题、正文、发件人或时间证据支持。

## 空结果

- 搜索和待回复均为空时明确说明未发现紧急邮件。
- 搜索有摘要但正文不可用时，不得编造正文结论。

## 上游失败

- 搜索失败则工作流失败；正文部分失败时返回 `partial` 并列出未读取的邮件 ID。
- 待回复查询失败不能被描述为“没有待回复邮件”。

## 结果要求

- 每项包含邮件 ID、发件人、主题、紧急依据和建议动作。
- 建议动作只能是建议，不能声称已经回复、处理或关闭。
