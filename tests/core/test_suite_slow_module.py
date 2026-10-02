"""The merge-pipeline module is slow under load; it gets a longer limit than other modules."""
import unittest

from core import suite


class SlowModuleTimeoutTests(unittest.TestCase):
    def test_merge_pipeline_gets_three_times_the_limit(self):
        self.assertEqual(suite.module_timeout("test_merge_pipeline", 1500.0), 4500.0)

    def test_other_modules_keep_the_normal_limit(self):
        self.assertEqual(suite.module_timeout("test_roles", 1500.0), 1500.0)

    def test_every_slow_timeout_module_is_deferred_from_per_task_judges(self):
        self.assertTrue(set(suite.SLOW_MODULES) <= suite.DEFER_SLOW)

    def test_deferred_modules_exist_in_the_repo(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        self.assertTrue(suite.DEFER_SLOW <= set(suite.modules(root)))


if __name__ == "__main__":
    unittest.main()
