from __future__ import annotations

import unittest
from types import SimpleNamespace

from wtup_standalone.image_resolver import WarThunderImageResolver


class TestImageResolver(unittest.TestCase):
    def test_resolve_decal_url(self):
        # War-Thunder-Decals 已停更废弃，贴花解析返回 None
        url1 = WarThunderImageResolver.resolve_decal_url("vj_day_2026_decal")
        self.assertIsNone(url1)

        url2 = WarThunderImageResolver.resolve_decal_url("88th_div_decal")
        self.assertIsNone(url2)

    def test_resolve_trophy_icon_url(self):
        url = WarThunderImageResolver.resolve_trophy_icon_url("001_steam_trophy")
        self.assertIsNone(url)

    def test_resolve_loading_screen_url(self):
        url = WarThunderImageResolver.resolve_loading_screen_url("f_16xl")
        self.assertIsNone(url)

    def test_find_images_in_facts(self):
        facts = SimpleNamespace(
            texts={
                "decal": ['new event decal: "vj_day_2026_decal"'],
                "trophy": ['new trophy text: "WTCS Esports Trophy VI"'],
                "loading_screen": ['new loading screen text: "F-16XL"'],
            }
        )
        raw_text = (
            "Check out the screenshot: https://cdn.imgchest.com/files/5f55f99f0cc2.png\n"
            "diff --git a/atlases.vromfs.bin_u/gameuiskin/new_medal.png b/atlases.vromfs.bin_u/gameuiskin/new_medal.png"
        )

        images = WarThunderImageResolver.find_images_in_facts(facts, raw_text)
        types = [img["type"] for img in images]
        # 避免盲猜 404 伪造链接，只保留真实外链与真实 diff 中的图片
        self.assertNotIn("decal", types)
        self.assertNotIn("trophy", types)
        self.assertNotIn("loading_screen", types)
        self.assertIn("screenshot", types)
        self.assertIn("asset", types)

        screenshot_img = [img for img in images if img["type"] == "screenshot"][0]
        self.assertEqual(screenshot_img["url"], "https://cdn.imgchest.com/files/5f55f99f0cc2.png")
        asset_img = [img for img in images if img["type"] == "asset"][0]
        self.assertEqual(asset_img["name"], "new_medal.png")


if __name__ == "__main__":
    unittest.main()
