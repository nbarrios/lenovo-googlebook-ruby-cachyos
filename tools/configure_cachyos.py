#!/usr/bin/env python3
"""Install CachyOS and configure the mounted encrypted root of a Ruby Googlebook."""
import argparse
import getpass
import os
from pathlib import Path
import re
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lapis.common import (ROOT, CACHYOS_INITRAMFS, CACHYOS_KERNEL, CACHYOS_KEY, board, output,
                          read_json, require, run, save_json, sha256)
from tools.native import STATE, TARGET, current, mounted, checked_backup
from tools.configure_target import put, mask, chroot

# Panther Lake has AVX2 but no AVX-512: x86-64-v3 builds.
CACHYOS_SERVERS = ['https://cdn77.cachyos.org/repo/{arch}/$repo', 'https://us.cachyos.org/repo/{arch}/$repo']
ARCH_MIRRORS = 'Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch\nServer = https://mirror.rackspace.com/archlinux/$repo/os/$arch\n'
REPOS = [('cachyos-v3', 'x86_64_v3'), ('cachyos-core-v3', 'x86_64_v3'), ('cachyos-extra-v3', 'x86_64_v3'), ('cachyos', 'x86_64')]
KERNEL_PKGS = ('linux-cachyos', 'linux-cachyos-headers')
PACKAGES = [
    'base', 'base-devel', *KERNEL_PKGS, 'linux-firmware', 'intel-ucode', 'sof-firmware', 'alsa-ucm-conf',
    'btrfs-progs', 'cryptsetup', 'mkinitcpio', 'sudo', 'python', 'git', 'nano', 'vim', 'man-db', 'fish',
    'cachyos-keyring', 'cachyos-mirrorlist', 'cachyos-v3-mirrorlist', 'cachyos-settings', 'cachyos-fish-config',
    'networkmanager', 'pipewire', 'pipewire-pulse', 'pipewire-alsa', 'wireplumber',
    'mesa', 'vulkan-intel', 'intel-media-driver', 'snapper',
]
XE_OPTIONS = 'gsc_firmware_path= enable_panel_replay=0 enable_psr=0 enable_fbc=0 enable_dsb=0 enable_sagv=0 enable_dc=0'


def pacman_conf(live):
    """Live (stock pacman) needs literal v3 URLs; the target's CachyOS pacman reads the mirrorlists."""
    text = '[options]\nArchitecture = x86_64 x86_64_v3\nSigLevel = Required DatabaseOptional\nLocalFileSigLevel = Optional\nParallelDownloads = 5\n'
    for repo, arch in REPOS:
        text += f'\n[{repo}]\n'
        if live:
            text += ''.join('Server = ' + s.format(arch=arch) + '\n' for s in CACHYOS_SERVERS)
        else:
            text += 'Include = /etc/pacman.d/' + ('cachyos-v3-mirrorlist' if arch == 'x86_64_v3' else 'cachyos-mirrorlist') + '\n'
    for repo in ('core', 'extra', 'multilib'):
        text += f'\n[{repo}]\n' + (ARCH_MIRRORS if live else 'Include = /etc/pacman.d/mirrorlist\n')
    return text


def kernel_release():
    found = [d for d in (TARGET / 'usr/lib/modules').iterdir()
             if (d / 'pkgbase').is_file() and (d / 'pkgbase').read_text().strip() == 'linux-cachyos']
    require(len(found) == 1, 'Expected exactly one linux-cachyos module tree')
    return found[0]


