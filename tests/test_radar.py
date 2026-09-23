"""python -m unittest -v   （完全離線，用 tests/fixtures）"""
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from radar import config as C
from radar import discord, score, sources
from radar.main import make_getter

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)


def load_all(snapshots=None):
    get = make_getter(FIX, None)
    items = (sources.fetch_github([{"q": "x {since}", "days": 7}], NOW, "tok", snapshots or {}, get=get, pause=0)
             + sources.fetch_hf_models(50, get=get)
             + sources.fetch_hf_papers(NOW, 3, get=get))
    score.annotate(items)
    score.compute_heat(items)
    return items


def by_key(items):
    return {i["key"]: i for i in items}


class Keywords(unittest.TestCase):
    def test_short_keyword_needs_word_boundary(self):
        p = score._compile("ai")
        self.assertTrue(p.search("an ai tool"))
        self.assertFalse(p.search("send mail"))
        self.assertFalse(p.search("airflow"))

    def test_prefix_match(self):
        self.assertTrue(score._compile("quantiz").search("4-bit quantization"))
        self.assertFalse(score._compile("quantiz").search("dequantize"))


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.items = load_all()
        self.k = by_key(self.items)

    def test_noise_filtered(self):
        self.assertTrue(self.k["gh:spam/free-gpt-airdrop"]["noise"])
        picked = score.select(self.items, {}, date(2026, 9, 23))
        all_keys = {i["key"] for s in ("big", "relevant", "collision") for i in picked[s]}
        self.assertNotIn("gh:spam/free-gpt-airdrop", all_keys)

    def test_star_farm_flagged(self):
        it = self.k["gh:star/farm-llm"]
        self.assertTrue(any("刷星" in f for f in it["flags"]))

    def test_old_paper_dropped(self):
        self.assertNotIn("hfp:2609.33333", self.k)

    def test_cross_source(self):
        paper = self.k["hfp:2609.22222"]
        self.assertIn("github", paper["sources"])
        self.assertTrue(any("GitHub" in f for f in paper["flags"]))

    def test_collision(self):
        self.assertIn("MOPS 財報數值幻覺 benchmark", self.k["hfp:2609.11111"]["collision"])
        self.assertEqual(self.k["hfp:2609.44444"]["collision"], [])

    def test_hf_model_base_and_modelid_fallback(self):
        self.assertIn("quantized of Qwen/Qwen3.5-9B-Instruct", self.k["hfm:someone/qwen3.5-9b-instruct-gguf"]["desc"])
        self.assertIn("hfm:stabilityai/sd-next", self.k)

    def test_big_respects_source_cap(self):
        picked = score.select(self.items, {}, date(2026, 9, 23))
        srcs = [i["source"] for i in picked["big"]]
        self.assertLessEqual(max(srcs.count(s) for s in set(srcs)), C.BIG_MAX_PER_SOURCE)

    def test_cooldown(self):
        first = score.select(self.items, {}, date(2026, 9, 23))
        posted = {i["key"]: "2026-09-22" for s in ("big", "relevant") for i in first[s]}
        second = score.select(self.items, posted, date(2026, 9, 23))
        again = {i["key"] for s in ("big", "relevant") for i in second[s]}
        self.assertFalse(again & set(posted))

    def test_measured_velocity_from_snapshot(self):
        key = "gh:jev-chat/jev-chat-jarvis"
        stars = self.k[key]["metric"]
        snap = {key: {"stars": stars - 500, "t": (NOW - timedelta(days=1)).isoformat()}}
        it = by_key(load_all(snap))[key]
        self.assertAlmostEqual(it["velocity"], 500, delta=1)
        self.assertIn("實測", it["velocity_label"])


class DiscordFormat(unittest.TestCase):
    def fake(self, n, long=False):
        return [{"key": f"k{i}", "source": "github", "title": "t" * (300 if long else 10), "url": "https://x",
                 "desc": "d" * (5000 if long else 50), "velocity_label": "v", "tags": ["a"], "collision": [],
                 "flags": [], "sources": ["github"], "meta": {}} for i in range(n)]

    def test_limits(self):
        picked = {"big": self.fake(3, True), "relevant": self.fake(12, True), "collision": [], "pool_size": 15}
        msgs = discord.build_messages(picked, date(2026, 9, 23), {}, [], None, False)
        for _, p, keys in msgs:
            embeds = p.get("embeds", [])
            self.assertLessEqual(len(embeds), discord.MAX_EMBEDS)
            self.assertLessEqual(sum(discord._embed_len(e) for e in embeds), discord.MAX_TOTAL)
            self.assertEqual(len(keys), len(embeds))
            for e in embeds:
                self.assertLessEqual(len(e["title"]), discord.MAX_TITLE)
        sent = [k for _, _, ks in msgs for k in ks]
        self.assertEqual(len(sent), 15)  # 沒有東西在切分時遺失

    def test_collision_mentions_only_user(self):
        items = self.fake(1)
        items[0]["collision"] = ["X"]
        picked = {"big": [], "relevant": [], "collision": items, "pool_size": 1}
        msgs = discord.build_messages(picked, date(2026, 9, 23), {}, [], "123", True)
        ch, p, _ = msgs[-1]
        self.assertEqual(ch, "collision")
        self.assertIn("<@123>", p["content"])
        self.assertEqual(p["allowed_mentions"], {"parse": [], "users": ["123"]})

    def test_payload_is_json_serializable(self):
        msgs = discord.build_messages({"big": self.fake(2), "relevant": [], "collision": [], "pool_size": 2},
                                      date(2026, 9, 23), {}, ["HF（timeout）"], None, False)
        json.dumps([p for _, p, _ in msgs], ensure_ascii=False)
        self.assertIn("來源失敗", msgs[0][1]["content"])


if __name__ == "__main__":
    unittest.main()
