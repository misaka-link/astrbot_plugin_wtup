# astrbot_plugin_wtup

AstrBot 的 War Thunder Datamine 更新监控插件。

插件固定监控：

```text
https://github.com/gszabi99/War-Thunder-Datamine
branch: master
mode: commit
```

发现新 commit 后，插件会获取 GitHub compare 数据，把 commit、文件列表和 patch 交给 AstrBot 已配置的大模型分析，然后使用 `templates/help_miku.html` 渲染图片并主动推送到配置的群聊列表。

## 配置

后台配置项：

- `provider_id`：模型 Provider ID，留空使用默认模型。
- `timeout_seconds`：单次模型请求的大模型分析超时时间，单位秒。
- `model_concurrency`：模型请求并发数，默认 1 表示串行。
- `target_groups`：推送群聊列表，每项一个群号或 `unified_msg_origin`。填写纯群号时会通过 OneBot `send_group_msg` 发送。
- `monitor_interval_minutes`：监控频率，默认 30 分钟。
- `analysis_prompt`：分析提示词。
- `enable_second_pass_analysis`：是否启用二次分析，默认关闭。
- `footer_note`：报告图片左下角文本，支持多行和简单 Markdown 链接，默认显示 `gszabi99/War-Thunder-Datamine` 仓库链接。
- `github_token`：GitHub Personal Access Token，可选。
- `max_files_per_report`：每次模型请求最多文件数，默认 0 表示不限制。
- `max_patch_chars`：每次模型请求最大 diff 字符数，默认 0 表示不限制。

`github_token` 获取位置：

```text
https://github.com/settings/tokens
```

公开仓库只需要只读能力。Fine-grained token 选择 Public repositories read-only；Classic token 可不勾选 scopes。留空也能使用匿名请求，但 GitHub API 限额较低。

## 命令

```text
/wtup_status
```

查看监控状态、最近 commit、检查间隔和限制配置。

```text
/wtup_bind
```

获取当前群聊的 `unified_msg_origin`。推送群聊列表也可以直接填写群号。

```text
/wtup_check
```

手动检查一次。首次运行只建立基线，不推送历史更新。

```text
/wtup_check 强制
```

强制分析最新一个 commit，用于测试图片渲染和模型分析，只回复当前会话，不群发。

```text
/wtup_check 强制全部
```

强制分析最新一个 commit，并推送到后台配置的全部 `target_groups`。仅 AstrBot 管理员可执行。

## 模型请求拆分规则

`max_files_per_report` 和 `max_patch_chars` 默认都是 `0`，表示不限制。

如果其中任意一个设置为大于 `0`，插件会按文件数和 diff 字符数拆分模型请求，并尽量把同目录、同后缀且文件名相近的改动放在同一个请求里。每次请求都会单独调用一次模型，所有分析结果会按分片顺序合并成一份报告，最终只生成一张图片并推送一次。

`model_concurrency` 控制同时进行的模型请求数量。默认 `1` 表示串行；设置为大于 `1` 时会并发分析多个分片，但不会按完成先后合并，最终仍按分片顺序整理报告。

如果某个模型请求失败，例如模型上下文超限、接口报错、超时或返回空内容，插件会把这个失败分片的文件数量减半后重新请求一次。重试仍然遵守 `model_concurrency` 限制；如果分片只有 1 个文件或拆半重试仍失败，才会生成需复核的兜底分析。

如果模型返回内容不是有效 JSON，插件会强制再发起一次模型请求，要求模型基于原始输出修复为严格 JSON。这个 JSON 修复请求不受 `enable_second_pass_analysis` 开关影响；如果修复仍失败，才会生成需复核的兜底分析。

`enable_second_pass_analysis` 默认关闭。关闭时，插件使用程序内置规则把多次模型分析结果直接合并为最终报告。

开启后，如果本次 diff 被拆成多次模型请求，插件会先按程序规则初步合并各分片结果，再额外请求一次模型对合并结果做二次整理。二次分析只基于已有分片分析结果，不重新读取原始 diff；它会尽量去重、合并相近条目并整理最终报告。该功能会增加一次模型调用和等待时间；如果二次分析失败或输出不是有效 JSON，插件会自动回退到程序初步合并结果继续推送。

如果开启了二次分析，但程序初步合并分片结果时发生异常，插件不会直接中断。本次检查会把各分片的分析 JSON、分片错误信息和原始模型输出文本交给二次分析模型生成最终报告；如果这一步仍然失败，才会生成需复核的兜底报告。

## 数据持久化

插件会在 AstrBot 插件数据目录中保存运行数据：

- `state.json`：保存最近检查 commit、最近一次生成任务 `last_generated_task`，以及最近一次群推送任务 `last_pushed_task`。
- `logs/`：保存每次最终文本报告。若报告标题是 `版本->版本` 格式，文件名会保存为 `旧版本_新版本.log`，例如 `2.56.0.38_2.56.0.39.log`；否则使用本地时间命名，例如 `2026年6月12日03：00：18.log`。
- `images/`：保存渲染后的报告图片。

## 首次运行

定时任务首次启动时会把当前最新 commit 记录为基线，不推送历史更新，避免刷屏。之后只有检测到新的 commit 才会分析和推送。
