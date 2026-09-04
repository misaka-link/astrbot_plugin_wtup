from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_logger = logging.getLogger("wtup_standalone.git")


class GitRepoManager:
    """基于本地/容器内 git CLI 的仓库与增量版本管理器。

    直接调用底层 git 命令：
    1. 使用 git ls-remote 秒级探测远程分支最新 Commit (0% 触发 GitHub API 速率限制)
    2. 支持本地浅克隆 (blobless/treeless) 缓存提交与元数据
    3. 支持通过 git diff base..head 毫秒级生成 100% 完整的未截断补丁
    4. 支持通过 git show ref:path 毫秒级读取任意历史提交下的特定文件
    """

    def __init__(self, repo_dir: str | Path | None = None, *, default_repo: str = "gszabi99/War-Thunder-Datamine") -> None:
        self.repo_dir = Path(repo_dir).resolve() if repo_dir else None
        self.default_repo = default_repo

    @staticmethod
    def is_git_available() -> bool:
        """检查环境中是否存在 git 可执行命令。"""
        return shutil.which("git") is not None

    def get_remote_head(self, repo: str | None = None, branch: str = "master") -> str | None:
        """通过 git ls-remote 秒级探测远程分支最新提交 SHA (不走 GitHub REST API，绝不被限流)。"""
        target_repo = repo or self.default_repo
        url = f"https://github.com/{target_repo}.git"
        cmd = ["git", "ls-remote", url, f"refs/heads/{branch}"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=12, check=True)
            output = res.stdout.strip()
            if output:
                # 输出格式: "<40-char-sha>\trefs/heads/<branch>"
                sha = output.split()[0].strip()
                if len(sha) == 40:
                    return sha
            # 尝试回退探测 HEAD
            cmd_head = ["git", "ls-remote", url, "HEAD"]
            res_head = subprocess.run(cmd_head, capture_output=True, text=True, timeout=12, check=True)
            head_out = res_head.stdout.strip()
            if head_out:
                sha = head_out.split()[0].strip()
                if len(sha) == 40:
                    return sha
        except Exception as exc:
            _logger.debug("git ls-remote 探测失败: %s", exc)
        return None

    def is_local_repo_ready(self) -> bool:
        """检查本地仓库目录是否已初始化。"""
        if not self.repo_dir or not self.repo_dir.exists():
            return False
        # 普通仓库含有 .git，bare 仓库根目录含有 HEAD 文件
        return (self.repo_dir / ".git").exists() or (self.repo_dir / "HEAD").exists()

    def sync_repo(self, repo: str | None = None, branch: str = "master", depth: int = 50) -> bool:
        """同步本地仓库：不存在则浅克隆 (bare)，存在则 git fetch 增量拉取。"""
        if not self.repo_dir:
            return False
        target_repo = repo or self.default_repo
        url = f"https://github.com/{target_repo}.git"

        try:
            if not self.is_local_repo_ready():
                _logger.info(f"本地 Git 仓库未就绪，正在浅克隆至 {self.repo_dir} (depth={depth})...")
                self.repo_dir.parent.mkdir(parents=True, exist_ok=True)
                cmd = [
                    "git", "clone", "--bare", f"--depth={depth}", "--filter=blob:none",
                    url, str(self.repo_dir)
                ]
                subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)
                _logger.info("本地 Git 仓库克隆完成")
                return True
            else:
                _logger.info(f"正在从远程拉取最新提交 git fetch origin +{branch}:{branch}...")
                cmd = ["git", "fetch", "origin", f"+{branch}:{branch}", f"--depth={depth}"]
                subprocess.run(cmd, cwd=str(self.repo_dir), capture_output=True, text=True, timeout=60, check=True)
                return True
        except Exception as exc:
            _logger.warning("同步本地 Git 仓库失败: %s", exc)
            return False

    def get_commits(self, branch: str = "master", limit: int = 25) -> list[dict[str, Any]]:
        """从本地 git log 毫秒级提取提交历史记录。"""
        if not self.is_local_repo_ready():
            return []
        cmd = [
            "git", "log", branch, f"-n{limit}",
            '--pretty=format:%H%x00%P%x00%an%x00%aI%x00%s'
        ]
        try:
            res = subprocess.run(cmd, cwd=str(self.repo_dir), capture_output=True, text=True, timeout=15, check=True)
            commits = []
            for line in res.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\x00")
                if len(parts) >= 5:
                    sha, parents_raw, author, date_str, msg = parts[0], parts[1], parts[2], parts[3], parts[4]
                    parents = [p.strip() for p in parents_raw.split() if p.strip()]
                    commits.append({
                        "sha": sha,
                        "author_name": author,
                        "authored_at": date_str,
                        "message": msg,
                        "html_url": f"https://github.com/{self.default_repo}/commit/{sha}",
                        "parents": parents,
                    })
            return commits
        except Exception as exc:
            _logger.warning("git log 获取提交失败: %s", exc)
            return []

    def get_diff(self, base_sha: str, head_sha: str) -> str:
        """从本地仓库直接生成两个提交之间的 100% 完整 diff (绝无 GitHub 截断)。"""
        if not self.is_local_repo_ready():
            return ""
        cmd = ["git", "diff", f"{base_sha}..{head_sha}"]
        try:
            res = subprocess.run(cmd, cwd=str(self.repo_dir), capture_output=True, text=True, timeout=30, check=True)
            return res.stdout
        except Exception as exc:
            _logger.warning(f"git diff {base_sha}..{head_sha} 失败: {exc}")
            return ""

    def get_file_content(self, ref: str, path: str) -> str:
        """从本地 git 对象数据库毫秒级读取特定提交下的文件完整文本 (git show ref:path)。"""
        if not self.is_local_repo_ready():
            return ""
        clean_p = path.lstrip("/")
        cmd = ["git", "show", f"{ref}:{clean_p}"]
        try:
            res = subprocess.run(cmd, cwd=str(self.repo_dir), capture_output=True, text=True, timeout=15, check=True)
            return res.stdout
        except Exception as exc:
            _logger.debug(f"git show {ref}:{clean_p} 失败: {exc}")
            return ""
