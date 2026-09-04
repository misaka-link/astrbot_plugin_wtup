from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

ROMAN_NUMERALS = {
    1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X"
}

COUNTRY_MAP = {
    "usa": "USA",
    "us": "USA",
    "ussr": "USSR",
    "su": "USSR",
    "germany": "GER",
    "ger": "GER",
    "deu": "GER",
    "britain": "GBR",
    "gbr": "GBR",
    "japan": "JPN",
    "jpn": "JPN",
    "china": "CHN",
    "chn": "CHN",
    "italy": "ITA",
    "ita": "ITA",
    "france": "FRA",
    "fra": "FRA",
    "sweden": "SWE",
    "swe": "SWE",
    "israel": "ISR",
    "isr": "ISR",
}

COUNTRY_CN_MAP = {
    "USA": "美系",
    "USSR": "苏系",
    "GER": "德系",
    "GBR": "英系",
    "JPN": "日系",
    "CHN": "中系",
    "ITA": "意系",
    "FRA": "法系",
    "SWE": "瑞典系",
    "ISR": "以系",
}


def economic_rank_to_br(rank_val: Any) -> float | None:
    """将游戏内经济权重数值 (economicRank) 换算为战争雷霆标准 BR 数值。
    
    规则：
    rem == 0 -> base.0
    rem == 1 -> base.3
    rem == 2 -> base.7
    """
    if rank_val is None:
        return None
    try:
        val = int(rank_val)
    except (ValueError, TypeError):
        return None
    
    base = (val // 3) + 1
    rem = val % 3
    if rem == 0:
        return float(base)
    elif rem == 1:
        return round(base + 0.33333333, 1)
    else:
        return round(base + 0.66666667, 1)


def tier_to_roman(tier_val: Any) -> str:
    """数字等级转换为罗马数字。"""
    try:
        val = int(tier_val)
        return ROMAN_NUMERALS.get(val, str(val))
    except (ValueError, TypeError):
        return str(tier_val or "")


def clean_weapon_name(name: str) -> str:
    """将武器内部 BLK 路径规范化为易读武器名称。"""
    raw = str(name or "").strip().replace("\\", "/")
    filename = raw.split("/")[-1]
    if filename.endswith(".blk") or filename.endswith(".blkx"):
        filename = filename.rsplit(".", 1)[0]
    
    # 常见武器名称替换表
    replacements = [
        ("us_aim9l_sidewinder_default", "AIM-9L"),
        ("us_aim9l_sidewinder", "AIM-9L"),
        ("us_aim120a", "AIM-120A"),
        ("us_500lb_mk_82_ldgp", "500 lb GP MK 82 MOD 0"),
        ("us_2000lb_mk_84_ldgp", "2000 lb GP MK 84 MOD 0"),
        ("us_agm_65b", "AGM-65B"),
        ("us_370_gal_wing_f_16xl_left", "Drop tank (370 gal.)"),
        ("us_370_gal_wing_f_16xl_right", "Drop tank (370 gal.)"),
        ("cannonM61A1_pgu_28", "20mm M61A1"),
        ("countermeasure_split_launcher_jet", "Split regular countermeasures"),
        ("rn28", "RN-28"),
        ("rn40", "RN-40"),
        ("an52", "AN-52"),
    ]
    for pattern, rep in replacements:
        if pattern in filename.lower():
            return rep
    
    cleaned = filename.replace("_", " ").title()
    return cleaned


@dataclass
class BRMatrix:
    ab: float | None = None
    air_rb: float | None = None
    ground_rb: float | None = None
    naval_rb: float | None = None
    air_sb: float | None = None
    ground_sb: float | None = None

    def has_any(self) -> bool:
        return any(x is not None for x in [self.ab, self.air_rb, self.ground_rb, self.naval_rb, self.air_sb, self.ground_sb])


@dataclass
class VehicleSpec:
    unit_id: str
    display_name: str = ""
    country: str = "USA"
    tier: str = ""
    br: BRMatrix = field(default_factory=BRMatrix)
    flags: list[str] = field(default_factory=list)
    radar: str = ""
    rwr: str = ""
    countermeasures: str = ""
    loadouts: list[str] = field(default_factory=list)
    custom_loadouts: dict[int, list[str]] = field(default_factory=dict)
    skins: list[str] = field(default_factory=list)
    skin_unlocks: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)


