import unittest

from pipeline import import_graph as IG
from tests.helpers import NEW, OLD

ENTRY = ["apps/point/src"]


class ReachabilityTest(unittest.TestCase):
    def setUp(self):
        self.new = IG.reachable_from(NEW, ENTRY)
        self.files = set(self.new["files"])

    def test_workspace_package_and_relative_imports(self):
        self.assertIn("apps/point/src/app/navigation/i18n.ts", self.files)
        self.assertIn("packages/monokit/features/orders-filter/OrdersFilter.tsx", self.files)
        # через относительный импорт из OrdersFilter
        self.assertIn("packages/monokit/shared/ui/select-box/SelectBox.tsx", self.files)
        self.assertIn("packages/monokit/shared/ui/select-box/i18n.ts", self.files)

    def test_other_app_code_is_not_reachable(self):
        self.assertNotIn("packages/monokit/widgets/report-panel/ReportPanel.point3.tsx", self.files)
        self.assertNotIn("apps/point3/src/main.tsx", self.files)

    def test_no_unresolved_imports(self):
        self.assertEqual(self.new["unresolved_internal"], {})

    def test_old_tree(self):
        old = set(IG.reachable_from(OLD, ENTRY)["files"])
        self.assertIn("packages/monokit/features/select-box-legacy/SelectBox.tsx", old)

    def test_specs(self):
        src = "import a from './a';\n// import b from './b';\nconst c = require('./c');\nconst d = import('./d');\n"
        self.assertEqual(sorted(IG.specs_of(src)), ["./a", "./c", "./d"])


if __name__ == "__main__":
    unittest.main()
