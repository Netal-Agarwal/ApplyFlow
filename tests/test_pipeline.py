import tempfile
import unittest
from pathlib import Path

from applyflow import config
from applyflow.cli import main
from applyflow.http import FetchError, PoliteClient
from applyflow.models import Company, Job
from applyflow.pipeline import collect, probe, score_all, slug_candidates
from applyflow.report import write_csv, write_html
from applyflow.store import Store
from tests.helpers import FakeClient, default_routes, example_profile

SEEDS = [
    Company("Acme", "greenhouse", "acme"),
    Company("Zeta", "lever", "zeta"),
    Company("Nimbus", "ashby", "nimbus"),
    Company("Gone", "greenhouse", "gone"),        # 404 -> recorded as a failed board
]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def _job(self, ext_id, title="Backend Engineer"):
        return Job(source="greenhouse", external_id=ext_id, company="Acme", title=title, url="u",
                   locations=["Bengaluru"], emails=["a@b.co"])

    def test_upsert_and_close(self):
        first = self.store.upsert_board("greenhouse:acme", [self._job("1"), self._job("2")])
        self.assertEqual(first, {"seen": 2, "new": 2, "closed": 0})
        second = self.store.upsert_board("greenhouse:acme", [self._job("2", "Backend Engineer II")])
        self.assertEqual(second, {"seen": 1, "new": 0, "closed": 1})
        active = self.store.active_jobs()
        self.assertEqual([j.id for j in active], ["greenhouse:2"])
        self.assertEqual(active[0].title, "Backend Engineer II")
        self.assertEqual(active[0].emails, ["a@b.co"])

    def test_empty_board_closes_everything(self):
        self.store.upsert_board("greenhouse:acme", [self._job("1")])
        self.assertEqual(self.store.upsert_board("greenhouse:acme", [])["closed"], 1)

    def test_reopened_job_is_active_again(self):
        self.store.upsert_board("greenhouse:acme", [self._job("1")])
        self.store.upsert_board("greenhouse:acme", [])
        self.store.upsert_board("greenhouse:acme", [self._job("1")])
        self.assertEqual(len(self.store.active_jobs()), 1)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.client = FakeClient(default_routes())
        self.profile = example_profile()

    def tearDown(self):
        self.store.close()

    def test_collect_score_report(self):
        logs = []
        summary = collect(self.store, self.client, SEEDS, log=logs.append)
        self.assertEqual(summary.boards_ok, 4)                        # 3 ATS + HN
        self.assertEqual([b for b, _ in summary.boards_failed], ["greenhouse:gone"])
        self.assertEqual(summary.jobs_seen, 8 + 2 + 2 + 3)

        results = score_all(self.store, self.profile)
        self.assertEqual(len(results), 15)
        ranked = self.store.ranked()
        self.assertTrue(ranked)
        self.assertEqual(ranked[0]["id"], "greenhouse:1001")         # best match first
        scores = [row["score"] for row in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

        stats = self.store.stats()
        self.assertEqual(stats["active_jobs"], 15)
        self.assertEqual(stats["failed_boards"][0][0], "greenhouse:gone")
        self.assertTrue(any("excluded term" in reason for reason, _ in stats["top_reject_reasons"]))

        with tempfile.TemporaryDirectory() as tmp:
            html_path = write_html(ranked, Path(tmp) / "r.html", stats)
            csv_path = write_csv(ranked, Path(tmp) / "r.csv")
            page = html_path.read_text(encoding="utf-8")
            self.assertIn("Backend Engineer Intern", page)
            self.assertIn("jobs@lumen.ai", page)
            self.assertNotIn("Senior Backend Engineer", page)
            self.assertEqual(len(csv_path.read_text(encoding="utf-8").strip().splitlines()), len(ranked) + 1)

    def test_rerun_is_idempotent(self):
        collect(self.store, self.client, SEEDS, log=lambda _: None)
        again = collect(self.store, self.client, SEEDS, log=lambda _: None)
        self.assertEqual((again.jobs_new, again.jobs_closed), (0, 0))

    def test_report_escapes_html(self):
        self.store.upsert_board("greenhouse:x", [Job(
            source="greenhouse", external_id="9", company="<script>x</script>",
            title="Backend Engineer Intern", url="https://e.com", locations=["Bengaluru"],
            description="New grad. Python FastAPI AWS PostgreSQL REST APIs microservices backend Lambda")])
        score_all(self.store, self.profile)
        with tempfile.TemporaryDirectory() as tmp:
            page = write_html(self.store.ranked(), Path(tmp) / "r.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>x</script>", page)


class ProbeTests(unittest.TestCase):
    def test_slug_candidates(self):
        self.assertEqual(slug_candidates("Sarvam AI")[:3], ["sarvamai", "sarvam-ai", "sarvam"])
        self.assertIn("acme", slug_candidates("Acme Technologies Pvt Ltd"))

    def test_probe_finds_board(self):
        found = probe(FakeClient(default_routes()), "Nimbus")
        self.assertEqual([(c.ats, c.slug, n) for c, n in found], [("ashby", "nimbus", 2)])

    def test_ashby_empty_board_is_not_a_hit(self):
        client = FakeClient({f"job-board/{slug}": {"jobs": []} for slug in slug_candidates("Nothing")})
        self.assertEqual(probe(client, "Nothing"), [])


class HttpTests(unittest.TestCase):
    class Resp:
        def __init__(self, status, payload=None, headers=None):
            self.status_code, self._payload, self.headers = status, payload, headers or {}

        def json(self):
            return self._payload

    class Session:
        def __init__(self, responses):
            self.responses, self.headers, self.calls = list(responses), {}, 0

        def get(self, url, params=None, timeout=None):
            self.calls += 1
            return self.responses.pop(0)

    def test_retries_then_succeeds(self):
        session = self.Session([self.Resp(429, headers={"Retry-After": "2"}), self.Resp(503), self.Resp(200, {"ok": 1})])
        sleeps = []
        client = PoliteClient("ua", session=session, sleep=sleeps.append, clock=lambda: 0.0, min_interval=0)
        self.assertEqual(client.get_json("https://x.test/a"), {"ok": 1})
        self.assertEqual(session.calls, 3)
        self.assertIn(2.0, sleeps)
        self.assertEqual(session.headers["User-Agent"], "ua")

    def test_404_raises_without_retry(self):
        session = self.Session([self.Resp(404)])
        client = PoliteClient("ua", session=session, sleep=lambda s: None, min_interval=0)
        with self.assertRaises(FetchError) as ctx:
            client.get_json("https://x.test/a")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(session.calls, 1)

    def test_rate_limits_per_host(self):
        session = self.Session([self.Resp(200, {}), self.Resp(200, {}), self.Resp(200, {})])
        sleeps = []
        client = PoliteClient("ua", session=session, sleep=sleeps.append, clock=lambda: 100.0, min_interval=1.5)
        client.get_json("https://a.test/1")
        client.get_json("https://b.test/1")      # different host: no wait
        client.get_json("https://a.test/2")      # same host: waits
        self.assertEqual(sleeps, [1.5])


class ConfigTests(unittest.TestCase):
    def test_example_profile_and_seeds_load(self):
        profile = example_profile()
        self.assertIn("fastapi", profile.skills)
        self.assertEqual(profile.skills["golang"], -3)
        seeds = config.load_seeds()
        self.assertGreaterEqual(len(seeds), 20)
        self.assertTrue(all(s.ats in config.SUPPORTED_ATS for s in seeds))

    def test_append_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seeds.yaml"
            path.write_text("# comment kept\ncompanies:\n  - {name: A, ats: lever, slug: a}\n", encoding="utf-8")
            self.assertTrue(config.append_seed(Company("Sarvam: AI", "ashby", "sarvam"), path))
            self.assertFalse(config.append_seed(Company("Sarvam", "ashby", "sarvam"), path))
            self.assertEqual([c.name for c in config.load_seeds(path)], ["A", "Sarvam: AI"])
            self.assertIn("# comment kept", path.read_text(encoding="utf-8"))

    def test_bad_tiers_rejected(self):
        import yaml
        data = yaml.safe_load(config.PROFILE_EXAMPLE_PATH.read_text(encoding="utf-8"))
        data["tiers"] = {"A": 40, "B": 50}
        with self.assertRaises(config.ConfigError):
            config.parse_profile(data)


class CliTests(unittest.TestCase):
    def test_score_report_explain_stats_on_seeded_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            store = Store(db)
            collect(store, FakeClient(default_routes()), SEEDS[:3], log=lambda _: None)
            store.close()
            args = ["--db", db, "--profile", str(config.PROFILE_EXAMPLE_PATH)]
            self.assertEqual(main(args + ["score"]), 0)
            self.assertEqual(main(args + ["report", "--out", tmp]), 0)
            self.assertTrue((Path(tmp) / "opportunities.html").exists())
            self.assertEqual(main(args + ["explain", "greenhouse:1001"]), 0)
            self.assertEqual(main(args + ["explain", "Product Designer"]), 0)
            self.assertEqual(main(args + ["explain", "no-such-job"]), 1)
            self.assertEqual(main(args + ["stats"]), 0)


if __name__ == "__main__":
    unittest.main()
