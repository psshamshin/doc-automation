"""Форма правки агента: поиск фрагмента с точностью до пробелов и тире, сохранение формата, таблицы, переменные."""

import tempfile
import unittest
from pathlib import Path

from pipeline import docs as DOCS
from pipeline.agent.verify import keep_unchanged, locate, table_width_problem

PAGE = "# Раздел\n\n- Пункт один — первый; \n- Пункт два — второй.\n\nКонец."


class LocateTest(unittest.TestCase):
    def test_dash_and_trailing_spaces_do_not_matter(self):
        agent_find = "- Пункт один - первый;\n- Пункт два - второй."
        self.assertEqual(locate(PAGE, agent_find), "- Пункт один — первый; \n- Пункт два — второй.")

    def test_ambiguous_or_missing_block_is_not_guessed(self):
        self.assertIsNone(locate(PAGE + "\n" + PAGE, "- Пункт два - второй."))
        self.assertIsNone(locate(PAGE, "- Совсем другое."))


class KeepUnchangedTest(unittest.TestCase):
    def test_lines_that_did_not_change_keep_their_formatting(self):
        find = "- Пункт один — первый; \n- Пункт два — второй."
        new = "- Пункт один - первый;\n- Пункт два - второй.\n- Пункт три."
        self.assertEqual(keep_unchanged(find, new), "- Пункт один — первый; \n- Пункт два — второй.\n- Пункт три.")

    def test_changed_line_is_taken_from_the_answer(self):
        self.assertEqual(keep_unchanged("а\nб", "а\nв"), "а\nв")


class TableTest(unittest.TestCase):
    FIND = "+-----+-----+\n| a   | b   |\n+-----+-----+"

    def test_same_width_is_fine_and_other_is_rejected(self):
        self.assertIsNone(table_width_problem(self.FIND, "+-----+-----+\n| aa  | b   |\n+-----+-----+"))
        self.assertIn("другой ширины", table_width_problem(self.FIND, "+-----+-----+\n| aaaa  | b   |\n+-----+-----+"))

    def test_text_without_table_is_not_checked(self):
        self.assertIsNone(table_width_problem("просто текст", "другой текст"))


class VariablesTest(unittest.TestCase):
    def test_values_are_read_from_ci_file_of_the_repo(self):
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t)
            (repo / "docs").mkdir()
            (repo / ".gitlab-ci.yml").write_text('variables: \n  A_NAME: "LUNA POINT"\n  PREFIX: LP5\n\nstages:\n  - x\n', encoding="utf-8")
            self.assertEqual(DOCS.ci_variables(repo / "docs"), {"A_NAME": "LUNA POINT", "PREFIX": "LP5"})

    def test_no_ci_file_gives_empty_values(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(DOCS.ci_variables(Path(t)), {})


if __name__ == "__main__":
    unittest.main()
