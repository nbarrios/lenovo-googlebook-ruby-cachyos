# CachyOS on Lenovo Googlebook 15 (Ruby)

Experimental port of the Lapis procedure to the Lenovo Googlebook 15 (DMI `Google / Ruby`, `Google_Fatcat` family, firmware `Google_Ruby.16650.143.0`). Read [install.md](install.md) and [recovery.md](recovery.md) first. This page lists only what differs.

## What was physically surveyed (9 October 2026, one laptop)

| Item | Ruby result |
| --- | --- |
| Live USB boot via **Boot from external drive** | Works |
| GPT | Byte-identical to `assets/lapis-512gb.json` (all 30 partitions, 512 GB disk) |
| Graphics | Same `0xb090` device. **One plain Xe load** hands the panel over cleanly; the Lapis unload/reload is not used |
| Hyprland live desktop, keyboard, ELAN touchpad, Goodix touchscreen | Work |
| Wi-Fi | Intel CNVi `8086:e440`, firmware loads |
| Audio | RT5682S headset codec + 4x TI TAS2563 amplifiers on I2S, SOF topology `sof-ptl-tas2563-rt5682.tplg`. Speakers verified 10 October 2026; headphones/mics enumerate |
| CachyOS internal install | Completed and booted from internal disk, 10 October 2026 |

## Differences from the Lapis/Omarchy install

- Install from the **console-only** live image (`tools/build_live.py --console-only`). Xe never loads there, so mkinitcpio's autodetect does not pull Xe and its firmware into the small boot initramfs.
- `tools/configure_cachyos.py` replaces `tools/configure_target.py`. It installs CachyOS `x86-64-v3` packages **over the network** (Panther Lake has AVX2, no AVX-512), with `linux-cachyos`.
- Xe is blacklisted during early boot (LUKS prompt on the coreboot framebuffer), then `ruby-gpu.service` loads it once.
- First boot is a **text login** (`multi-user.target`). niri/Noctalia and audio come after.
- The running kernel lives in signed Android partitions, not `/boot`. Pacman hooks preserve its modules and build and validate a new boot set when `linux-cachyos` changes; writing it to the disk stays a manual `kernel_update.py commit`.

## Preparation

- Live USB written with the console-only image.
- A **separate backup drive formatted ext4** with at least 1.3 GB free (the factory partition backup is ~762 MiB plus LUKS header). Not the live USB, not FAT/exFAT.
- AC power, Wi-Fi credentials, the Googlebook recovery USB.

## Procedure (root, console-only live session)

```sh
cd /opt/asus-googlebook-omarchy

# Network
iwctl            # device list; station wlan0 connect YOUR_SSID
curl -I https://cdn77.cachyos.org

# 1. Inspect (read-only) and back up
python3 tools/native.py plan --device /dev/nvme0n1
mount /dev/sdX1 /run/media/backup     # your ext4 backup drive (mkdir first)
python3 tools/native.py backup --destination /run/media/backup/ruby-original
python3 tools/native.py verify-backup /run/media/backup/ruby-original

# 2. Format userdata only (dry run, then apply)
python3 tools/native.py format
python3 tools/native.py format --apply

# 3. Install and configure CachyOS
python3 tools/configure_cachyos.py packages --user YOU --name 'Your Name' --timezone America/Chicago --keymap us
python3 tools/configure_cachyos.py configure

# 4. Build and validate the boot set
mkdir -p /mnt/opt/lapis-kit && cp -a . /mnt/opt/lapis-kit/
arch-chroot /mnt python3 /opt/lapis-kit/tools/build_loader.py   # target has base-devel
python3 tools/native.py build
python3 tools/native.py validate --loader /mnt/opt/lapis-kit/build/load_android_test

# 5. Commit (dry run, then apply)
python3 tools/native.py commit
python3 tools/native.py commit --apply
sync; umount -R /mnt; cryptsetup close omarchy_root; poweroff
```

`plan` must pass before anything else. It also checks that slot A is the preferred successful slot and that the original `pvmfw_a/b` labels are present. Any failure is a stop condition.

## First boot

Remove the USB drives. Choose **Boot from internal disk**, type the disk password at the text prompt, then log in.

```sh
findmnt /                         # btrfs on /dev/mapper/omarchy_root
uname -r                          # the pinned *-cachyos release
systemctl status ruby-gpu.service
systemctl --failed
nmtui                             # connect Wi-Fi
sudo pacman -Syu                  # a kernel upgrade is staged, not written
sudo pacman -S niri noctalia noctalia-greeter xwayland-satellite
```

The mapper name `omarchy_root` and filesystem label are kept from the upstream tooling.

## Audio

`dspcfg` auto-detection picks legacy HDA (HDMI only), the TAS2563 tuning firmware is not in linux-firmware, and alsa-ucm-conf has no `sof-rt5682` profile. After the first boot:

```sh
sudo pacman -S --needed alsa-utils android-tools erofs-utils
sudo ~/googlebook-ruby/tools/install_ruby_audio.sh   # or /opt/lapis-kit/tools/...
systemctl reboot
```

The script reads Lenovo's `tas2563RCA4.bin` and `tas2563_coef.bin` from the untouched factory `super` partition (`vendor_a`) into `/usr/lib/firmware/updates`, forces SOF (`ruby-audio.conf`), loads SOF after Xe (`ruby-gpu.service`), and installs a UCM profile converted from the factory CRAS config. The UCM Speaker sequence selects TAS2563 DSP configuration 0; the driver otherwise starts with configuration -1 and the amplifiers stay silent.

