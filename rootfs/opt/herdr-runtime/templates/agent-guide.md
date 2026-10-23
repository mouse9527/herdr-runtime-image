# herdr runtime：全局说明

你运行在 `herdr-runtime-image` 容器里（非 root，用户 `agent`，home 是持久卷）。

- 工作区：`~/workspaces/assistant/`。开工前读它的 `README.md`（本 Pod 的规则），以及 skill
  `workspace`、`herdr-workers`（`~/.agents/skills/<名>/SKILL.md`，与 `~/.claude/skills/` 相同；来源是公开仓库
  mouse9527/skills，由 supervisor 每 5 分钟同步，不要在这里改）。
- 你如果是某个 topic 的 lead：先读 `~/workspaces/assistant/topics/<topic>/TOPIC.md` 和 `NOTES.md`；任务从
  `topics/<topic>/tasks/<任务号>/TASK.md` 来，进度写同目录 `REPORT.md`，首行 `STATUS:`。可能有多个任务同时在飞。
- 不能直接问用户：用 `task-ask --task <任务号> "问题"` 或报告首行 `STATUS: blocked（需要用户决定：…）`。
- 派 worker 用 herdr（session `assistant`），yolo 启动、显式 `--model`、只信产物。
- 不把任何凭据写进文件、日志、提交、PR。
