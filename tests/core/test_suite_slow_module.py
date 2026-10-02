"""The merge-pipeline module is slow under load; it gets a longer limit than other modules."""
import unittest

from core import suite


class SlowModuleTimeoutTests(unittest.TestCase):
    def test_merge_pipeline_gets_three_times_the_limit(self):
        self.assertEqual(suite.module_timeout("test_merge_pipeline", 1500.0), 4500.0)

    def test_other_modules_keep_the_normal_limit(self):
        self.assertEqual(suite.module_timeout("test_roles", 1500.0), 1500.0)


if __name__ == "__main__":
    unittest.main()