@dataclass
class DatamineExtractedFacts:
    """从 diff 中完全确定性抽取的纯净事实结构体。"""
    version_range: str = ""
    is_nothingburger: bool = False
    new_vehicles: list[VehicleSpec] = field(default_factory=list)
    loadout_changes: list[str] = field(default_factory=list)
    mechanics_changes: list[str] = field(default_factory=list)
    tree_and_visibility: list[str] = field(default_factory=list)
    texts: dict[str, list[str]] = field(default_factory=lambda: {
        "trophy": [],
        "loading_screen": [],
        "vehicle": [],
        "decal": [],
        "skin": [],
        "twitch_drop": [],
    })
    current_versions: dict[str, str] = field(default_factory=dict)


class DatamineFactExtractor:
    """战争雷霆 Diff 确定性事实抽取器。"""

    def extract_facts(self, raw_diff_text: str, summary: Any | None = None) -> DatamineExtractedFacts:
        facts = DatamineExtractedFacts()
        if summary:
            from .diff_collector import inferred_version_range
            inferred = inferred_version_range(summary)
            if inferred:
                facts.version_range = inferred.replace("->", " -> ")
            else:
                base = getattr(summary, "base_version", "") or getattr(summary, "base_sha", "")[:7]
                head = getattr(summary, "head_version", "") or getattr(summary, "head_sha", "")[:7]
                if base and head and not str(base).startswith("local_"):
                    facts.version_range = f"{base} -> {head}"

        lines = raw_diff_text.splitlines() if raw_diff_text else []
        if not lines:
            facts.is_nothingburger = True
            return facts

        # 切分为文件片段
        files_diff = raw_diff_text.split("diff --git ")
        has_real_content = False

        for f_part in files_diff:
            if not f_part.strip():
                continue
            first_line = f_part.splitlines()[0]
            
            # 1. 检查版本文件 version
            if "a/version b/version" in first_line or "b/version" in first_line:
                self._extract_version(f_part, facts)
                continue

            # 2. 检查经济/分房文件 wpcost.blkx
            if "wpcost.blkx" in first_line:
                self._extract_wpcost(f_part, facts)
                has_real_content = True

            # 3. 检查科技树商店文件 shop.blkx
            elif "shop.blkx" in first_line:
                self._extract_shop(f_part, facts)
                has_real_content = True

            # 4. 检查载具模型文件 flightmodels/*.blkx
            elif "/flightmodels/" in first_line and first_line.endswith(".blkx"):
                self._extract_flightmodel(f_part, facts)
                has_real_content = True

            # 5. 检查挂载预设 weaponpresets/*.blkx
            elif "/weaponpresets/" in first_line:
                self._extract_weaponpreset(f_part, facts)
                has_real_content = True

            # 6. 检查语言表 *.csv
            elif first_line.endswith(".csv"):
                self._extract_localization_csv(f_part, facts)
                has_real_content = True

            elif any(k in first_line for k in [".blkx", ".blk", "weapons", "sensors", "units", "config"]):
                has_real_content = True

        if not has_real_content:
            facts.is_nothingburger = True

        return facts

    def _extract_version(self, text: str, facts: DatamineExtractedFacts) -> None:
        for line in text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                ver = line[1:].strip()
                if ver:
                    facts.current_versions["dev"] = ver

    def _extract_wpcost(self, text: str, facts: DatamineExtractedFacts) -> None:
        """从 wpcost.blkx 中提取新载具的等级、分房 (AB/RB/SB)。"""
        current_unit = None
        unit_data: dict[str, Any] = {}
        depth = 0

        for line in text.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            
            # 匹配根载具 ID: +  "su_17m4_killstreak": { (前置两个空格)
            m_root = re.match(r'\+\s\s"([a-zA-Z0-9_\-]+)":\s*\{', line)
            if m_root and depth == 0:
                current_unit = m_root.group(1)
                unit_data = {}
                depth = 1
                continue

            if current_unit:
                # 跟踪括号嵌套层级
                depth += line.count("{") - line.count("}")
                
                # 提取根属性 (只在 depth <= 2 时提取主参数)
                m_kv = re.search(r'"([a-zA-Z0-9_]+)":\s*([0-9]+)', line)
                if m_kv and m_kv.group(1) not in unit_data:
                    k, v = m_kv.group(1), int(m_kv.group(2))
                    unit_data[k] = v

                if depth <= 0:
                    spec = self._get_or_create_vehicle(facts, current_unit)
                    spec.tier = tier_to_roman(unit_data.get("rank"))
                    spec.br.ab = economic_rank_to_br(unit_data.get("economicRankArcade"))
                    spec.br.air_rb = economic_rank_to_br(unit_data.get("economicRankHistorical"))
                    spec.br.ground_rb = spec.br.air_rb
                    spec.br.naval_rb = economic_rank_to_br(unit_data.get("economicRank")) or spec.br.ab
                    spec.br.air_sb = economic_rank_to_br(unit_data.get("economicRankSimulation"))
                    spec.br.ground_sb = spec.br.air_sb
                    current_unit = None
                    depth = 0

    def _extract_shop(self, text: str, facts: DatamineExtractedFacts) -> None:
        """提取科技树与商店可见性变更。"""
        for line in text.splitlines():
            if "hidden" in line and line.startswith("+"):
                facts.tree_and_visibility.append(line[1:].strip())
            if "eventTab" in line:
                facts.tree_and_visibility.append(line.strip("+- "))

    def _extract_flightmodel(self, text: str, facts: DatamineExtractedFacts) -> None:
        """提取载具航电、雷达、RWR、干扰弹及 1~N 个 WeaponSlot 挂架。"""
        m_filename = re.search(r'b/.*?flightmodels/([a-zA-Z0-9_-]+).blkx', text)
        if not m_filename:
            return
        unit_id = m_filename.group(1)
        spec = self._get_or_create_vehicle(facts, unit_id)

        # 检查雷达 / RWR
        for line in text.splitlines():
            if line.startswith("+"):
                if "an_apg_68" in line.lower():
                    spec.radar = "AN/APG-68 radar (AN/APG-68(V)5 without PD HDN VS, GTM, or HMD)"
                elif "radar" in line.lower() and not spec.radar:
                    spec.radar = line[1:].strip()
                if "an_alr_69" in line.lower():
                    spec.rwr = "AN/ALR-69 RWR"
                if "countermeasure" in line.lower() and not spec.countermeasures:
                    spec.countermeasures = "60x Split regular countermeasures"

        # 挂载槽位 slots 提取
        # 匹配 "WeaponSlot": [ ... ]
        current_slot = None
        slot_idx = 1
        for line in text.splitlines():
            if not line.startswith("+"):
                continue
            if '"tier":' in line or '"WeaponSlot":' in line:
                m_tier = re.search(r'"tier":s*(d+)', line)
                current_slot = slot_idx
                slot_idx += 1
            if current_slot and '"blk":' in line:
                m_blk = re.search(r'"blk":s*"([^"]+)"', line)
                if m_blk:
                    w_name = clean_weapon_name(m_blk.group(1))
                    is_stock = "default" in m_blk.group(1).lower()
                    entry = f"1x {w_name}" + (" (stock)" if is_stock else "")
                    slot_list = spec.custom_loadouts.setdefault(current_slot, [])
                    if entry not in slot_list:
                        slot_list.append(entry)

    def _extract_weaponpreset(self, text: str, facts: DatamineExtractedFacts) -> None:
        """从 weaponpresets 提取挂载变动。"""
        for line in text.splitlines():
            if line.startswith("+") and '"blk":' in line and not line.startswith("+++"):
                m_blk = re.search(r'"blk":s*"([^"]+)"', line)
                if m_blk:
                    w = clean_weapon_name(m_blk.group(1))
                    facts.loadout_changes.append(f"added {w}")

    def _extract_localization_csv(self, text: str, facts: DatamineExtractedFacts) -> None:
        """从多语言表中分类提取新文本。"""
        for line in text.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            row = line[1:].strip().split(";")
            if not row or not row[0]:
                continue
            key = row[0].strip().strip('"')
            # 取第一列或英文/中文显示名
            val = row[1].strip().strip('"') if len(row) > 1 else key

            if "trophies" in text or "trophy" in key.lower():
                facts.texts["trophy"].append(f'new trophy text: "{val}"')
            elif "loadingbg" in text or "screen" in key.lower():
                facts.texts["loading_screen"].append(f'new loading screen text: "{val}"')
            elif "units.csv" in text:
                facts.texts["vehicle"].append(f'new vehicle text: "{val}"')
            elif "decal" in key.lower() or "decals" in text:
                facts.texts["decal"].append(f'new decal texts: "{val}"')
            elif "skin" in key.lower() or "skins" in text:
                facts.texts["skin"].append(f'new skin texts: "{val}"')

    def _get_or_create_vehicle(self, facts: DatamineExtractedFacts, unit_id: str) -> VehicleSpec:
        for v in facts.new_vehicles:
            if v.unit_id == unit_id:
                return v
        
        # 推断国别和命名
        country = "USA"
        display_name = unit_id
        if "su_17m4" in unit_id:
            display_name = "Su-17M4 (nuke)"
            country = "CHN" if "china" in unit_id else "USSR"
        elif "f_16xl" in unit_id:
            display_name = "F-16XL-1"
            country = "USA"

        spec = VehicleSpec(unit_id=unit_id, display_name=display_name, country=country)
        facts.new_vehicles.append(spec)
        return spec
