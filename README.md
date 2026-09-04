# astrbot_plugin_wtup · War Thunder Datamine 监控与分析系统

本项目为《战争雷霆》（War Thunder）拆包更新（[gszabi99/War-Thunder-Datamine](https://github.com/gszabi99/War-Thunder-Datamine)）自动化监控、大模型语义分析与高清长图推送系统。

项目已全面升级为**前后端彻底解耦架构**，分为独立的轻量级 AstrBot 插件客户端与高可用独立分析后端：

```text
[War Thunder Datamine 仓库 (GitHub)]
                  │ (定时监测 Commit / Compare / Diff)
                  ▼
   ┌───────────────────────────────────────────┐
   │ wtup_standalone (独立后端引擎 / Docker)   │
   │  ├─ 结构化语义 Diff (Struct Diff + wt_ext)│
   │  ├─ 大模型分片分析、修复、合并与复核      │
   │  ├─ HTML/CSS 报告渲染引擎 (Discord/Miku) │
   │  └─ FastAPI RESTful 接口 (端口 1883)      │
   └───────────────────────────────────────────┘
                  │ (HTTP API 轮询 / 长图下载)
                  ▼
   ┌───────────────────────────────────────────┐
   │ astrbot_plugin_wtup (轻量级 AstrBot 插件) │
   │  ├─ 零重型依赖、零大模型请求、无渲染负载  │
   │  ├─ 定时查询新报告并持久化已推送状态      │
   │  └─ 自动推送高清长图与摘要至 QQ / TG / DC │
   └───────────────────────────────────────────┘
```

---

## 目录结构

```text
astrbot_plugin_wtup/
├── astrbot_plugin_wtup/    # 【新插件】轻量级 AstrBot 插件端 (API 轮询与长图分发)
│   ├── main.py             # 插件核心逻辑
│   ├── metadata.yaml       # 插件元数据 (v1.0.0)
│   ├── _conf_schema.json   # 插件可视化配置项定义
│   ├── .gitignore          # 插件端忽略规则
│   └── README.md           # 插件端详细使用与配置文档
│
├── wtup_standalone/        # 【新后端】独立 Datamine 分析核心与 Web/Docker 服务端
│   ├── web/                # FastAPI Web 路由、异步调度器、渲染器与数据存储
│   │   ├── server.py       # REST API 服务端
│   │   ├── scheduler.py    # 定时抓取与分析调度器
│   │   └── renderer.py     # HTML 模板与长图渲染引擎
│   ├── analyzer.py         # LLM 分析核心
│   ├── diff_collector.py   # Diff 分片与估算
│   ├── struct_diff.py      # 结构化语义对比引擎
│   ├── ext_cli.py          # wt_ext_cli 二进制解包调用封装
│   ├── tests/              # 完整的后端单元测试套件
│   ├── Dockerfile          # Docker 镜像构建配置
│   ├── docker-compose.yml  # 一键部署编排文件
│   └── README.md           # 后端与 CLI 命令行详细文档
│
└── README.md               # 项目总体介绍（本文档）
```

---

## 快速开始

### 1. 部署后端 (`wtup_standalone`)

支持 Docker 容器一键部署或单机 CLI 运行：

```bash
cd wtup_standalone

# 使用 Docker Compose 一键启动服务
docker compose up -d --build

# 查看后端运行日志
docker logs -f wtup-datamine-monitor

# 验证 API 状态
curl http://127.0.0.1:1883/api/status
```

详细的环境变量配置、自定义模型提供商（DeepSeek、OpenAI、SiliconFlow 等）以及 CLI 命令行用法详见 [wtup_standalone/README.md](wtup_standalone/README.md)。

---

### 2. 安装 AstrBot 插件 (`astrbot_plugin_wtup`)

将 `astrbot_plugin_wtup` 目录放置在 AstrBot 的插件目录中：

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

在 AstrBot 管理面板中配置：
- **`api_base_url`**：后端 API 地址（如 `http://10.10.10.99:1883`）
- **`interval_minutes`**：轮询检查周期（默认 5 分钟）
- **`push_targets`**：推送目标群组或私聊列表
- **`render_template`**：渲染风格（`discord` 黑暗风格或 `miku` 初音风格）

详细配置说明详见 [astrbot_plugin_wtup/README.md](astrbot_plugin_wtup/README.md)。

---
