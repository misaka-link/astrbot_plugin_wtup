# ==========================================
# WTUP Standalone Datamine Monitor & WebUI
# ==========================================
FROM python:3.11-slim

# 设置非交互环境与时区 (统一为中国标准北京时间 UTC+8)
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai \
    DATA_DIR=/app/data \
    PORT=8080

# 容器内配置高速镜像源并安装 tzdata、git、curl、chromium、中文字体与颜色表情字体 (fonts-noto-color-emoji)
RUN (sed -i 's@deb.debian.org@mirrors.aliyun.com@g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
     sed -i 's@deb.debian.org@mirrors.aliyun.com@g' /etc/apt/sources.list 2>/dev/null || true) \
    && apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    git \
    curl \
    xz-utils \
    ca-certificates \
    chromium \
    chromium-driver \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fonts-liberation \
    && ln -fs /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# 预装官方编译的 wt_ext_cli (Rust musl 静态链接版，单文件仅 1.5MB，秒级解包 BLK/VROMFS)
RUN curl -fsSL https://github.com/Warthunder-Open-Source-Foundation/wt_ext_cli/releases/latest/download/wt_ext_cli-x86_64-unknown-linux-musl.tar.xz \
    | tar -xJ -C /usr/local/bin/ wt_ext_cli \
    && chmod +x /usr/local/bin/wt_ext_cli || true

# 设置容器工作目录
WORKDIR /app

# 拷贝依赖定义与安装
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt || true

# 拷贝独立模块与 WebUI 代码
COPY . /app/wtup_standalone

# 创建持久化数据目录
RUN mkdir -p /app/data /app/data/reports /app/data/images /app/data/repo

# 声明持久化数据卷 (保存配置、缓存、历史报告、图片与本地 Git 仓库)
VOLUME ["/app/data"]

# 暴露 Web 服务端口
EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/status || exit 1

# 启动 WebUI 服务与后台定时调度器
CMD ["python3", "-m", "wtup_standalone.web.server"]
