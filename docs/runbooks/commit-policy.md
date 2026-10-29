# Commit Policy

状态: Active

## 适用范围

本文档定义 `arb` 仓库中，agent 在什么情况下应当提交、什么情况下不应当提交。

`arb` 是 crypto / Polymarket 研究产品仓库。任务状态以 Multica / GitHub 侧的 `ARB-*` ticket 与 Issue 为准，不为普通任务强制维护 `docs/exec-plans/`。

## Purpose

agent 工作不应停在“本地代码已经改完”。

本页用于区分：

- 仅本地完成
- `commit-ready`
- isolated worktree 的 `merge-ready`
- 应当形成实际 Git commit

## Core Rule

worker 回报 `DONE` 不等于应当立刻提交。

默认不自动提交；用户明确要求时才提交。以下条件需同时满足，结果才算 `commit-ready`：

1. required review 已通过，或被明确延期
2. 要求的验证已有 fresh evidence（可观察命令输出或 evidence path）
3. 要求的权威文档更新已同步
4. 当前 `ARB-*` ticket / GitHub Issue / handoff 状态已同步
5. diff 已检查，且不含无关 dirt

对 isolated worktree，`commit-ready` 只说明当前分支内部已经闭合。合回目标分支前，还必须确认 base / target branch freshness；通过后才称为 `merge-ready`。

上游通用 implement skill 的 commit 指令不覆盖本仓库规则；是否提交由用户要求和本页决定。

## Commit Levels

### Task-Level Commit

适用于：

- 单个 `ARB-*` ticket 已形成清晰、可独立理解的增量
- diff 可独立 review 和回滚
- 下一个 ticket 不需要立即重写或 squash

### Batch-Level Commit

适用于：

- 多个 ticket 被有意作为同一批次完成
- 它们共享同一个用户-facing 或架构-facing 结果
- 拆成多个 commit 反而会让历史更难读

### Spec-Level Commit

适用于：

- 整份 spec 已完成
- final review 与 global verification 已完成
- tracker 状态已关闭或 handoff 已写清

## Do Not Commit Yet

在以下情况下默认不提交：

- review findings 仍未清空
- 某条 required review lane 仍是 `pending`、`blocked` 或 `hold_for_confirmation`
- 验证缺失、过期或不完整
- 只写了验证结论，但没有留下 evidence path 或可观察命令输出
- 文档尚未同步
- ticket / Issue 状态不准确
- 工作树混有无法安全分离的无关改动
- 用户未要求提交，且仓库规则要求先确认

## Commit Message Contract

默认使用 Conventional Commits：

```text
type(scope): summary
```

或：

```text
type: summary
```

默认允许的 `type`：

- `feat`
- `fix`
- `docs`
- `refactor`
- `test`
- `chore`
- `build`
- `ci`
- `perf`
- `revert`

默认规则：

- `scope` 可选；当子系统、目录或文档面足够明确时优先补上（例如 `web`、`research`、`scaffold`、`docs`）
- `summary` 使用简短祈使式短语；除非专有名词或缩写，默认从小写动词短语开始
- `summary` 不以句号结尾
- ticket 标识可写入正文或括号，例如：`feat(web): add SSR sample chart (ARB-2)`
- 不要把旧 plan ID 延伸为新工作的身份系统；以当前 `ARB-*` ticket / Issue 为准

## Staging Hygiene

- 只 stage 当前任务 ownership 内的路径
- 禁止无审查地 `git add .`
- 不把 runtime、cache、secret、交易密钥、无关本地文件带入提交
- 所有交易密钥与交易配置只放 NAS secret，不放仓库或前端环境变量

## When No Commit Is Made

如果决定暂不提交，仍必须留下清晰状态：

- 在当前 `ARB-*` ticket / Issue / handoff 记录实际状态、未提交原因、验证证据和下一次入口
- 明确当前结果是“review 通过但暂未提交”，还是“仍在本地工作区中”
- 不要为记录状态另行创建平行 todo 或第二套执行计划系统

## Minimum Output

在决定是否提交前，controller 至少应能回答：

- 当前要提交的是哪个有边界的变更单元
- 它对应哪个 `ARB-*` ticket / Issue
- 哪些 review lanes 已闭合，哪些仍开放
- 哪些验证证据支持这次提交
- verification record 或 evidence artifact 在哪里
- 文档与 ticket 状态是否已经同步

## 相关链接

- `AGENTS.md`
- `docs/index.md`
- `README.md`