def packages(args):
    current(); mounted()
    require(board()[0] == 'Ruby', 'CachyOS target has only been surveyed on Ruby')
    require(sys.stdin.isatty(), 'Package setup needs a local terminal for the private account-password prompt')
    require(not (STATE / 'packages-started').exists(), 'Packages already attempted; inspect failure before manual resumption')
    require(re.fullmatch('[a-z][a-z0-9_-]{0,30}', args.user) and args.user not in ('root', 'asuslive'), 'Invalid username')
    require('\n' not in args.name and '\r' not in args.name and ':' not in args.name, 'Invalid full name')
    require((Path('/usr/share/zoneinfo') / args.timezone).is_file() and '..' not in args.timezone.split('/'), 'Invalid timezone')
    require(re.fullmatch('[a-zA-Z0-9_-]+', args.keymap), 'Invalid console keymap')
    require(re.fullmatch('[a-z0-9][a-z0-9-]{0,62}', args.hostname), 'Invalid hostname')
    require(args.shell in ('/usr/bin/fish', '/bin/bash'), 'Shell must be /usr/bin/fish or /bin/bash')
    probe = subprocess_ok(['curl', '-sfI', '--max-time', '15', 'https://cdn77.cachyos.org/repo/x86_64/cachyos/cachyos.db'])
    require(probe, 'No network: connect Wi-Fi with iwctl first')
    save_json(STATE / 'account.json', dict(user=args.user, name=args.name, timezone=args.timezone,
                                           keymap=args.keymap, hostname=args.hostname, shell=args.shell))
    (STATE / 'packages-started').touch(mode=0o600)
    # Trust the bundled public CachyOS key in the live (RAM) keyring; pacstrap copies it to the target.
    keyfile = ROOT / 'assets/cachyos/cachyos.gpg'
    shown = output(['gpg', '--show-keys', '--with-colons', keyfile])
    require(f'fpr:::::::::{CACHYOS_KEY}:' in shown, 'Bundled CachyOS key fingerprint mismatch')
    run(['pacman-key', '--add', keyfile])
    run(['pacman-key', '--lsign-key', CACHYOS_KEY])
    conf = STATE / 'cachyos-live-pacman.conf'
    conf.write_text(pacman_conf(live=True))
    run(['pacstrap', '-C', conf, '-M', TARGET, *PACKAGES])
    put('/etc/pacman.d/mirrorlist', ARCH_MIRRORS)
    modules = kernel_release()
    vmlinuz = TARGET / CACHYOS_KERNEL.lstrip('/')
    if not vmlinuz.exists():
        vmlinuz.write_bytes((modules / 'vmlinuz').read_bytes())
    require(sha256(vmlinuz) == sha256(modules / 'vmlinuz'), 'Installed /boot kernel differs from its module tree')
    chroot('useradd', '-m', '-s', args.shell, '-G', 'wheel', '-c', args.name, args.user)
    password = getpass.getpass('New login/sudo password (may differ from disk password): ')
    require(len(password) >= 12 and '\n' not in password and '\0' not in password, 'Use at least 12 characters')
    require(password == getpass.getpass('Repeat login password: '), 'Passwords differ')
    chroot('chpasswd', input=(args.user + ':' + password + '\n').encode())
    del password
    chroot('passwd', '-l', 'root')
    save_json(STATE / 'packages.json', dict(status='CachyOS packages installed', kernel_release=modules.name,
                                            kernel_sha256=sha256(vmlinuz)))
    print('Packages installed for kernel', modules.name, '- next: configure')


def subprocess_ok(argv):
    return subprocess.run(argv, capture_output=True).returncode == 0


