# wtup_standalone (War Thunder Datamine 独立分析核心)

从 `astrbot_plugin_wtup` 项目中完全解耦抽离的 **War Thunder Datamine 更新自动化分析引擎**。

纯 Python 3.10+ 标准库实现，**零必需第三方外部依赖**，可单机运行、作为 CLI 命令行工具或嵌入到任意 Python 项目中使用。

---

## 核心特性

- 🎯 **War Thunder 专属拆包分析能力**：内置针对《战争雷霆》载具、挂载、雷达、飞行模型、分房权重（BR）、经济系统、本地化文本等专门调优的提示词与分析逻辑。
- ⚡ **零外部依赖与通用模型兼容**：基于标准库 `urllib` 实现 OpenAI 兼容协议（支持 DeepSeek、OpenAI、SiliconFlow、OpenRouter、本地 Ollama/vLLM 等），支持流式 SSE 汇聚与多模型自动容灾降级。
- 🧩 **高精度结构化对比（Struct Diff）**：针对 `.blkx` / JSON 等数据文件提供字段级语义对比（如 `[修改] $.unit.br: 5.7 -> 6.0`），遇到二进制打包格式可结合 `wt_ext_cli` 自动解包比对。
- 📏 **智能切片与 Token 预算控制**：根据模型上下文窗口自动进行文件关联性分组与平衡切片，单文件超大保护与多语言大 CSV 自动子节点细分。
- 🛠 **自动化 JSON 修复与归一化**：自动过滤 Markdown 代码块外废话，对截断或非标准 JSON 启动多轮修复模型，字段严格标准化清洗。
- 🔄 **分片报告程序化合并与复核**：多分片结果去重整合、跨文件共性合并、点名册覆盖率检查（Coverage Enforcement）以及可选的监督模型质检复核（Energy/Quality 档位）。
- 📝 **多格式结果导出**：一键生成结构化数据字典（`to_dict()`）或美观排版的 Markdown 文本报告（`to_markdown()`）。

---

## 快速上手

### 1. 环境变量配置 (可选)

可预先导出 OpenAI 兼容 API 相关的环境变量：

```bash
export OPENAI_API_KEY="sk-xxxxxxxxxxxxxxxxxxxxxxxx"
export OPENAI_BASE_URL="https://api.deepseek.com/v1"
export OPENAI_MODEL="deepseek-chat"
```

### 2. 命令行 (CLI) 使用

```bash
# 1. 分析本地 diff 文件并直接输出 Markdown 报告到终端
python3 -m wtup_standalone -f update.diff

# 2. 从标准输入读取 diff (如管道接收 git diff)
git diff HEAD~1 | python3 -m wtup_standalone -s

# 3. 指定模型、API 地址并导出为 JSON 格式文件
python3 -m wtup_standalone -f update.diff \
    --model gpt-4o \
    --base-url https://api.openai.com/v1 \
    --api-key sk-xxxx \
    --format json \
    -o result.json

# 4. 直接比对 GitHub 仓库提交范围 (自动获取 compare 与 diff)
python3 -m wtup_standalone -c 2.56.0.38...2.56.0.39 -o report.md
```

命令行参数说明：

| 参数 | 说明 |
| :--- | :--- |
| `-f, --file` | 本地 unified diff 补丁文件路径 |
| `-c, --compare` | GitHub 提交范围比对 (如 `base...head`) |
| `-s, --stdin` | 从标准输入读取 diff 文本 |
| `-o, --output` | 输出文件路径 (默认输出到 stdout) |
| `--format` | 输出格式：`markdown` (默认) 或 `json` |
| `-m, --model` | 模型名称 (覆盖配置) |
| `--base-url` | API 基础地址 (默认 `https://api.openai.com/v1`) |
| `--api-key` | 模型 API 密钥 |
| `--review-mode` | 监督复核档位：`auto` (默认), `energy`, `quality`, `off` |
| `--no-struct-diff` | 禁用新旧版本结构化语义对比 |
| `-v, --verbose` | 输出详细运行日志 |

---

## Python API 调用示例

```python
import asyncio
from wtup_standalone import DatamineAnalyzer, AnalyzerConfig

async def main():
    # 1. 初始化配置
    config = AnalyzerConfig(
        api_key="sk-...",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        review_mode="auto",
    )

    # 2. 创建分析器实例
    analyzer = DatamineAnalyzer(config)

    # 3. 分析本地 diff 文件
    diff_text = """
    diff --git a/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx b/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx
    --- a/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx
    +++ b/aces.vromfs.bin_u/gamedata/weapons/bomb.blkx
    @@ -1,3 +1,4 @@
    +mass: 500kg
    """
    result = await analyzer.analyze_diff_text(diff_text)

    # 4. 获取报告
    print("报告标题:", result.report_title)
    print("重要程度:", result.importance)
    print("标签:", result.tags)
    print("Token 消耗:", result.token_usage)

    # 输出排版好的 Markdown 文本
    print(result.to_markdown())

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 模块结构

```text
wtup_standalone/
├── __init__.py           # 公共导出接口
├── __main__.py          # 模块 CLI 入口 (python3 -m wtup_standalone)
├── cli.py               # 命令行解析与主控
├── analyzer.py          # 高阶 DatamineAnalyzer 分析器门面
├── config.py            # 独立配置对象 AnalyzerConfig 与默认提示词
├── models.py            # 数据模型 (TokenUsage, AnalysisResult, DiffSummary 等)
├── client.py            # 基于标准库的 OpenAI 兼容 HTTP/流式调用客户端
├── diff_collector.py    # Git diff 统一格式解析、关联分组与分片渲染
├── struct_diff.py       # JSON/BLKX 结构化字段级语义对比算法
├── diff_enricher.py     # 结构化对比上下文增强与二进制解包调度
├── ext_cli.py           # War Thunder 官方解包器 wt_ext_cli 二进制管理
├── tokens.py            # 输入 token 估算与动态切分算法
├── prompts.py           # 针对 War Thunder 优化的全套系统提示词
├── normalize.py         # 模型输出抽取、清洗、去重与 JSON 归一化
├── repair.py            # JSON 修复请求链路
├── merge.py             # 多分片分析合并与去重机制
├── review.py            # 监督/质检模型复核流水线
├── coverage.py          # 变更覆盖点名册与覆盖率强制保障
├── csv_subdivide.py     # 超大多语言 localization CSV 智能细分
├── fallback.py          # 全链路降级兜底报告生成
├── errors.py            # 错误分类与智能识别
├── analysis_cache.py    # 磁盘分析结果哈希缓存
├── github_client.py     # GitHub API 客户端 (标准库实现)
├── github_cache.py      # GitHub compare/diff 磁盘缓存
└── tests/               # 独立完整单元测试套件
```

---

## 运行测试

在项目根目录下执行：

```bash
python3 -m unittest discover wtup_standalone/tests
```
---

## WebUI 界面与 REST API 服务

`wtup_standalone` 内置了现代化的交互式 Web 控制台与完整的 REST API 服务。

### 1. 本地启动 WebUI

```bash
# 启动 WebUI 服务 (默认端口 8080)
python3 -m wtup_standalone.web.server

