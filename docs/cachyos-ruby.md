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
- `linux-cachyos` and its headers are pinned (`IgnorePkg` plus a pacman hook). The running kernel lives in signed Android partitions; updating it means rebuilding, validating and committing a new boot set.

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
sudo pacman -Syu                  # kernel stays pinned
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

## Kernel updates

`linux-cachyos` is pinned (`IgnorePkg`), and a pacman hook blocks any change to `usr/lib/modules/*/vmlinuz` unless `tools/kernel_update.py` holds its one-time pass. Update in two steps, rebooting between them:

```sh
sudo pacman -Syu                                   # everything except the kernel
sudo ~/googlebook-ruby/tools/kernel_update.py prepare
sudo ~/googlebook-ruby/tools/kernel_update.py commit
systemctl reboot                                   # unlock, log in
sudo ~/googlebook-ruby/tools/kernel_update.py confirm
```

`prepare` keeps a reflinked copy of the running kernel's modules, installs the new kernel, restores the old modules so the running system and any rollback keep working, builds the small encrypted-root initramfs, signs the four slot-A images with the device's AVB key (`/etc/asus-boot/avb-private.pem`), and validates them with the ChromiumOS Android loader against the live partition table. It writes nothing to the boot partitions.

`commit` backs up the current slot-A images under `/var/lib/asus-boot/kernel-update/slots/`, writes the validated set with read-back, and restores every attempted region on a write error. `confirm` (on the new kernel) drops the preserved modules and keeps the two newest backups.

If the new kernel does not boot: start the live USB, `cryptsetup open` the userdata partition as `omarchy_root`, mount subvolume `@` on `/mnt`, then run `kernel_update.py rollback --root /mnt` from the kit. From a working system, `kernel_update.py rollback` restores the previous set. `prepare --no-upgrade` rebuilds the boot set for the installed kernel, for example after changing `/etc/asus-boot/mkinitcpio.conf`.

The build and loader-validation pipeline was exercised on the device with 7.2.9-1-cachyos and the device GPT; a physical kernel upgrade has not yet been performed.
