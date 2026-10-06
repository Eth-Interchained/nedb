import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('storage', Path(__file__).parents[1] / 'postgres_storage.py')
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class StorageMeasurements(unittest.TestCase):
    def test_payload_is_reproducible_and_changes_with_version(self):
        body = storage.payload(12, 0, 16385)
        self.assertEqual(len(body.encode()), 16385)
        self.assertEqual(body, storage.payload(12, 0, 16385))
        self.assertNotEqual(body, storage.payload(12, 1, 16385))
        self.assertNotEqual(body, storage.payload(13, 0, 16385))

    def test_target_rounds_up_without_undersizing(self):
        rows, actual = storage.layout(1_000_000_000, 16384)
        self.assertGreaterEqual(actual, 1_000_000_000)
        self.assertLess(actual - 1_000_000_000, 16384)
        self.assertEqual(rows * 16384, actual)

    def test_sparse_file_distinguishes_apparent_and_allocated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sparse'
            with path.open('wb') as stream:
                stream.seek(10_000_000)
                stream.write(b'x')
            measured = storage.footprint(directory)
            self.assertEqual(measured['apparent_bytes'], 10_000_001)
            self.assertLess(measured['allocated_bytes'], measured['apparent_bytes'])


if __name__ == '__main__':
    unittest.main()
