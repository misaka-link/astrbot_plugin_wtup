#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 确保可以直接执行此脚本
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wtup_standalone import DatamineAnalyzer, AnalyzerConfig

SAMPLE_DIFF = """diff --git a/aces.vromfs.bin_u/gamedata/flightmodels/jh_7a.blkx b/aces.vromfs.bin_u/gamedata/flightmodels/jh_7a.blkx
index 1000000..2000000 100644
--- a/aces.vromfs.bin_u/gamedata/flightmodels/jh_7a.blkx
+++ b/aces.vromfs.bin_u/gamedata/flightmodels/jh_7a.blkx
@@ -10,3 +10,6 @@
 weapons {
+  weapon: "gb_500_guided_bomb"
+  slots: [3, 4, 5, 6]
 }
diff --git a/char.vromfs.bin_u/config/battle_rating.blkx b/char.vromfs.bin_u/config/battle_rating.blkx
index 3000000..4000000 100644
--- a/char.vromfs.bin_u/config/battle_rating.blkx
+++ b/char.vromfs.bin_u/config/battle_rating.blkx
@@ -5,3 +5,3 @@
-jh_7a: 11.0
+jh_7a: 11.3
"""


async def main():
    print("=== War Thunder Datamine 独立分析模块示例 ===")

    # 1. 模拟本地测试 (使用 mock LLM 快速演示无网络调用)
    async def mock_llm_response(prompt: str, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({
                            "report_title": "2.56.0.40->2.56.0.41",
                            "summary": "JH-7A 新增 GB-500 制导炸弹挂载，且分房权重(BR)微调至 11.3。",
                            "importance": "高",
                            "update_sections": [
                                {
                                    "title": "载具与武器调整",
                                    "items": [
                                        {
                                            "text": "JH-7A(飞豹) 战术扩展",
                                            "source_ids": ["C001-001"],
                                            "children": [
                                                {"text": "新增 GB-500 精确制导炸弹挂载能力", "source_ids": ["C001-001"]},
                                                {"text": "分房权重 BR 从 11.0 调整至 11.3", "source_ids": ["C001-002"]}
                                            ]
                                        }
                                    ]
                                }
                            ],
                            "bulk_repeat_content": {
                                "batch": [],
                                "repeated": [],
                                "needs_verification": [],
                            },
                            "ai_analysis": {
                                "changed_content": [
                                    "JH-7A 获得防区外精确对地打击能力",
                                    "全模式 BR 调整为 11.3"
                                ],
                                "player_impact": [
                                    "陆战历史对地攻顶威胁大幅提升，对敌方中近程防空系统提出更高拦截要求"
                                ],
                                "uncertainties": [
                                    "激光照准吊舱视角倍率与热像代数需进入自定义房验证"
                                ],
                                "recommendation": "推荐主玩空中对地支援及陆战高权玩家优先点出对应改装件。"
                            },
                            "highlights": [
                                "JH-7A(飞豹) 新增 GB-500 制导炸弹",
                                "JH-7A BR 调整为 11.3"
                            ],
                            "tags": ["载具调整", "空战", "对地挂载", "BR变动"],
                        })
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 320,
                "completion_tokens": 150,
                "total_tokens": 470,
            }
        }

    config = AnalyzerConfig(
        model="demo-model",
        review_mode="off",
        enable_struct_diff=False,
    )
    analyzer = DatamineAnalyzer(config, llm_context=mock_llm_response)

    print("\n正在分析 diff 文本...")
    result = await analyzer.analyze_diff_text(SAMPLE_DIFF)

    print("\n--- 生成的 Markdown 报告预览 ---")
    print(result.to_markdown())

    print("\n--- JSON 数据结构字段预览 ---")
    data = result.to_dict()
    print(f"标题: {data['report_title']}")
    print(f"标签: {data['tags']}")
    print(f"重要程度: {data['importance']}")
    print(f"Token 统计: {data['token_usage']}")


if __name__ == "__main__":
    asyncio.run(main())
