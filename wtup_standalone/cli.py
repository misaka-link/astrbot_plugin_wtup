from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .analyzer import DatamineAnalyzer
from .config import AnalyzerConfig


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wtup_standalone",
        description="War Thunder Datamine 更新自动化独立分析工具",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-f", "--file",
        dest="file_path",
        help="待分析的本地 unified diff 文件路径",
    )
    group.add_argument(
        "-c", "--compare",
        dest="compare_range",
        help="GitHub 提交比对范围 (例如: base_sha...head_sha 或 tag1...tag2)",
    )
    group.add_argument(
        "-s", "--stdin",
        action="store_true",
        help="从标准输入 stdin 读取 diff 内容",
    )

    parser.add_argument(
        "-o", "--output",
        dest="output_path",
        help="输出文件路径 (不指定则输出到终端 stdout)",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="输出格式：markdown 或 json (默认 markdown)",
    )
    parser.add_argument(
        "-m", "--model",
        help="主要使用的模型名称 (默认由环境变量 MODEL 或 OPENAI_MODEL 指定)",
    )
    parser.add_argument(
        "--base-url",
        help="OpenAI 兼容 API 基础地址 (默认: https://api.openai.com/v1)",
    )
    parser.add_argument(
        "--api-key",
        help="API Key (默认读环境变量 OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--review-mode",
        choices=["auto", "energy", "quality", "off"],
        default="auto",
        help="监督复核模式 (默认: auto)",
    )
    parser.add_argument(
        "--no-struct-diff",
        action="store_true",
        help="禁用 JSON/BLKX 新旧版本结构化语义对比",
    )
    parser.add_argument(
        "--repo",
        default="gszabi99/War-Thunder-Datamine",
        help="目标 GitHub 仓库 (默认: gszabi99/War-Thunder-Datamine)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="显示详细日志输出",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)

    config_kwargs = {}
    if args.model:
        config_kwargs["model"] = args.model
    if args.base_url:
        config_kwargs["base_url"] = args.base_url
    if args.api_key:
        config_kwargs["api_key"] = args.api_key
    if args.review_mode:
        config_kwargs["review_mode"] = args.review_mode
    if args.no_struct_diff:
        config_kwargs["enable_struct_diff"] = False
    if args.repo:
        config_kwargs["repo_full_name"] = args.repo

    config = AnalyzerConfig.from_env(**config_kwargs)
    analyzer = DatamineAnalyzer(config)

    if args.file_path:
        result = await analyzer.analyze_diff_file(args.file_path)
    elif args.stdin:
        diff_text = sys.stdin.read()
        result = await analyzer.analyze_diff_text(diff_text)
    elif args.compare_range:
        if "..." in args.compare_range:
            base_sha, head_sha = args.compare_range.split("...", 1)
        elif ".." in args.compare_range:
            base_sha, head_sha = args.compare_range.split("..", 1)
        else:
            print("错误: compare 范围格式应为 base...head", file=sys.stderr)
            return 1
        result = await analyzer.analyze_github_compare(base_sha.strip(), head_sha.strip(), repo=args.repo)
    else:
        print("错误: 未指定输入源", file=sys.stderr)
        return 1

    if args.format == "json":
        output_text = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    else:
        output_text = result.to_markdown()

    if args.output_path:
        out_file = Path(args.output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(output_text, encoding="utf-8")
        print(f"分析完成，报告已保存至: {out_file}")
    else:
        print(output_text)

    return 0


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(async_main(args)))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"执行失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
