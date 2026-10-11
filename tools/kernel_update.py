#!/usr/bin/env python3
"""Update the pinned CachyOS kernel on an installed Ruby system.

The running kernel lives in AVB-signed slot-A Android partitions, so a package
upgrade alone would leave the boot images and /usr/lib/modules out of step.

  prepare   upgrade linux-cachyos, build and loader-validate a new boot set
  commit    back up slot A, write the validated set with read-back
  confirm   after booting the new kernel, drop the preserved old modules
  rollback  restore the previous slot-A set (also from the live USB: --root /mnt)
  status    show running, installed and recorded kernels

Run as root. Nothing but `commit` and `rollback` writes to the disk, and both
require a typed confirmation.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.boot import build_set
from lapis.common import (ROOT, CACHYOS_INITRAMFS, CACHYOS_KERNEL, board, output, read_json, require, run,
                          save_json, sha256)
from lapis.gpt import read_gpt
from tools.native import write_all

NAMES = ('boot_a', 'init_boot_a', 'vendor_boot_a', 'vbmeta_a')
PACKAGES = ('linux-cachyos', 'linux-cachyos-headers')
LOCK = Path('/run/asus-boot/kernel-update.lock')
GUARD = '''#!/bin/sh
# Kernel files may change only while tools/kernel_update.py prepare holds the
# one-time pass; the Android boot images must be rebuilt alongside them.
[ -e /run/asus-boot/kernel-update.lock ] && exit 0
echo "Kernel update blocked: run kernel_update.py prepare to upgrade and rebuild the boot images." >&2
exit 1
'''


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def paths(root):
    root = Path(root)
    state = root / 'var/lib/asus-boot/kernel-update'
    return root, state, state / 'state.json'


def kit(name):
    """Prefer this checkout's build outputs, else the copy made at install time."""
    local = {'vendor': ROOT / 'vendor', 'loader': ROOT / 'build/load_android_test'}[name]
    installed = {'vendor': Path('/opt/lapis-kit/vendor'), 'loader': Path('/opt/lapis-kit/build/load_android_test')}[name]
    found = local if local.exists() else installed
    require(found.exists(), f'Missing {name}: run tools/fetch_sources.py and tools/build_loader.py')
    return found


def disk_and_gpt(root):
    plan = read_json(Path(root) / 'etc/asus-boot/install.json')
    device = Path(plan['device'])
    size = int(output(['blockdev', '--getsize64', device]))
    with device.open('rb') as f:
        gpt = read_gpt(f, size)
    require(gpt.guid == plan['disk_guid'], 'Disk GUID differs from the installation record')
    names = {p['name'] for p in gpt.partitions()}
    require({'factory_pvmfw_a', *NAMES} <= names, 'This disk has no committed boot set')
    return plan, device, gpt


def slot_parts(gpt):
    parts = {p['name']: p for p in gpt.partitions()}
    return [parts[n] for n in NAMES], parts['misc']


def read_region(device, offset, size):
    with Path(device).open('rb') as f:
        f.seek(offset)
        data = f.read(size)
    require(len(data) == size, 'Short disk read')
    return data


def kernel_dirs():
    found = {}
    for d in Path('/usr/lib/modules').iterdir():
        base = d / 'pkgbase'
        if base.is_file() and base.read_text().strip() == 'linux-cachyos' and (d / 'vmlinuz').is_file():
            found[d.name] = d
    return found


def build_candidate(vmlinuz, initramfs, cmdline, dest, sizes, key):
    require(initramfs.stat().st_size < 29 * 1024**2, 'Initramfs exceeds the vendor_boot budget')
    return build_set(vmlinuz, initramfs, cmdline, dest, kit('vendor'), sizes=sizes, installed=True,
                     kernel_sha256=sha256(vmlinuz), key=key)


