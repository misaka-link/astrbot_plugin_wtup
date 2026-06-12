# astrbot_plugin_wtup

AstrBot 的 War Thunder Datamine 更新监控插件。

插件固定监控：

```text
https://github.com/gszabi99/War-Thunder-Datamine
branch: master
mode: commit
```

感谢仓库 [gszabi99/War-Thunder-Datamine](https://github.com/gszabi99/War-Thunder-Datamine) 开源贡献的内容。

发现新 commit 后，插件会获取 GitHub compare 数据，把 commit、文件列表和 patch 交给 AstrBot 已配置的大模型分析，然后使用 `templates/help_miku.html` 渲染图片并主动推送到配置的群聊列表。

## 项目结构

主要代码按职责拆分：

- `main.py`：AstrBot 插件入口，负责生命周期、命令注册和调用检查服务。
- `wtup/service.py`：一次更新检查的主流程编排，包括拉取 GitHub 数据、生成报告、推送结果。
- `wtup/runtime.py`：运行时状态、错误日志、报告日志和推送附加文字格式化。
- `wtup/analysis/`：模型分析相关模块，包含提示词、模型请求、JSON 修复、失败重试、结果合并和结构标准化。
- `wtup/analyzer.py`：兼容导出层，保留旧的 `wtup.analyzer` 导入路径。
- `wtup/diff_collector.py`：GitHub compare/diff 数据整理和文件分片。
- `wtup/renderer.py`：报告 HTML、纯文本和图片渲染辅助。
- `wtup/notifier.py`：群消息、文字消息和日志文件推送。
- `templates/help_miku.html`：报告 HTML 骨架。
- `templates/help_miku.css`：报告样式文件，由 `renderer.py` 读取后注入 HTML。

## 配置

后台配置项：

- `provider_id`：分析模型 Provider ID，留空使用默认模型。
- `summary_provider_id`：总结模型 Provider ID，留空使用分析模型；分析模型也留空时使用默认模型。
- `timeout_seconds`：单次模型请求的大模型分析超时时间，单位秒。
- `model_concurrency`：模型请求并发数，默认 1 表示串行。
- `target_groups`：推送群聊列表，每项一个群号或 `unified_msg_origin`。填写纯群号时会通过 OneBot `send_group_msg` 发送。
- `analysis_file_groups`：发送分析文件的群列表，分析推送完成后会把本次 `.log` 文件发送到这些群。
- `monitor_interval_minutes`：监控频率，默认 30 分钟。
- `analysis_prompt`：分析提示词。
- `summary_prompt`：总结提示词，用于总结模型整理全部分片分析结果。
- `enable_summary_model`：是否启动总结模型，默认关闭。兼容旧配置项 `enable_second_pass_analysis`。
- `enable_push_append_text`：推送时是否启动追加文字内容推送，默认关闭。
- `push_append_text_template`：追加文字内容模板，支持 `{version_range}`、`{token_count}`、`{elapsed_minutes}`、`{analysis_model}`、`{summary_model}`。
- `footer_note`：报告图片左下角文本，支持多行和简单 Markdown 链接，默认显示 `gszabi99/War-Thunder-Datamine` 仓库链接。
- `github_token`：GitHub Personal Access Token，可选。
- `max_files_per_report`：每次模型请求最多文件数，默认 0 表示不限制。
- `max_input_tokens`：每次模型请求最大 token 输入，默认 0 表示不限制。
- `max_input_token_unit`：token 输入单位，可选 `K` 或 `M`。
- `max_retry_count`：最大重试次数，默认 2。每次重试都会把失败任务按文件边界拆成两半。

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

`max_files_per_report` 和 `max_input_tokens` 默认都是 `0`，表示不限制。

如果其中任意一个设置为大于 `0`，插件会先按文件数估算基础分片数量，再按完整模型输入估算 token。某一次请求超过最大 token 输入时，会在保证文件完整性的前提下继续拆分任务。拆分前仍会优先把同目录、同后缀且文件名相近的改动排在一起。每次请求都会单独调用一次分析模型，所有分析结果会按分片顺序合并成一份报告，最终只生成一张图片并推送一次。

插件只在文件边界拆分，不会拆开单个文件 patch。由于要保证文件完整性，实际每个分片的文件数或 token 估算值可能会围绕配置值浮动；如果某个文件本身超过 `max_input_tokens`，它会完整进入某个分片，不会被截断。

`max_input_token_unit` 控制 `max_input_tokens` 的单位：`K` 表示千 token，`M` 表示百万 token。

`model_concurrency` 控制同时进行的模型请求数量。默认 `1` 表示串行；设置为大于 `1` 时会并发分析多个分片，但不会按完成先后合并，最终仍按分片顺序整理报告。

如果某个模型请求失败，例如模型上下文超限、接口报错、超时或返回空内容，插件会把这个失败分片的文件数量减半后重试。`max_retry_count` 控制最多重试几轮，默认 2；每一轮仍然只按文件边界拆分，并遵守 `model_concurrency` 限制。如果分片只有 1 个文件或达到最大重试次数，才会生成需复核的兜底分析。

如果模型返回内容不是有效 JSON，插件会强制再发起一次模型请求，要求模型基于原始输出修复为严格 JSON。这个 JSON 修复请求不受“是否启动总结模型”开关影响；如果修复仍失败，才会生成需复核的兜底分析。

`enable_summary_model` 默认关闭。关闭时，插件使用程序内置规则把多次模型分析结果直接合并为最终报告。

开启后，如果本次 diff 被拆成多次模型请求，插件会先按程序规则初步合并各分片结果，再额外请求一次总结模型整理最终报告。总结模型只基于已有分片分析结果，不重新读取原始 diff；它会尽量去重、合并相近条目并整理最终报告。该功能会增加一次模型调用和等待时间；如果总结模型失败或输出不是有效 JSON，插件会自动回退到程序初步合并结果继续推送。

如果开启了总结模型，但程序初步合并分片结果时发生异常，插件不会直接中断。本次检查会把各分片的分析 JSON、分片错误信息和原始模型输出文本交给总结模型生成最终报告；如果这一步仍然失败，才会生成需复核的兜底报告。

总结模型、JSON 修复和失败拆分重试是三套独立机制：总结模型只负责最终整理；JSON 修复只负责把非 JSON 输出修复为严格 JSON；失败拆分重试只处理模型请求失败。

## 推送附加内容

开启 `enable_push_append_text` 后，报告图片推送完成后会追加一条文字消息。默认模板示例：

```text
{version_range} 分析完成
消耗token:{token_count}
耗时{elapsed_minutes}分钟
分析模型:{analysis_model}
总结模型:{summary_model}
```

配置 `analysis_file_groups` 后，分析推送完成会把本次 `.log` 文件发送到这些群。纯群号会优先通过 OneBot `upload_group_file` 上传；如果平台或目标不支持文件发送，会直接跳过，不再兜底发送日志文本。

## 数据持久化

插件会在 AstrBot 插件数据目录中保存运行数据：

- `state.json`：保存最近检查 commit、最近一次生成任务 `last_generated_task`，以及最近一次群推送任务 `last_pushed_task`。
- `logs/`：保存每次最终文本报告，不再记录图片文件路径。若报告标题是 `版本->版本` 格式，文件名会保存为 `旧版本_新版本.log`，例如 `2.56.0.38_2.56.0.39.log`；否则使用本地时间命名，例如 `2026年6月12日03：00：18.log`。
- `errors/`：保存模型请求、JSON 修复和总结模型相关错误日志，文件名精确到秒，例如 `2026年6月12日09时49分02秒.log`。
- `images/`：保存渲染后的报告图片。

## 首次运行

定时任务首次启动时会把当前最新 commit 记录为基线，不推送历史更新，避免刷屏。之后只有检测到新的 commit 才会分析和推送。