Factory per-speaker calibration lives in coreboot VPD (`dsm_calib_register_array`, `dsm_calib_value_0..3`), not on the erased disk. `ruby-speaker-calibration.service` writes it to the driver's write-only `Speaker Calibrated Data` control at boot.

Dolby DAX: Lenovo ships a Ruby-specific SOF firmware with a built-in Dolby module (`intel/sof-ipc4/ptl/ruby/sof-ptl.ri`, 2.14.1008.1) and a topology with `dolby-dax` pipelines. `ruby-dax.conf` points `snd_sof_pci` at them, as Googlebook OS's `mod_options --sof-fw` does, and a conditional UCM BootSequence block loads `dax_param.bin` and the factory switch/profile values. Remove `/etc/modprobe.d/ruby-dax.conf` to return to the stock sof-firmware files; the UCM block then skips itself. Toggle live with `amixer -c sofrt5682 cset name='DAX Speaker Switch' on|off`.

## Display

The Samsung ATNA53JB03 OLED runs at 2880x1800@120 (set in the niri output block). Xe feature switches live only in `/etc/modprobe.d/ruby-xe.conf`, not the kernel command line, so changing them needs a reboot but no boot-set rebuild. DC states, FBC, DSB and SAGV are at driver defaults (verified 10 October 2026); `enable_psr=0 enable_panel_replay=0` remain until panel self-refresh is tested. Systems installed earlier: remove the `xe.enable_*` entries from `/etc/kernel/cmdline`, then `kernel_update.py prepare --no-upgrade` and `commit`.

## Kernel updates

Kernel upgrades arrive with ordinary `pacman -Syu`. Only the disk write is manual:

```sh
sudo pacman -Syu                                   # stages and validates a new kernel
sudo /opt/lapis-kit/tools/kernel_update.py commit  # y/N, shows the version
systemctl reboot                                   # unlock, log in
```

Two pacman hooks, triggered by any change to `usr/lib/modules/*/vmlinuz`, run `/opt/lapis-kit/tools/kernel_update.py` (root-owned; never code in a home directory):

- `05-asus-kernel-snapshot.hook` (PreTransaction, `hook-pre`) reflink-copies the running kernel's module tree, and slot A's if the state record names a different one, to `/var/lib/asus-boot/kernel-update/modules/`. It aborts the transaction if a committed update is not yet confirmed, or if it finds no module tree to protect (for example `pacman` in a chroot from the live USB; `KERNEL_UPDATE_UNPROTECTED=1` overrides).
- `zz-asus-kernel-stage.hook` (PostTransaction, after depmod and mkinitcpio, `hook-post`) restores those trees if the package removed them, builds the small encrypted-root initramfs with `/etc/asus-boot/mkinitcpio.conf`, signs the four slot-A images with the device's AVB key (`/etc/asus-boot/avb-private.pem`) and validates them with the ChromiumOS Android loader against the live partition table. It writes nothing to the boot partitions. On success it prints `Kernel X staged and validated`. On failure the new package stays installed but slot A still boots the old kernel; fix the cause and retry with `sudo /opt/lapis-kit/tools/kernel_update.py prepare --no-upgrade`.

`commit` backs up the current slot-A images under `/var/lib/asus-boot/kernel-update/slots/`, writes the validated set with read-back, and restores every attempted region on a write error. `asus-kernel-confirm.timer` runs `confirm --auto` ten minutes after boot; it acts only when the state is `committed` and `uname -r` is the new kernel, then drops the preserved modules and keeps the two newest backups. Run `confirm` by hand to do it sooner.

If the new kernel does not boot: start the live USB, `cryptsetup open` the userdata partition as `omarchy_root`, mount subvolume `@` on `/mnt`, then run `python3 /mnt/opt/lapis-kit/tools/kernel_update.py rollback --root /mnt` (typed confirmation). From a working system, `kernel_update.py rollback` restores the previous set. `prepare --no-upgrade` rebuilds the boot set for the installed kernel, for example after changing `/etc/asus-boot/mkinitcpio.conf`; `prepare` alone runs `pacman -S linux-cachyos linux-cachyos-headers` and lets the hooks stage it. Running `prepare`, `commit` or `install-hooks` from a checkout first copies its `lapis/` and `tools/` to `/opt/lapis-kit`, so the hooks and the rescue copy match the code you ran.

### Migrating from the pinned kernel

Systems installed before the hooks pin `linux-cachyos` with `IgnorePkg` and a blocking guard hook. With no update pending (`kernel_update.py status`), run once from the checkout:

```sh
sudo ~/googlebook-ruby/tools/kernel_update.py install-hooks
```

It refreshes `/opt/lapis-kit/{lapis,tools}` as root-owned copies, writes both hooks and the confirm service and timer, removes `00-asus-kernel-guard.hook` and `/usr/local/libexec/asus-boot-update-guard`, and removes only `linux-cachyos` and `linux-cachyos-headers` from `IgnorePkg` in `/etc/pacman.conf`, keeping a backup at `/etc/pacman.conf.kernel-pin.bak`.

First physical upgrade, 7.2.9-1 to 7.2.9-2-cachyos, completed on 10 October 2026: prepare, commit, reboot and confirm, with audio, Xe and speaker calibration working afterwards.
