"""Ворота ревью и мержа: review.py на подделке gh, без opencode и без сети."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import ProjectCase, aiflow

import review

DIFF = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,3 @@
 keep
-old
+new
+more
"""


def out(stdout: str = "", rc: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


class FakeRunner:
    """Отвечает на вызовы gh из review.main по их аргументам; мержи и публикации записывает."""

    def __init__(self, case):
        self.case = case
        self.cwd = case.root
        self.repo = "o/r"
        self.plain_env: dict = {}
        self.gh_env: dict = {}
        self.pr = {"state": "OPEN", "title": "t", "body": "", "comments": [], "headRefOid": "sha1",
                   "author": {"login": "me"}}
        self.reviews: list[dict] = []
        self.diff = DIFF
        self.files = ["src/a.py"]
        self.checks: list = [[{"name": "build", "bucket": "pass"}]]  # ответы gh pr checks --json по очереди
        self.behind = 0
        self.merges: list[tuple] = []
        self.published: list[dict] = []
        self.labels: list[str] = []

    def gh_run(self, *args, check=True, timeout=180):
        a = [str(x) for x in args]
        if a[:2] == ["api", "user"]:
            return out("me\n")
        if a[:2] == ["pr", "view"]:
            fields = a[a.index("--json") + 1]
            if fields == "mergeable":
                return out(json.dumps({"mergeable": "MERGEABLE"}))
            return out(json.dumps(self.pr))
        if a[:2] == ["pr", "diff"]:
            return out("\n".join(self.files) + "\n" if "--name-only" in a else self.diff)
        if a[:2] == ["pr", "checks"]:
            if "--watch" in a:
                return out()
            cur = self.checks[0] if len(self.checks) == 1 else self.checks.pop(0)
            return out(json.dumps(cur)) if cur else out("", 1, "no checks reported on the 'ai/a' branch")
        if a[:2] == ["pr", "merge"]:
            self.merges.append(tuple(a[2:]))
            return out()
        if a[:2] == ["pr", "edit"]:
            self.labels.append(a[-1])
            return out()
        if a[0] == "label":
            return out()
        path = next((x for x in a if x.startswith("repos/")), "")
        if "/compare/" in path:
            return out(f"{self.behind}\n")
        if path.endswith("/reviews") and "--method" not in a:
            return out("\n".join(json.dumps(x) for x in self.reviews))
        if path.endswith("/reviews"):
            self.published.append(json.loads(review.Path(a[a.index("--input") + 1]).read_text(encoding="utf-8")))
            return out(json.dumps({"id": 1}))
        if a[0] == "api" and a[1] == "graphql":
            return out("{}")
        if a[0] == "api":
            return out("")
        raise AssertionError(f"неожиданный вызов gh: {a}")

    def gh_json(self, *args):
        return json.loads(self.gh_run(*args).stdout or "null")


class ReviewMain(ProjectCase):
    """Целый раунд aiflow review с подделанным gh и ревьюером."""

    def setUp(self):
        super().setUp()
        t = self.add("a")
        t.update(state="in_review", pr=1, plan_approved=True, comments_floor=aiflow.iso())
        self.store.save_task(t)
        self.r = FakeRunner(self)
        self.answer = {"summary": "ок", "comments": [], "threads": []}
        self.reviewer_calls: list[str] = []

        def reviewer(r, pr, rnd, max_rounds, model, brief, tmp, *rest):
            self.reviewer_calls.append(brief)
            return self.answer

        patches = [mock.patch.object(review, "Runner", lambda *a, **k: self.r),
                   mock.patch.object(review, "call_reviewer", reviewer),
                   mock.patch.object(review, "patch_id", lambda r, diff: "pid-" + str(abs(hash(diff)))),
                   mock.patch.object(review.shutil, "which", lambda name: name),
                   mock.patch.object(aiflow, "repo_root", lambda *a, **k: self.root),
                   mock.patch.object(review.time, "sleep", lambda s: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_review(self) -> dict:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            review.main(["--pr", "1"])
        return json.loads(buf.getvalue())

    def last_report(self) -> dict:
        return self.store.report("a") or {}

    def test_clean_review_merges_exactly_the_reviewed_commit(self):
        res = self.run_review()
        self.assertEqual(res["status"], "approved")
        self.assertEqual(len(self.r.merges), 1)
        self.assertIn("--match-head-commit", self.r.merges[0])
        self.assertIn("sha1", self.r.merges[0])
        self.assertEqual(self.last_report()["status"], "approved")
        self.assertTrue(self.store.approved_path("a").exists())

    def test_paused_queue_blocks_review_and_merge(self):
        q = self.store.queue()
        q["pauses"]["integration_red"] = {"at": aiflow.iso(), "until": None}
        self.store.save_queue(q)
        res = self.run_review()
        self.assertEqual(res["status"], "paused")
        self.assertEqual(self.reviewer_calls, [])
        self.assertEqual(self.r.merges, [])
        self.assertEqual(self.last_report()["status"], "paused")

    def test_pause_that_arrives_during_review_blocks_the_merge(self):
        def reviewer(r, pr, rnd, max_rounds, model, brief, tmp, *rest):
            q = self.store.queue()
            q["pauses"]["manual"] = {"at": aiflow.iso(), "until": None}
            self.store.save_queue(q)
            return self.answer

        with mock.patch.object(review, "call_reviewer", reviewer):
            res = self.run_review()
        self.assertEqual(res["status"], "paused")
        self.assertEqual(self.r.merges, [])
        self.assertTrue(self.store.approved_path("a").exists())  # одобрение сохранено: второй раз не ревьюим

    def test_no_checks_is_not_a_pass_when_ci_is_required(self):
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
        aiflow.write_json(self.root / "runs" / "config.json", {"ci_appear_minutes": 0.005})
        self.r.checks = [[]]
        res = self.run_review()
        self.assertEqual(res["status"], "no_ci")
        self.assertIn("ни одной проверки CI", res["message"])
        self.assertEqual(self.last_report()["status"], "no_ci")
        self.assertEqual(self.reviewer_calls, [])
        self.assertEqual(self.r.merges, [])

    def test_checks_that_appear_late_are_waited_for(self):
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
        self.r.checks = [[], [], [{"name": "build", "bucket": "pass"}]]
        self.assertEqual(self.run_review()["status"], "approved")

    def test_default_wait_covers_a_slow_github(self):
        """GitHub создавал запуск CI через 3,5 минуты после push: ждать по умолчанию надо заметно дольше."""
        self.assertGreaterEqual(aiflow.DEFAULT_CONFIG["ci_appear_minutes"], 8)
        self.assertNotIn("ci_appear_seconds", aiflow.DEFAULT_CONFIG)

    def test_repository_without_ci_still_works(self):
        self.r.checks = [[]]
        self.assertEqual(self.run_review()["status"], "approved")

    def test_red_ci_is_capped(self):
        aiflow.write_json(self.root / "runs" / "config.json", {"max_ci_fixes": 2})
        self.r.checks = [[{"name": "build", "bucket": "fail", "link": ""}]]
        self.assertEqual(self.run_review()["status"], "ci_failed")
        self.assertEqual(self.run_review()["status"], "ci_failed")
        res = self.run_review()
        self.assertEqual(res["status"], "limit_reached")
        self.assertEqual(self.last_report()["status"], "limit_reached")
        self.assertEqual(self.reviewer_calls, [])

    def test_green_ci_resets_the_cap(self):
        aiflow.write_json(self.root / "runs" / "config.json", {"max_ci_fixes": 1})
        self.r.checks = [[{"name": "build", "bucket": "fail", "link": ""}]]
        self.assertEqual(self.run_review()["status"], "ci_failed")
        self.r.checks = [[{"name": "build", "bucket": "pass"}]]
        self.answer = {"summary": "плохо", "comments": [{"severity": "blocking", "file": "src/a.py", "line": 2, "text": "x"}]}
        self.assertEqual(self.run_review()["status"], "changes_requested")
        self.r.checks = [[{"name": "build", "bucket": "fail", "link": ""}]]
        self.assertEqual(self.run_review()["status"], "ci_failed")

    def test_unanswered_human_comment_is_capped(self):
        self.r.pr["comments"] = [{"author": {"login": "me"}, "body": "<!-- human -->\nпоправь",
                                  "createdAt": "2099-01-01T00:00:00Z"}]
        for _ in range(review.HUMAN_INPUT_REPEATS):
            self.assertEqual(self.run_review()["status"], "human_input")
        self.assertEqual(self.run_review()["status"], "error")

    def test_branch_behind_base_is_not_merged(self):
        self.r.behind = 2
        res = self.run_review()
        self.assertEqual(res["status"], "merge_conflict")
        self.assertEqual(self.r.merges, [])
        self.assertEqual(len(self.r.published), 1)

    def test_same_diff_after_rebase_is_not_reviewed_again(self):
        self.r.behind = 1
        self.assertEqual(self.run_review()["status"], "merge_conflict")
        self.r.behind = 0
        self.r.pr["headRefOid"] = "sha2"  # rebase: новый коммит, те же изменения
        self.r.reviews = [{"id": 1, "state": "COMMENTED", "body": review.REVIEW_MARKER, "submitted_at": "2026-01-01T00:00:00Z"}]
        res = self.run_review()
        self.assertEqual(res["status"], "approved")
        self.assertTrue(res["reused_approval"])
        self.assertEqual(len(self.reviewer_calls), 1)
        self.assertEqual(len(self.r.published), 1)
        self.assertIn("sha2", self.r.merges[0])

    def test_reused_approval_is_not_stopped_by_the_round_limit(self):
        aiflow.write_json(self.root / "runs" / "config.json", {"max_rounds": 1})
        self.r.behind = 1
        self.assertEqual(self.run_review()["status"], "merge_conflict")
        self.r.behind = 0
        self.r.reviews = [{"id": 1, "state": "COMMENTED", "body": review.REVIEW_MARKER, "submitted_at": "2026-01-01T00:00:00Z"}]
        self.assertEqual(self.run_review()["status"], "approved")

    def test_changed_diff_after_approval_is_reviewed_again(self):
        self.r.behind = 1
        self.run_review()
        self.r.behind = 0
        self.r.diff = DIFF + "+another line\n"
        self.assertEqual(self.run_review()["status"], "approved")
        self.assertEqual(len(self.reviewer_calls), 2)

    def test_round_limit(self):
        aiflow.write_json(self.root / "runs" / "config.json", {"max_rounds": 2})
        self.r.reviews = [{"id": i, "state": "COMMENTED", "body": review.REVIEW_MARKER, "submitted_at": "2026-01-01T00:00:00Z"}
                          for i in (1, 2)]
        self.assertEqual(self.run_review()["status"], "limit_reached")
        self.assertEqual(self.reviewer_calls, [])

    def test_protected_path_blocks_without_the_reviewer(self):
        self.r.files = ["src/a.py", ".github/workflows/ci.yml"]
        res = self.run_review()
        self.assertEqual(res["status"], "changes_requested")
        self.assertEqual(self.reviewer_calls, [])

    def test_task_without_plan_gets_its_text_as_the_contract(self):
        self.patch("a", direct=True, brief="правь src/a.py:2", text="переименуй old в new")
        self.assertEqual(self.run_review()["status"], "approved")
        self.assertIn("переименуй old в new", self.reviewer_calls[0])
        self.assertIn("правь src/a.py:2", self.reviewer_calls[0])

    def test_task_with_plan_has_no_extra_contract(self):
        self.run_review()
        self.assertNotIn("Task without a plan", self.reviewer_calls[0])

    def test_big_change_without_plan_goes_to_a_human(self):
        self.patch("a", direct=True)
        self.r.files = [f"src/f{i}.py" for i in range(7)]
        res = self.run_review()
        self.assertEqual(res["status"], "too_large")
        self.assertEqual(self.reviewer_calls, [])
        self.assertEqual(self.r.merges, [])
        self.assertEqual(self.last_report()["status"], "too_large")
        self.patch("a", size_ok=True)  # человек нажал «Повторить»
        self.assertEqual(self.run_review()["status"], "approved")

    def test_nits_block_only_the_first_round(self):
        self.answer = {"summary": "мелочи", "comments": [{"severity": "nit", "file": "src/a.py", "line": 2, "text": "имя"}]}
        self.assertEqual(self.run_review()["status"], "changes_requested")
        self.r.reviews = [{"id": 1, "state": "COMMENTED", "body": review.REVIEW_MARKER, "submitted_at": "2026-01-01T00:00:00Z"}]
        res = self.run_review()
        self.assertEqual(res["status"], "approved")
        self.assertIn("необязательны", self.r.published[-1]["body"])

    def test_phase_is_written_while_the_round_runs_and_removed_after(self):
        phase_file = self.root / "runs" / "a" / "phase.json"
        seen = []

        def reviewer(r, pr, rnd, max_rounds, model, brief, tmp, *rest):
            seen.append(aiflow.read_json(phase_file))
            return self.answer

        real_gh = self.r.gh_run

        def gh_run(*args, **kw):
            if "--watch" in args:
                seen.append(aiflow.read_json(phase_file))
            if args[:2] == ("pr", "merge"):
                seen.append(aiflow.read_json(phase_file))
            return real_gh(*args, **kw)

        self.r.gh_run = gh_run
        with mock.patch.object(review, "call_reviewer", reviewer):
            self.assertEqual(self.run_review()["status"], "approved")
        self.assertEqual([p["phase"] for p in seen][:3], ["ci", "reviewer", "merge"])
        self.assertEqual(seen[1]["round"], 1)
        self.assertEqual(seen[0]["pid"], review.os.getpid())
        self.assertFalse(phase_file.exists())

    def test_phase_says_when_github_has_not_started_ci_yet(self):
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / ".github" / "workflows" / "ci.yml").write_text("name: CI\n", encoding="utf-8")
        phase_file = self.root / "runs" / "a" / "phase.json"
        seen = []
        self.r.checks = [[], [], [{"name": "build", "bucket": "pass"}]]
        with mock.patch.object(review.time, "sleep", lambda s: seen.append(aiflow.read_json(phase_file)["phase"])):
            self.assertEqual(self.run_review()["status"], "approved")
        self.assertEqual(seen, ["ci_appear", "ci_appear"])

    def test_closed_pr(self):
        self.r.pr["state"] = "MERGED"
        self.assertEqual(self.run_review()["status"], "already_closed")


class Pieces(unittest.TestCase):
    def test_final_verdict(self):
        nit = [{"severity": "nit"}]
        blocking = [{"severity": "blocking"}, {"severity": "nit"}]
        fv = review.final_verdict
        self.assertEqual(fv("approve", [], [], 1, 5, 1), "approve")
        self.assertEqual(fv("changes", nit, [], 1, 5, 1), "changes")
        self.assertEqual(fv("changes", nit, [], 2, 5, 1), "approve")
        self.assertEqual(fv("changes", nit, [], 2, 5, 3), "changes")
        self.assertEqual(fv("changes", nit, [], 5, 5, 9), "approve")  # последний раунд — всегда
        self.assertEqual(fv("changes", nit, [], 1, 1, 1), "approve")
        self.assertEqual(fv("changes", nit, [], 1, 5, 0), "approve")
        self.assertEqual(fv("changes", blocking, [], 5, 5, 1), "changes")
        self.assertEqual(fv("approve", [], [{"comment_id": 1}], 3, 5, 1), "changes")
        self.assertEqual(fv("changes", nit, [{"comment_id": 1}], 3, 5, 1), "changes")

    def test_unknown_severity_is_blocking(self):
        verdict, cs, _ = review.normalize({"comments": [{"severity": "wat", "text": "x"}]}, False)
        self.assertEqual((verdict, cs[0]["severity"]), ("changes", "blocking"))
        verdict, cs, _ = review.normalize({"comments": []}, True)
        self.assertEqual(verdict, "changes")  # большой дифф без подтверждения, что дочитан
        self.assertEqual(review.normalize({"comments": [], "diff_fully_read": True}, True)[0], "approve")

    def test_diff_size_and_limits(self):
        self.assertEqual(review.diff_size(DIFF), 3)
        cfg = {"direct_max_files": 2, "direct_max_lines": 10}
        direct = {"direct": True}
        self.assertEqual(review.too_large(direct, cfg, ["a"], DIFF), "")
        self.assertIn("3 файлов", review.too_large(direct, cfg, ["a", "b", "c"], DIFF))
        self.assertTrue(review.too_large(direct, {"direct_max_files": 9, "direct_max_lines": 2}, ["a"], DIFF))
        self.assertEqual(review.too_large({**direct, "size_ok": True}, cfg, ["a", "b", "c"], DIFF), "")
        self.assertEqual(review.too_large({"plan_approved": True}, cfg, ["a", "b", "c"], DIFF), "")  # с планом предела нет
        self.assertTrue(review.too_large({"self_executed": True}, cfg, ["a", "b", "c"], DIFF))
        self.assertTrue(review.too_large({"complexity": "trivial"}, cfg, ["a", "b", "c"], DIFF))
        self.assertEqual(review.too_large(None, cfg, ["a", "b", "c"], DIFF), "")
        self.assertEqual(review.too_large({**direct, "items": 2}, cfg, ["a", "b", "c"], DIFF), "")  # предел — на пункт
        many = [str(i) for i in range(int(cfg["direct_max_files"]) * review.MAX_SIZE_ITEMS + 1)]
        self.assertTrue(review.too_large({**direct, "items": 50}, cfg, many, DIFF))  # но не безгранично

    def test_wait_checks_outcomes(self):
        def runner(answers):
            answers = list(answers)

            def gh_run(*args, **kw):
                if "--watch" in args:
                    return out()
                cur = answers.pop(0) if len(answers) > 1 else answers[0]
                return out(json.dumps(cur)) if cur else out("", 1, "no checks reported")
            return SimpleNamespace(gh_run=gh_run)

        slept: list = []
        ok = [{"name": "b", "bucket": "pass"}]
        self.assertEqual(review.wait_checks(runner([[]]), 1, 1)[0], "none")
        self.assertEqual(review.wait_checks(runner([[], [], ok]), 1, 1, True, 60, slept.append)[0], "pass")
        self.assertEqual(len(slept), 2)
        self.assertEqual(review.wait_checks(runner([[]]), 1, 1, True, 0, slept.append)[0], "none")
        self.assertEqual(review.wait_checks(runner([[{"name": "b", "bucket": "fail"}]]), 1, 1)[0], "fail")
        self.assertEqual(review.wait_checks(runner([[{"name": "b", "bucket": "pending"}]]), 1, 1)[0], "pending")

    def test_timeout_kills_the_whole_process_tree(self):
        """Обёртка запускает потомка, который держит вывод открытым: таймаут должен убить обоих и вернуться."""
        import subprocess
        import sys
        import time
        child = "import time; time.sleep(120)"
        parent = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}]).wait()"
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            aiflow.run_tree([sys.executable, "-c", parent], timeout=2)
        self.assertLess(time.time() - started, 30)
        ok = aiflow.run_tree([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="hello")
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "HELLO"))

    def test_github_500_is_retried_for_reads_only(self):
        busy = "HTTP 500: Server Error: Sorry, this diff is temporarily unavailable due to heavy server load."
        self.assertTrue(aiflow.GH_NET_ANY.search(busy))
        calls = []

        def call():
            calls.append(1)
            return SimpleNamespace(returncode=0 if len(calls) > 1 else 1, stdout="", stderr=busy)

        with mock.patch.object(aiflow.time, "sleep", lambda s: None):
            self.assertEqual(aiflow.gh_with_retry(call, ["pr", "diff", "1"]).returncode, 0)
            self.assertEqual(len(calls), 2)
            calls.clear()
            self.assertEqual(aiflow.gh_with_retry(call, ["pr", "merge", "1"]).returncode, 1)  # запись не повторяем
            self.assertEqual(len(calls), 1)

    def test_diff_lines(self):
        self.assertEqual(review.diff_lines(DIFF), {"src/a.py": {1, 2, 3}})

    @unittest.skipUnless(shutil.which("git"), "нужен git")
    def test_patch_id_ignores_line_numbers(self):
        r = review.Runner("gh", "opencode", None, review.Path.cwd())
        moved = DIFF.replace("@@ -1,2 +1,3 @@", "@@ -41,2 +41,3 @@")
        self.assertEqual(review.patch_id(r, DIFF), review.patch_id(r, moved))
        self.assertNotEqual(review.patch_id(r, DIFF), review.patch_id(r, DIFF.replace("+more", "+other")))
        self.assertIsNone(review.patch_id(r, "  "))


