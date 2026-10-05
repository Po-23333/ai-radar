"""python -m unittest -v   （完全離線，用 tests/fixtures）"""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from radar import config as C
from radar import discord, score, sources
from radar import main as radar_main
from radar.main import make_getter

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)


def load_all(snapshots=None, now=NOW):
    get = make_getter(FIX, None)
    items = (sources.fetch_github([{"q": "x {since}", "days": 7}], now, "tok", snapshots or {}, get=get, pause=0)
             + sources.fetch_hf_models(50, get=get, family_relations=C.FAMILY_RELATIONS)
             + sources.fetch_hf_papers(now, C.HF_PAPERS_MAX_AGE_DAYS, get=get))
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

    def _picked_keys(self, picked):
        return {i["key"] for sec in ("big", "relevant", "collision") for i in picked[sec]}

    def test_no_repost_without_surge(self):
        first = score.select(self.items, {}, date(2026, 9, 23))
        posted = {i["key"]: {"d": "2026-09-23", "v": i["velocity"]}
                  for sec in ("big", "relevant") for i in first[sec]}
        # 7 天冷卻已經過了，但熱度沒變 → 仍然不推
        second = score.select(load_all(), posted, date(2026, 10, 5))
        self.assertFalse(self._picked_keys(second) & set(posted))

    def test_resurge_reposts_with_flag(self):
        key = "gh:jev-chat/jev-chat-jarvis"
        v = self.k[key]["velocity"]
        posted = {key: {"d": "2026-09-18", "v": v / 3}}
        picked = score.select(load_all(), posted, date(2026, 9, 23))
        hit = [i for sec in ("big", "relevant") for i in picked[sec] if i["key"] == key]
        self.assertTrue(hit)
        self.assertTrue(any("再度升溫" in f for f in hit[0]["flags"]))

    def test_resurge_needs_min_days(self):
        key = "gh:jev-chat/jev-chat-jarvis"
        posted = {key: {"d": "2026-09-22", "v": self.k[key]["velocity"] / 3}}
        picked = score.select(load_all(), posted, date(2026, 9, 23))
        self.assertNotIn(key, self._picked_keys(picked))

    def test_papers_never_resurge(self):
        key = "hfp:2609.22222"
        posted = {key: {"d": "2026-09-10", "v": 1}}
        picked = score.select(load_all(), posted, date(2026, 9, 23))
        self.assertNotIn(key, self._picked_keys(picked))

    def test_legacy_string_posted_never_reposts(self):
        posted = {"gh:jev-chat/jev-chat-jarvis": "2026-09-01"}
        picked = score.select(self.items, posted, date(2026, 9, 23))
        self.assertNotIn("gh:jev-chat/jev-chat-jarvis", self._picked_keys(picked))

    def test_family_dedup_same_day(self):
        base, gguf = "hfm:qwen/qwen3.5-9b-instruct", "hfm:someone/qwen3.5-9b-instruct-gguf"
        self.assertEqual(self.k[base]["family"], self.k[gguf]["family"])
        picked = score.select(self.items, {}, date(2026, 9, 23))
        keys = self._picked_keys(picked)
        self.assertIn(base, keys)
        self.assertNotIn(gguf, keys)
        kept = next(i for sec in ("big", "relevant") for i in picked[sec] if i["key"] == base)
        self.assertTrue(any("同系列" in f for f in kept["flags"]))

    def test_family_posted_blocks_derivatives(self):
        fam = self.k["hfm:qwen/qwen3.5-9b-instruct"]["family"]
        picked = score.select(self.items, {fam: {"d": "2026-09-20", "v": 410}}, date(2026, 9, 23))
        keys = self._picked_keys(picked)
        self.assertNotIn("hfm:someone/qwen3.5-9b-instruct-gguf", keys)

    def test_repack_not_matched_via_base_model_tag(self):
        it = self.k["hfm:someone/qwen3.5-9b-instruct-gguf"]
        self.assertNotIn("base_model", it["text"])
        self.assertTrue(it["meta"]["repack"])
        self.assertTrue(any("量化轉檔" in f for f in it["flags"]))

    def test_paper_window_survives_weekend(self):
        # 論文 9/22 發布，週末過後（約 4 天後）跑，仍要抓得到
        later = NOW + timedelta(days=3, hours=12)
        got = {i["key"] for i in sources.fetch_hf_papers(later, C.HF_PAPERS_MAX_AGE_DAYS,
                                                          get=make_getter(FIX, None))}
        self.assertIn("hfp:2609.11111", got)

    def test_relevant_source_cap(self):
        picked = score.select(self.items, {}, date(2026, 9, 23))
        srcs = [i["source"] for i in picked["relevant"]]
        self.assertLessEqual(max(srcs.count(x) for x in set(srcs)), C.RELEVANT_MAX_PER_SOURCE)

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


