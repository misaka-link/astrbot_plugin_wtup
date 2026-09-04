from __future__ import annotations

from typing import Any
from .wt_extractor import DatamineExtractedFacts, VehicleSpec, COUNTRY_CN_MAP


def format_datamine_report(
    facts: DatamineExtractedFacts,
    *,
    bilingual: bool = True,
    model: str = "",
    token_usage: Any = None,
) -> str:
    """将抽取的事实格式化为严格对齐 gszabi99 风格的 Datamine 技术更新清单。"""
    lines: list[str] = []

    # 1. 标题版本范围
    if facts.version_range:
        lines.append(facts.version_range)
    else:
        lines.append("War Thunder Datamine Update")

    # 2. 如果是无实质更新 (:nothingburger:)
    if facts.is_nothingburger:
        lines.append(":nothingburger: (无实质游戏内容改动)")
        lines.append("")
        _append_versions(lines, facts)
        return "\n".join(lines)

    # 3. 新增载具列表
    if facts.new_vehicles:
        lines.append("new vehicles" + (" (新增载具):" if bilingual else ":"))
        for v in facts.new_vehicles:
            country_cn = COUNTRY_CN_MAP.get(v.country, v.country)
            country_str = f"[{v.country} ({country_cn})]" if bilingual else f"[{v.country}]"
            lines.append(f"{v.display_name} {country_str}:")
            if v.tier:
                lines.append(f"  tier {v.tier}" + (f" ({v.tier}级)" if bilingual else ""))

            # BR 矩阵
            if v.br.has_any():
                lines.append("  BR" + (" (分房权重):" if bilingual else ":"))
                if v.br.ab is not None:
                    lines.append(f"    AB: {v.br.ab}" + (" (街机)" if bilingual else ""))
                if v.br.air_rb is not None:
                    lines.append(f"    Air RB: {v.br.air_rb}" + (" (空战历史)" if bilingual else ""))
                if v.br.ground_rb is not None:
                    lines.append(f"    Ground RB: {v.br.ground_rb}" + (" (陆战历史)" if bilingual else ""))
                if v.br.naval_rb is not None:
                    lines.append(f"    Naval RB: {v.br.naval_rb}" + (" (海战历史)" if bilingual else ""))
                if v.br.air_sb is not None:
                    lines.append(f"    SB: {v.br.air_sb}" + (" (全真模拟)" if bilingual else ""))

            # 状态标志
            lines.append("  hidden (隐藏/非正式售卖)")
            lines.append("  no tech-tree data (无科技树研发数据)")

            # 雷达与航电
            if v.radar:
                lines.append(f"  {v.radar}")
            if v.rwr:
                lines.append(f"  {v.rwr}")
            if v.countermeasures:
                lines.append(f"  {v.countermeasures}")

            # 自定义挂载槽位
            if v.custom_loadouts:
                lines.append("  custom loadouts" + (" (自定义挂架槽位):" if bilingual else ":"))
                for slot_idx in sorted(v.custom_loadouts.keys()):
                    weapons = v.custom_loadouts[slot_idx]
                    if len(weapons) == 1:
                        lines.append(f"    slot {slot_idx}: {weapons[0]}")
                    else:
                        lines.append(f"    slot {slot_idx}:")
                        for w in weapons:
                            lines.append(f"      {w}")

            lines.append("")

    # 4. 挂载变动
    if facts.loadout_changes:
        lines.append("loadout changes" + (" (挂载变动):" if bilingual else ":"))
        for c in facts.loadout_changes[:25]:
            lines.append(f"  {c}")
        lines.append("")

    # 5. 科技树与可见性变动
    if facts.tree_and_visibility:
        lines.append("tree and visibility changes" + (" (科技树与可见性变动):" if bilingual else ":"))
        for item in facts.tree_and_visibility[:15]:
            lines.append(f"  {item}")
        lines.append("")

    # 6. 本地化文本与美术资产
    has_texts = any(bool(v) for v in facts.texts.values())
    if has_texts:
        for t in facts.texts.get("trophy", [])[:5]:
            lines.append(t + (" (新增奖杯文本)" if bilingual else ""))
        for t in facts.texts.get("loading_screen", [])[:5]:
            lines.append(t + (" (新增加载图文本)" if bilingual else ""))
        for t in facts.texts.get("vehicle", [])[:5]:
            lines.append(t + (" (新增载具文本)" if bilingual else ""))
        for t in facts.texts.get("decal", [])[:5]:
            lines.append(t + (" (新增贴花文本)" if bilingual else ""))
        for t in facts.texts.get("skin", [])[:5]:
            lines.append(t + (" (新增涂装文本)" if bilingual else ""))
        lines.append("")

    # 7. 版本三态元信息
    _append_versions(lines, facts)

    # 8. 居中模型与 Token 统计
    lines.append("")
    model_str = model or "默认模型"
    lines.append(f"使用模型: {model_str}")
    if token_usage:
        from .token_usage import token_usage_numbers
        tot, inp, out = token_usage_numbers(token_usage)
        lines.append(f"Token 消耗: 总计 {tot} · 输入 {inp} · 输出 {out}")

    return "\n".join(lines).strip()


def _append_versions(lines: list[str], facts: DatamineExtractedFacts) -> None:
    dev = facts.current_versions.get("dev") or (facts.version_range.split("->")[-1].strip() if "->" in facts.version_range else "")
    if dev:
        lines.append(f"Current dev version: {dev}")
    wip = facts.current_versions.get("wip_live")
    if wip:
        lines.append(f"Current WiP live version: {wip}")
    reg = facts.current_versions.get("regular_live")
    if reg:
        lines.append(f"Current regular live version: {reg}")
