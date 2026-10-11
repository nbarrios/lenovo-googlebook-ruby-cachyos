import contextlib
import io
import os
from pathlib import Path
import shutil
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


PINNED = """[options]
IgnorePkg = linux-cachyos linux-cachyos-headers
#IgnorePkg = linux-cachyos
Architecture = x86_64 x86_64_v3

[cachyos-v3]
Include = /etc/pacman.d/cachyos-v3-mirrorlist
"""


class PacmanConfTests(unittest.TestCase):
    def test_unpin_drops_line_when_only_kernel(self):
        text, changed = ku.unpin(PINNED)
        self.assertTrue(changed)
        self.assertEqual(text, PINNED.replace('IgnorePkg = linux-cachyos linux-cachyos-headers\n', ''))
        self.assertIn('#IgnorePkg = linux-cachyos\n', text)
        self.assertEqual(ku.unpin(text), (text, False))

    def test_unpin_keeps_other_entries(self):
        text, changed = ku.unpin('[options]\nIgnorePkg   = foo linux-cachyos bar linux-cachyos-headers\nIgnorePkg=baz\n')
        self.assertTrue(changed)
        self.assertEqual(text, '[options]\nIgnorePkg   = foo bar\nIgnorePkg=baz\n')

    def test_still_pinned_sees_globs(self):
        self.assertEqual(ku.still_pinned('IgnorePkg = foo linux-cachyos*\n'), ['linux-cachyos*'])
        self.assertEqual(ku.still_pinned(ku.unpin(PINNED)[0]), [])

    def test_install_files_migrates_a_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ku.LEGACY:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text('old')
            conf = root / 'etc/pacman.conf'
            conf.write_text(PINNED)
            conf.chmod(0o644)
            backup = ku.install_files(root)
            self.assertEqual(backup.read_text(), PINNED)
            self.assertNotIn('\nIgnorePkg', conf.read_text())
            self.assertEqual(conf.stat().st_mode & 0o777, 0o644)
            for rel in ku.LEGACY:
                self.assertFalse((root / rel).exists())
            for rel, text in ku.SYSTEM_FILES.items():
                self.assertEqual((root / rel).read_text(), text)
                self.assertEqual((root / rel).stat().st_mode & 0o777, 0o644)
            self.assertIsNone(ku.install_files(root))  # idempotent, no second backup
            self.assertEqual(len(list((root / 'etc').glob('pacman.conf.*'))), 1)


class HookFileTests(unittest.TestCase):
    def hooks(self):
        return {Path(k).name: v for k, v in ku.SYSTEM_FILES.items() if k.startswith('etc/pacman.d/hooks/')}

    def test_triggers_and_actions(self):
        pre, post = self.hooks()['05-asus-kernel-snapshot.hook'], self.hooks()['zz-asus-kernel-stage.hook']
        for text in (pre, post):
            for op in ('Install', 'Upgrade', 'Remove'):
                self.assertIn(f'Operation = {op}\n', text)
            self.assertIn('Target = usr/lib/modules/*/vmlinuz\n', text)
            self.assertNotIn('/home/', text)
        self.assertIn('When = PreTransaction\nExec = /usr/bin/python3 -I /opt/lapis-kit/tools/kernel_update.py hook-pre\nAbortOnFail\n', pre)
        self.assertIn('When = PostTransaction\nExec = /usr/bin/python3 -I /opt/lapis-kit/tools/kernel_update.py hook-post\n', post)
        self.assertNotIn('AbortOnFail', post)  # invalid for PostTransaction

    def test_stage_hook_runs_after_depmod_and_mkinitcpio(self):
        names = sorted([*self.hooks(), '60-depmod.hook', '90-mkinitcpio-install.hook', 'dbus-reload.hook'])
        self.assertEqual(names[-1], 'zz-asus-kernel-stage.hook')

    def test_confirm_units(self):
        service = ku.SYSTEM_FILES['etc/systemd/system/asus-kernel-confirm.service']
        self.assertIn('confirm --auto', service)
        self.assertIn('WantedBy=timers.target', ku.SYSTEM_FILES['etc/systemd/system/' + ku.TIMER])


