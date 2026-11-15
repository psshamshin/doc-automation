"""Форма правки агента: поиск фрагмента с точностью до пробелов и тире, сохранение формата, таблицы, переменные."""

import tempfile
import unittest
from pathlib import Path

from pipeline import docs as DOCS
from pipeline.agent.verify import rename_pairs, fit_table, keep_unchanged, locate, match_dash_style

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


class DashStyleTest(unittest.TestCase):
    PAGE = '- "А" \u2014 раздел а\n- "Б" \u2014 раздел б\n- "В" \u2014 раздел в\n'

    def test_new_line_follows_the_dash_of_the_page(self):
        self.assertEqual(match_dash_style(self.PAGE, '- "А" \u2014 раздел а', '- "А" \u2014 раздел а\n- "Г" - раздел г'),
                         '- "А" \u2014 раздел а\n- "Г" \u2014 раздел г')

    def test_page_without_such_dashes_is_left_alone(self):
        self.assertEqual(match_dash_style("текст", "а", '- "Г" - раздел'), '- "Г" - раздел')


class RenamePairsTest(unittest.TestCase):
    def test_similar_names_are_paired_and_deleted_ones_are_not(self):
        find = 'Разделы: "Картотека", "Списки", "Источники событий".'
        new = 'Разделы: "Картотека и списки", "Источники".'
        self.assertEqual(rename_pairs(find, new), [("Источники событий", "Источники"), ("Картотека", "Картотека и списки")])


class TableTest(unittest.TestCase):
    FIND = "+------+------+\n| a    | b    |\n+------+------+"

    def test_row_is_fitted_to_the_frame(self):
        new, problem = fit_table(self.FIND, "+------+------+\n| a    | bbb |\n+------+------+")
        self.assertIsNone(problem)
        self.assertEqual(new.split("\n")[1], "| a    | bbb  |")

    def test_text_that_does_not_fit_is_rejected(self):
        _, problem = fit_table(self.FIND, "+------+------+\n| a    | bbbbbbbbb |\n+------+------+")
        self.assertIn("не помещается", problem)

    def test_shifted_column_is_rejected(self):
        _, problem = fit_table(self.FIND, "+------+------+\n| aaaaaa| b    |\n+------+------+")
        self.assertIn("колонки", problem)

    def test_text_without_table_is_not_checked(self):
        self.assertEqual(fit_table("просто текст", "другой текст"), ("другой текст", None))


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