class EndToEnd(unittest.TestCase):
    """跑完整的 main()（假 webhook），檢查 state 寫入格式。"""

    def run_main(self, state_path, now="2026-09-23T00:00:00+00:00"):
        env = {"DISCORD_WEBHOOK_URL": "https://example.invalid/hook"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(radar_main.discord, "post", return_value="msg-id"), \
                mock.patch.object(radar_main.time, "sleep"):
            return radar_main.main(["--fixtures", str(FIX), "--state", str(state_path), "--now", now])

    def test_state_records(self):
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "state.json"
            self.assertEqual(self.run_main(sp), 0)
            st = json.loads(sp.read_text(encoding="utf-8"))
            recs = [r for m in st["messages"] for r in m.get("items", [])]
            self.assertTrue(recs)
            for r in recs:
                self.assertIn(r["section"], ("big", "relevant", "collision"))
                self.assertTrue(r["title"])
            self.assertTrue(any(r["collision"] for r in recs))  # 撞題能事後回查是哪條規則
            entry = st["posted"]["hfm:qwen/qwen3.5-9b-instruct"]
            self.assertEqual(set(entry), {"d", "v"})
            self.assertIn("fam:qwen3.5-9b-instruct", st["posted"])

            # 隔天再跑：昨天推過的不再出現
            self.run_main(sp, now="2026-09-24T00:00:00+00:00")
            st2 = json.loads(sp.read_text(encoding="utf-8"))
            day1 = {k for m in st["messages"] for k in m["keys"]}
            day2 = {k for m in st2["messages"] if m["date"] == "2026-09-24" for k in m["keys"]}
            self.assertFalse(day1 & day2)

    def test_dry_run_preview_hides_user_id(self):
        with tempfile.TemporaryDirectory() as d:
            prev = Path(d) / "preview.json"
            env = {"DISCORD_USER_ID": "987654321098765432", "DISCORD_COLLISION_WEBHOOK_URL": "https://x.invalid/c"}
            with mock.patch.dict(os.environ, env, clear=False):
                radar_main.main(["--dry-run", "--fixtures", str(FIX), "--state", str(Path(d) / "s.json"),
                                 "--preview", str(prev), "--now", "2026-09-23T00:00:00+00:00"])
            text = prev.read_text(encoding="utf-8")
            self.assertNotIn("987654321098765432", text)
            self.assertIn("<@DISCORD_USER_ID>", text)  # 預覽仍看得到「這裡會 @你」

    def test_legacy_state_migrates(self):
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "state.json"
            sp.write_text(json.dumps({"snapshots": {}, "messages": [], "runs": [],
                                      "posted": {"hfm:qwen/qwen3.5-9b-instruct": "2026-09-20"}}))
            self.run_main(sp)
            st = json.loads(sp.read_text(encoding="utf-8"))
            sent = {k for m in st["messages"] for k in m["keys"]}
            self.assertNotIn("hfm:someone/qwen3.5-9b-instruct-gguf", sent)  # 家族被舊紀錄擋下


if __name__ == "__main__":
    unittest.main()
