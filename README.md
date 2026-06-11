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
- `target_groups`：推送群聊列表，每项一个群号或 `unified_msg_origin`。填写纯群号时会通过 OneBot `send_group_msg` 发送。
- `monitor_interval_minutes`：监控频率，默认 30 分钟。
- `analysis_prompt`：分析提示词。
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

如果其中任意一个设置为大于 `0`，插件会按文件数和 diff 字符数拆分模型请求。每次请求都会单独调用一次模型，所有分析结果会按原始文件顺序合并成一份报告，最终只生成一张图片并推送一次。

## 首次运行

定时任务首次启动时会把当前最新 commit 记录为基线，不推送历史更新，避免刷屏。之后只有检测到新的 commit 才会分析和推送。
