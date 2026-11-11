"""Хук перед поиском: один раз за сессию задачи напомнить про LSP вместо текстового поиска."""
from __future__ import annotations

import json
import unittest

from helpers import ProjectCase, aiflow  # noqa: F401  (helpers добавляет scripts в sys.path)

import search_hook


class SearchHook(ProjectCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.add("a")
        self.worktree = self.root / ".claude" / "worktrees" / "a"
        self.worktree.mkdir(parents=True)
        self.plugins({"csharp-lsp@claude-plugins-official": True})

    def plugins(self, enabled: dict, name: str = "settings.json") -> None:
        (self.root / ".claude").mkdir(exist_ok=True)
        (self.root / ".claude" / name).write_text(json.dumps({"enabledPlugins": enabled}), encoding="utf-8")

    def call(self, command: str | None = "git grep -n ShizukaDbContext -- 'src/*.cs'", tool: str = "Bash",
             session: str = "s1", cwd=None) -> str | None:
        tool_input = {"command": command} if tool == "Bash" else {"pattern": "ShizukaDbContext"}
        return search_hook.decide({"session_id": session, "cwd": str(cwd or self.worktree), "tool_name": tool,
                                   "tool_input": tool_input}, self.home)

    def test_first_text_search_is_stopped_once_and_names_lsp(self):
        text = self.call()
        self.assertIn("LSP", text)
        self.assertIn("select:LSP", text)
        self.assertIn("csharp-lsp", text)
        self.assertIn("aiflow:explorer", text)
        self.assertIsNone(self.call())  # повтор той же команды проходит
        self.assertIsNone(self.call(tool="Grep"))
        self.assertIsNotNone(self.call(session="s2"))  # в другой сессии — своё напоминание

    def test_grep_tool_counts_as_search(self):
        self.assertIsNotNone(self.call(tool="Grep"))

    def test_commands_that_are_not_a_code_search_pass(self):
        for command in ("dotnet test 2>&1 | grep -E 'Failed|Passed'", "git status", "ls src | grep -c cs",
                        "gh pr checks 7", "cat file.txt"):
            self.assertIsNone(self.call(command), command)
        self.assertIsNone(self.call(tool="Read"))
        for command in ("grep -rn Foo src", "cd src && rg Foo", "git -C src grep Foo", "(grep -n Foo a.cs)"):
            self.assertIsNotNone(self.call(command, session=command), command)

    def test_only_aiflow_task_sessions_are_touched(self):
        self.assertIsNone(self.call(cwd=self.root))  # обычная сессия в корне проекта
        other = self.root / ".claude" / "worktrees" / "not-a-task"
        other.mkdir()
        self.assertIsNone(self.call(cwd=other))
        deep = self.worktree / "src" / "Api"
        deep.mkdir(parents=True)
        self.assertIsNotNone(self.call(cwd=deep))

    def test_silent_without_an_lsp_plugin(self):
        self.plugins({"csharp-lsp@claude-plugins-official": False, "aiflow@aiflow-local": True})
        self.assertIsNone(self.call())
        self.plugins({"csharp-lsp@claude-plugins-official": True}, "settings.local.json")  # local перекрывает project
        self.assertIsNotNone(self.call())

    def test_project_explorer_is_not_advertised_and_setting_turns_it_off(self):
        aiflow.write_json(self.root / "runs" / "config.json", {"explorer": "project"})
        text = self.call()
        self.assertIn("LSP", text)
        self.assertNotIn("aiflow:explorer", text)
        aiflow.write_json(self.root / "runs" / "config.json", {"search_nudge": False})
        self.assertIsNone(self.call(session="s3"))

    def test_main_blocks_with_exit_code_two(self):
        import io
        import os
        from unittest import mock
        payload = json.dumps({"session_id": "m1", "cwd": str(self.worktree), "tool_name": "Grep", "tool_input": {}})
        err = io.BytesIO()
        fake_err = mock.Mock(buffer=err)
        with mock.patch.dict(os.environ, {"AIFLOW_HOME": str(self.home)}), \
                mock.patch.object(search_hook.sys, "stdin", io.StringIO(payload)), \
                mock.patch.object(search_hook.sys, "stderr", fake_err):
            self.assertEqual(search_hook.main(), 2)
        self.assertIn("LSP", err.getvalue().decode("utf-8"))
        with mock.patch.dict(os.environ, {"AIFLOW_HOME": str(self.home)}), \
                mock.patch.object(search_hook.sys, "stdin", io.StringIO(payload)):
            self.assertEqual(search_hook.main(), 0)  # второй раз — пропустить


if __name__ == "__main__":
    unittest.main()
