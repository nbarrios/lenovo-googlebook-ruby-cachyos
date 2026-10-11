#!/usr/bin/env python3
"""Update the CachyOS kernel on an installed Ruby system.

The running kernel lives in AVB-signed slot-A Android partitions, so a package
upgrade alone would leave the boot images and /usr/lib/modules out of step.
Pacman hooks (install-hooks) make `pacman -Syu` do the safe part on its own:

  hook-pre       PreTransaction: preserve the running and slot-A module trees
  hook-post      PostTransaction: restore them, build and loader-validate the new set
  commit         back up slot A, write the validated set with read-back
  confirm        after booting the new kernel, drop the preserved old modules
  rollback       restore the previous slot-A set (also from the live USB: --root /mnt)
  prepare        upgrade linux-cachyos now (--no-upgrade: rebuild for the installed one)
  install-hooks  install the hooks and confirm timer, unpin the kernel in pacman.conf
  status         show running, installed and recorded kernels

Run as root. Nothing but `commit` and `rollback` writes to the disk, and both
ask first.
"""
import argparse
from datetime import datetime
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.boot import build_set
from lapis.common import (ROOT, ROOT_MAPPERS, CACHYOS_INITRAMFS, CACHYOS_KERNEL, board, output, read_json, require, run,
                          save_json, sha256)
from lapis.gpt import read_gpt
from tools.native import write_all

NAMES = ('boot_a', 'init_boot_a', 'vendor_boot_a', 'vbmeta_a')
PACKAGES = ('linux-cachyos', 'linux-cachyos-headers')
MODULES = Path('/usr/lib/modules')
# Root-owned rescue copy; hooks run from here, never from a user's checkout.
KIT = Path('/opt/lapis-kit')
SCRIPT = KIT / 'tools/kernel_update.py'
RETRY = f'sudo {SCRIPT} prepare --no-upgrade'
TRIGGER = ('[Trigger]\nOperation = Install\nOperation = Upgrade\nOperation = Remove\nType = Path\n'
           'Target = usr/lib/modules/*/vmlinuz\n\n[Action]\n')
TIMER = 'asus-kernel-confirm.timer'
# Paths relative to the target root. The stage hook sorts after depmod and mkinitcpio.
SYSTEM_FILES = {
    'etc/pacman.d/hooks/05-asus-kernel-snapshot.hook': TRIGGER +
        "Description = Preserving the running kernel's modules (Ruby slot A)\nWhen = PreTransaction\n"
        f'Exec = /usr/bin/python3 -I {SCRIPT} hook-pre\nAbortOnFail\n',
    'etc/pacman.d/hooks/zz-asus-kernel-stage.hook': TRIGGER +
        'Description = Building and validating the Ruby slot-A boot set\nWhen = PostTransaction\n'
        f'Exec = /usr/bin/python3 -I {SCRIPT} hook-post\n',
    'etc/systemd/system/asus-kernel-confirm.service':
        '[Unit]\nDescription=Confirm a committed kernel update once it has booted\n'
        'ConditionPathExists=/var/lib/asus-boot/kernel-update/state.json\n\n'
        f'[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -I {SCRIPT} confirm --auto\n',
    f'etc/systemd/system/{TIMER}':
        '[Unit]\nDescription=Confirm a committed kernel update 10 minutes after boot\n\n'
        '[Timer]\nOnBootSec=10min\n\n[Install]\nWantedBy=timers.target\n',
}
LEGACY = ('etc/pacman.d/hooks/00-asus-kernel-guard.hook', 'usr/local/libexec/asus-boot-update-guard')
SETTLED = ('prepared', 'confirmed', 'rolled-back')


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def paths(root):
    root = Path(root)
    state = root / 'var/lib/asus-boot/kernel-update'
    return root, state, state / 'state.json'


def kit(name):
    """Prefer this checkout's build outputs, else the copy made at install time."""
    local = {'vendor': ROOT / 'vendor', 'loader': ROOT / 'build/load_android_test'}[name]
    installed = {'vendor': KIT / 'vendor', 'loader': KIT / 'build/load_android_test'}[name]
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


def kernel_dirs(modules=MODULES):
    found = {}
    for d in modules.iterdir():
        base = d / 'pkgbase'
        if base.is_file() and base.read_text().strip() == 'linux-cachyos' and (d / 'vmlinuz').is_file():
            found[d.name] = d
    return found


