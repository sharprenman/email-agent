---
name: weekly-email-summary
description: 汇总最近一段时间的邮件活动、待回复事项和日历安排；用户请求周报、近期邮件概览或行动摘要时使用。
allowed-tools: task prepare_skill_workflow merge_subagent_results
---

# 周度邮件摘要

## 适用条件

- 用户要求最近几天或一周的邮箱总结、待办概览、重要线程和日历安排。
- 这是只读工作流，不适用于发送邮件、修改日历或改变邮箱状态。

## 输入规则

- `days` 默认 7，最小 1，最大 30；超出范围时收敛到边界。
- 邮箱搜索使用服务端返回的 Provider 无关 `search_criteria`，最多读取 100 条摘要。
- 邮件与日历窗口从当前时间向前回溯 `days` 天，到当前时间结束，时间必须包含时区。

## 执行流程

开始前必须调用 `prepare_skill_workflow` 获取规范化时间窗口、`search_criteria` 和结果上限。

1. 只委派一次 `mailbox-reader`，在同一个任务内调用 `get_mailbox_identity`、
   `search_skill_emails`；后者必须传入 `skill_name="weekly-email-summary"`、
   原始天数、上限和 `include_unanswered=true`，由服务端同时执行时间窗口内搜索，并在内部
   调用 `get_unanswered_emails` 完成待回复查询。
2. 只有用户明确要求日程、日历或会议摘要时，才委派一次 `calendar-agent` 调用
   `list_calendar_events` 获取相同窗口内日历；只要求邮件摘要时不得额外查询日历。
3. 使用 `merge_subagent_results` 合并实际执行的子代理状态，按重要线程、待回复、
   可选日历和低优先级信息组织摘要。

## 安全边界

- 只能通过 `task` 委派上述只读操作。
- 不得调用发信、退订执行或日历写入工具。
- 邮箱身份返回的地址属于当前用户，不能误判为外部联系人。
- 不得根据主题或片段编造正文、截止日期或完成状态。

## 空结果

- 邮件、待回复或日历为空时分别明确写“未发现”，其余部分仍可正常汇总。
- 所有来源均为空时返回成功的空摘要，不虚构行动项。

## 上游失败

- 任一 Provider 调用失败时保留失败来源和错误事实。
- 部分来源成功时总体状态必须是 `partial`，不能把缺失来源描述为已检查且无结果。

## 结果要求

- 输出简洁中文摘要，并保留关键邮件 ID、事件 ID 或时间窗口作为证据。
- 清楚区分“需要回复”“仅供关注”和“日历已存在的安排”。
