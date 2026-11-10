"""Переходы очереди: Dispatcher на подделках claude и gh."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest

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
        fixes = [r for r in self.claude.resumed if "упали проверки CI" in r["prompt"]]
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

    def test_too_large_needs_a_human_and_retry_lifts_the_limit(self):
        self.start()
        self.report("a", "worker", "too_large", 1, summary="9 файлов")
        self.claude.finish_turn("w-a")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertIn("needs-human", self.gh.labels(1))
        self.store.command({"cmd": "retry", "id": "a"})
        self.tick()
        self.assertTrue(self.task("a")["size_ok"])
        self.assertEqual(self.state("a"), "in_review")

    def test_review_turn_without_result_is_reminded_first(self):
        self.start()
        self.claude.finish_turn("w-a", minutes_ago=10)
        self.age("a", 10)
        self.tick()
        self.assertEqual(self.state("a"), "in_review")
        self.assertIn("aiflow review --pr 1", self.claude.resumed[-1]["prompt"])

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
        self.assertIn("уже есть PR #5", prompt)
        self.assertIn("Плана нет", prompt)
        self.assertNotIn("План — в описании PR", prompt)

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
        self.report("a", "worker", "error", 1, summary="новая ошибка")
        self.tick()
        self.assertEqual(self.state("a"), "needs_human")
        self.assertEqual(self.task("a")["human_reason"], "новая ошибка")

    def to_human_with_working_session(self) -> None:
        """Задача на ревью ушла к человеку, а её сессия продолжает работать."""
        self.to_ready("a", 1, files=["a.py"])
        self.tick()
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

    def test_reason_is_one_line_and_says_what_to_do(self):
        self.to_ready("a", 1, files=["a.py"])
        self.tick()
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
                                ("too_large", "9 файлов"), ("blocked", "нужен доступ к базе")):
            tid = f"t{len(advice)}"
            self.to_ready(tid, 10 + len(advice), files=[f"{tid}.py"])
            self.tick()
            self.report(tid, "worker", status, 10 + len(advice), summary=summary)
            self.tick()
            advice[summary] = self.task(tid)["human_action"]
        self.assertEqual(len(set(advice.values())), 4)
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

    def enqueue(self, items) -> None:
        path = self.tmp / "tasks.json"
        path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            aiflow.cmd_enqueue(argparse.Namespace(file=str(path)), self.root)

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

    def test_auto_approve_is_gone(self):
        t = aiflow.normalize_task({"id": "a", "text": "t", "complexity": "simple", "auto_approve_plan": True})
        self.assertNotIn("auto_approve_plan", t)

    def test_batch_done_is_announced_once(self):
        self.add("a")
        self.tick()
        self.report("a", "planner", "done", summary="сделано")
        self.assertIn("batch_done", self.kinds(self.tick()))
        self.assertNotIn("batch_done", self.kinds(self.tick()))


if __name__ == "__main__":
    unittest.main()
