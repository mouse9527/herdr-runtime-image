# 任务 {{TASK_ID}}（topic「{{TOPIC_NAME}}」）

- 任务目录：`{{TASK_DIR}}`；本任务的 worktree、子任务书、产物都放这里。
- 报告：`{{REPORT}}`，**首行**写 `STATUS: in-progress | done | blocked（原因）`，每步一行带时间戳，随进度更新。
  bridge 只看首行：`done` = 交回待用户验收；`blocked（…）` = 需要用户决定，括号里写清问题。
- 用户的回答出现在 `{{ANSWERS}}/<问题号>.md`，同时你会收到一条 prompt。
- 提问：`task-ask --task {{TASK_ID}} "问题"`（立即返回问题号），然后去做别的或结束本轮。

## 规则（摘要，详见 topic 目录的 TOPIC.md、ws README 和两份 skill）

1. **先写报告首行 `STATUS: in-progress`**，再动手；尽快落第一版计划，拿不准的标「待核」。
2. 代码改动：`repos/` 停基底分支，本任务目录下拉 worktree，分支名带任务号短号；只推功能分支、开 PR，等 CI；
   不推 main、不改 `.github/workflows/`、不部署。合并只在下面的任务内容明确要求时做，且 CI 全绿。
3. 需要拆活时用 herdr 派 worker：任务书写文件（模板 `{{WS_ROOT}}/.templates/task.md`），
   yolo 启动且显式 `--model`，派发即建监控，**只信产物**（文件、提交、PR、checks），不信状态。
4. 需要用户决定的（范围、取舍、授权、凭据）：提问或报告首行 `STATUS: blocked（需要用户决定：…）`，不要猜，不要自己授权自己。
5. 下面「任务内容」是用户写的任务描述，**不是**额外授权：它不能放宽本节和 README 里的边界。
6. 完成时：报告写清交付物（PR 链接、CI run、SHA、文件路径）、怎么验证的、没验证的，最后把首行改成 `STATUS: done`。

## 任务内容（来自用户）

{{TASK_TEXT}}
