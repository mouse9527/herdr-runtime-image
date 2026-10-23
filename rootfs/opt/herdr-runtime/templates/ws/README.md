# assistant ws（herdr runtime Pod）

本文件由镜像 `herdr-runtime-image` 首次启动时放进 PVC，之后归本 Pod 所有，可以改；镜像升级不会覆盖它。
通用纪律见 skill `workspace` 和 `herdr-workers`（`~/.agents/skills/` / `~/.claude/skills/`，来源是公开仓库 mouse9527/skills）；
本文件只写本 Pod 的参数和与通用规则不同的地方（回答 workspace skill §2 的七问）。

## 1. 这是什么 ws

一个用户的后台执行站：用户从聊天界面派来的任务，由 task bridge 交给本 Pod 里 herdr 的 lead 执行。
lead 按 skill 纪律工作：任务书写文件、派 worker 用 herdr、合并只走 PR + CI、只信产物。

## 2. 锚点

**锚点是 bridge 派来的任务号**（`deleg_<32 位十六进制>`）。任务按 topic 归组：一个 topic 一个常驻 lead、一个 herdr tab，
目录 `topics/<topic_id>/`（`TOPIC.md` 说明、`NOTES.md` 结论），每个任务在 `topics/<topic_id>/tasks/<任务号>/`
（`TASK.md`、`REPORT.md`、`answers/`），这些都由 bridge 建。同一 topic 可同时有多个任务，lead 自己调度。
任务涉及某个 GitHub Issue 时，PR body 写 `Part of <owner>/<repo>#<n>`，并在报告里写出 Issue 链接。
没有任务号的工作不开工。

## 3. 权威来源表

| 信息 | 权威来源 | 本地做什么 |
|---|---|---|
| 这次要做什么 / 不做什么 / 止损 | `topics/<topic>/tasks/<任务号>/TASK.md`（bridge 写入，来自用户） | 只读 |
| 任务涉及的 Issue | GitHub Issue | 只读，`gh issue view` |
| 候选版本 | PR + commit SHA | 只读 |
| 验证结果 | PR checks（GitHub Actions） | 只读，`gh pr checks` |
| 任务进度 / 交回 | `topics/<topic>/tasks/<任务号>/REPORT.md` 首行 `STATUS:` | lead 写，bridge 读 |
| topic 的结论 / 交接 | `topics/<topic>/NOTES.md` | lead 收尾时写，新 lead 先读 |
| 用户的回答 | `topics/<topic>/tasks/<任务号>/answers/<问题号>.md` | 只读 |
| 当前在做什么 | `NOW.md` | 随手改，丢了无所谓 |

## 4. 仓库表

| repo | remote | 基底分支 | PR 目标 | CI | 说明 |
|---|---|---|---|---|---|
| 待定 | `https://github.com/<owner>/<repo>.git` | main | main | GitHub Actions | 用到时 `gh repo clone` 到 `repos/` |

- GitHub 身份是 Owner 在 Pod 里 `gh auth login --with-token` 存下的登录态（`~/.config/gh/hosts.yml`，在持久卷上）；
  部署也可以改用环境变量 `GH_TOKEN`。用 `gh` 和 HTTPS remote；`gh auth setup-git` 让 git 走同一身份。
- 本 Pod 没有 docker、kubectl、编译工具链；需要构建 / 测试的交给 PR 的 CI。

## 5. 权限边界

- 能做：clone、在 worktree 改代码、推**功能分支**、开 PR、看 CI、在 PR 里回复。
- 不做：直接推 main / 默认分支、改 `.github/workflows/`（凭据也没有这个权限）、删远端分支以外的远端资源、
  任何部署、碰别的用户的数据。合并 PR 只有在 TASK.md 明确授权时才做，且满足 workspace skill §5。
- 本 Pod 里所有 agent 都以跳过审批（yolo）方式运行：没有事前拦截，所以任务书必须写清能写哪些路径、
  禁止哪些操作；验收时核对实际改动范围。
- 凭据（`GH_TOKEN`、CLI 登录态）不写进任何文件、日志、PR、报告。

## 6. 验收判据

- 代码任务：PR 存在、checks 全绿、diff 只含任务范围内的改动；报告写 PR 链接、CI run、候选 SHA。
- 分析任务：报告文件落盘，首行 `STATUS: done`，结论标来源。
- `STATUS: done` 只代表 lead 认为做完，用户验收前不算完成。

## 7. 本 ws 追加的规则

- **不能直接问用户**。需要用户决定时：运行 `task-ask --task <任务号> "问题"`（立即返回问题号），
  或把报告首行写成 `STATUS: blocked（需要用户决定：…）`；然后结束本轮等待。回答会写到
  `answers/<问题号>.md`，并有一条 prompt 通知你。不要自己替用户定。
- herdr session 名是 `assistant`。pane 里的 herdr 命令自动指向它；在 SSH 里用 `herdr --session assistant …`。
- 用户可能通过 `herdr machine` 从自己的电脑接管某个 tab；看到有人在操作就停手，不往同一个 pane 打字。
- bridge 只读 `REPORT.md` 首行；`STATUS: done` 前确认交付物都已落盘、已推送。