def validate_candidate(cand, gpt, misc_part, misc_sector, workdir):
    """Assemble a sparse copy of the disk as it would be after commit and boot-test it."""
    slots, _ = slot_parts(gpt)
    image = workdir / 'loader-test.sparse.img'
    image.unlink(missing_ok=True)
    regions = []
    with image.open('xb') as f:
        f.truncate(gpt.disk_bytes)
        f.seek(0); f.write(gpt.primary)
        f.seek(gpt.disk_bytes - len(gpt.secondary)); f.write(gpt.secondary)
        f.seek(misc_part['start_lba'] * 512); f.write(misc_sector)
        for part in slots:
            data = (cand / (part['name'] + '.img')).read_bytes()
            require(len(data) == part['size'], f"{part['name']}: image size differs from partition")
            f.seek(part['start_lba'] * 512); f.write(data)
            regions.append(dict(name=part['name'], offset=part['start_lba'] * 512, size=part['size'], sha256=sha256(cand / (part['name'] + '.img'))))
    result = subprocess.run([str(kit('loader')), '-b', '1', str(image)], cwd=workdir, capture_output=True, text=True)
    (workdir / 'loader-validation.log').write_text(result.stdout + result.stderr)
    image.unlink()
    (workdir / 'loaded-ramdisk.img').unlink(missing_ok=True)
    require(result.returncode == 0 and 'vb2api_load_kernel() returned 0' in result.stdout,
            f'Loader rejected the candidate; see {workdir}/loader-validation.log')
    return regions


def check_initramfs(path):
    listing = output(['lsinitcpio', path])
    require(all(x not in listing for x in ('avb-private', 'luks-header', '.nmconnection', 'authorized_keys')), 'Private file in initramfs')
    require(all(x in listing for x in ('corebootdrm', 'encrypt', 'nvme')), 'Initramfs lacks framebuffer, encrypt or NVMe support')


def installed_system():
    require(os.geteuid() == 0, 'Run as root')
    require(board()[0] == 'Ruby', 'Not a Ruby board')
    require(output(['findmnt', '-no', 'SOURCE', '/']).startswith('/dev/mapper/omarchy_root'), 'Run on the installed system')
    supplies = Path('/sys/class/power_supply').glob('*/online')
    require(any(p.read_text().strip() == '1' for p in supplies), 'Connect AC power')


def typed(phrase):
    require(sys.stdin.isatty(), 'Disk writes require an interactive terminal')
    require(input('Type exactly ' + phrase + ': ') == phrase, 'Cancelled')


def write_regions(device, regions, record):
    """Write (offset, new, old) regions with read-back; restore all attempted regions on failure."""
    fd = os.open(device, os.O_RDWR | os.O_SYNC)
    attempted = []
    try:
        for offset, new, old in regions:
            attempted.append((offset, old))
            write_all(fd, offset, new)
            require(os.pread(fd, len(new), offset) == new, 'Disk read-back failed')
    except BaseException:
        status = []
        for offset, old in reversed(attempted):
            try:
                write_all(fd, offset, old)
                require(os.pread(fd, len(old), offset) == old, 'Rollback read-back failed')
                status.append(dict(offset=offset, restored=True))
            except BaseException as error:
                status.append(dict(offset=offset, restored=False, error=str(error)))
        save_json(record, status)
        print(f'Write failed; attempted regions restored, see {record}. Do not reboot until resolved.')
        raise
    finally:
        os.close(fd)


