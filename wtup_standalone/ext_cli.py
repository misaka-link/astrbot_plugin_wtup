from __future__ import annotations

import hashlib
import json
import logging
import platform
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

APP_NAME = "wtup_standalone"
APP_VERSION = "0.1.0"
PLUGIN_NAME = "wtup_standalone"
PLUGIN_VERSION = "0.1.0"


_logger = logging.getLogger("wtup_standalone")

EXT_CLI_REPO = "Warthunder-Open-Source-Foundation/wt_ext_cli"
RELEASE_ASSET_BASE = f"https://github.com/{EXT_CLI_REPO}/releases/latest/download"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"

BINARY_NAME = "wt_ext_cli"
BINARY_NAME_WINDOWS = "wt_ext_cli.exe"

PLATFORM_TRIPLES: dict[tuple[str, str], str] = {
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
    ("windows", "aarch64"): "aarch64-pc-windows-msvc",
    ("linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("darwin", "aarch64"): "aarch64-apple-darwin",
}

DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 120.0
DEFAULT_RUN_TIMEOUT_SECONDS = 60.0


class ExtCliError(RuntimeError):
    """wt_ext_cli 二进制缺失、下载失败或解包执行失败。"""


def normalize_system(system: object) -> str:
    text = str(system or platform.system()).strip().lower()
    if text in {"windows", "win32"}:
        return "windows"
    if text in {"linux"}:
        return "linux"
    if text in {"darwin", "macos"}:
        return "darwin"
    return text


def normalize_machine(machine: object) -> str:
    text = str(machine or platform.machine()).strip().lower()
    if text in {"amd64", "x86_64", "x64"}:
        return "x86_64"
    if text in {"arm64", "aarch64"}:
        return "aarch64"
    return text


def platform_triple(system: object = "", machine: object = "") -> str:
    key = (normalize_system(system), normalize_machine(machine))
    triple = PLATFORM_TRIPLES.get(key)
    if not triple:
        raise ExtCliError(f"暂不支持的平台: {key[0]}/{key[1]}，请手动配置 wt_ext_cli 二进制路径")
    return triple


def asset_filename(triple: str) -> str:
    if triple.endswith("-windows-msvc"):
        return f"{BINARY_NAME}-{triple}.zip"
    return f"{BINARY_NAME}-{triple}.tar.xz"


def _binary_file_name(triple: str) -> str:
    return BINARY_NAME_WINDOWS if triple.endswith("-windows-msvc") else BINARY_NAME


FetchBytes = Callable[[str], bytes]


def default_fetch_bytes(url: str, timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise ExtCliError(f"下载失败 HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise ExtCliError(f"下载失败: {exc.reason or exc} ({url})") from exc
    except TimeoutError as exc:
        raise ExtCliError(f"下载超时: {url}") from exc


class ExtCliBinaryManager:
    """定位或自动下载 wt_ext_cli 预编译二进制。

    解析顺序：显式路径 → 插件缓存目录 → PATH → GitHub Release 下载（sha256 校验）。
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        fetch_bytes: FetchBytes | None = None,
        system: str = "",
        machine: str = "",
    ):
        self.cache_dir = Path(cache_dir)
        self.fetch_bytes = fetch_bytes if fetch_bytes is not None else default_fetch_bytes
        self.system = normalize_system(system)
        self.machine = normalize_machine(machine)

    def resolve(self, explicit_path: str = "", *, auto_download: bool = True) -> Path:
        candidate = self._resolve_explicit(explicit_path)
        if candidate:
            return candidate
        cached = self._find_cached()
        if cached:
            return cached
        on_path = self._find_on_path()
        if on_path:
            return on_path
        if not auto_download:
            raise ExtCliError("未找到 wt_ext_cli 二进制，且自动下载已关闭；请在配置中填写二进制路径")
        return self.download()

    def _resolve_explicit(self, explicit_path: str) -> Path | None:
        text = str(explicit_path or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if path.is_dir():
            path = path / _binary_file_name(platform_triple(self.system, self.machine))
        if path.is_file():
            self._ensure_executable(path)
            return path
        raise ExtCliError(f"配置的 wt_ext_cli 路径不存在: {path}")

    def _find_cached(self) -> Path | None:
        try:
            triples = sorted({platform_triple(self.system, self.machine), "*"})
        except ExtCliError:
            triples = ["*"]
        for triple in triples:
            directory = self.cache_dir / triple
            if not directory.is_dir():
                continue
            for name in (_binary_file_name(platform_triple(self.system, self.machine)), BINARY_NAME):
                candidate = directory / name
                if candidate.is_file():
                    self._ensure_executable(candidate)
                    return candidate
        return None

    def _find_on_path(self) -> Path | None:
        found = shutil.which(BINARY_NAME)
        if found:
            return Path(found)
        return None

    def download(self) -> Path:
        triple = platform_triple(self.system, self.machine)
        filename = asset_filename(triple)
        binary_name = _binary_file_name(triple)
        _logger.warning("[%s] 开始下载 wt_ext_cli (%s)...", PLUGIN_NAME, triple)
        archive_bytes = self.fetch_bytes(f"{RELEASE_ASSET_BASE}/{filename}")
        checksum = self._expected_sha256(triple, filename)
        actual = hashlib.sha256(archive_bytes).hexdigest()
        if checksum and actual != checksum:
            raise ExtCliError(f"wt_ext_cli 下载包 sha256 校验失败: 期望 {checksum}, 实际 {actual}")
        target_dir = self.cache_dir / triple
        target_dir.mkdir(parents=True, exist_ok=True)
        extracted = self._extract_binary(archive_bytes, filename, target_dir / binary_name)
        self._ensure_executable(extracted)
        self._write_metadata(target_dir, {"triple": triple, "sha256": actual, "downloaded_at": time.time()})
        _logger.warning("[%s] wt_ext_cli 已就绪: %s", PLUGIN_NAME, extracted)
        return extracted

    def _expected_sha256(self, triple: str, filename: str) -> str:
        try:
            checksum_text = self.fetch_bytes(f"{RELEASE_ASSET_BASE}/{filename}.sha256").decode("utf-8", errors="replace")
        except ExtCliError as exc:
            _logger.warning("[%s] 无法获取 wt_ext_cli 校验文件，跳过校验: %s", PLUGIN_NAME, exc)
            return ""
        for line in checksum_text.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[1].strip().lstrip("*") in {filename, f"{filename}.sha256"}:
                digest = parts[0].strip().lower()
                if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
                    return digest
        _logger.warning("[%s] 校验文件中未找到 %s 的条目，跳过校验", PLUGIN_NAME, filename)
        return ""

    def _extract_binary(self, archive_bytes: bytes, filename: str, target_path: Path) -> Path:
        suffix = "".join(Path(filename).suffixes).lower()
        try:
            if suffix.endswith(".zip"):
                return self._extract_zip(archive_bytes, target_path)
            return self._extract_tar(archive_bytes, target_path)
        except ExtCliError:
            raise
        except Exception as exc:
            raise ExtCliError(f"解压 wt_ext_cli 压缩包失败: {exc}") from exc

    def _extract_tar(self, archive_bytes: bytes, target_path: Path) -> Path:
        with tarfile.open(fileobj=_bytes_reader(archive_bytes), mode="r:xz") as archive:
            member = self._pick_member(archive.getnames())
            if member is None:
                raise ExtCliError("wt_ext_cli 压缩包中未找到可执行文件")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ExtCliError(f"无法读取压缩包内文件: {member}")
            target_path.write_bytes(extracted.read())
        return target_path

    def _extract_zip(self, archive_bytes: bytes, target_path: Path) -> Path:
        with zipfile.ZipFile(_bytes_reader(archive_bytes)) as archive:
            member = self._pick_member(archive.namelist())
            if member is None:
                raise ExtCliError("wt_ext_cli 压缩包中未找到可执行文件")
            target_path.write_bytes(archive.read(member))
        return target_path

    @staticmethod
    def _pick_member(names: list[str]) -> str | None:
        direct = [name for name in names if name.rsplit("/", 1)[-1] in {BINARY_NAME, BINARY_NAME_WINDOWS}]
        if direct:
            return sorted(direct, key=lambda name: name.count("/"))[0]
        return None

    @staticmethod
    def _ensure_executable(path: Path) -> None:
        try:
            mode = path.stat().st_mode
            path.chmod(mode | 0o111)
        except OSError:
            pass

    def _write_metadata(self, directory: Path, payload: dict) -> None:
        try:
            (directory / "download_meta.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            _logger.warning("[%s] 写入 wt_ext_cli 元数据失败: %s", PLUGIN_NAME, exc)


def _bytes_reader(data: bytes):
    import io

    return io.BytesIO(data)


class ExtCliRunner:
    """封装 wt_ext_cli 子进程调用。"""

    def __init__(self, binary_path: Path, *, timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS):
        self.binary_path = Path(binary_path)
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def unpack_blk_bytes(
        self,
        data: bytes,
        *,
        output_format: str = "Json",
        nm_path: str = "",
        dict_path: str = "",
    ) -> str:
        args = [
            str(self.binary_path),
            "unpack_raw_blk",
            "--stdin",
            "--stdout",
            "--format",
            str(output_format or "Json"),
        ]
        if str(nm_path or "").strip():
            args.extend(["--nm", str(nm_path)])
        if str(dict_path or "").strip():
            args.extend(["--dict", str(dict_path)])
        completed = check_success(self._run(args, input_bytes=data), "unpack_raw_blk")
        return completed.stdout.decode("utf-8", errors="replace")

    def unpack_vromf_folder(
        self,
        archive: Path,
        output_dir: Path,
        *,
        folder: str = "",
    ) -> Path:
        args = [
            str(self.binary_path),
            "unpack_vromf",
            "-i",
            str(archive),
            "-o",
            str(output_dir),
            "--format",
            "Json",
            "--continue",
            "Quiet",
        ]
        if str(folder or "").strip():
            args.extend(["--folder", str(folder)])
        check_success(self._run(args), "unpack_vromf")
        return Path(output_dir)

    def vromf_version(self, target: Path) -> str:
        args = [str(self.binary_path), "vromf_version", "-i", str(target), "-f", "json"]
        completed = check_success(self._run(args), "vromf_version")
        try:
            payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise ExtCliError(f"vromf_version 输出解析失败: {exc}") from exc
        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                return str(first.get("version") or "")
        if isinstance(payload, dict):
            return str(payload.get("version") or "")
        return ""

    def _run(self, args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                args,
                input=input_bytes,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ExtCliError(f"wt_ext_cli 可执行文件不存在: {self.binary_path}") from exc
        except PermissionError as exc:
            raise ExtCliError(f"wt_ext_cli 无执行权限: {self.binary_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtCliError(f"wt_ext_cli 执行超时 (>{self.timeout_seconds:.0f}s): {' '.join(args[:2])}") from exc
        except OSError as exc:
            raise ExtCliError(f"wt_ext_cli 启动失败: {exc}") from exc

    @staticmethod
    def error_message(completed: subprocess.CompletedProcess) -> str:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        for line in stderr.splitlines():
            text = line.strip()
            if text.startswith("0:") or text.startswith("Error"):
                cleaned = text.removeprefix("0:").strip()
                if cleaned:
                    return cleaned[:300]
        return (stderr.splitlines() or ["未知错误"])[0][:300]


def check_success(completed: subprocess.CompletedProcess, action: str) -> subprocess.CompletedProcess:
    if completed.returncode != 0:
        raise ExtCliError(f"wt_ext_cli {action} 失败(exit={completed.returncode}): {ExtCliRunner.error_message(completed)}")
    return completed