def configure(args):
    p, device, _ = current(); mounted(); backup = checked_backup(device)
    require(board()[0] == 'Ruby', 'CachyOS target has only been surveyed on Ruby')
    installed = read_json(STATE / 'packages.json')
    require(not (STATE / 'configure-started').exists(), 'Configuration already attempted; inspect before resuming')
    account = read_json(STATE / 'account.json')
    user, release = account['user'], installed['kernel_release']
    require(sha256(TARGET / CACHYOS_KERNEL.lstrip('/')) == installed['kernel_sha256'], 'Kernel changed since package stage')
    (STATE / 'configure-started').touch(mode=0o600)
    fs = read_json(STATE / 'format.json')['btrfs_uuid']
    put('/etc/fstab', ''.join(f'UUID={fs} {mount} btrfs noatime,compress=zstd:3,subvol={sub} 0 0\n' for mount, sub in
                              [('/', '@'), ('/home', '@home'), ('/var/log', '@log'), ('/var/cache/pacman/pkg', '@pkg')]))
    host = account['hostname']
    put('/etc/hostname', host + '\n')
    put('/etc/hosts', f'127.0.0.1 localhost\n::1 localhost\n127.0.1.1 {host}.localdomain {host}\n')
    put('/etc/locale.gen', 'en_US.UTF-8 UTF-8\n')
    put('/etc/locale.conf', 'LANG=en_US.UTF-8\n')
    put('/etc/vconsole.conf', 'KEYMAP=' + account['keymap'] + '\n')
    localtime = TARGET / 'etc/localtime'
    localtime.unlink(missing_ok=True)
    localtime.symlink_to('../usr/share/zoneinfo/' + account['timezone'])
    put('/etc/adjtime', '0.0 0 0.0\n0\nUTC\n')
    put('/etc/sudoers.d/10-wheel', '%wheel ALL=(ALL:ALL) ALL\n', 0o440)
    chroot('locale-gen'); chroot('systemd-machine-id-setup')
    # Graphics: firmware framebuffer through LUKS unlock, then one explicit Xe load.
    put('/etc/modprobe.d/ruby-xe.conf', 'blacklist xe\noptions xe ' + XE_OPTIONS + '\n')
    put('/etc/systemd/system/ruby-gpu.service', (ROOT / 'assets/installed/ruby-gpu.service').read_text())
    put('/etc/modprobe.d/ruby-audio.conf', (ROOT / 'assets/installed/ruby-audio.conf').read_text())
    # The factory ESP and Android partitions must not be auto-mounted.
    mask('/etc/systemd/system-generators/systemd-gpt-auto-generator')
    resolv = TARGET / 'etc/resolv.conf'; resolv.unlink(missing_ok=True); resolv.symlink_to('../run/systemd/resolve/stub-resolv.conf')
    chroot('visudo', '-c')
    chroot('systemctl', 'enable', 'ruby-gpu.service', 'NetworkManager.service', 'systemd-resolved.service', 'systemd-timesyncd.service')
    chroot('systemctl', 'mask', 'sshd.service', 'efi.mount', 'efi.automount')
    chroot('systemctl', 'set-default', 'multi-user.target')
    # Kernel pin: the running kernel lives in signed Android partitions, not /boot.
    conf = pacman_conf(live=False).replace('[options]\n', '[options]\nIgnorePkg = ' + ' '.join(KERNEL_PKGS) + '\n', 1)
    put('/etc/pacman.conf', conf)
    put('/usr/local/libexec/asus-boot-update-guard', '#!/bin/sh\necho "Kernel update blocked: rebuild and validate Android boot images first." >&2\nexit 1\n', 0o755)
    put('/etc/pacman.d/hooks/00-asus-kernel-guard.hook', '[Trigger]\nOperation = Upgrade\nOperation = Install\nOperation = Remove\nType = Path\nTarget = usr/lib/modules/*/vmlinuz\n[Action]\nDescription = Protect kernel and Android boot pair\nWhen = PreTransaction\nExec = /usr/local/libexec/asus-boot-update-guard\nAbortOnFail\n')
    put('/etc/asus-boot/mkinitcpio.conf', (ROOT / 'assets/installed/mkinitcpio.conf').read_text())
    save_json(TARGET / 'etc/asus-boot/install.json', p)
    (TARGET / 'etc/asus-boot/luks-header.bin').write_bytes((backup / 'luks-header.bin').read_bytes())
    (TARGET / 'etc/asus-boot/luks-header.bin').chmod(0o600)
    chroot('/usr/bin/mkinitcpio', '-c', '/etc/asus-boot/mkinitcpio.conf', '-k', release, '-g', CACHYOS_INITRAMFS)
    listing = output(['arch-chroot', TARGET, 'lsinitcpio', CACHYOS_INITRAMFS])
    require(all(x not in listing for x in ('avb-private', 'luks-header', '.nmconnection', 'authorized_keys')), 'Private file in initramfs')
    require(all(x in listing for x in ('corebootdrm', 'encrypt')), 'Initramfs lacks framebuffer or encrypt support')
    require((TARGET / CACHYOS_INITRAMFS.lstrip('/')).stat().st_size < 29 * 1024**2, 'Initramfs exceeds partition budget')
    chroot('chown', '-R', user + ':' + user, '/home/' + user)
    save_json(STATE / 'configured.json', dict(status='Configured; boot partitions not yet committed', distro='cachyos',
                                              kernel=CACHYOS_KERNEL, initramfs=CACHYOS_INITRAMFS,
                                              kernel_release=release, kernel_sha256=installed['kernel_sha256']))
    print('Configuration complete. First boot is a text login; connect Wi-Fi with nmtui, then install niri/noctalia.')


def main():
    os.umask(0o022)
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(required=True)
    q = subs.add_parser('packages'); q.add_argument('--user', required=True); q.add_argument('--name', required=True)
    q.add_argument('--timezone', required=True); q.add_argument('--keymap', default='us')
    q.add_argument('--hostname', default='googlebook'); q.add_argument('--shell', default='/usr/bin/fish')
    q.set_defaults(fn=packages)
    q = subs.add_parser('configure'); q.set_defaults(fn=configure)
    a = p.parse_args(); a.fn(a)


if __name__ == '__main__':
    main()