# 或指定端口与数据目录
PORT=8080 DATA_DIR=data python3 -m wtup_standalone.web.server
```

启动后在浏览器打开 `http://127.0.0.1:8080` 即可访问监控大屏。

### 2. WebUI 功能清单

- 📊 **监控大屏 (Dashboard)**：实时展示最新生成的分析报告，支持查看版本范围、标签徽章、重要度、更新详情层级、AI 智能战术研判与 Token 统计；支持一键生成/预览全长高清报告图片，支持直接下载 Markdown 文档。
- 🔍 **GitHub 最新提交 (Commits)**：直连 GitHub 仓库读取最新 Commit 列表，展示提交者、时间、SHA 与提交说明；支持一键“以此提交为基线发起比对”。
- 🗂 **历史报告 (History)**：按时间倒序归档所有历史分析结果，支持随时回溯查看或下载任意历史更新的长图与 JSON 数据。
- ⚡ **手动触发 (Manual Trigger)**：支持指定范围比对（如 `2.56.0.38...2.56.0.39`）或直接粘贴/上传本地 `.diff` 补丁进行分析，前端带实时进度条与执行日志流。
- ⚙️ **系统设置 (Settings)**：
  - **自定义 OpenAI 供应商**：自定义 API Base URL 与 Key，提供**“动态拉取模型列表”**按钮一键获取网关所有可用模型。
  - **多角色模型分配**：分别配置主分析模型、备用容灾模型、总结模型与监督复核模型。
  - **思考强度设置 (Thinking & Reasoning Effort)**：支持配置思考强度（`off` / `low` / `medium` / `high` / `custom`）、思考 Token 预算上限（`thinking_budget_tokens`）与采样温度（`temperature`），完美适配 DeepSeek-R1、o1、o3-mini 等推理模型。
  - **定时任务与周期**：自由设置自动轮询周期（分钟）及一键开启/暂停后台自动调度器。

### 3. 查询与控制 REST API 规范

| 接口方法与路径 | 功能说明 | 返回格式示例 |
| :--- | :--- | :--- |
| `GET /api/status` | 查询系统调度器状态、当前任务进度、最新检查时间 | `{"scheduler_running": true, "current_task": {...}}` |
| `GET /api/latest` | **查询最新一次成功生成的分析结果与图片地址** | `{"report": {...}, "image_url": "/api/report-image/xxx"}` |
| `GET /api/history` | 获取历史报告列表索引 | `{"items": [...], "total": 12}` |
| `GET /api/history/{id}` | 获取特定报告完整详情 | `{"report": {...}, "html_url": "..."}` |
| `GET /api/report-image/{id}` | 获取报告渲染的高清 PNG 图片 | `image/png` 二进制数据 |
| `GET /api/github/commits` | 获取 GitHub 目标仓库最新提交记录 | `{"commits": [...], "last_checked_commit": "..."}` |
| `POST /api/models/fetch` | 向供应商请求可用模型列表 | 请求: `{"base_url": "..."}` -> 返回: `{"models": [...]}` |
| `GET /api/config` | 获取系统当前配置（API Key 自动脱敏） | `{"model": "deepseek-chat", "openai_api_key_masked": "..."}` |
| `POST /api/config` | 修改并保存系统配置 | 提交欲更新的配置项字典 |
| `POST /api/analyze/trigger` | 手动触发更新检查与分析 | 请求: `{"mode": "latest"|"compare"|"diff", ...}` |
| `POST /api/scheduler/toggle` | 开启或暂停定时自动轮询 | 请求: `{"enabled": true/false}` |

---

## Docker 容器化部署

提供了生产级 `Dockerfile` 与 `docker-compose.yml`，容器内预装了 **Git 客户端环境**、中文字体与 Chromium 无头截图引擎。

### 使用 Docker Compose 一键启动

```bash
cd wtup_standalone

# 启动容器
docker-compose up -d

# 查看运行日志
docker-compose logs -f
```

服务将运行在 `http://宿主机IP:1883` (外部映射端口 1883)，数据持久化保存在宿主机的 `./data` 目录中。
