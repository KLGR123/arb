# AGENTS.md

## Scope

本文件只保留稳定、仓库级、值得在每次对话中注入的协作规则。
专题事实、运行手册与产品说明进入 `docs/` 与 `README.md`。

## Project Overview

`arb` 是 crypto / Polymarket 研究产品的最小地基，当前提供：

- `apps/web/`：TanStack Start SSR 示例、shadcn/ui 与 Vela 假数据图
- `research/`：独立 Python 双引擎 smoke（VectorBT `fast` / NautilusTrader `realistic`）
- `tests/`：浏览器集成 smoke

当前没有业务策略、真实行情、下单服务或密钥配置。交易密钥只放 NAS secret。

## Repository Map

权威入口：

- `README.md`：本地启动与验证命令
- `docs/index.md`：文档入口
- `docs/runbooks/commit-policy.md`：何时可提交、Conventional Commits 与 staging 卫生

关键目录：

- `apps/web/`：TanStack Start + React + TypeScript
- `research/`：独立 Python 研究侧与 `uv.lock`
- `tests/`：Playwright 集成 smoke
- `docs/`：权威文档与运行手册

## Workflow

默认 workflow：

1. 收敛需求、边界、约束与验收标准；复用已接受的 `ARB-*` ticket / Issue。
2. 在明确 ownership 内实现；共享 integration surface、文档入口与提交决策由 controller 负责。
3. 宣称完成前检查计划对齐、质量风险、验证证据和文档同步。
4. **提交前必须阅读并遵守 `docs/runbooks/commit-policy.md`。** worker 完成不等于应当立刻提交；默认只有在 review、verification、文档同步和 ticket/Issue 状态同步都完成后，结果才算 `commit-ready`；isolated worktree 还必须通过 target branch freshness check 后，才算 `merge-ready`。

## Commit Convention

- 格式：`type(scope): summary` 或 `type: summary`
- 常用 `type`：`feat`、`fix`、`docs`、`refactor`、`test`、`chore`、`build`、`ci`、`perf`、`revert`
- ticket 可写入括号，例如：`feat(scaffold): ... (ARB-2)`
- 完整规则与 Do-Not-Commit 条件见 `docs/runbooks/commit-policy.md`

## Safety And Git

- 修改前检查当前 branch、worktree 和 dirty state；保留无关改动。
- 只 stage 当前任务 ownership 内的路径；禁止无审查 `git add .`。
- 不把 worker 或 subagent 的“完成”当成验证证据；检查 diff 并运行可观察验证。
- destructive command、凭据、发布和安全边界需要显式确认。
- 不主动扩大 scope；发现产品、架构或兼容性决策时回到用户。

## Definition Of Done

- 变更行为已反映在代码与受影响文档中
- 相关验证命令已执行并有证据
- `ARB-*` ticket / Issue 状态已同步，或留下明确 handoff
- 提交边界显式：要么变更 `commit-ready`，要么下一次入口已记录
