# astrbot_plugin_wtup (API 推送客户端版)

AstrBot 的 War Thunder Datamine 更新自动推送客户端。

与传统的本地重型分析插件不同，本版本为**纯轻量级客户端**：
- **零分析开销**：不包含任何大模型请求、Git 仓库拉取、二进制 BLK 解包或 Playwright 渲染依赖。
- **定时查询 API**：默认每隔 **5 分钟** 向独立的 `wtup_standalone`（本地或远程 Docker 容器）请求最新更新报告。
- **自动分发长图**：当后台生成新报告后，自动下载对应模板（Discord 黑暗风格或 Miku 风格）的高清长图并推送到配置的 QQ / Telegram 群聊列表中。

---

## 配置说明

在 AstrBot 管理后台中配置插件参数：

| 配置项 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `api_base_url` | 字符串 | `http://10.10.10.99:1883` | WTUP 后台独立分析服务或 Docker 容器的访问地址 (外部映射端口 1883) |
| `interval_minutes` | 整数 | `5` | **API 定时查询周期 (默认 5 分钟)** |
| `push_targets` | 列表 | `[]` | 接收更新推送的群聊或用户，如 `12345678` 或 `qq:group:12345678` |
| `render_template` | 字符串 | `discord` | 图片排版风格：`discord` (黑暗风格，默认) 或 `miku` (初音风格) |
| `enable_push_image` | 布尔 | `true` | 是否从后台自动获取高清渲染长图并推送 |
| `enable_push_text` | 布尔 | `true` | 是否在图片上方附带简要版本号与概述说明 |
| `admin_targets` | 列表 | `[]` | 允许执行 `/wtup_check` 的管理员 QQ 号列表 |

---

## 机器人指令

- `/wtup_check`：向后台 API 立即发起一次检查；若有新分析报告则立即向当前聊天推送长图。
- `/wtup_status`：查看推送客户端当前的运行状态（后台 API 地址、轮询周期、上次检查时间、已推送报告 ID）。

---

## 安装方式

将本文件夹直接复制到 AstrBot 的插件目录：
```text
AstrBot/
└── data/
    └── plugins/
        └── astrbot_plugin_wtup/
            ├── main.py
            ├── _conf_schema.json
            ├── metadata.yaml
            └── README.md
```
在 AstrBot 控制面板重新加载插件即可。
