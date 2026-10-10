import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import kernel_update as ku


class WriteRegionsTests(unittest.TestCase):
    def disk(self, tmp):
        path = Path(tmp) / 'disk.img'
        path.write_bytes(b'A' * 4096 + b'B' * 4096 + b'C' * 4096)
        return path

    def test_writes_and_reads_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = self.disk(tmp)
            regions = [(0, b'x' * 4096, b'A' * 4096), (8192, b'z' * 4096, b'C' * 4096)]
            ku.write_regions(disk, regions, Path(tmp) / 'rollback.json')
            data = disk.read_bytes()
            self.assertEqual(data, b'x' * 4096 + b'B' * 4096 + b'z' * 4096)
            self.assertFalse((Path(tmp) / 'rollback.json').exists())

    def test_failure_restores_every_attempted_region(self):
        with tempfile.TemporaryDirectory() as tmp:
            disk = self.disk(tmp)
            original = disk.read_bytes()
            real = ku.write_all
            calls = []

            def flaky(fd, offset, data):
                calls.append(offset)
                if len(calls) == 2:  # second region: partial write, then failure
                    real(fd, offset, data[:100])
                    raise OSError('simulated I/O error')
                real(fd, offset, data)

            regions = [(0, b'x' * 4096, b'A' * 4096), (4096, b'y' * 4096, b'B' * 4096),
                       (8192, b'z' * 4096, b'C' * 4096)]
            with mock.patch.object(ku, 'write_all', side_effect=flaky):
                with self.assertRaises(OSError):
                    ku.write_regions(disk, regions, Path(tmp) / 'rollback.json')
            self.assertEqual(disk.read_bytes(), original)
            record = (Path(tmp) / 'rollback.json').read_text()
            self.assertIn('"restored": true', record)
            self.assertNotIn('"restored": false', record)


class GuardTests(unittest.TestCase):
    def test_guard_blocks_without_lock_and_allows_with_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'kernel-update.lock'
            script = Path(tmp) / 'guard'
            script.write_text(ku.GUARD.replace('/run/asus-boot/kernel-update.lock', str(lock)))
            script.chmod(0o755)
            self.assertNotEqual(os.system(f'{script} 2>/dev/null'), 0)
            lock.touch()
            self.assertEqual(os.system(str(script)), 0)


if __name__ == '__main__':
    unittest.main()