class ModuleTreeTests(unittest.TestCase):
    def tree(self, modules, rel):
        (modules / rel / 'kernel').mkdir(parents=True)
        (modules / rel / 'kernel/a.ko').write_text(rel)
        (modules / rel / 'modules.dep').write_text('dep')

    def test_snapshot_then_restore_after_removal(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch('builtins.print'):
            modules, keep = Path(tmp) / 'modules', Path(tmp) / 'state/modules'
            self.tree(modules, '1-cachyos')
            self.assertEqual(ku.snapshot_modules(modules, keep, ['1-cachyos', 'absent']), ['1-cachyos'])
            self.assertEqual(ku.snapshot_modules(modules, keep, ['1-cachyos']), [])  # already kept
            shutil.rmtree(modules / '1-cachyos' / 'kernel')  # pacman leaves depmod leftovers behind
            self.assertEqual(ku.restore_modules(modules, keep), ['1-cachyos'])
            self.assertEqual((modules / '1-cachyos/kernel/a.ko').read_text(), '1-cachyos')
            self.assertEqual(ku.restore_modules(modules, keep), [])  # intact trees are left alone

    def test_partial_snapshot_is_replaced_and_never_restored(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch('builtins.print'):
            modules, keep = Path(tmp) / 'modules', Path(tmp) / 'keep'
            (keep / '1-cachyos.partial').mkdir(parents=True)
            self.assertEqual(ku.restore_modules(modules, keep), [])
            self.tree(modules, '1-cachyos')
            ku.snapshot_modules(modules, keep, ['1-cachyos'])
            self.assertFalse((keep / '1-cachyos.partial').exists())
            self.assertTrue((keep / '1-cachyos/kernel/a.ko').exists())

    def test_drop_snapshots_of_owned_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp)
            (keep / 'a').mkdir(); (keep / 'b').mkdir()
            ku.drop_snapshots(keep, ['a', 'c'])
            self.assertEqual([p.name for p in keep.iterdir()], ['b'])


class StateTests(unittest.TestCase):
    def test_releases_to_keep(self):
        self.assertEqual(ku.releases_to_keep({}, 'r1'), ['r1'])
        self.assertEqual(ku.releases_to_keep(dict(status='prepared', previous='r1', new='r2'), 'r1'), ['r1'])
        self.assertEqual(ku.releases_to_keep(dict(status='confirmed', previous='r0', new='r1'), 'live'), ['live', 'r1'])
        self.assertEqual(ku.releases_to_keep(dict(status='rolled-back', new='r2', restored='r1'), 'r2'), ['r2', 'r1'])
        for status in ('committing', 'committed'):
            with self.assertRaises(RuntimeError):
                ku.releases_to_keep(dict(status=status, previous='r1', new='r2'), 'r1')

    def test_confirm_due(self):
        self.assertTrue(ku.confirm_due(dict(status='committed', new='r2'), 'r2'))
        self.assertFalse(ku.confirm_due(dict(status='committed', new='r2'), 'r1'))
        self.assertFalse(ku.confirm_due(dict(status='confirmed', new='r2'), 'r2'))
        self.assertFalse(ku.confirm_due({}, 'r2'))

    def test_commit_prompt_is_yes_no(self):
        with mock.patch('sys.stdin') as stdin, mock.patch('builtins.input') as answer:
            stdin.isatty.return_value = True
            for reply in ('y', 'YES\n'):
                answer.return_value = reply
                ku.ask('Write?')
            for reply in ('', 'n', 'WRITE KERNEL 1'):
                answer.return_value = reply
                with self.assertRaises(RuntimeError):
                    ku.ask('Write?')
            stdin.isatty.return_value = False
            answer.return_value = 'y'
            with self.assertRaises(RuntimeError):
                ku.ask('Write?')


class HookPostTests(unittest.TestCase):
    def run_post(self, **patches):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), \
                mock.patch.object(ku, 'paths', return_value=(Path(tmp), Path(tmp), Path(tmp) / 'state.json')), \
                mock.patch.object(ku, 'refresh_kit'), mock.patch.object(ku, 'installed_system'), \
                mock.patch.object(ku, 'owned_kernels', return_value={}), \
                mock.patch.multiple(ku, **patches):
            return ku.hook_post(None), out.getvalue()

    def test_failure_reports_without_writing(self):
        code, text = self.run_post(stage=mock.Mock(side_effect=RuntimeError('Loader rejected the candidate')))
        self.assertEqual(code, 1)
        self.assertIn('Loader rejected the candidate', text)
        self.assertIn('slot A still boots the previous kernel', text)
        self.assertIn('prepare --no-upgrade', text)

    def test_success_names_commit(self):
        code, text = self.run_post(stage=mock.Mock(return_value='9.9.9-1-cachyos'))
        self.assertIsNone(code)
        self.assertIn('Kernel 9.9.9-1-cachyos staged and validated', text)
        self.assertIn('kernel_update.py commit', text)


class KitTests(unittest.TestCase):
    def test_refresh_replaces_code_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dest = Path(tmp) / 'src', Path(tmp) / 'kit'
            for sub in ('lapis', 'tools'):
                (src / sub / '__pycache__').mkdir(parents=True)
                (src / sub / 'x.py').write_text('new')
                (dest / sub).mkdir(parents=True)
                (dest / sub / 'stale.py').write_text('old')
            (dest / 'vendor').mkdir()
            self.assertTrue(ku.refresh_kit(src, dest))
            self.assertEqual(sorted(p.name for p in dest.iterdir()), ['lapis', 'tools', 'vendor'])
            self.assertEqual([p.name for p in (dest / 'tools').iterdir()], ['x.py'])
            self.assertFalse(ku.refresh_kit(dest, dest))
            self.assertFalse(ku.refresh_kit(src, Path(tmp) / 'missing'))


if __name__ == '__main__':
    unittest.main()