def owned_kernels(modules=MODULES):
    """linux-cachyos releases whose vmlinuz a package owns (pacman -Q needs no db lock)."""
    kernels = kernel_dirs(modules)
    return {r: d for r, d in kernels.items()
            if subprocess.run(['pacman', '-Qqo', str(d / 'vmlinuz')], capture_output=True).returncode == 0}


def snapshot_modules(modules, keep, releases):
    """Reflink-copy each release's module tree into keep/ before the transaction can delete it."""
    done = []
    for rel in releases:
        src, dst, tmp = modules / rel, keep / rel, keep / (rel + '.partial')
        if dst.exists() or not (src / 'kernel').is_dir():
            continue
        keep.mkdir(parents=True, exist_ok=True, mode=0o700)
        if tmp.exists():
            shutil.rmtree(tmp)
        run(['cp', '-a', '--reflink=auto', src, tmp])
        tmp.rename(dst)
        done.append(rel)
    return done


def restore_modules(modules, keep):
    """Put back every preserved tree the transaction removed."""
    done = []
    for snap in sorted(keep.iterdir()) if keep.is_dir() else ():
        dest = modules / snap.name
        if snap.name.endswith('.partial') or (dest / 'kernel').exists():
            continue
        # The directory may survive with leftover depmod indexes; copy contents, not the folder.
        dest.mkdir(parents=True, exist_ok=True)
        run(['cp', '-a', '--reflink=auto', f'{snap}/.', f'{dest}/'])
        done.append(snap.name)
    return done


def drop_snapshots(keep, owned):
    """A package-owned tree is intact; the next pre hook snapshots it again if needed."""
    for rel in owned:
        if (keep / rel).is_dir():
            shutil.rmtree(keep / rel)


def slot_release(state):
    """The kernel release slot A boots according to the state record, if known."""
    return {'prepared': state.get('previous'), 'confirmed': state.get('new'), 'committed': state.get('new'),
            'rolled-back': state.get('restored')}.get(state.get('status'))


def releases_to_keep(state, running):
    status = state.get('status')
    require(status not in ('committing', 'committed'),
            f"Slot A holds {state.get('new')} (update {status}): boot it and run kernel_update.py confirm, "
            'or roll back, before changing linux-cachyos')
    return list(dict.fromkeys(r for r in (running, slot_release(state)) if r))


def confirm_due(state, running):
    return state.get('status') == 'committed' and state.get('new') == running


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


def installed_system(power=False):
    require(os.geteuid() == 0, 'Run as root')
    require(board()[0] == 'Ruby', 'Not a Ruby board')
    require(output(['findmnt', '-no', 'SOURCE', '/']).startswith(tuple('/dev/mapper/' + m for m in ROOT_MAPPERS)),
            'Run on the installed system')
    if power:
        supplies = Path('/sys/class/power_supply').glob('*/online')
        require(any(p.read_text().strip() == '1' for p in supplies), 'Connect AC power')


def typed(phrase):
    require(sys.stdin.isatty(), 'Disk writes require an interactive terminal')
    require(input('Type exactly ' + phrase + ': ') == phrase, 'Cancelled')


def ask(question):
    require(sys.stdin.isatty(), 'Disk writes require an interactive terminal')
    require(input(question + ' [y/N] ').strip().lower() in ('y', 'yes'), 'Cancelled')


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


