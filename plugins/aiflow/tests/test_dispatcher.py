"""Переходы очереди: Dispatcher на подделках claude и gh."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest
from unittest import mock

from helpers import PLAN, ProjectCase, ago, aiflow


class Planning(ProjectCase):
    def test_new_task_gets_a_planner_within_concurrency(self):
        for tid in ("a", "b", "c"):
            self.add(tid)
        self.tick()
        self.assertEqual([self.state(x) for x in ("a", "b", "c")], ["planning", "planning", "new"])
        self.assertEqual([x["agent"] for x in self.claude.launched], ["aiflow:planner"] * 2)

    def test_plan_ready_then_label_then_worker(self):
        self.to_plan_review("a", 7)
        self.assertEqual(self.state("a"), "plan_review")
        self.gh.add_label(7, "plan-approved")
        self.tick()
        self.assertEqual(self.state("a"), "in_progress")  # одобрено → «ждёт исполнителя» → слот свободен
        self.assertEqual(self.claude.launched[-1]["agent"], "aiflow:worker")
        history = [h.get("to") for h in self.task("a")["history"]]
        self.assertIn("approved", history)

    def test_approved_waits_for_a_slot(self):
        self.to_plan_review("a", 7)
        self.add("b")
        self.add("c")
        self.tick()  # b и c заняли оба слота планированием
        self.gh.add_label(7, "plan-approved")
        self.tick()
        self.assertEqual(self.state("a"), "approved")
        self.report("b", "planner", "done", summary="готово")
        self.tick()
        self.assertEqual(self.state("a"), "in_progress")

    def test_delegate_starts_a_worker_without_a_plan(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "delegate", summary="мелочь", details="поправь README.md:3")
        self.tick()
        self.assertEqual(self.state("a"), "in_progress")
        self.assertIn("поправь README.md:3", self.claude.launched[-1]["prompt"])

    def test_delegated_trivial_task_goes_to_the_cheapest_worker(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "delegate", summary="опечатка", details="README.md:3", complexity="trivial")
        self.tick()
        self.assertEqual(self.task("a")["complexity"], "trivial")
        last = self.claude.launched[-1]
        self.assertEqual((last["agent"], last["model"], last["effort"]), ("aiflow:worker", "haiku", "high"))

    def test_delegated_task_without_a_level_goes_to_sonnet(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "delegate", summary="мелочь", details="указания")
        self.tick()
        last = self.claude.launched[-1]
        self.assertEqual((last["model"], last["effort"]), ("sonnet", "medium"))

    def test_complex_plan_goes_to_opus_and_simple_plan_to_sonnet(self):
        for tid, pr, cx, want in (("a", 7, "complex", ("opus", "medium")), ("b", 8, "simple", ("sonnet", "medium"))):
            self.add(tid)
            self.tick()
            self.gh.add_pr(pr, tid, body=PLAN)
            self.report(tid, "planner", "plan_ready", pr=pr, complexity=cx)
            self.claude.finish_turn(self.sid(tid, "planner"))
            self.tick()
            self.gh.add_label(pr, "plan-approved")
            self.tick()
            last = self.claude.launched[-1]
            self.assertEqual((last["agent"], last["model"], last["effort"]), ("aiflow:worker", *want), tid)

    def test_trivial_is_not_a_level_for_a_task_with_a_plan(self):
        self.add("a")
        self.tick()
        self.gh.add_pr(7, "a", body=PLAN)
        self.report("a", "planner", "plan_ready", pr=7, complexity="trivial")
        self.tick()
        self.assertEqual(self.task("a")["complexity"], "simple")
        self.assertFalse(aiflow.is_direct(self.task("a")))

    def test_planner_notes_reach_the_worker_and_survive_a_report_without_notes(self):
        self.add("a")
        self.tick()
        self.gh.add_pr(7, "a", body=PLAN)
        self.report("a", "planner", "plan_ready", pr=7, complexity="simple", details="Base commit: abc\n## Code map")
        self.tick()
        notes = self.store.notes_path("a")
        self.assertIn("## Code map", notes.read_text(encoding="utf-8"))
        self.report("a", "planner", "plan_ready", pr=7)  # доработка плана без новых заметок
        self.tick()
        self.assertIn("## Code map", notes.read_text(encoding="utf-8"))
        self.claude.finish_turn(self.sid("a", "planner"))
        self.gh.add_label(7, "plan-approved")
        self.tick()
        self.assertIn(str(notes), self.claude.launched[-1]["prompt"])

    def test_worker_prompt_has_no_notes_line_without_notes(self):
        self.to_plan_review("a", 7)
        self.gh.add_label(7, "plan-approved")
        self.tick()
        self.assertNotIn("notes", self.claude.launched[-1]["prompt"])

    def test_planner_doing_the_task_itself_is_shown_as_work_not_planning(self):
        self.add("a")
        self.tick()
        row = aiflow.build_state(self.store)["tasks"][0]
        self.assertEqual((row["label"], row["stage"]), ("планирование", aiflow.STAGES.index("planning")))
        self.report("a", "planner", "self_execute", summary="переношу кнопку влево")
        events = self.tick()
        self.assertEqual(self.state("a"), "planning")  # итог придёт отчётом ready_for_review
        self.assertIn("self_start", self.kinds(events))
        row = aiflow.build_state(self.store)["tasks"][0]
        self.assertEqual(row["label"], "планировщик делает сам")
        self.assertEqual(row["stage"], aiflow.STAGES.index("in_progress"))
        self.assertEqual(row["activity"], "планировщик делает сам, PR появится по готовности. Что делает: переношу кнопку влево")
        self.gh.add_pr(7, "a", body="Что сделано")
        self.report("a", "planner", "ready_for_review", 7)
        self.claude.finish_turn(self.sid("a", "planner"))
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.assertNotEqual(aiflow.build_state(self.store)["tasks"][0]["label"], "планировщик делает сам")

    def test_planner_that_announced_and_went_silent_is_still_reminded(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "self_execute", summary="делаю")
        self.tick()
        self.claude.finish_turn(self.sid("a", "planner"), minutes_ago=5)
        self.age("a", 5)
        self.tick()
        self.assertEqual(len(self.claude.resumed), 1)
        self.assertEqual(self.state("a"), "planning")

    def test_old_hidden_state_becomes_approved(self):
        """Запись старой версии: plan_review с флагом plan_approved."""
        self.to_plan_review("a", 7)
        self.patch("a", plan_approved=True)
        self.claude.fail_launch = True
        self.tick()
        self.assertEqual(self.state("a"), "approved")

    def test_closed_pr_while_waiting_for_worker_cancels(self):
        self.to_plan_review("a", 7)
        self.claude.fail_launch = True
        self.gh.add_label(7, "plan-approved")
        self.tick()
        self.gh.prs[7]["state"] = "CLOSED"
        self.tick()
        self.assertEqual(self.state("a"), "cancelled")


class ReportChecks(ProjectCase):
    def start(self):
        self.add("a")
        self.tick()

    def test_pr_of_another_branch_is_sent_back_then_to_human(self):
        self.start()
        self.gh.add_pr(7, "a", branch="ai/other")
        for attempt in (1, 2):
            self.report("a", "planner", "plan_ready", 7)
            self.claude.finish_turn(self.sid("a", "planner"))
            events = self.tick()
            self.assertEqual(self.state("a"), "planning", attempt)
            self.assertIn("report_rejected", self.kinds(events))
            self.assertIn("ai/other", self.claude.resumed[-1]["prompt"])
        self.report("a", "planner", "plan_ready", 7)
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertIsNone(self.task("a")["pr"])

    def test_plan_without_marker_is_rejected(self):
        self.start()
        self.gh.add_pr(7, "a", body="просто текст")
        self.report("a", "planner", "plan_ready", 7)
        self.claude.finish_turn(self.sid("a", "planner"))
        self.tick()
        self.assertEqual(self.state("a"), "planning")
        self.assertIn("ai-plan", self.claude.resumed[-1]["prompt"])

    def test_correct_report_after_rejection_is_accepted(self):
        self.start()
        self.gh.add_pr(7, "a", body="нет плана")
        self.report("a", "planner", "plan_ready", 7)
        self.claude.finish_turn(self.sid("a", "planner"))
        self.tick()
        self.gh.prs[7]["body"] = PLAN
        self.report("a", "planner", "plan_ready", 7)
        self.tick()
        self.assertEqual(self.state("a"), "plan_review")

    def test_report_survives_github_outage(self):
        self.start()
        self.gh.add_pr(7, "a")
        self.report("a", "planner", "plan_ready", 7)
        self.gh.down = True
        events = self.tick()
        self.assertEqual(self.state("a"), "planning")
        self.assertIn("error", self.kinds(events))
        self.gh.down = False
        self.tick()
        self.assertEqual(self.state("a"), "plan_review")

    def test_worker_report_for_draft_pr_is_rejected(self):
        self.to_plan_review("a", 7)
        self.gh.add_label(7, "plan-approved")
        self.tick()
        self.gh.prs[7]["isDraft"] = True
        self.report("a", "worker", "ready_for_review", 7)
        self.claude.finish_turn(self.sid("a", "worker"))
        self.tick()
        self.assertEqual(self.state("a"), "in_progress")
        self.assertIn("gh pr ready", self.claude.resumed[-1]["prompt"])
        self.gh.prs[7]["isDraft"] = False
        self.report("a", "worker", "ready_for_review", 7)
        self.tick()
        self.assertIn(self.state("a"), ("ready", "in_review"))


class TurnWithoutReport(ProjectCase):
    def test_planner_is_reminded_once_then_human(self):
        self.add("a")
        self.tick()
        sid = self.sid("a", "planner")
        self.claude.finish_turn(sid, minutes_ago=5)
        events = self.tick()
        self.assertEqual(self.state("a"), "planning")
        self.assertIn("nudged", self.kinds(events))
        self.assertIn("aiflow report a --role planner", self.claude.resumed[-1]["prompt"])
        # снимок сессии ещё старый: сразу к человеку не уходим
        self.claude.finish_turn(sid, minutes_ago=5)
        self.tick()
        self.assertEqual(self.state("a"), "planning")
        self.age("a", 5, "planner")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertEqual(len(self.claude.resumed), 1)

    def test_report_resets_the_reminder(self):
        self.to_plan_review("a", 7)
        self.gh.add_label(7, "plan-approved")
        self.tick()
        sid = self.sid("a", "worker")
        self.claude.finish_turn(sid, minutes_ago=5)
        self.age("a", 5)
        self.tick()
        self.assertEqual(len(self.claude.resumed), 1)
        self.report("a", "worker", "ready_for_review", 7)
        self.tick()
        self.assertNotIn("worker", self.task("a").get("nudged") or {})

    def test_worker_that_cannot_be_woken_goes_to_human(self):
        self.to_plan_review("a", 7)
        self.gh.add_label(7, "plan-approved")
        self.tick()
        self.claude.finish_turn(self.sid("a", "worker"), minutes_ago=5)
        self.age("a", 5)
        self.claude.fail_resume = True
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")


class ReviewQueue(ProjectCase):
    def in_review(self) -> list[str]:
        return sorted(t["id"] for t in self.store.tasks() if t["state"] == "in_review")

    def test_disjoint_prs_are_reviewed_together(self):
        self.to_ready("a", 1, files=["src/a.py"], ready_minutes_ago=3)
        self.to_ready("b", 2, files=["src/b.py"], ready_minutes_ago=2)
        self.tick()
        self.assertEqual(self.in_review(), ["a", "b"])
        self.assertEqual(self.store.queue()["review_locks"], ["a", "b"])

    def test_overlapping_prs_go_one_by_one(self):
        self.to_ready("a", 1, files=["src/a.py", "src/shared.py"], ready_minutes_ago=3)
        self.to_ready("b", 2, files=["src/shared.py"], ready_minutes_ago=2)
        self.tick()
        self.assertEqual(self.in_review(), ["a"])
        self.gh.prs[1]["state"] = "MERGED"
        self.tick()
        self.assertEqual(self.state("a"), "merged")
        self.assertEqual(self.in_review(), ["b"])

    def test_unknown_files_mean_overlap(self):
        self.to_ready("a", 1, files=["a.py"], ready_minutes_ago=3)
        self.to_ready("b", 2, files=["b.py"], ready_minutes_ago=2)
        self.gh.files[2] = None
        self.tick()
        self.assertEqual(self.in_review(), ["a"])

    def test_limit_of_one_keeps_strict_order(self):
        aiflow.write_json(self.root / "runs" / "config.json", {"review_concurrency": 1})
        self.to_ready("a", 1, files=["a.py"], ready_minutes_ago=3)
        self.to_ready("b", 2, files=["b.py"], ready_minutes_ago=2)
        self.tick()
        self.assertEqual(self.in_review(), ["a"])

    def test_later_pr_does_not_jump_over_an_earlier_one_it_touches(self):
        aiflow.write_json(self.root / "runs" / "config.json", {"review_concurrency": 3})
        self.to_ready("a", 1, files=["a.py"], ready_minutes_ago=5)
        self.to_ready("b", 2, files=["a.py", "b.py"], ready_minutes_ago=4)  # ждёт a
        self.to_ready("c", 3, files=["b.py"], ready_minutes_ago=3)  # не задевает a, но задевает b
        self.to_ready("d", 4, files=["d.py"], ready_minutes_ago=2)
        self.tick()
        self.assertEqual(self.in_review(), ["a", "d"])

    def test_session_still_working_is_not_woken(self):
        self.to_ready("a", 1, files=["a.py"])
        self.claude.sessions["w-a"]["state"] = "working"
        self.tick()
        self.assertEqual(self.state("a"), "ready")
        self.assertEqual(self.claude.resumed, [])

    def test_paused_queue_takes_nothing(self):
        self.to_ready("a", 1, files=["a.py"])
        self.store.command({"cmd": "pause"})
        self.tick()
        self.assertEqual(self.state("a"), "ready")


class InReview(ProjectCase):
    def start(self) -> None:
        self.to_ready("a", 1, files=["a.py"])
        self.tick()
        self.assertEqual(self.state("a"), "in_review")

    def test_merged_pr_finishes_the_task(self):
        self.start()
        self.gh.prs[1]["state"] = "MERGED"
        events = self.tick()
        self.assertEqual(self.state("a"), "merged")
        self.assertIn("merged", self.kinds(events))
        self.assertEqual(self.store.queue()["review_locks"], [])

    def test_approved_but_not_merged_frees_the_queue(self):
        self.start()
        self.to_ready("b", 2, files=["a.py"])
        self.report("a", "worker", "approved", 1)
        self.claude.finish_turn("w-a")
        self.tick()
        self.assertEqual(self.state("a"), "in_review")  # ждём мержа
        self.patch("a", approved_at=ago(61))
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertIn("одобрен, но не смержен", self.task("a")["human_reason"])
        self.assertEqual(self.state("b"), "in_review")  # очередь ревью пошла дальше
        self.gh.prs[1]["state"] = "MERGED"
        self.tick()
        self.assertEqual(self.state("a"), "merged")

    def test_failed_ci_after_approval_wakes_the_worker_once(self):
        self.start()
        self.report("a", "worker", "approved", 1)
        self.claude.finish_turn("w-a")
        self.gh.prs[1]["statusCheckRollup"] = [{"name": "build", "conclusion": "FAILURE"}]
        self.tick()
        self.tick()
        fixes = [r for r in self.claude.resumed if "CI checks failed" in r["prompt"]]
        self.assertEqual(len(fixes), 1)

    def test_paused_review_returns_to_the_queue(self):
        self.start()
        self.store.command({"cmd": "pause"})
        self.report("a", "worker", "paused", 1, summary="очередь на паузе")
        self.claude.finish_turn("w-a")
        self.tick()
        self.assertEqual(self.state("a"), "ready")
        self.assertEqual(self.store.queue()["review_locks"], [])
        self.store.command({"cmd": "resume"})
        self.tick()
        self.assertEqual(self.state("a"), "in_review")

    def activity(self, tid: str = "a") -> str | None:
        sessions = self.claude.agents()
        # фаза в тестах «записана» раньше, чем стартовал сам тест: не считаем её из-за этого чужой
        with mock.patch.object(aiflow, "process_start", lambda pid: None):
            return self._activity(tid, sessions)

    def _activity(self, tid: str, sessions: dict) -> str | None:
        return next(t for t in aiflow.build_state(self.store, sessions)["tasks"] if t["id"] == tid)["activity"]

    def phase(self, kind: str, minutes_ago: float = 0, **detail) -> None:
        import os
        aiflow.write_json(self.root / "runs" / "a" / "phase.json",
                          {"pid": os.getpid(), "at": ago(minutes_ago), "phase": kind, **detail})

    def test_activity_tells_ci_from_the_reviewer(self):
        self.to_ready("a", 1, files=["a.py"])
        self.gh.prs[1]["statusCheckRollup"] = [{"name": "backend", "conclusion": "SUCCESS"},
                                               {"name": "frontend", "conclusion": "SUCCESS"},
                                               {"name": "analysis", "status": "IN_PROGRESS", "conclusion": None}]
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.assertEqual(self.task("a")["ci"]["passed"], 2)
        self.assertEqual(self.activity(), "исполнитель: слияние с базой, тесты или правки")  # aiflow review ещё не запущен
        self.phase("ci_appear")
        self.assertEqual(self.activity(), "ждёт, пока GitHub запустит CI")
        self.phase("ci", minutes_ago=4)
        self.assertEqual(self.activity(), "ждёт CI: прошло 2 из 3 (4 мин)")
        self.phase("reviewer", minutes_ago=7, round=2)
        self.assertEqual(self.activity(), "ревьюер opencode, раунд 2/5 (7 мин)")
        self.phase("merge")
        self.assertEqual(self.activity(), "мерж")

    def test_phase_of_a_dead_review_process_is_ignored(self):
        self.start()
        aiflow.write_json(self.root / "runs" / "a" / "phase.json", {"pid": 0, "at": ago(1), "phase": "reviewer", "round": 1})
        self.assertEqual(self.activity(), "исполнитель: слияние с базой, тесты или правки")

    def test_activity_for_failed_ci_and_for_the_queue(self):
        self.to_ready("a", 1, files=["a.py"])
        self.to_ready("b", 2, files=["a.py"])
        self.gh.prs[1]["statusCheckRollup"] = [{"name": "backend", "conclusion": "FAILURE"}, {"context": "lint", "state": "SUCCESS"}]
        self.gh.prs[2]["statusCheckRollup"] = [{"name": "backend", "conclusion": "SUCCESS"}]
        self.tick()
        self.tick()
        self.phase("ci")
        self.assertEqual(self.activity("a"), "ждёт CI: прошло 1 из 2, упало 1")
        self.assertEqual(self.activity("b"), "ждёт ревью a: общие файлы; CI: прошло 1 из 1")
        self.assertEqual(aiflow.ci_summary(None)["total"], 0)

    def test_missing_ci_gets_its_own_advice(self):
        self.start()
        self.report("a", "worker", "no_ci", 1, summary="за 10 мин после push на PR не появилось ни одной проверки CI")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertIn("Actions", self.task("a")["human_action"])
        self.assertNotIn("opencode", self.task("a")["human_action"])

    def test_review_turn_without_result_is_reminded_first(self):
        self.start()
        self.claude.finish_turn("w-a", minutes_ago=10)
        self.age("a", 10)
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.assertIn("aiflow review --pr 1", self.claude.resumed[-1]["prompt"])

    def test_busy_session_without_progress_is_a_stall(self):
        """Сессия закончила ход и ждёт фоновую команду, которая зависла: числится занятой, ревью стоит."""
        self.start()
        self.assertEqual(self.claude.sessions["w-a"]["state"], "working")
        self.age("a", 90)
        self.tick()
        self.assertEqual(self.state("a"), "in_review")  # ожидание CI и ревьюера столько длиться может
        self.age("a", 106)
        self.to_ready("b", 2, files=["a.py"])
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertIn("не двигается", self.task("a")["human_reason"])
        self.assertIn("зависла", self.task("a")["human_action"])
        self.assertEqual(self.state("b"), "in_review")  # очередь ревью освободилась

    def test_recent_transcript_activity_is_not_a_stall(self):
        self.start()
        self.age("a", 300)
        folder = self.tmp / "claude" / "projects" / "p"
        folder.mkdir(parents=True)
        (folder / "uuid-w-a.jsonl").write_text("{}\n", encoding="utf-8")  # сессия только что писала в транскрипт
        self.tick()
        self.assertEqual(self.state("a"), "in_review")

    def test_amendment_drops_the_stored_approval(self):
        self.start()
        self.claude.finish_turn("w-a")
        approved = self.store.approved_path("a")
        aiflow.write_json(approved, {"patch_id": "abc"})
        self.store.command({"cmd": "amend", "id": "a", "text": "ещё кнопку"})
        self.tick()
        self.assertEqual(self.state("a"), "in_progress")
        self.assertFalse(approved.exists())
        self.assertEqual(self.store.queue()["review_locks"], [])


class Retry(ProjectCase):
    def test_direct_task_with_pr_gets_a_new_worker_on_the_same_branch(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "delegate", summary="мелочь", details="указания")
        self.tick()
        self.gh.add_pr(5, "a", body="Что сделано")
        self.patch("a", pr=5)
        self.report("a", "worker", "blocked", summary="не могу")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.claude.fail_resume = True  # прежнюю сессию не вернуть
        self.store.command({"cmd": "retry", "id": "a"})
        events = self.tick()
        self.assertIn("retry", self.kinds(events))
        self.assertEqual(self.state("a"), "in_progress")
        prompt = self.claude.launched[-1]["prompt"]
        self.assertIn("already has PR #5", prompt)
        self.assertIn("There is no plan", prompt)
        self.assertNotIn("The plan is in the PR description", prompt)

    def test_retry_without_pr_starts_over(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "blocked", summary="нет доступа")
        self.tick()
        self.claude.fail_resume = True
        self.store.command({"cmd": "retry", "id": "a"})
        self.tick()
        self.assertEqual(self.state("a"), "planning")
        self.assertEqual(len(self.claude.launched), 2)

    def test_retry_resumes_the_same_session_when_possible(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "blocked", summary="нет доступа")
        self.tick()
        self.store.command({"cmd": "retry", "id": "a"})
        self.tick()
        self.assertEqual(self.state("a"), "planning")
        self.assertEqual(len(self.claude.launched), 1)
        self.assertEqual(len(self.claude.resumed), 1)


class NeedsHuman(ProjectCase):
    def test_report_written_while_waiting_does_not_fire_after_retry(self):
        """Сессия отчиталась об ошибке, пока задача ждала человека: после «Повторить» это не новость."""
        self.to_ready("a", 1, files=["a.py"])
        self.tick()
        self.patch("a", review_retried=True)
        self.report("a", "worker", "error", 1, summary="ревьюер не вернул валидный JSON")
        self.claude.finish_turn("w-a")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.report("a", "worker", "error", 1, summary="та же ошибка ещё раз")  # задача уже ждёт человека
        self.patch("a", human_watch=None)  # человек сессии не отвечал
        self.store.command({"cmd": "retry", "id": "a"})
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.tick()
        self.assertEqual(self.state("a"), "in_review")  # старый отчёт не вернул задачу к человеку
        self.patch("a", review_retried=True)  # иначе новый сбой aiflow сначала повторит сам
        self.report("a", "worker", "error", 1, summary="новая ошибка")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertEqual(self.task("a")["human_reason"], "новая ошибка")

    def to_human_with_working_session(self) -> None:
        """Задача на ревью ушла к человеку, а её сессия продолжает работать."""
        self.to_ready("a", 1, files=["a.py"])
        self.tick()
        self.patch("a", review_retried=True)
        self.report("a", "worker", "error", 1, summary="сбой ревьюера")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertEqual(self.claude.sessions["w-a"]["state"], "working")
        self.patch("a", human_watch=None)

    def test_retry_with_a_working_session_goes_straight_back_to_review(self):
        self.to_human_with_working_session()
        woken = len(self.claude.resumed)
        self.store.command({"cmd": "retry", "id": "a"})
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.assertEqual(self.store.queue()["review_locks"], ["a"])
        self.assertEqual(len(self.claude.resumed), woken)  # работающую сессию не будим
        self.to_ready("b", 2, files=["a.py"])  # PR с теми же файлами ждёт: место занято
        self.tick()
        self.assertEqual(self.state("b"), "ready")

    def test_retry_with_a_working_session_waits_when_review_is_taken(self):
        self.to_human_with_working_session()
        self.to_ready("b", 2, files=["a.py"])
        self.tick()
        self.assertEqual(self.state("b"), "in_review")
        self.store.command({"cmd": "retry", "id": "a"})
        self.tick()
        self.assertEqual(self.state("a"), "ready")  # те же файлы на ревью — в очередь
        self.assertEqual(self.store.queue()["review_locks"], ["b"])

    def test_panel_says_which_review_a_pr_with_shared_files_waits_for(self):
        self.to_ready("a", 1, files=["a.py", "x.py"])
        self.tick()
        self.to_ready("b", 2, files=["a.py"])
        self.tick()
        self.assertEqual(self.state("b"), "ready")
        row = next(r for r in aiflow.build_state(self.store)["tasks"] if r["id"] == "b")
        self.assertEqual(row["activity"], "ждёт ревью a: общие файлы")

    def test_review_tool_failure_is_retried_once_before_a_human(self):
        self.to_ready("a", 1, files=["a.py"])
        self.tick()
        self.report("a", "worker", "error", 1, summary="ревьюер не вернул валидный JSON")
        events = self.tick()
        self.assertEqual(self.state("a"), "ready")
        self.assertIn("retry", self.kinds(events))
        self.claude.finish_turn("w-a")
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.report("a", "worker", "error", 1, summary="ревьюер не вернул валидный JSON")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")

    def test_stale_shell_directory_is_not_passed_to_child_programs(self):
        self.assertNotIn("PWD", aiflow.os.environ)

    def test_reason_is_one_line_and_says_what_to_do(self):
        self.to_ready("a", 1, files=["a.py"])
        self.tick()
        self.patch("a", review_retried=True)
        self.report("a", "worker", "error", 1, summary="ревьюер не вернул валидный JSON. Конец вывода:\n(пусто)\n"
                                                       "stderr opencode: ! permission requested " + "x" * 400)
        events = self.tick()
        t = self.task("a")
        self.assertEqual(t["human_reason"], "ревьюер не вернул валидный JSON. Конец вывода:")
        self.assertIn("сбой инструмента", t["human_action"])
        text = next(e["text"] for e in events if e["kind"] == "needs_human")
        self.assertIn("Что сделать:", text)
        self.assertNotIn("permission requested", text)
        row = aiflow.build_state(self.store)["tasks"][0]
        self.assertEqual(row["action"], t["human_action"])

    def test_each_cause_has_its_own_advice(self):
        advice = {}
        for status, summary in (("limit_reached", "лимит раундов исчерпан"), ("limit_reached", "CI красный 5 раз подряд"),
                                ("blocked", "нужен доступ к базе")):
            tid = f"t{len(advice)}"
            self.to_ready(tid, 10 + len(advice), files=[f"{tid}.py"])
            self.tick()
            self.report(tid, "worker", status, 10 + len(advice), summary=summary)
            self.tick()
            advice[summary] = self.task(tid)["human_action"]
        self.assertEqual(len(set(advice.values())), 3)
        self.assertIn("не договорились", advice["лимит раундов исчерпан"])
        self.assertIn("CI", advice["CI красный 5 раз подряд"])
        self.assertIn("ответьте ей", advice["нужен доступ к базе"])

    def test_protected_path_reason_points_to_the_button(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "blocked", summary="нужно менять защищённый путь .github/workflows/ci.yml")
        self.tick()
        self.assertIn("Разрешить пути", self.task("a")["human_action"])

    def test_retry_clears_the_advice(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "blocked", summary="нет доступа")
        self.tick()
        self.assertTrue(self.task("a")["human_action"])
        self.store.command({"cmd": "retry", "id": "a"})
        self.tick()
        self.assertNotIn("human_action", self.task("a"))

    def test_short_reason(self):
        self.assertEqual(aiflow.short_reason("\n\n  первая строка \nвторая"), "первая строка")
        self.assertEqual(len(aiflow.short_reason("я" * 500)), 220)
        self.assertEqual(aiflow.short_reason(None), "")


class TaskDates(ProjectCase):
    def test_state_gives_the_creation_time_and_the_last_change(self):
        self.add("a")
        self.patch("a", created_at=ago(120), state_at=ago(120), history=[{"at": ago(120), "event": "created"}])
        row = aiflow.build_state(self.store)["tasks"][0]
        self.assertEqual(row["created_at"], row["updated_at"])
        self.tick()  # задача ушла в планирование: обновление — сейчас, создание — прежнее
        row = aiflow.build_state(self.store)["tasks"][0]
        self.assertEqual(row["created_at"], self.task("a")["created_at"])
        self.assertGreater(aiflow.parse_iso(row["updated_at"]), aiflow.parse_iso(row["created_at"]))

    def test_an_amendment_counts_as_an_update(self):
        t = {"created_at": ago(60), "state_at": ago(50), "amendments": [{"at": ago(5)}]}
        self.assertEqual(aiflow.task_updated_at(t), t["amendments"][0]["at"])

    def test_status_table_shows_both_dates(self):
        self.add("a")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            aiflow.print_status(aiflow.build_state(self.store))
        head = out.getvalue().splitlines()[1]
        self.assertIn("создана", head)
        self.assertIn("обновлена", head)
        self.assertEqual(aiflow.fmt_when(None), "—")


class Notify(unittest.TestCase):
    def test_default_tells_the_orchestrator_only_answers(self):
        cfg = aiflow.DEFAULT_CONFIG
        self.assertTrue(aiflow.should_notify(cfg, "answered"))
        for kind in ("plan_ready", "needs_human", "merged", "cancelled", "error"):
            self.assertFalse(aiflow.should_notify(cfg, kind), kind)
        self.assertFalse(aiflow.notify_status(cfg))

    def test_levels(self):
        self.assertTrue(aiflow.should_notify({"notify": "important"}, "needs_human"))
        self.assertFalse(aiflow.should_notify({"notify": "important"}, "started"))
        self.assertTrue(aiflow.notify_status({"notify": "important"}))
        self.assertTrue(aiflow.should_notify({"notify": "all"}, "started"))
        self.assertFalse(aiflow.should_notify({"notify": "none"}, "answered"))
        self.assertFalse(aiflow.notify_status({"notify": "none"}))
        self.assertEqual(aiflow.notify_level({"notify": "что-то"}), "answers")


class SessionTab(unittest.TestCase):
    def test_title_is_the_task_name_and_safe_for_the_terminal(self):
        import aiflow_ui
        title = aiflow_ui.session_title({"id": "csv-export", "title": "Экспорт; в \"CSV\" & <b>"}, "abc123")
        self.assertTrue(title.startswith("csv-export: Экспорт"))
        for ch in ';"&<>|':
            self.assertNotIn(ch, title)
        self.assertEqual(aiflow_ui.session_title({"id": "a", "title": "a"}, "s1"), "a")
        self.assertEqual(aiflow_ui.session_title(None, "s1"), "aiflow s1")
        self.assertLessEqual(len(aiflow_ui.session_title({"id": "a", "title": "я" * 200}, "s1")), 60)


class Pauses(ProjectCase):
    def pauses(self) -> list[str]:
        return sorted(self.store.queue()["pauses"])

    def test_reasons_do_not_overwrite_each_other(self):
        self.store.command({"cmd": "pause"})
        self.tick()
        self.add("a")
        self.tick()
        self.assertEqual(self.state("a"), "new")
        q = self.store.queue()
        q["pauses"]["rate_limit"] = {"at": ago(40), "until": ago(10), "note": ""}
        self.store.save_queue(q)
        events = self.tick()
        self.assertEqual(self.pauses(), ["manual"])  # лимит истёк — ручная пауза осталась
        self.assertNotIn("unpaused", self.kinds(events))
        self.assertEqual(self.state("a"), "new")

    def test_red_base_branch_pauses_and_green_resumes(self):
        self.gh.push_run = {"status": "completed", "conclusion": "failure", "url": "u"}
        self.add("a")
        events = self.tick()
        self.assertEqual(self.pauses(), ["integration_red"])
        self.assertIn("paused", self.kinds(events))
        self.assertEqual(self.state("a"), "new")
        self.assertEqual(self.kinds(self.tick()).count("paused"), 0)  # не повторяем каждый проход
        self.store.command({"cmd": "resume"})
        self.tick()
        self.assertEqual(self.pauses(), ["integration_red"])  # ручное «продолжить» красную ветку не снимает
        self.gh.push_run = {"status": "completed", "conclusion": "success"}
        events = self.tick()
        self.assertEqual(self.pauses(), [])
        self.assertIn("unpaused", self.kinds(events))
        self.assertEqual(self.state("a"), "planning")

    def test_manual_resume_with_another_reason_left(self):
        self.gh.push_run = {"status": "completed", "conclusion": "failure"}
        self.store.command({"cmd": "pause"})
        self.tick()
        self.assertEqual(self.pauses(), ["integration_red", "manual"])
        self.store.command({"cmd": "resume"})
        events = self.tick()
        self.assertEqual(self.pauses(), ["integration_red"])
        self.assertIn("pause_kept", self.kinds(events))

    def test_queue_file_of_the_old_version(self):
        aiflow.write_json(self.root / "runs" / "queue.json",
                          {"paused": {"reason": "manual", "at": ago(5), "until": None}, "review_lock": "x"})
        q = self.store.queue()
        self.assertEqual(sorted(q["pauses"]), ["manual"])
        self.assertEqual(q["review_locks"], ["x"])
        self.assertEqual(q["paused"]["reason"], "manual")

    def test_primary_pause_for_the_panel(self):
        q = aiflow.sync_queue({"pauses": {"rate_limit": {"at": ago(9), "until": ago(-20)},
                                          "manual": {"at": ago(1), "until": None}}, "review_locks": ["a", "b"]})
        self.assertEqual(q["paused"]["reason"], "manual")
        self.assertEqual(q["paused"]["reasons"], ["manual", "rate_limit"])
        self.assertEqual(q["review_lock"], "a")
        self.assertIsNone(aiflow.sync_queue({"pauses": {}})["paused"])


class RateLimit(ProjectCase):
    def start(self) -> str:
        self.add("a")
        self.tick()
        return self.sid("a", "planner")

    def test_stopped_session_with_limit_text_pauses_the_queue(self):
        sid = self.start()
        self.claude.log_text = "You've hit your usage limit. Resets at 5pm."
        self.claude.finish_turn(sid, "stopped")
        self.tick()
        self.assertIn("rate_limit", self.store.queue()["pauses"])
        self.assertEqual(self.state("a"), "planning")

    def test_blocked_session_still_tells_the_user(self):
        sid = self.start()
        self.claude.log_text = "the rate limit middleware returns 429"
        self.claude.sessions[sid].update(state="blocked", waitingFor="разрешение на команду")
        events = self.tick()
        self.assertIn("needs_input", self.kinds(events))

    def test_blocked_session_of_a_finished_task_does_not_wait_for_the_user(self):
        sid = self.start()
        live = {sid: {"state": "blocked", "waitingFor": "ответ"}}
        self.assertIn("ждёт ответа", aiflow.build_state(self.store, live)["tasks"][0]["needs"])
        t = self.task("a")
        t["state"] = "merged"
        self.store.save_task(t)
        self.assertIsNone(aiflow.build_state(self.store, live)["tasks"][0]["needs"])

    def test_words_far_above_the_end_of_the_log_are_ignored(self):
        sid = self.start()
        self.claude.log_text = "реализуем rate limit для API\n" + "x" * 5000
        self.claude.finish_turn(sid, "stopped")
        self.tick()
        self.assertEqual(self.store.queue()["pauses"], {})
        self.assertEqual(len(self.claude.resumed), 1)  # обычное автовозобновление

    def test_same_session_cannot_pause_forever(self):
        sid = self.start()
        self.claude.log_text = "rate limit"
        self.claude.finish_turn(sid, "stopped")
        for _ in range(aiflow.LIMIT_HITS_MAX):
            self.tick()
            q = self.store.queue()
            self.assertIn("rate_limit", q["pauses"])
            q["pauses"] = {}
            self.store.save_queue(q)
        self.tick()
        self.assertEqual(self.store.queue()["pauses"], {})
        self.assertEqual(len(self.claude.resumed), 1)

    def test_bare_number_is_not_a_limit(self):
        self.assertIsNone(aiflow.LIMIT_RE.search("порт 4290, код 429 в таблице"))
        self.assertIsNotNone(aiflow.LIMIT_RE.search("API Error: 429 too many"))
        self.assertIsNotNone(aiflow.LIMIT_RE.search("usage limit reached"))


class Dependencies(ProjectCase):
    def test_dependent_waits_for_merge(self):
        self.add("a")
        self.add("b", depends_on=["a"])
        self.tick()
        self.assertEqual(self.state("b"), "new")
        self.patch("a", state="merged", cleaned=True)
        self.tick()
        self.assertEqual(self.state("b"), "planning")

    def test_cancelled_dependency_is_reported(self):
        self.add("a")
        self.add("b", depends_on=["a"])
        self.patch("a", state="cancelled", cleaned=True)
        events = self.tick()
        self.assertEqual(self.state("b"), "needs_human")
        self.assertIn("needs_human", self.kinds(events))
        self.assertIn("a", self.task("b")["human_reason"])

    def test_cycle_detection(self):
        mk = lambda i, deps: {"id": i, "depends_on": deps}  # noqa: E731
        self.assertEqual(aiflow.dependency_cycle([mk("a", ["b"]), mk("b", ["c"]), mk("c", [])]), [])
        self.assertEqual(aiflow.dependency_cycle([mk("a", ["a"])]), ["a", "a"])
        cycle = aiflow.dependency_cycle([mk("a", ["b"]), mk("b", ["c"]), mk("c", ["a"]), mk("d", ["x"])])
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle), {"a", "b", "c"})

    def enqueue(self, items, separate=True) -> None:
        path = self.tmp / "tasks.json"
        path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            aiflow.cmd_enqueue(argparse.Namespace(file=str(path), separate=separate), self.root)

    def test_several_tasks_in_one_call_need_the_separate_flag(self):
        two = [{"id": "x1", "text": "one"}, {"id": "x2", "text": "two"}]
        with self.assertRaisesRegex(aiflow.AiflowError, "numbered list"):
            self.enqueue(two, separate=False)
        self.assertIsNone(self.store.task("x1"))
        self.enqueue(two)
        self.assertIsNotNone(self.store.task("x2"))
        self.enqueue([{"id": "x3", "text": "alone"}], separate=False)
        self.assertIsNotNone(self.store.task("x3"))

    def test_enqueue_rejects_a_cycle_and_a_cancelled_dependency(self):
        with self.assertRaisesRegex(aiflow.AiflowError, "цикл"):
            self.enqueue([{"id": "a", "text": "t", "depends_on": ["b"]}, {"id": "b", "text": "t", "depends_on": ["a"]}])
        self.assertEqual(self.store.tasks(), [])
        self.add("old")
        self.patch("old", state="cancelled")
        with self.assertRaisesRegex(aiflow.AiflowError, "снята"):
            self.enqueue([{"id": "n", "text": "t", "depends_on": ["old"]}])
        self.enqueue([{"id": "x", "text": "t"}, {"id": "y", "text": "t", "depends_on": ["x"]}])
        self.assertEqual(sorted(t["id"] for t in self.store.tasks()), ["old", "x", "y"])


class Cost(ProjectCase):
    M = 1_000_000

    def test_price_depends_on_model_and_cache(self):
        table = aiflow.price_table()
        u = {"input": self.M, "output": self.M, "cache_read": self.M, "cache_write": self.M, "cache_write_1h": 0}
        # sonnet 5.5: вход 2, выход 10, чтение кэша 0.10, запись на 5 минут 2 × 1.25
        self.assertAlmostEqual(aiflow.usage_cost("claude-sonnet-5-5", u, table), 2 + 10 + 0.10 + 2.5)
        # opus 5.5: 4, 20, 0.20, запись 4 × 1.25
        self.assertAlmostEqual(aiflow.usage_cost("claude-opus-5-5", u, table), 4 + 20 + 0.20 + 5)
        hour = {**u, "cache_write_1h": self.M}  # вся запись — на час: × 2
        self.assertAlmostEqual(aiflow.usage_cost("claude-sonnet-5-5", hour, table), 2 + 10 + 0.10 + 4)
        half = {**u, "cache_write_1h": self.M // 2}
        self.assertAlmostEqual(aiflow.usage_cost("claude-sonnet-5-5", half, table), 2 + 10 + 0.10 + 1.25 + 2)

    def test_model_is_matched_by_the_longest_prefix(self):
        table = aiflow.price_table()
        self.assertEqual(aiflow.model_price("claude-opus-5-5", table)[0], 4.0)
        self.assertEqual(aiflow.model_price("claude-opus-5", table)[0], 5.0)
        self.assertEqual(aiflow.model_price("claude-sonnet-5-5[1m]", table)[0], 2.0)
        self.assertEqual(aiflow.model_price("claude-opus-4-8", table)[0], 5.0)
        self.assertIsNone(aiflow.model_price("deepseek-chat", table))
        self.assertIsNone(aiflow.usage_cost("?", {"input": 5}, table))

    def test_own_prices_from_the_global_config(self):
        aiflow.write_json(aiflow.aiflow_home() / "config.json",
                          {"prices": {"deepseek": {"input": 1, "output": 2}, "claude-opus-5-5": {"input": 40, "output": 0,
                                                                                                "cache_read": 0}, "bad": 5}})
        table = aiflow.price_table()
        self.assertEqual(table["deepseek"], (1.0, 2.0, 0.1))
        self.assertAlmostEqual(aiflow.usage_cost("claude-opus-5-5", {"input": self.M}, table), 40)
        self.assertNotIn("bad", table)

    def test_session_in_two_roles_is_counted_once(self):
        """Планировщик сделал задачу сам: его сессия записана и как исполнитель — расход не должен обнулиться."""
        folder = self.tmp / "claude" / "projects" / "p"
        folder.mkdir(parents=True)
        msg = {"message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 1000, "output_tokens": 500}}}
        (folder / "uuid-s1.jsonl").write_text(json.dumps(msg), encoding="utf-8")
        self.add("a")
        self.tick()
        self.gh.add_pr(7, "a", body="Что сделано")
        self.report("a", "planner", "ready_for_review", 7)
        self.claude.finish_turn(self.sid("a", "planner"))
        self.tick()
        self.tick()
        self.assertEqual(self.task("a")["sessions"]["worker"][-1]["id"], self.sid("a", "planner"))
        row = aiflow.build_state(self.store)["tasks"][0]
        self.assertEqual(row["usage"]["claude-opus-5-5"]["output"], 500)
        self.assertEqual(row["tokens"], 1500)

    def test_task_and_project_totals(self):
        new = {"input": self.M, "output": 0, "cache_read": 0, "cache_write": 0, "cache_write_1h": 0}
        old = {"input": 0, "output": 0, "cache_read": 0, "cache_write": self.M}  # запись старой версии, без разбивки
        self.add("a", usage={"s1": {"role": "worker", "model": "claude-sonnet-5-5",
                                    "models": {"claude-sonnet-5-5": new, "mystery-model": new}}})
        self.add("b", usage={"s2": {"role": "planner", "model": "claude-opus-5-5", "models": {"claude-opus-5-5": old}},
                             "s3": {"role": "worker", "model": "claude-sonnet-5-5", "models": {"claude-sonnet-5-5": new}}})
        st = aiflow.build_state(self.store)
        a, b = st["tasks"]
        self.assertAlmostEqual(a["cost"], 2.0)  # модель без цены в сумму не входит
        self.assertIsNone(a["usage"]["mystery-model"]["cost"])
        self.assertAlmostEqual(b["cost"], 8.0 + 2.0)  # старая запись кэша считается часовой: 4 × 2
        self.assertAlmostEqual(st["cost"], 12.0)
        self.assertEqual(st["unpriced"], ["mystery-model"])
        self.assertEqual(st["usage"]["claude-sonnet-5-5"]["input"], 2 * self.M)
        self.assertAlmostEqual(st["usage"]["claude-sonnet-5-5"]["cost"], 4.0)


class ReviewerUsage(ProjectCase):
    def run_of(self, **kw) -> dict:
        return {"session": "ses_1", "round": 1, "model": "opencode-go/deepseek", "input": 1000, "output": 200,
                "cache_read": 5000, "cache_write": 0, "cost": 0.02, **kw}

    def test_reviewer_spend_joins_the_task_and_project_totals(self):
        self.to_ready("a", 1, files=["a.py"])
        aiflow.write_json(self.root / "runs" / "a" / "reviewer.json",
                          {"runs": [self.run_of(), self.run_of(session="ses_2", round=2, cost=0.03)]})
        self.tick()
        st = aiflow.build_state(self.store)
        task = st["tasks"][0]
        self.assertAlmostEqual(task["usage"]["opencode-go/deepseek"]["cost"], 0.05)  # цена — от opencode, не из прайса
        self.assertEqual(task["usage"]["opencode-go/deepseek"]["input"], 2000)
        self.assertAlmostEqual(task["cost"], 0.05)
        self.assertAlmostEqual(st["cost"], 0.05)
        self.assertEqual(st["unpriced"], [])
        self.assertEqual(task["reviewer_session"], "ses_2")

    def test_last_round_is_counted_when_the_pr_merges(self):
        self.to_ready("a", 1, files=["a.py"])
        self.tick()
        aiflow.write_json(self.root / "runs" / "a" / "reviewer.json", {"runs": [self.run_of()]})
        self.gh.prs[1]["state"] = "MERGED"
        self.tick()
        self.assertEqual(self.state("a"), "merged")
        self.assertAlmostEqual(aiflow.build_state(self.store)["tasks"][0]["cost"], 0.02)

    def test_activity_shows_what_the_reviewer_is_doing(self):
        import os
        self.to_ready("a", 1, files=["a.py"])
        self.tick()
        aiflow.write_json(self.root / "runs" / "a" / "phase.json",
                          {"pid": os.getpid(), "at": aiflow.iso(), "since": aiflow.iso(), "phase": "reviewer", "round": 2,
                           "doing": "читает Import.cs", "steps": 7, "tokens": 66000})
        task = aiflow.build_state(self.store, self.claude.agents())["tasks"][0]
        self.assertEqual(task["activity"], "ревьюер opencode, раунд 2/5: читает Import.cs, шаг 7, 66k токенов")


class SessionModel(ProjectCase):
    def test_woken_session_keeps_the_model_it_was_started_with(self):
        self.add("a")
        self.tick()
        entry = self.task("a")["sessions"]["planner"][-1]
        self.assertEqual((entry["model"], entry["effort"]), ("opus", "medium"))
        self.claude.finish_turn(self.sid("a", "planner"), minutes_ago=5)
        self.tick()  # напоминание об отчёте — это пробуждение
        self.assertEqual((self.claude.resumed[-1]["model"], self.claude.resumed[-1]["effort"]), ("opus", "medium"))

    def test_planner_that_did_the_task_reviews_on_its_own_model(self):
        """Иначе сессия Opus просыпается на ревью под моделью по умолчанию и заново кэширует весь диалог."""
        self.add("a")
        self.tick()
        self.gh.add_pr(7, "a", body="Что сделано")
        self.report("a", "planner", "ready_for_review", 7)
        self.claude.finish_turn(self.sid("a", "planner"))
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.assertEqual(self.claude.resumed[-1]["model"], "opus")

    def test_named_model_survives_a_wake(self):
        self.add("a", planner_model="fable", planner_effort="high")
        self.tick()
        self.claude.finish_turn(self.sid("a", "planner"), minutes_ago=5)
        self.tick()
        self.assertEqual((self.claude.resumed[-1]["model"], self.claude.resumed[-1]["effort"]), ("fable", "high"))

    def test_sessions_recorded_before_this_version_get_the_configured_model(self):
        self.to_ready("a", 1, files=["a.py"])  # запись исполнителя без поля model
        self.tick()
        self.assertEqual(self.claude.resumed[-1]["model"], "sonnet")
        self.to_ready("b", 2, files=["b.py"])
        t = self.task("b")
        t["self_executed"] = True
        t["sessions"]["worker"][-1]["name"] = "planner: b"  # сессия планировщика, который сделал задачу сам
        self.store.save_task(t)
        self.tick()
        self.assertEqual(self.claude.resumed[-1]["model"], "opus")


class AutoReview(ProjectCase):
    """Ревью без пробуждения модели, когда ветка PR не отстаёт от базовой."""

    def ready(self, tid: str = "a", pr: int = 1) -> None:
        self.to_ready(tid, pr, files=[f"{tid}.py"])
        t = self.task(tid)
        t["sessions"]["worker"][-1]["cwd"] = str(self.tmp)  # рабочая копия задачи
        self.store.save_task(t)

    def finish_review(self, status: str, tid: str = "a") -> None:
        aiflow.write_json(self.root / "runs" / tid / "review.json", {"at": ago(-0.05), "pr": 1, "status": status})

    def test_up_to_date_branch_is_reviewed_without_waking_the_session(self):
        self.ready()
        self.gh.behind_by = 0
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.assertEqual(self.spawned, [{"task": "a", "pr": 1, "cwd": str(self.tmp)}])
        self.assertEqual(self.claude.resumed, [])
        self.assertEqual(self.store.queue()["review_locks"], ["a"])
        row = aiflow.build_state(self.store, self.claude.agents())["tasks"][0]
        self.assertEqual(row["activity"], "aiflow запускает ревью")

    def test_idle_session_during_auto_review_is_not_a_problem(self):
        self.ready()
        self.gh.behind_by = 0
        self.tick()
        self.age("a", 30)  # сессия давно «закончила ход» — так и должно быть, ревью ведёт aiflow
        self.patch("a", auto_review={**self.task("a")["auto_review"], "at": aiflow.iso()})
        with mock.patch.object(aiflow, "process_start", lambda pid: None):
            self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.assertEqual(self.claude.resumed, [])

    def test_approval_arrives_as_usual(self):
        self.ready()
        self.gh.behind_by = 0
        self.tick()
        self.finish_review("approved")
        self.report("a", "worker", "approved", 1)
        self.tick()
        t = self.task("a")
        self.assertEqual(t["approved_sha"], "sha1")
        self.assertNotIn("auto_review", t)
        self.assertEqual(self.claude.resumed, [])
        self.gh.prs[1]["state"] = "MERGED"
        self.tick()
        self.assertEqual(self.state("a"), "merged")

    def test_findings_wake_the_worker_with_the_result(self):
        self.ready()
        self.gh.behind_by = 0
        self.tick()
        self.finish_review("changes_requested")
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.assertEqual(len(self.claude.resumed), 1)
        prompt = self.claude.resumed[0]["prompt"]
        self.assertIn("changes_requested", prompt)
        self.assertIn("review.json", prompt)
        self.assertNotIn("auto_review", self.task("a"))
        self.tick()
        self.assertEqual(len(self.claude.resumed), 1)  # дальше цикл ведёт исполнитель

    def test_terminal_failure_goes_to_human_without_a_wake(self):
        self.ready()
        self.gh.behind_by = 0
        self.tick()
        self.finish_review("error")
        self.patch("a", review_retried=True)  # первый сбой aiflow уже повторил сам
        self.report("a", "worker", "error", 1, summary="ревьюер не вернул валидный JSON")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertEqual(self.claude.resumed, [])

    def test_branch_behind_base_wakes_the_worker_for_a_merge(self):
        self.ready()
        self.gh.behind_by = 2
        self.tick()
        self.assertEqual(self.spawned, [])
        self.assertIn("git merge origin/", self.claude.resumed[0]["prompt"])
        self.assertNotIn("force-with-lease", self.claude.resumed[0]["prompt"])
        self.assertEqual(self.state("a"), "in_review")

    def test_unknown_or_disabled_falls_back_to_the_worker(self):
        self.ready()
        self.gh.behind_by = None  # GitHub не сказал, отстаёт ли ветка
        self.tick()
        self.assertEqual((self.spawned, len(self.claude.resumed)), ([], 1))
        aiflow.write_json(self.root / "runs" / "config.json", {"auto_review": False})
        self.ready("b", 2)
        self.gh.behind_by = 0
        self.tick()
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.state("b"), "in_review")

    def test_review_process_that_died_hands_over_to_the_worker(self):
        self.ready()
        self.gh.behind_by = 0
        self.spawn_pid = 0x7FFFFFF0  # такого процесса нет
        self.tick()
        self.patch("a", auto_review={**self.task("a")["auto_review"], "at": ago(3)})
        self.tick()
        self.assertEqual(len(self.claude.resumed), 1)
        self.assertNotIn("auto_review", self.task("a"))
        self.assertEqual(self.state("a"), "in_review")

    def test_retry_forgets_a_stale_auto_review(self):
        self.ready()
        self.gh.behind_by = 0
        self.tick()
        self.finish_review("error")
        self.report("a", "worker", "error", 1, summary="сбой")
        self.tick()
        self.gh.behind_by = 3
        self.store.command({"cmd": "retry", "id": "a"})
        self.tick()
        self.assertNotIn("auto_review", self.task("a"))
        self.assertEqual(self.state("a"), "in_review")


class PlannerInstructions(ProjectCase):
    """Правила плана и разговора вынесены из planner.md в файлы, которые планировщик читает по надобности."""

    def test_parts_exist_and_launch_prompts_point_at_them(self):
        self.assertTrue(aiflow.PLANNER_PLAN_MD.is_file())
        self.assertTrue(aiflow.PLANNER_TALK_MD.is_file())
        t = self.add("a")
        cfg = self.store.config()
        task_prompt = aiflow.planner_prompt(self.store, t, cfg)
        self.assertIn(str(aiflow.PLANNER_PLAN_MD), task_prompt)
        self.assertIn(str(aiflow.WORKER_MD), task_prompt)
        self.assertNotIn(str(aiflow.PLANNER_TALK_MD), task_prompt)  # обычной задаче правила разговора не нужны
        for build in (aiflow.ask_prompt, aiflow.discuss_prompt):
            text = build(self.store, t, cfg)
            self.assertIn(str(aiflow.PLANNER_TALK_MD), text)
            self.assertIn(str(aiflow.PLANNER_PLAN_MD), text)

    def test_core_file_has_no_plan_template_and_parts_have_it(self):
        core = (aiflow.PLUGIN_ROOT / "agents" / "planner.md").read_text(encoding="utf-8")
        plan = aiflow.PLANNER_PLAN_MD.read_text(encoding="utf-8")
        talk = aiflow.PLANNER_TALK_MD.read_text(encoding="utf-8")
        self.assertNotIn("<!-- ai-plan v1 -->", core)
        self.assertNotIn("## Discussion mode", core)
        for needed in ("<!-- ai-plan v1 -->", "gh pr create --draft", "git switch --detach", "<!-- aiflow:planner -->"):
            self.assertIn(needed, plan)
        for needed in ("--status answered", "aiflow discussion", "aiflow enqueue"):
            self.assertIn(needed, talk)
        for needed in ("self_execute", "--status delegate", "Task assessment"):
            self.assertIn(needed, core)
        self.assertLess(len(core), 0.7 * (len(core) + len(plan) + len(talk)))

    def test_task_sessions_cannot_create_worktrees(self):
        for name in ("planner.md", "worker.md"):
            head = (aiflow.PLUGIN_ROOT / "agents" / name).read_text(encoding="utf-8").split("---")[1]
            self.assertIn("EnterWorktree", head, name)


class ServiceVersion(ProjectCase):
    """Служба одна и самой новой версии, кто бы её ни поднимал."""

    def install(self, *versions: str):
        import aiflow_service as svc
        cache = self.tmp / "cache" / "aiflow"
        for v in versions:
            (cache / v / "scripts").mkdir(parents=True)
            (cache / v / "scripts" / "aiflow.py").write_text("", encoding="utf-8")
        return svc, cache

    def test_newest_installed_version_is_found_next_to_this_one(self):
        svc, cache = self.install("0.2.9", "0.2.18", "0.2.46", "0.10.1", "not-a-version")
        with mock.patch.object(aiflow, "VERSION", "0.2.18"):
            version, script = svc.newest_install(cache / "0.2.18")
        self.assertEqual(version, "0.10.1")  # по числам, а не по алфавиту
        self.assertEqual(script, cache / "0.10.1" / "scripts" / "aiflow.py")

    def test_own_version_wins_when_nothing_newer_is_installed(self):
        svc, cache = self.install("0.2.18", "0.2.9")
        with mock.patch.object(aiflow, "VERSION", "0.2.18"):
            self.assertEqual(svc.newest_install(cache / "0.2.18")[0], "0.2.18")
        with mock.patch.object(aiflow, "VERSION", "0.3.0"):  # запуск из репозитория: соседних версий нет
            self.assertEqual(svc.newest_install(self.tmp / "repo" / "plugins" / "aiflow")[0], "0.3.0")

    def test_running_service_older_than_the_newest_install_is_outdated(self):
        svc, cache = self.install("0.2.18", "0.2.46")
        with mock.patch.object(aiflow, "VERSION", "0.2.18"), mock.patch.object(aiflow, "PLUGIN_ROOT", cache / "0.2.18"):
            self.assertTrue(svc.outdated({"version": "0.2.18"}))  # даже для монитора версии 0.2.18
            self.assertFalse(svc.outdated({"version": "0.2.46"}))
            self.assertFalse(svc.outdated(None))


class SearchRule(ProjectCase):
    """Кто ищет по коду: подагент aiflow или сам проект — настройка explorer."""

    def prompts(self, **cfg) -> list[str]:
        aiflow.write_json(self.root / "runs" / "config.json", cfg)
        conf = self.store.config()
        t = self.add("a")
        with_pr = {**t, "pr": 7}
        return [aiflow.planner_prompt(self.store, t, conf), aiflow.ask_prompt(self.store, t, conf),
                aiflow.discuss_prompt(self.store, t, conf), aiflow.worker_prompt(self.store, with_pr, conf),
                aiflow.direct_prompt(self.store, t, conf)]

    def test_rule_names_the_plugin_explorer_and_lsp_by_default(self):
        for text in self.prompts():
            self.assertIn("aiflow:explorer", text)
            self.assertIn("LSP", text)

    def test_rule_comes_right_after_the_task_file_line(self):
        """В конце длинного сообщения правило не замечали: сессия начинала искать раньше."""
        for text in self.prompts():
            lines = text.split("\n")
            at = next(i for i, ln in enumerate(lines) if "Task file" in ln)
            self.assertTrue(lines[at + 1].startswith("How to search the code"), lines[at + 1][:60])
            self.assertLess(text.index("How to search the code"), text.index("aiflow report"))

    def test_project_mode_points_to_the_project_rules(self):
        """Своих средств aiflow не называет: иначе в сессии две инструкции о поиске, и они спорят."""
        for text in self.prompts(explorer="project"):
            self.assertNotIn("aiflow:explorer", text)
            self.assertNotIn("LSP", text)
            self.assertIn("the project CLAUDE.md", text)
            self.assertIn("do not answer it with grep", text)

    def test_unknown_value_falls_back_to_the_plugin_agent(self):
        self.assertEqual(aiflow.explorer_mode({"explorer": "что-то"}), "aiflow")
        self.assertEqual(aiflow.explorer_mode({"explorer": "PROJECT"}), "project")
        self.assertEqual(aiflow.explorer_mode({}), "aiflow")

    def test_agent_files_do_not_hardcode_the_explorer_and_no_search_hook_is_left(self):
        for rel in ("agents/planner.md", "agents/worker.md", "prompts/planner-talk.md"):
            self.assertNotIn("aiflow:explorer", (aiflow.PLUGIN_ROOT / rel).read_text(encoding="utf-8"), rel)
        hooks = json.loads((aiflow.PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        self.assertNotIn("PreToolUse", hooks)

    def test_launched_session_gets_no_rule_in_project_mode(self):
        aiflow.write_json(self.root / "runs" / "config.json", {"explorer": "project"})
        self.add("a")
        self.tick()
        self.assertNotIn("aiflow:explorer", self.claude.launched[-1]["prompt"])


class BoardSteps(ProjectCase):
    """У вопроса и обсуждения нет шагов после «плана» — панель их не рисует."""

    def row(self, tid: str = "a") -> dict:
        return next(t for t in aiflow.build_state(self.store)["tasks"] if t["id"] == tid)

    def test_question_stops_at_its_own_last_step(self):
        plan = aiflow.STAGES.index("planning")
        self.add("a", kind="ask")
        self.assertEqual(self.row()["last_station"], plan)
        self.tick()
        self.assertEqual((self.row()["stage"], self.row()["last_station"]), (plan, plan))
        self.report("a", "planner", "answered", summary="вот ответ")
        self.tick()
        self.assertEqual((self.row()["state"], self.row()["stage"], self.row()["last_station"]), ("answered", plan, plan))
        self.patch("a", state="done")
        self.assertEqual((self.row()["stage"], self.row()["last_station"]), (plan, plan))  # не «смержено»

    def test_question_that_became_a_plan_shows_the_whole_path(self):
        self.add("a", kind="ask")
        self.tick()
        self.gh.add_pr(7, "a")
        self.report("a", "planner", "plan_ready", 7, complexity="complex")
        self.tick()
        self.assertEqual(self.state("a"), "plan_review")
        self.assertIsNone(self.row()["last_station"])

    def test_ordinary_task_always_shows_every_step(self):
        self.add("a")
        self.assertIsNone(self.row()["last_station"])
        self.patch("a", state="done")
        self.assertEqual(self.row()["stage"], aiflow.STAGES.index("merged"))


class Models(ProjectCase):
    def test_model_named_at_intake_is_used_for_the_planner(self):
        t = aiflow.normalize_task({"id": "a", "text": "t", "planner_model": "fable", "planner_effort": "high"})
        self.store.save_task(t)
        self.add("b")
        self.tick()
        by_name = {x["name"]: x for x in self.claude.launched}
        self.assertEqual((by_name["planner: a"]["model"], by_name["planner: a"]["effort"]), ("fable", "high"))
        self.assertEqual((by_name["planner: b"]["model"], by_name["planner: b"]["effort"]), ("opus", "medium"))

    def test_model_can_be_set_for_a_queued_task(self):
        self.store.command({"cmd": "pause"})
        self.tick()
        self.add("a")
        now_set, notes = aiflow.set_models(self.store, "a", {"planner_model": "fable", "worker_model": "claude-opus-5-5",
                                                             "worker_effort": "MAX"})
        self.assertEqual(notes, [])
        self.assertEqual(now_set["worker_effort"], "max")
        self.store.command({"cmd": "resume"})
        self.tick()
        self.assertEqual(self.claude.launched[-1]["model"], "fable")
        self.report("a", "planner", "delegate", summary="мелочь")
        self.tick()
        worker = self.claude.launched[-1]
        self.assertEqual((worker["agent"], worker["model"], worker["effort"]), ("aiflow:worker", "claude-opus-5-5", "max"))

    def test_setting_a_model_for_a_running_planner_warns(self):
        self.add("a")
        self.tick()
        now_set, notes = aiflow.set_models(self.store, "a", {"planner_model": "fable", "worker_model": "opus"})
        self.assertEqual(len(notes), 1)
        self.assertIn("планировщик", notes[0])
        self.assertEqual(self.task("a")["planner_model"], "fable")

    def test_default_returns_the_project_setting(self):
        self.add("a", planner_model="fable")
        aiflow.set_models(self.store, "a", {"planner_model": "default"})
        self.assertNotIn("planner_model", self.task("a"))
        self.tick()
        self.assertEqual(self.claude.launched[-1]["model"], "opus")

    def test_question_and_discussion_use_the_named_model_too(self):
        t = aiflow.normalize_task({"id": "q", "text": "где у нас экспорт?", "kind": "ask", "planner_model": "fable"})
        self.store.save_task(t)
        self.add("d", kind="discuss")
        self.tick()
        by_name = {x["name"]: x for x in self.claude.launched}
        self.assertEqual(by_name["planner: q"]["model"], "fable")
        self.assertEqual(by_name["planner: d"]["model"], "opus")  # discuss_model проекта

    def test_bad_values_are_rejected(self):
        with self.assertRaisesRegex(aiflow.AiflowError, "имя модели"):
            aiflow.normalize_task({"id": "a", "text": "t", "planner_model": "fable; rm -rf"})
        with self.assertRaisesRegex(aiflow.AiflowError, "effort"):
            aiflow.normalize_task({"id": "a", "text": "t", "worker_effort": "huge"})
        self.add("a")
        with self.assertRaisesRegex(aiflow.AiflowError, "укажите"):
            aiflow.set_models(self.store, "a", {})
        self.patch("a", state="merged")
        with self.assertRaisesRegex(aiflow.AiflowError, "завершена"):
            aiflow.set_models(self.store, "a", {"planner_model": "fable"})
        self.assertEqual(aiflow.check_model_field("worker_model", "claude-sonnet-5-5[1m]"), "claude-sonnet-5-5[1m]")

    def test_models_are_visible_in_the_state(self):
        self.add("a", planner_model="fable")
        self.assertEqual(aiflow.build_state(self.store)["tasks"][0]["models"], {"planner_model": "fable"})


class Coordinator(ProjectCase):
    def session(self, sid: str, name: str, state: str = "done", **extra) -> None:
        self.claude.sessions[sid] = {"id": sid, "name": name, "state": state, "sessionId": f"uuid-{sid}",
                                     "cwd": str(self.root), "startedAt": len(self.claude.sessions), **extra}

    def test_session_running_the_monitor_is_the_coordinator(self):
        self.session("w1", "worker: a")
        self.session("o1", "db-schema-cache-test-container")  # имя сессии Claude переименовал сам
        self.session("o2", "aiflow")
        aiflow.write_json(self.root / "runs" / "orchestrator.json", {"session_id": "uuid-o1"})
        self.assertEqual(aiflow.orchestrator_session(self.store, self.claude.agents()), "o1")
        self.assertEqual(aiflow.open_orchestrator(self.store, self.claude), ("o1", False))
        self.assertEqual(self.claude.launched, [])

    def test_falls_back_to_a_session_named_aiflow(self):
        self.session("old", "aiflow")
        self.session("new", "aiflow")
        self.session("far", "aiflow", cwd=str(self.tmp / "other-project"))
        self.session("w1", "worker: a")
        self.assertEqual(aiflow.orchestrator_session(self.store, self.claude.agents()), "new")

    def test_stopped_or_missing_coordinator_is_started_anew(self):
        self.session("o1", "aiflow", state="stopped")
        aiflow.write_json(self.root / "runs" / "orchestrator.json", {"session_id": "uuid-gone"})
        self.assertIsNone(aiflow.orchestrator_session(self.store, self.claude.agents()))
        sid, started = aiflow.open_orchestrator(self.store, self.claude)
        self.assertTrue(started)
        self.assertEqual(self.claude.launched[-1]["agent"], "aiflow:orchestrator")
        self.assertEqual(self.claude.launched[-1]["name"], "aiflow")
        self.assertEqual(aiflow.open_orchestrator(self.store, self.claude), (sid, False))  # второй раз — та же

    def test_launch_failure_is_reported(self):
        self.claude.fail_launch = True
        with self.assertRaisesRegex(aiflow.AiflowError, "координатора"):
            aiflow.open_orchestrator(self.store, self.claude)

    def test_tab_is_named_after_the_project(self):
        import aiflow_ui
        self.assertEqual(aiflow_ui.session_title({"id": "shizuka"}, "abc"), "shizuka")


class Misc(ProjectCase):
    def test_graphql_pr_matches_gh_pr_view_shape(self):
        node = {"state": "OPEN", "isDraft": False, "headRefOid": "abc", "headRefName": "ai/a", "body": None,
                "labels": {"nodes": [{"name": "plan-approved"}]},
                "comments": {"nodes": [{"author": {"login": "u"}, "body": "hi", "createdAt": "2026-01-01T00:00:00Z"}]},
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
                    {"__typename": "CheckRun", "name": "build", "conclusion": "FAILURE", "status": "COMPLETED"}]}}}}]}}
        pr = aiflow.pr_from_graphql(node)
        self.assertEqual(pr["labels"], [{"name": "plan-approved"}])
        self.assertEqual(pr["comments"][0]["author"]["login"], "u")
        self.assertEqual(pr["statusCheckRollup"][0]["conclusion"], "FAILURE")
        self.assertEqual(pr["body"], "")
        empty = aiflow.pr_from_graphql({"state": "MERGED", "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}})
        self.assertEqual(empty["statusCheckRollup"], [])

    def test_task_keeps_the_number_of_its_items(self):
        self.assertEqual(aiflow.normalize_task({"id": "a", "text": "1. x\n2. y", "items": 2})["items"], 2)
        self.assertEqual(aiflow.normalize_task({"id": "a", "text": "t"})["items"], 1)
        with self.assertRaisesRegex(aiflow.AiflowError, "items"):
            aiflow.normalize_task({"id": "a", "text": "t", "items": "много"})

    def test_auto_approve_is_gone(self):
        t = aiflow.normalize_task({"id": "a", "text": "t", "complexity": "simple", "auto_approve_plan": True})
        self.assertNotIn("auto_approve_plan", t)

    def test_batch_done_is_announced_once(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "done", summary="сделано")
        self.assertIn("batch_done", self.kinds(self.tick()))
        self.assertNotIn("batch_done", self.kinds(self.tick()))

    def test_sessions_of_a_merged_task_stay_too(self):
        self.add("a", state="merged", pr=1, sessions={"planner": [{"id": "p-a"}], "worker": [{"id": "w-a"}]})
        self.claude.sessions.update({"p-a": {"state": "done"}, "w-a": {"state": "done"}})
        self.tick()
        self.assertEqual(self.claude.removed, [])
        self.assertFalse(self.task("a").get("cleaned"))
        self.patch("a", state_at=ago(15 * 24 * 60))
        self.tick()
        self.assertEqual(sorted(self.claude.removed), ["p-a", "w-a"])

    def test_keep_days_zero_never_removes_sessions(self):
        cfg = self.store.config()
        cfg["session_keep_days"] = 0
        aiflow.write_json(self.store.runs / "config.json", cfg)
        self.add("a", state="merged", pr=1, state_at=ago(400 * 24 * 60), sessions={"worker": [{"id": "w-a"}]})
        self.claude.sessions["w-a"] = {"state": "done"}
        self.tick()
        self.assertEqual(self.claude.removed, [])

    def test_session_of_a_task_done_without_pr_stays_to_be_read(self):
        """Итог такой задачи (обзор, ответ) — в самой сессии: её нельзя удалять, пока пользователь читает."""
        self.add("a")
        self.tick()
        sid = self.sid("a", "planner")
        self.report("a", "planner", "done", summary="обзор готов")
        self.tick()
        self.tick()
        self.assertEqual(self.state("a"), "done")
        self.assertEqual(self.claude.removed, [])
        row = aiflow.build_state(self.store)["tasks"][0]
        self.assertEqual(row["answer"], "обзор готов")
        self.patch("a", state_at=ago(15 * 24 * 60))
        self.tick()
        self.assertEqual(self.claude.removed, [sid])
        self.assertTrue(self.task("a")["cleaned"])


class ToastLink(unittest.TestCase):
    PANEL = {"url": "http://127.0.0.1:8765/", "project": "C:/p 1", "token": "tok",
             "repo_url": "https://github.com/o/r", "prs": {"a": 12}}

    def test_click_on_the_toast_opens_the_panel(self):
        self.assertEqual(aiflow.toast_link(self.PANEL), "http://127.0.0.1:8765/")
        self.assertIsNone(aiflow.toast_link(None))

    def test_plan_event_has_approve_chat_and_pr_buttons(self):
        acts = aiflow.toast_actions(self.PANEL, {"kind": "plan_ready", "task": "a", "text": "a: план готов"})
        self.assertEqual([label for label, _ in acts], ["Одобрить план", "Открыть чат", "Открыть PR"])
        self.assertEqual(acts[0][1], "http://127.0.0.1:8765/#approve=a&project=C%3A%2Fp%201&k=tok")
        self.assertEqual(acts[1][1], "http://127.0.0.1:8765/#open=a&project=C%3A%2Fp%201")
        self.assertEqual(acts[2][1], "https://github.com/o/r/pull/12")

    def test_other_events_get_only_what_fits(self):
        human = aiflow.toast_actions(self.PANEL, {"kind": "needs_human", "task": "a"})
        self.assertEqual([label for label, _ in human], ["Повторить", "Открыть чат", "Открыть PR"])
        no_pr = aiflow.toast_actions(self.PANEL, {"kind": "needs_input", "task": "b"})
        self.assertEqual([label for label, _ in no_pr], ["Открыть чат"])
        self.assertEqual(aiflow.toast_actions(self.PANEL, {"kind": "paused", "text": "пауза"}), [])
        no_token = aiflow.toast_actions(dict(self.PANEL, token=""), {"kind": "plan_ready", "task": "a"})
        self.assertNotIn("Одобрить план", [label for label, _ in no_token])

    def test_toast_xml_is_valid_and_escapes_text(self):
        import xml.dom.minidom
        acts = aiflow.toast_actions(self.PANEL, {"kind": "plan_ready", "task": "a"})
        doc = xml.dom.minidom.parseString(aiflow.toast_xml("aiflow · p", 'a: <план> & "PR"', "http://x/", acts))
        self.assertEqual(doc.documentElement.getAttribute("launch"), "http://x/")
        got = [(n.getAttribute("content"), n.getAttribute("arguments")) for n in doc.getElementsByTagName("action")]
        self.assertEqual(got, acts)
        self.assertEqual(doc.getElementsByTagName("text")[1].firstChild.data, 'a: <план> & "PR"')

    def test_notify_passes_the_link_and_the_buttons(self):
        seen = []
        orig = aiflow._toast
        aiflow._toast = lambda title, text, link=None, actions=None: seen.append((link, actions))
        try:
            aiflow.desktop_notify({}, [{"kind": "plan_ready", "task": "a", "text": "x"}], panel=self.PANEL)
            aiflow.desktop_notify({}, [{"kind": "plan_ready", "task": "a", "text": "x"}])
        finally:
            aiflow._toast = orig
        self.assertEqual(seen[0][0], "http://127.0.0.1:8765/")
        self.assertEqual(len(seen[0][1]), 3)
        self.assertEqual(seen[1], (None, []))


class ContextWindow(ProjectCase):
    def test_window_is_set_for_each_role(self):
        cfg = dict(aiflow.DEFAULT_CONFIG, planner_context=200000, worker_context={"complex": 300000, "simple": 150000})
        self.assertEqual(aiflow.context_choice(cfg, {}, "planner"), 200000)
        self.assertEqual(aiflow.context_choice(cfg, {"complexity": "complex"}, "worker"), 300000)
        self.assertEqual(aiflow.context_choice(cfg, {}, "worker"), 150000)

    def test_zero_leaves_the_default_window_and_limits_apply(self):
        self.assertEqual(aiflow.context_choice({"planner_context": 0}, {}, "planner"), 0)
        self.assertEqual(aiflow.context_choice({}, {}, "worker"), 0)
        self.assertEqual(aiflow.context_choice({"worker_context": 5000}, {}, "worker"), 100000)

    def test_launch_passes_the_window(self):
        self.add("a")
        self.tick()
        self.assertEqual(self.claude.launched[0]["window"], 180000)


class EarlyPr(ProjectCase):
    def test_planner_that_does_the_task_itself_shows_its_draft_pr_at_once(self):
        self.add("a")
        self.tick()
        self.gh.prs[7] = {"state": "OPEN", "headRefName": "ai/a", "isDraft": True, "body": "", "labels": []}
        self.report("a", "planner", "self_execute", 7, summary="делаю")
        self.tick()
        self.assertEqual((self.state("a"), self.task("a")["pr"]), ("planning", 7))
        self.assertEqual(aiflow.build_state(self.store)["tasks"][0]["pr"], 7)

    def test_pr_of_another_branch_is_not_taken(self):
        self.add("a")
        self.tick()
        self.gh.prs[7] = {"state": "OPEN", "headRefName": "ai/other", "isDraft": True, "body": "", "labels": []}
        self.report("a", "planner", "self_execute", 7, summary="делаю")
        self.tick()
        self.assertIsNone(self.task("a").get("pr"))

    def test_worker_without_a_plan_reports_its_draft_pr(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "delegate", summary="поручаю")
        self.tick()
        self.assertEqual(self.state("a"), "in_progress")
        self.gh.prs[8] = {"state": "OPEN", "headRefName": "ai/a", "isDraft": True, "body": "", "labels": []}
        self.report("a", "worker", "started", 8)
        self.tick()
        self.assertEqual((self.state("a"), self.task("a")["pr"]), ("in_progress", 8))


class McpServer(ProjectCase):
    def setUp(self):
        super().setUp()
        import aiflow_mcp
        self.mcp = aiflow_mcp
        self.orig = aiflow_mcp.load_projects
        aiflow_mcp.load_projects = lambda: [{**aiflow.build_state(self.store), "key": "k1", "name": "demo"}]

    def tearDown(self):
        self.mcp.load_projects = self.orig
        super().tearDown()

    def ask(self, method, params=None, mid=1):
        return self.mcp.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def test_handshake_tools_and_ui_resource(self):
        self.assertEqual(self.ask("initialize", {"protocolVersion": "2025-06-18"})["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(self.ask("server/discover")["error"]["code"], -32601)
        self.assertIsNone(self.mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = self.ask("tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["aiflow_tasks", "aiflow_usage"])
        self.assertTrue(all(t["_meta"]["ui"]["resourceUri"] == self.mcp.APP_URI for t in tools))
        page = self.ask("resources/read", {"uri": self.mcp.APP_URI})["result"]["contents"][0]
        self.assertEqual(page["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("ui/initialize", page["text"])
        self.assertIn("error", self.ask("resources/read", {"uri": "ui://other"}))

    def test_tasks_tool_filters_by_project_and_returns_the_board(self):
        self.add("a")
        res = self.ask("tools/call", {"name": "aiflow_tasks", "arguments": {"project": "DEM"}})["result"]
        self.assertIn("demo: 1 in work", res["content"][0]["text"])
        board = res["structuredContent"]
        self.assertEqual((board["view"], board["projects"][0]["tasks"][0]["id"]), ("tasks", "a"))
        none = self.ask("tools/call", {"name": "aiflow_tasks", "arguments": {"project": "zzz"}})["result"]
        self.assertEqual(none["structuredContent"]["projects"], [])
        self.assertTrue(self.ask("tools/call", {"name": "nope"})["result"]["isError"])

    def test_usage_tool_sums_cost_by_day_and_model(self):
        self.mcp.load_projects = lambda: [{"name": "demo", "key": "k1", "tasks": [
            {"id": "a", "title": "t", "cost": 3.0, "tokens": 30, "updated_at": aiflow.iso(),
             "usage": {"claude-opus-5-5": {"input": 1, "output": 2, "cache_read": 20, "cache_write": 7, "cost": 2.5},
                       "opencode-go/deepseek": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.5}}},
            {"id": "old", "title": "t", "cost": 9.0, "tokens": 1, "updated_at": "2020-01-01T00:00:00+00:00",
             "usage": {"claude-sonnet-5-5": {"cost": 9.0}}}]}]
        use = self.ask("tools/call", {"name": "aiflow_usage", "arguments": {"days": 7}})["result"]["structuredContent"]
        self.assertEqual((use["total"], use["models"]), (3.0, ["Opus", "Ревьюер"]))
        self.assertEqual(use["by_day"][0]["Opus"], 2.5)
        self.assertEqual([t["id"] for t in use["top"]], ["a"])

    def test_stdio_loop_answers_line_by_line(self):
        src = io.BytesIO(b'{"jsonrpc":"2.0","id":7,"method":"ping"}' + b"\nnot json\n\n")
        out = io.BytesIO()
        self.mcp.serve(src, out)
        lines = [json.loads(x) for x in out.getvalue().decode("utf-8").splitlines()]
        self.assertEqual(lines[0], {"jsonrpc": "2.0", "id": 7, "result": {}})
        self.assertEqual(lines[1]["error"]["code"], -32700)


class ApproveWhilePlannerWorks(ProjectCase):
    def plan_review(self) -> str:
        self.add("a")
        self.tick()
        self.gh.prs[1] = {"state": "OPEN", "headRefName": "ai/a", "isDraft": True, "body": PLAN, "labels": [], "comments": []}
        self.report("a", "planner", "plan_ready", 1, complexity="complex")
        sid = self.task("a")["sessions"]["planner"][-1]["id"]
        self.claude.finish_turn(sid)
        self.tick()
        self.assertEqual(self.state("a"), "plan_review")
        return sid

    def test_approval_waits_for_the_end_of_the_planner_turn(self):
        sid = self.plan_review()
        self.claude.sessions[sid]["state"] = "working"  # пользователь ответил в чате планировщика
        self.gh.add_label(1, "plan-approved")
        self.tick()
        self.assertEqual(self.state("a"), "plan_review")
        self.assertTrue(self.task("a")["approve_hold"])
        self.assertIsNone(aiflow.build_state(self.store, self.claude.agents())["tasks"][0]["needs"])
        self.claude.finish_turn(sid)  # планировщик только ответил, план не менял
        self.tick()
        self.assertIn(self.state("a"), ("approved", "in_progress"))

    def test_plan_changed_after_the_approval_needs_a_new_approval(self):
        sid = self.plan_review()
        self.claude.sessions[sid]["state"] = "working"
        self.gh.add_label(1, "plan-approved")
        self.tick()
        self.report("a", "planner", "plan_ready", 1, complexity="complex")
        self.claude.finish_turn(sid)
        events = self.tick()
        self.assertEqual(self.state("a"), "plan_review")
        self.assertNotIn("plan-approved", self.gh.labels(1))
        self.assertIn("после одобрения", " ".join(e["text"] for e in events))
        self.assertEqual(aiflow.build_state(self.store)["tasks"][0]["needs"], "обсудить и одобрить план")


class BlockedSessionSaysWhy(ProjectCase):
    def start(self) -> str:
        self.add("a")
        self.tick()
        return self.sid("a", "planner")

    def test_last_words_reads_the_last_assistant_text(self):
        base = self.tmp / "claude"
        log = base / "projects" / "p" / "sess-1.jsonl"
        log.parent.mkdir(parents=True)
        rows = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
                {"type": "user", "message": {"content": "ok"}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Need the path\n.claude/x allowed. " + "y" * 600}]}}]
        log.write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
        with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(base)}):
            said = aiflow.last_words("sess-1")
            self.assertTrue(said.startswith("Need the path .claude/x allowed."))
            self.assertLessEqual(len(said), 420)
            self.assertEqual(aiflow.last_words("nope"), "")

    def test_panel_shows_what_the_blocked_session_asks(self):
        sid = self.start()
        self.claude.sessions[sid].update(state="blocked", waitingFor="ответ")
        with mock.patch.object(aiflow, "last_words", return_value="Разрешите защищённый путь .claude/skills/x"):
            events = self.tick()
        self.assertIn("защищённый путь", " ".join(e["text"] for e in events))
        row = aiflow.build_state(self.store, self.claude.agents())["tasks"][0]
        self.assertEqual(row["needs"], "сессия ждёт ответа: Разрешите защищённый путь .claude/skills/x")

    def test_path_request_stays_until_the_path_is_allowed(self):
        sid = self.start()
        self.claude.sessions[sid].update(state="blocked", waitingFor="ответ")
        with mock.patch.object(aiflow, "last_words", return_value="Нужен защищённый путь `.claude/skills/x/SKILL.md`"):
            self.tick()
        row = aiflow.build_state(self.store, self.claude.agents())["tasks"][0]
        self.assertEqual((row["asks_paths"], row["wants_paths"]), (True, [".claude/skills/x/SKILL.md"]))
        # пользователь ответил в чате: сессия работает, вопроса в панели больше нет, а путь всё ещё нужен
        self.claude.sessions[sid].update(state="working", waitingFor=None)
        with mock.patch.object(aiflow, "last_words", return_value="Понял, жду"):
            self.tick()
        row = aiflow.build_state(self.store, self.claude.agents())["tasks"][0]
        self.assertIsNone(row["needs"])
        self.assertTrue(row["asks_paths"])
        with mock.patch.object(aiflow, "queue_or_apply"):
            aiflow.allow_paths(self.store, "a", [".claude/skills/x/SKILL.md"])
        row = aiflow.build_state(self.store, self.claude.agents())["tasks"][0]
        self.assertEqual((row["asks_paths"], row["wants_paths"]), (False, []))

    def test_review_finding_on_a_protected_file_asks_for_the_path(self):
        t = {"id": "a", "state": "in_review", "allowed_protected_paths": []}
        review = {"comments": [{"file": ".claude/skills/x/SKILL.md"}, {"file": "src/a.py"}]}
        cfg = {"protected_paths": [".claude/**"]}
        self.assertEqual(aiflow.wanted_paths(t, review, cfg), [".claude/skills/x/SKILL.md"])
        t["allowed_protected_paths"] = [".claude/skills/**"]
        self.assertEqual(aiflow.wanted_paths(t, review, cfg), [])
        self.assertEqual(aiflow.wanted_paths({"id": "a", "state": "merged"}, review, cfg), [])

    def test_allowed_path_wakes_the_session_that_asked(self):
        sid = self.start()
        self.claude.sessions[sid].update(state="blocked", waitingFor="ответ")
        self.tick()
        self.store.command({"cmd": "paths_allowed", "id": "a", "paths": [".claude/skills/x/SKILL.md"]})
        self.tick()
        self.assertIn("allowed these protected paths", self.claude.resumed[-1]["prompt"])


class SettleWaitingSessions(ProjectCase):
    def test_planner_question_is_closed_when_the_plan_is_approved(self):
        self.to_plan_review("a", 1)
        sid = self.sid("a", "planner")
        self.claude.sessions[sid]["state"] = "blocked"  # закончил ход словами «одобрите план»
        self.gh.add_label(1, "plan-approved")
        self.tick()
        self.assertEqual(self.claude.sessions[sid]["state"], "stopped")
        self.assertEqual(self.claude.removed, [])  # разговор остаётся

    def test_waiting_sessions_of_a_finished_task_are_closed(self):
        self.to_plan_review("a", 1)
        sid = self.sid("a", "planner")
        self.claude.sessions[sid]["state"] = "blocked"
        t = self.task("a")
        t["state"] = "merged"
        self.store.save_task(t)
        self.tick()
        self.assertEqual(self.claude.sessions[sid]["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