def prepare(args):
    installed_system()
    root, state_dir, state_file = paths('/')
    if state_file.exists():
        status = read_json(state_file)['status']
        require(status in ('confirmed', 'rolled-back') or args.force,
                f'Previous update is "{status}": confirm, commit or roll it back first (or --force)')
    plan, device, gpt = disk_and_gpt(root)
    current = os.uname().release
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not args.no_upgrade:
        # Keep the running kernel's modules: the package upgrade deletes them.
        keep = state_dir / 'modules' / current
        if not keep.exists():
            keep.parent.mkdir(parents=True, exist_ok=True)
            run(['cp', '-a', '--reflink=auto', f'/usr/lib/modules/{current}', keep])
        put_guard()
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(str(os.getpid()))
        try:
            run(['pacman', '-S', '--needed', *PACKAGES])
        finally:
            LOCK.unlink(missing_ok=True)
        if not (Path('/usr/lib/modules') / current / 'kernel').exists():
            # The directory may survive with leftover depmod indexes; copy contents, not the folder.
            (Path('/usr/lib/modules') / current).mkdir(exist_ok=True)
            run(['cp', '-a', '--reflink=auto', f'{keep}/.', f'/usr/lib/modules/{current}/'])
            print(f'Restored running kernel modules for {current}')
    kernels = kernel_dirs()
    require(len(kernels) >= 1, 'No linux-cachyos module tree found')
    owned = [r for r in kernels if subprocess.run(['pacman', '-Qqo', str(kernels[r] / 'vmlinuz')], capture_output=True).returncode == 0]
    require(len(owned) == 1, f'Expected one package-owned kernel, found {owned}')
    new = owned[0]
    if new == current and not args.no_upgrade:
        print(f'linux-cachyos is already {current}; nothing to update. Use --no-upgrade to rebuild the boot set.')
        return
    vmlinuz = kernels[new] / 'vmlinuz'
    boot_copy = Path(CACHYOS_KERNEL)
    if not boot_copy.exists() or sha256(boot_copy) != sha256(vmlinuz):
        shutil.copyfile(vmlinuz, boot_copy)
    initramfs = Path(CACHYOS_INITRAMFS)
    run(['mkinitcpio', '-c', '/etc/asus-boot/mkinitcpio.conf', '-k', new, '-g', initramfs])
    check_initramfs(initramfs)
    cmdline = Path('/etc/kernel/cmdline').read_text().strip()
    cand = state_dir / 'candidate'
    if cand.exists():
        shutil.rmtree(cand)
    sizes = {p['name']: p['size'] for p in gpt.partitions()}
    manifest = build_candidate(vmlinuz, initramfs, cmdline, cand, sizes, Path('/etc/asus-boot/avb-private.pem'))
    _, misc = slot_parts(gpt)
    misc_sector = read_region(device, misc['start_lba'] * 512, 512)
    require(misc_sector[:32] == bytes(32), 'Android misc boot command is set; inspect before updating')
    regions = validate_candidate(cand, gpt, misc, misc_sector, state_dir)
    save_json(state_file, dict(status='prepared', previous=current, new=new, disk_guid=gpt.guid,
                               manifest_sha256=sha256(cand / 'manifest.json'), regions=regions,
                               kernel_sha256=manifest['kernel_sha256'], initramfs_sha256=manifest['initramfs_sha256'],
                               prepared=datetime.now().isoformat(timespec='seconds')))
    print(f'Boot set for {new} built and accepted by the ChromiumOS Android loader. No disk writes yet.')
    print('Next: kernel_update.py commit')


def commit(args):
    installed_system()
    root, state_dir, state_file = paths('/')
    state = read_json(state_file)
    require(state['status'] == 'prepared', f"State is {state['status']}, expected prepared")
    plan, device, gpt = disk_and_gpt(root)
    require(gpt.guid == state['disk_guid'], 'Disk changed since prepare')
    cand = state_dir / 'candidate'
    require(sha256(cand / 'manifest.json') == state['manifest_sha256'], 'Candidate changed since validation')
    slots, _ = slot_parts(gpt)
    regions, backup = [], {}
    for part, rec in zip(slots, state['regions']):
        require(rec['name'] == part['name'] and rec['offset'] == part['start_lba'] * 512, 'Partition layout changed')
        new = (cand / (part['name'] + '.img')).read_bytes()
        require(len(new) == rec['size'] and sha256(cand / (part['name'] + '.img')) == rec['sha256'], 'Candidate image changed')
        old = read_region(device, rec['offset'], rec['size'])
        regions.append((rec['offset'], new, old))
        backup[part['name']] = old
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    bdir = state_dir / 'slots' / f"{state['previous']}-{stamp}"
    bdir.mkdir(parents=True, mode=0o700)
    for name, data in backup.items():
        (bdir / f'{name}.img').write_bytes(data)
        require(sha256(bdir / f'{name}.img') == sha256_bytes(data), 'Backup write verification failed')
    records = [dict(name=p['name'], offset=p['start_lba'] * 512, size=p['size'], sha256=sha256_bytes(backup[p['name']])) for p in slots]
    save_json(bdir / 'manifest.json', dict(kernel=state['previous'], disk_guid=gpt.guid, regions=records))
    os.sync()
    print(f"Current slot A ({state['previous']}) backed up to {bdir}")
    print(f"Will write {', '.join(NAMES)} for {state['new']}. GPT and misc stay unchanged.")
    typed(f"WRITE KERNEL {state['new']}")
    state.update(status='committing', backup=str(bdir))
    save_json(state_file, state)
    write_regions(device, regions, state_dir / 'rollback.json')
    state.update(status='committed', committed=datetime.now().isoformat(timespec='seconds'))
    save_json(state_file, state)
    os.sync()
    print(f"Slot A now holds {state['new']}. Reboot, unlock, then run: kernel_update.py confirm")
    print(f"If it fails to boot: boot the live USB, open and mount the root, then kernel_update.py rollback --root /mnt")


