---
name: bug-issue-triage
description: 查找构建失败、测试失败、缺陷、回归、事故、宕机和阻断类邮件；用户要求工程问题或 Bug 分诊时使用。
allowed-tools: task prepare_skill_workflow merge_subagent_results
---

# Bug 与工程问题分诊

## 适用条件

- 用户要求汇总 Bug、CI 失败、测试失败、生产事故、回归或阻断问题。
- 仅分析邮件证据，不操作外部工单系统。

## 输入规则

- `days` 默认 7，最小 1，最大 7。
- 查询限定收件箱，关键词覆盖 bug、defect、regression、build failed、tests failed、incident、outage、blocker。
- 首次搜索最多 100 条，随后读取所有匹配项正文。

## 执行流程

开始前必须调用 `prepare_skill_workflow`，不得由模型自行扩大关键词或时间范围。

1. 委派 `mailbox-reader` 调用 `search_emails` 执行工程问题查询。
2. 对每个匹配邮件 ID 委派调用 `get_email` 获取完整正文。
3. 使用 `merge_subagent_results` 聚合搜索与正文读取结果。
4. 按生产事故或宕机、阻断性回归、构建或测试失败、一般缺陷信息排序。

## 安全边界

- 只能通过 `task` 委派只读邮箱操作。
- 不得调用发信、日历、退订执行或修改邮箱状态的工具。
- 不得把普通讨论或历史转发误报为当前未解决事故。
- 不得编造工单状态、负责人、影响范围或修复时间。

## 空结果

- 没有搜索命中时明确返回“未发现近期 Bug 或工程故障邮件”。
- 不为凑数量展示与工程问题无关的邮件。

## 上游失败

- 搜索失败时返回 `failed`。
- 个别正文读取失败时返回 `partial`，保留可用邮件并列出失败邮件 ID。

## 结果要求

- 每项包含邮件 ID、来源、主题、问题证据、可确认的影响以及建议下一步。
- 结论必须区分“邮件明确说明”和“基于证据的待确认事项”。
