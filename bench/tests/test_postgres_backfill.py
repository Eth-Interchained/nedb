import importlib.util
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1]))
spec = importlib.util.spec_from_file_location('backfill', Path(__file__).parents[1] / 'postgres_backfill.py')
backfill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backfill)


class BackfillMetrics(unittest.TestCase):
    def test_ratio_uses_logical_payload_not_pg_cluster_bytes(self):
        self.assertEqual(backfill.ratio(150_000_000, 100_000_000), 1.5)
        self.assertEqual(backfill.ratio(1_500_000_000, 1_000_000_000), 1.5)
        self.assertEqual(backfill.ratio(150_000_000, 100_000_000) * 10_000_000_000_000,
                         15_000_000_000_000)

    def test_zero_source_cannot_produce_a_projection(self):
        with self.assertRaises(ValueError):
            backfill.ratio(150, 0)


if __name__ == '__main__':
    unittest.main()