def ev(kind: str, **part) -> dict:
    return {"type": kind, "timestamp": 1, "sessionID": "ses_test1", "part": part}


# события настоящего прогона opencode 1.19.5 (--format json), сокращённо
PROBE = [
    ev("step_start", type="step-start"),
    ev("tool_use", type="tool", tool="read", state={"status": "completed", "input": {"filePath": "C:\\repo\\src\\Import.cs"}}),
    ev("step_finish", type="step-finish", reason="tool-calls", cost=0.0014,
       tokens={"total": 10683, "input": 8757, "output": 99, "reasoning": 35, "cache": {"write": 0, "read": 1792}}),
    ev("step_start", type="step-start"),
    ev("text", type="text", text='{"summary": "ок", "comments": [], "threads": []}'),
    ev("step_finish", type="step-finish", reason="stop", cost=0.0006,
       tokens={"total": 900, "input": 700, "output": 60, "reasoning": 0, "cache": {"write": 5, "read": 100}}),
]


class Reviewer(ProjectCase):
    """Запуск ревьюера: поток событий opencode, расход, сторож тишины. Вместо opencode — скрипт на Python."""

    def fake_opencode(self, body: str) -> list[str]:
        script = self.tmp / "fake_opencode.py"
        script.write_text("import sys, json, time\nsys.stdin.read()\n" + body, encoding="utf-8")
        return [review.sys.executable, str(script)]

    def emit(self, events: list[dict]) -> str:
        return "".join(f"print({json.dumps(json.dumps(e))}, flush=True)\n" for e in events)

    def test_usage_and_answer_from_events(self):
        u = review.events_usage(PROBE)
        self.assertEqual((u["input"], u["output"], u["cache_read"], u["cache_write"], u["steps"]), (9457, 194, 1892, 5, 2))
        self.assertAlmostEqual(u["cost"], 0.002)
        self.assertEqual(review.extract_json(review.events_text(PROBE))["summary"], "ок")
        self.assertEqual(review.events_text([ev("text", type="text", text="черновик"), *PROBE[1:]])[:1], "{")
        self.assertEqual(review.tool_note(PROBE[1]["part"]), "читает Import.cs")
        self.assertEqual(review.tool_note({"tool": "bash", "state": {"input": {"command": "gh pr diff 7"}}}), "выполняет gh pr diff 7")

    def test_stream_reads_events_as_they_come(self):
        seen = []
        cmd = self.fake_opencode('print("not json")\n' + self.emit(PROBE))
        code, events, other, err = review.stream_events(cmd, env=None, cwd=self.tmp, input="brief", timeout=60, idle=30,
                                                        on_event=seen.append)
        self.assertEqual(code, 0)
        self.assertEqual([e["type"] for e in events], [e["type"] for e in PROBE])
        self.assertEqual(len(seen), len(PROBE))
        self.assertEqual(other, "not json")

    def test_silent_reviewer_is_stopped_early(self):
        cmd = self.fake_opencode(self.emit(PROBE[:2]) + "time.sleep(120)\n")
        started = review.time.time()
        with self.assertRaises(review.ReviewerStalled):
            review.stream_events(cmd, env=None, cwd=self.tmp, input="", timeout=600, idle=2)
        self.assertLess(review.time.time() - started, 40)

    def test_total_timeout_still_applies(self):
        cmd = self.fake_opencode("while True:\n    print(json.dumps({'type': 'step_start', 'part': {}}), flush=True)\n    time.sleep(0.2)\n")
        with self.assertRaises(review.subprocess.TimeoutExpired):
            review.stream_events(cmd, env=None, cwd=self.tmp, input="", timeout=2, idle=30)

    def test_round_records_usage_session_and_phase(self):
        t = self.add("a")
        t.update(state="in_review", pr=7)
        self.store.save_task(t)
        review.CTX.update(root=self.root, task=t, pr=7, round=1, phase=None)
        self.addCleanup(lambda: review.CTX.update(root=None, task=None, pr=None, round=None, phase=None))
        cmd = self.fake_opencode(self.emit(PROBE))
        r = SimpleNamespace(opencode=cmd[0], cwd=self.tmp, plain_env={})
        phases = []
        real_phase, real_popen = review.set_phase, review.subprocess.Popen

        def spy(phase, **detail):
            phases.append((phase, detail))
            real_phase(phase, **detail)

        # вместо «opencode run …» запускаем подделку, остальное (каналы, окружение) — как в жизни
        with mock.patch.object(review, "reviewer_config", lambda r, tmp: None), mock.patch.object(review, "set_phase", spy), \
                mock.patch.object(review.subprocess, "Popen", lambda c, **kw: real_popen(cmd, **kw)):
            data = review.call_reviewer(r, 7, 1, 5, "opencode-go/deepseek", "brief", self.tmp)
        self.assertEqual(data["summary"], "ок")
        runs = aiflow.reviewer_runs(self.store, "a")
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["session"], runs[0]["round"], runs[0]["model"], runs[0]["input"]),
                         ("ses_test1", 1, "opencode-go/deepseek", 9457))
        self.assertAlmostEqual(runs[0]["cost"], 0.002)
        self.assertEqual(phases[0][1]["doing"], "читает Import.cs")
        self.assertEqual(phases[-1][1]["steps"], 2)
        usage = aiflow.reviewer_usage(self.store, "a")
        self.assertEqual(usage["role"], "reviewer")
        self.assertAlmostEqual(usage["models"]["opencode-go/deepseek"]["reported_cost"], 0.002)


class Streak(ProjectCase):
    def test_streak_counts_only_repeats(self):
        (self.root / "runs" / "a").mkdir()
        self.assertEqual(review.bump_streak(self.root, "a", "ci_failed"), 1)
        self.assertEqual(review.bump_streak(self.root, "a", "ci_failed"), 2)
        self.assertEqual(review.bump_streak(self.root, "a", "human_input", "t1"), 1)
        self.assertEqual(review.bump_streak(self.root, "a", "human_input", "t2"), 1)
        review.clear_streak(self.root, "a")
        self.assertEqual(review.bump_streak(self.root, "a", "human_input", "t2"), 1)

    def test_ci_required(self):
        self.assertFalse(review.ci_required({"require_ci": "auto"}, self.root))
        self.assertTrue(review.ci_required({"require_ci": True}, self.root))
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / ".github" / "workflows" / "ci.yaml").write_text("x", encoding="utf-8")
        self.assertTrue(review.ci_required({"require_ci": "auto"}, self.root))
        self.assertTrue(review.ci_required({}, self.root))
        self.assertFalse(review.ci_required({"require_ci": False}, self.root))
        self.assertFalse(review.ci_required({"require_ci": "false"}, self.root))


if __name__ == "__main__":
    unittest.main()