def write_file(path, text, mode=0o644):
    require(not path.is_symlink(), f'Refuse writing through symlink: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    tmp.write_text(text)
    tmp.chmod(mode)
    os.replace(tmp, path)


def refresh_kit(src=ROOT, dest=KIT):
    """Make the rescue copy's code (used by the hooks and live-USB rollback) equal to this code."""
    if not dest.is_dir() or src.resolve() == dest.resolve():
        return False
    for sub in ('lapis', 'tools'):
        new, old = dest / f'.{sub}.new', dest / f'.{sub}.old'
        for p in (new, old):
            if p.exists():
                shutil.rmtree(p)
        shutil.copytree(src / sub, new, ignore=shutil.ignore_patterns('__pycache__'))
        if (dest / sub).exists():
            (dest / sub).rename(old)
        new.rename(dest / sub)
        if old.exists():
            shutil.rmtree(old)
    return True


def unpin(text, packages=PACKAGES):
    """Drop the kernel packages from IgnorePkg lines; other entries and lines stay as they are."""
    out, changed = [], False
    for line in text.splitlines(keepends=True):
        m = re.match(r'(\s*IgnorePkg\s*=\s*)(.*?)(\s*)$', line)
        if m:
            keep = [p for p in m[2].split() if p not in packages]
            if keep != m[2].split():
                changed = True
                if not keep:
                    continue
                line = m[1] + ' '.join(keep) + m[3]
        out.append(line)
    return ''.join(out), changed


def still_pinned(text, packages=PACKAGES):
    """Remaining IgnorePkg patterns (globs allowed) that still match a kernel package."""
    found = []
    for m in re.finditer(r'^\s*IgnorePkg\s*=(.*)$', text, re.M):
        found += [p for p in m[1].split() if any(fnmatch.fnmatchcase(k, p) for k in packages)]
    return found


def install_files(root):
    """Write hooks and confirm units, drop the legacy guard, then unpin. Returns the pacman.conf backup."""
    root = Path(root)
    for rel, text in SYSTEM_FILES.items():
        write_file(root / rel, text)
    for rel in LEGACY:
        (root / rel).unlink(missing_ok=True)
    conf = root / 'etc/pacman.conf'
    text, changed = unpin(conf.read_text())
    backup = None
    if changed:
        backup = conf.with_name('pacman.conf.kernel-pin.bak')
        if backup.exists():
            backup = conf.with_name(f"pacman.conf.kernel-pin-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak")
        shutil.copy2(conf, backup)
        write_file(conf, text, conf.stat().st_mode & 0o777)
    for p in still_pinned(text):
        print(f'Warning: IgnorePkg entry {p!r} in {conf} still holds back the kernel; edit it by hand.')
    return backup


def stage(current, rebuild=False):
    """Build and loader-validate a slot-A set for the package-owned kernel. Never writes the disk."""
    root, state_dir, state_file = paths('/')
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        plan, device, gpt = disk_and_gpt(root)
        owned = owned_kernels()
        require(len(owned) == 1, f'Expected one package-owned kernel, found {sorted(owned)}')
        new, kdir = next(iter(owned.items()))
        if new == current and not rebuild:
            return None
        vmlinuz = kdir / 'vmlinuz'
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
        return new


def staged_message(new, current):
    return (f'==> Kernel {new} staged and validated by the ChromiumOS Android loader. No disk writes yet;\n'
            f'==> slot A still boots {current}. To switch: sudo {SCRIPT} commit, then reboot.')


def hook_pre(args):
    """PreTransaction: keep the module trees the running system and slot A need. Never calls pacman."""
    _, state_dir, state_file = paths('/')
    state = read_json(state_file) if state_file.exists() else {}
    keep = releases_to_keep(state, os.uname().release)
    present = [r for r in keep if (MODULES / r / 'kernel').is_dir() or (state_dir / 'modules' / r).is_dir()]
    require(present or os.environ.get('KERNEL_UPDATE_UNPROTECTED') == '1',
            f'No module tree for {keep} here (chroot?): changing linux-cachyos could strand slot A. '
            'Set KERNEL_UPDATE_UNPROTECTED=1 to proceed anyway')
    for rel in snapshot_modules(MODULES, state_dir / 'modules', keep):
        print(f'Preserved modules for {rel}')


def hook_post(args):
    """PostTransaction: the package is already installed, so report failures instead of aborting."""
    current = os.uname().release
    _, state_dir, _ = paths('/')
    try:
        for rel in restore_modules(MODULES, state_dir / 'modules'):
            print(f'Restored modules for {rel}')
        refresh_kit()
        installed_system()
        new = stage(current)
        drop_snapshots(state_dir / 'modules', owned_kernels())
    except Exception as error:
        print(f'==> ERROR: the new kernel package is installed but its boot set was NOT staged:\n==>   {error}\n'
              f'==> Nothing was written to disk; slot A still boots the previous kernel ({current} is running).\n'
              f'==> Fix the cause, then retry with: {RETRY}')
        return 1
    if new is None:
        print(f'linux-cachyos {current} is already running; slot A unchanged.')
    else:
        print(staged_message(new, current))


def prepare(args):
    installed_system()
    _, state_dir, state_file = paths('/')
    status = read_json(state_file)['status'] if state_file.exists() else None
    require(status in (None, *SETTLED) or args.force,
            f'Previous update is "{status}": confirm, commit or roll it back first (or --force)')
    refresh_kit()
    current = os.uname().release
    if args.no_upgrade:
        new = stage(current, rebuild=True)
    else:
        require(all(Path('/', rel).exists() for rel in SYSTEM_FILES), 'Kernel hooks missing: run kernel_update.py install-hooks first')
        # The hooks preserve modules and stage the set; prepare only retries if they could not.
        run(['pacman', '-S', '--needed', *PACKAGES])
        new = next(iter(owned_kernels()), None)
        state = read_json(state_file) if state_file.exists() else {}
        if new == current:
            print(f'linux-cachyos is already {current}; nothing to update. Use --no-upgrade to rebuild the boot set.')
            return
        if state.get('status') == 'prepared' and state.get('new') == new:
            return  # the stage hook built it and said so
        new = stage(current)
    print(staged_message(new, current))


def commit(args):
    installed_system(power=True)
    root, state_dir, state_file = paths('/')
    state = read_json(state_file)
    require(state['status'] == 'prepared', f"State is {state['status']}, expected prepared")
    plan, device, gpt = disk_and_gpt(root)
    require(gpt.guid == state['disk_guid'], 'Disk changed since prepare')
    cand = state_dir / 'candidate'
    require(sha256(cand / 'manifest.json') == state['manifest_sha256'], 'Candidate changed since validation')
    refresh_kit()
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
    ask(f"Write kernel {state['new']} to slot A, replacing {state['previous']}?")
    state.update(status='committing', backup=str(bdir))
    save_json(state_file, state)
    write_regions(device, regions, state_dir / 'rollback.json')
    state.update(status='committed', committed=datetime.now().isoformat(timespec='seconds'))
    save_json(state_file, state)
    os.sync()
    print(f"Slot A now holds {state['new']}. Reboot and unlock; {TIMER} confirms it 10 minutes after boot"
          f' (or run: sudo {SCRIPT} confirm).')
    print(f'If it fails to boot: boot the live USB, open and mount the root, then python3 /mnt{SCRIPT} rollback --root /mnt')


def confirm(args):
    _, state_dir, state_file = paths('/')
    state = read_json(state_file) if state_file.exists() else {}
    running = os.uname().release
    if args.auto and not confirm_due(state, running):
        print(f"Nothing to confirm (update {state.get('status', 'none')}, running {running})")
        return
    installed_system()
    require(state.get('status') == 'committed', f"State is {state.get('status')}, expected committed")
    require(running == state['new'], f"Running {running}, not {state['new']}: reboot into the new kernel first")
    prev = state['previous']
    stale = MODULES / prev
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


def install_hooks(args):
    require(os.geteuid() == 0, 'Run as root')
    require((KIT / 'vendor').is_dir() and (KIT / 'build/load_android_test').is_file(),
            f'{KIT} lacks vendor/ or build/load_android_test: copy the kit there first')
    refresh_kit()
    backup = install_files('/')
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', TIMER])
    print('Kernel hooks installed; the legacy guard is gone.')
    print(f'linux-cachyos unpinned in /etc/pacman.conf (backup: {backup}).' if backup else 'pacman.conf had no kernel pin.')
    print(f'From now on `pacman -Syu` stages kernel updates; then run: sudo {SCRIPT} commit')


def status(args):
    root, state_dir, state_file = paths(args.root)
    print('running  :', os.uname().release)
    owned = output(['pacman', '-Q', *PACKAGES]) if args.root == '/' else '(see target)'
    print('package  :', owned.replace('\n', ', '))
    hooks = all((root / rel).exists() for rel in SYSTEM_FILES)
    print('hooks    :', 'installed' if hooks else 'missing (run install-hooks)',
          '+ legacy guard' if (root / LEGACY[0]).exists() else '')
    if state_file.exists():
        state = read_json(state_file)
        print('update   :', state['status'], f"{state.get('previous')} -> {state.get('new')}")
    else:
        print('update   : none recorded (slot A holds the installer-built set)')


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = p.add_subparsers(required=True)
    q = subs.add_parser('prepare'); q.add_argument('--no-upgrade', action='store_true', help='rebuild for the installed kernel')
    q.add_argument('--force', action='store_true'); q.set_defaults(fn=prepare)
    subs.add_parser('commit').set_defaults(fn=commit)
    q = subs.add_parser('confirm'); q.add_argument('--auto', action='store_true', help='only if committed and running the new kernel')
    q.set_defaults(fn=confirm)
    q = subs.add_parser('rollback'); q.add_argument('--root', default='/'); q.add_argument('--backup'); q.set_defaults(fn=rollback)
    q = subs.add_parser('status'); q.add_argument('--root', default='/'); q.set_defaults(fn=status)
    subs.add_parser('hook-pre').set_defaults(fn=hook_pre)
    subs.add_parser('hook-post').set_defaults(fn=hook_post)
    subs.add_parser('install-hooks').set_defaults(fn=install_hooks)
    a = p.parse_args()
    try:
        sys.exit(a.fn(a))
    except (RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f'kernel_update: {error}')


if __name__ == '__main__':
    main()