def confirm(args):
    installed_system()
    root, state_dir, state_file = paths('/')
    state = read_json(state_file)
    require(state['status'] == 'committed', f"State is {state['status']}, expected committed")
    running = os.uname().release
    require(running == state['new'], f"Running {running}, not {state['new']}: reboot into the new kernel first")
    prev = state['previous']
    stale = Path('/usr/lib/modules') / prev
    if stale.exists() and subprocess.run(['pacman', '-Qqo', str(stale)], capture_output=True).returncode != 0:
        shutil.rmtree(stale)
    keep = state_dir / 'modules' / prev
    if keep.exists():
        shutil.rmtree(keep)
    backups = sorted((state_dir / 'slots').iterdir(), key=lambda p: p.stat().st_mtime)
    for old in backups[:-2]:
        shutil.rmtree(old)
    state.update(status='confirmed', confirmed=datetime.now().isoformat(timespec='seconds'))
    save_json(state_file, state)
    print(f'{running} confirmed. Kept the two most recent slot-A backups.')


def rollback(args):
    require(os.geteuid() == 0, 'Run as root')
    root, state_dir, state_file = paths(args.root)
    plan, device, gpt = disk_and_gpt(root)
    if args.backup:
        bdir = Path(args.backup)
    else:
        candidates = sorted((state_dir / 'slots').iterdir(), key=lambda p: p.stat().st_mtime)
        require(candidates, 'No slot-A backups recorded')
        bdir = candidates[-1]
    manifest = read_json(bdir / 'manifest.json')
    require(manifest['disk_guid'] == gpt.guid, 'Backup belongs to another disk')
    slots, _ = slot_parts(gpt)
    regions = []
    for part, rec in zip(slots, manifest['regions']):
        require(rec['name'] == part['name'] and rec['offset'] == part['start_lba'] * 512 and rec['size'] == part['size'],
                'Backup does not match the partition layout')
        data = (bdir / f"{rec['name']}.img").read_bytes()
        require(sha256_bytes(data) == rec['sha256'], f"Backup {rec['name']} is corrupt")
        regions.append((rec['offset'], data, read_region(device, rec['offset'], rec['size'])))
    print(f"Will restore slot A to {manifest['kernel']} from {bdir}")
    typed(f"RESTORE KERNEL {manifest['kernel']}")
    write_regions(device, regions, state_dir / 'rollback-restore.json')
    if state_file.exists():
        state = read_json(state_file)
        state.update(status='rolled-back', restored=manifest['kernel'])
        save_json(state_file, state)
    os.sync()
    print(f"Slot A restored to {manifest['kernel']}. Its modules are kept under /usr/lib/modules/{manifest['kernel']}.")


def status(args):
    root, state_dir, state_file = paths(args.root)
    print('running  :', os.uname().release)
    owned = output(['pacman', '-Q', *PACKAGES]) if args.root == '/' else '(see target)'
    print('package  :', owned.replace('\n', ', '))
    if state_file.exists():
        state = read_json(state_file)
        print('update   :', state['status'], f"{state.get('previous')} -> {state.get('new')}")
    else:
        print('update   : none recorded (slot A holds the installer-built set)')


def put_guard():
    path = Path('/usr/local/libexec/asus-boot-update-guard')
    if not path.exists() or path.read_text() != GUARD:
        path.write_text(GUARD)
        path.chmod(0o755)


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = p.add_subparsers(required=True)
    q = subs.add_parser('prepare'); q.add_argument('--no-upgrade', action='store_true', help='rebuild for the installed kernel')
    q.add_argument('--force', action='store_true'); q.set_defaults(fn=prepare)
    subs.add_parser('commit').set_defaults(fn=commit)
    subs.add_parser('confirm').set_defaults(fn=confirm)
    q = subs.add_parser('rollback'); q.add_argument('--root', default='/'); q.add_argument('--backup'); q.set_defaults(fn=rollback)
    q = subs.add_parser('status'); q.add_argument('--root', default='/'); q.set_defaults(fn=status)
    a = p.parse_args(); a.fn(a)


if __name__ == '__main__':
    main()
