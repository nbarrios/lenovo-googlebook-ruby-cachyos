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
| Audio | RT5682S headset codec + 4x TI TAS2563 amplifiers on I2S, SOF topology `sof-ptl-tas2563-rt5682.tplg`. Speakers, headphones (auto-switch) and internal mics verified 10 October 2026 |
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
sync; umount -R /mnt; cryptsetup close cachyos-root; poweroff
```

`plan` must pass before anything else. It also checks that slot A is the preferred successful slot and that the original `pvmfw_a/b` labels are present. Any failure is a stop condition.

## First boot

Remove the USB drives. Choose **Boot from internal disk**, type the disk password at the text prompt, then log in.

```sh
findmnt /                         # btrfs on /dev/mapper/cachyos-root
uname -r                          # the pinned *-cachyos release
systemctl status ruby-gpu.service
systemctl --failed
nmtui                             # connect Wi-Fi
sudo pacman -Syu                  # a kernel upgrade is staged, not written
sudo pacman -S niri noctalia noctalia-greeter xwayland-satellite
```

On Ruby the LUKS mapper is `cachyos-root`, the LUKS label `CACHYOS` and the Btrfs label `CachyOS`; the Omarchy path on Lapis keeps `omarchy_root`. Systems installed before 11 October 2026 use `omarchy_root`; see [Renaming the root mapper](#renaming-the-root-mapper).

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

Microphones: there is no NHLT table, so `ruby-audio.conf` sets `dmic_num=4`. The DMICs are rated -26 dBFS at 94 dB SPL and SOF capture gain tops out at 0 dB, so speech arrives near -60 dBFS. Googlebook OS added gain in software; `/etc/pipewire/pipewire.conf.d/ruby-mic.conf` does the same with a filter-chain source ("Internal Microphone", `ruby_mic`): an 80 Hz high-pass and +36 dB in two `linear` stages (the builtin clamps `Mult` to 10). It outranks the raw mic and yields to a plugged-in headset mic.

## Suspend and power key

`s2idle` (the default in `/sys/power/mem_sleep`) works: on 10 October 2026 a 30 s RTC-woken suspend spent 30.06 s in S0ix (`slp_s0_residency_usec`), resumed with no Xe, SOF, Wi-Fi or `cros_ec_usb` errors, Wi-Fi reassociated within about 4 s, and audio, input and `fprintd-verify` worked afterwards. `tools/test_suspend.sh` repeats the check. `ruby-power-key.conf` in `/etc/systemd/logind.conf.d/` makes a short power-key press suspend and a long press shut down (logind's default is to power off on a short press); power key and lid suspend verified. Overnight on battery (lid closed, 11 h 18 min in one uninterrupted s2idle sleep) the battery went from 100% to 96% (182 of 4525 mAh), about 0.35 %/h or 16 mA. After one resume the kernel logged `cros_ec_ucsi GOOG0021:00: failed to re-enable notifications (-110)`, but it did not recur, and on 11 October 2026 a USB-C external display worked before suspend and after resume, including unplugging and replugging it twice.

## Camera

The integrated camera is USB UVC (`30c9:013f`, `uvcvideo`, no setup): `/dev/video0` offers MJPG up to 2592×1944 and 1920×1080 at 30 fps, and YUYV up to 640×480 at 30 fps; `/dev/video1` is its metadata node. Verified in Chrome, 11 October 2026.

## Thunderbolt

The Thunderbolt/USB4 controller runs at security level `user` with IOMMU DMA protection, so a dock does nothing until `boltd` authorizes it. `configure_cachyos.py` installs `bolt`; on older installs run `pacman -S bolt`. Enroll a dock once with `boltctl enroll --policy auto <uuid>` (the UUID is in `boltctl list`); bolt records the policy as `iommu` and then authorizes the dock on every plug-in. Verified 11 October 2026 with a Belkin Thunderbolt 3 Dock Plus (40 Gb/s, USB Power Delivery charging, USB and its Realtek Ethernet adapter enumerated, a USB mouse working), including after resume and after unplugging and replugging. Plugging in a dock logs `cros_ec_ucsi ... UCSI_GET_PDOS failed (-5)`, and a resume with a dock attached logged `failed to re-enable notifications (-110)` again; neither affected charging or hotplug. Displays through the dock are untested.

## Bluetooth

Intel CNVi Bluetooth (`hci0`, bluez 5.87) works without setup; pair from Noctalia's control center or `bluetoothctl`. The kernel logs `Failed to send Intel_Write_DDC (-95)` at startup; it is harmless. Verified 11 October 2026 with earbuds: playback uses A2DP with AAC, and opening the headset microphone makes WirePlumber switch to HFP with mSBC (16 kHz), then back to A2DP when recording stops. The first second or so of a recording is lost during that switch. The internal-microphone boost does not apply to Bluetooth microphones.

## Keyboard backlight

`cros_kbd_led_backlight` exposes `chromeos::kbd_backlight` (0–100). `brightnessctl -d chromeos::kbd_backlight set 40%` works without root through logind, and `systemd-backlight` restores the level at boot. The ChromeOS convention is Alt plus the screen-brightness keys.

## Top-row keys

The top row reports, left to right: `KEY_BACK`, `KEY_HOMEPAGE`, `KEY_SCALE` (overview, XKB `XF86LaunchA`), `KEY_FULL_SCREEN`, `KEY_SYSRQ` (screenshot, `Print`), brightness down and up, `KEY_KBDILLUMTOGGLE`, `KEY_MICMUTE`, `KEY_MUTE` and the volume keys; two further keys send `KEY_ACCESSIBILITY` and `KEY_DICTATE` (XKB `XF86Accessibility`, `XF86Dictate`, xkeyboard-config 2.48). All arrive on the `AT Translated Set 2 keyboard` device and need no remapping; bind them in the compositor.

## Battery charge limit

The EC's charge control (`cros_charge_control`) exposes `charge_control_start_threshold` and `charge_control_end_threshold` on `BAT0`; they reset at boot. The optional `assets/installed/udev/90-ruby-charge-limit.rules` holds the battery between 75 and 80% on AC:

```sh
sudo install -Dm644 assets/installed/udev/90-ruby-charge-limit.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --action=add /sys/class/power_supply/BAT0
```

Edit the two numbers in the rule for other limits.

## Display

The Samsung ATNA53JB03 OLED runs at 2880x1800@120 (set in the niri output block). Xe feature switches live only in `/etc/modprobe.d/ruby-xe.conf`, not the kernel command line, so changing them needs a reboot but no boot-set rebuild. All Xe display power features run at driver defaults (10 October 2026: stable at 120 Hz, DC3→DC5→DC6 entered, driver selects Panel Replay with selective update, which disables FBC). On a still screen `i915_edp_psr_status` shows `ctl: enabled` and status `SLEEP`, and DC5→DC6 entries keep rising (28 in 10 s). External displays work on USB-C DisplayPort and on the built-in HDMI port (11 October 2026; HDMI tested with a 1920x1080@60 monitor; niri extends the desktop automatically). A 4K OLED monitor on USB-C offered 3840x2160 at 60, 120, 180 and 240 Hz and ran cleanly at 240 Hz; niri picks the monitor's preferred mode (60 Hz here), so set a higher rate with `niri msg output <name> mode ...` or a `mode` line in that output's block. To fall back, add `enable_panel_replay=0 enable_psr=0` (then `enable_fbc=0 enable_dsb=0 enable_sagv=0 enable_dc=0`) to the `options xe` line and reboot. Systems installed earlier: remove the `xe.enable_*` entries from `/etc/kernel/cmdline`, then `kernel_update.py prepare --no-upgrade` and `commit`.

## Fingerprint

The reader is a Google fingerprint MCU on internal USB (`18d1:5402`, interface `ff/5a/00`). Linux 7.2 has no driver for that transport. Google's `cros_ec_usb` driver was posted to LKML (v6, September 2025) but is not merged. Research and the security model: [research/fingerprint.md](research/fingerprint.md). **Status (10 October 2026, `7.2.9-2-cachyos`):** the DKMS module binds the FPMCU, `/dev/cros_fp` appears and unloading is clean. The FPMCU runs `srebrna` RW `v2.0.31814-42f9aa9` (RO `v2.0.31294-70ef16a`) and answers `FP_INFO` only in version 3. The userspace package below is built and passes the unit and replay tests. The first `fprintd-enroll` on the laptop (package `1.94.100-1`) failed with `enroll-unknown-error` (`ioctl failed: No space left on device`), which `0004` fixes in `1.94.100-2`. With `-2`, fprintd crashed (SIGSEGV) on the first MKBP event, which proves events arrive over USB; `0005` fixes that in `1.94.100-3`. With `-3`, `fprintd-enroll` completed (one stage passed, then retry-scan results until complete), and `fprintd-verify` returned `verify-match` for the enrolled finger and `verify-no-match` for another finger. Enrolled prints survive a reboot (persistent seed), and `install_fingerprint_driver.sh --autoload` loads the driver at boot.

Authentication (10 October 2026): `sudo` accepts the fingerprint first and falls back to the password after three misses or 15 s, via `auth sufficient pam_fprintd.so max-tries=3 timeout=15` as the first `auth` line of `/etc/pam.d/sudo` (keep a root shell open while editing; a `sudo` package update may leave a `.pacnew`). Noctalia's lock screen talks to fprintd itself. The greeter stays password-only, because the login password unlocks the GNOME keyring (SSH key, browser and Noctalia secrets).

`assets/installed/cros-ec-usb/` holds:

- the unmodified v6 `cros_ec_usb.c`;
- the private `cros_ec.h` header from Linux v7.2, which `linux-cachyos-headers` lacks;
- two patches that DKMS applies when it builds:
  - `0001` ports the driver to the 6.18+ cros_ec core, which no longer allocates buffers or initialises the lock in `cros_ec_register()`;
  - `0002` unregisters the EC when the module is unloaded, so `rmmod` is safe.

Install:

```sh
sudo pacman -S --needed dkms linux-cachyos-headers
sudo ~/googlebook-ruby/tools/install_fingerprint_driver.sh
dkms status cros-ec-usb          # cros-ec-usb/6.1, <kernel>, x86_64: installed
```

The installer does three things:

- copies the package to `/usr/src/cros-ec-usb-6.1`;
- runs `dkms install` for every installed kernel that has headers;
- writes `/etc/modprobe.d/ruby-fingerprint.conf` (`blacklist cros_ec_usb`). Without that file udev would load the module at the next boot. It does not stop an explicit `modprobe`.

DKMS signs the module with its own generated key (`/var/lib/dkms/mok.key`). The kernel does not trust that key. CachyOS does not enforce module signatures, so the module loads and the kernel is marked tainted.

**Kernel updates.** pacman runs hooks in file-name order:

1. `05-asus-kernel-snapshot.hook` copies the running kernel's module tree, including `updates/dkms/cros_ec_usb.ko*`.
2. DKMS's `71-dkms-remove.hook` then removes the module from the old tree. The old kernel's paths count as removed, because the release string changes with every package version.
3. After the upgrade, `70-dkms-install.hook` builds the module for the new kernel. This happens before `zz-asus-kernel-stage.hook`, which restores the old tree with the module.

The headers must change in the same transaction as the kernel. `pacman -Syu` and `kernel_update.py prepare` both do this. No mkinitcpio hook adds `cros_ec_usb`, so the small boot initramfs never contains it. Only the blacklist line goes into the initramfs, through `modconf`.

### First test

Do this after the driver is installed, from a normal boot:

```sh
sudo modprobe cros_ec_usb        # also loads cros_ec and cros_ec_proto
sudo dmesg | tail -n 30          # expect "Chrome EC device registered" on 3-7:1.0
ls -l /dev/cros_fp               # root-only character device
```

If `/dev/cros_fp` is missing but dmesg shows the registration, load the children by hand: `sudo modprobe cros_ec_dev cros_ec_chardev`. If dmesg shows `Timed out waiting for response` or `Cannot identify the EC`, save the log and unload the driver.

The Googlebook OS `ectool` is an Android binary and won't run here. EC `main`'s `ectool` no longer builds outside ChromiumOS: at `fc818f5c` it needs `libchrome` and `libec`. The R107 branch builds with only libusb:

```sh
sudo pacman -S --needed base-devel libusb
mkdir -p ~/src/ec-r107 && cd ~/src/ec-r107
curl -L https://chromium.googlesource.com/chromiumos/platform/ec/+archive/24a1cee59942b595f459d69c2aa2f97d37b268df.tar.gz | bsdtar -xf -
make BOARD=host LIBFTDIUSB_CFLAGS=-I/usr/include/libusb-1.0 LIBFTDIUSB_LDLIBS=-lusb-1.0 build/host/util/ectool
sudo ./build/host/util/ectool --name=cros_fp version
sudo ./build/host/util/ectool --name=cros_fp fpinfo
sudo ./build/host/util/ectool --name=cros_fp fpencstatus
```

About the AUR alternatives:

- `chromium-ectool` packages the same R107 branch, but it downloads an unpinned tarball (`sha256sums=SKIP`) and needs libftdi.
- The AUR package named `ectool` is coreboot's tool, which is unrelated.

`version` should print the RO and RW firmware versions, which should be `srebrna` builds. `fpinfo` should report the Egis sensor. `fpencstatus` shows whether a seed is already set. If Googlebook OS ran before a warm reboot, reset the MCU with `ectool --name=cros_fp reboot_ec`. The MCU then re-enumerates, and `/dev/cros_fp` stays.

Unload with `sudo modprobe -r cros_ec_usb`. First close everything that holds `/dev/cros_fp` open, such as fprintd or ectool. Unloading while it is open is a use-after-free in `cros_ec_chardev`, and the same applies to every cros_ec transport.

Once the test passes, enable loading at boot with `sudo ~/googlebook-ruby/tools/install_fingerprint_driver.sh --autoload`. This removes the blacklist file; rerunning without `--autoload` puts it back. To uninstall:

```sh
sudo dkms remove cros-ec-usb/6.1 --all
sudo rm -rf /usr/src/cros-ec-usb-6.1 /etc/modprobe.d/ruby-fingerprint.conf
```

### Userspace: libfprint-crfpmoc and fprintd

`assets/packages/libfprint-crfpmoc/` builds a pacman package `libfprint-crfpmoc`. It is the CachyOS/Arch `libfprint` 1.94.100 PKGBUILD with four patches:

- `0001`: libfprint [MR !512](https://gitlab.freedesktop.org/libfprint/libfprint/-/merge_requests/512) (driver `crfpmoc`), pinned to head `56442591`. This is the unmodified MR diff; it applies to 1.94.100 with offsets only.
- `0002`: `FP_INFO` v2/v3. The MR asks for v1, which srebrna rejects with `INVALID_VERSION`. At open the driver queries `GET_CMD_VERSIONS` and uses the highest of v3/v2/v1, falling back to v1 if the query fails, so older Chromebooks keep working. A v3 response is the header plus 10 frame entries (236 bytes); the EC refuses a smaller buffer. The driver reads only the template fields. Frame sizes are logged, never used.
- `0003`: a persistent seed. The MR picks a random `FP_SEED` and user id on every open, so prints would stop matching after fprintd restarts or a reboot. The patched driver:
  - reads 32 bytes from `/var/lib/fprint/crfpmoc-seed`, creating the file on first use with `getrandom(2)`;
  - refuses the file unless it is root-owned with mode 0600 or stricter, in a root-owned directory that is not group or world writable;
  - derives the `FP_CONTEXT` user id as SHA-256(`crfpmoc-userid-v1` ‖ seed), one id for the whole machine;
  - sets the seed only when `fpencstatus` reports it unset, and treats `ACCESS_DENIED` as "already set".
- `0004`: reply sizes. `FP_MODE` always returns its 4-byte mode, but the MR sent the sensor reset (end of enroll/verify) and the clear-storage `FP_MODE` commands with `insize` 0. The EC transports reject a reply larger than `insize`: over USB the ioctl fails with `ENOSPC` and the kernel logs `Incorrect number of expected bytes: 12`. Every `FP_MODE` now passes `insize` 4. All other commands already used the `ec_commands.h` response sizes; `FP_SEED`, `FP_CONTEXT` v1 and `FP_TEMPLATE` return no data. The replay traces already recorded `insize` 4 (umockdev does not compare `insize`), so they are unchanged.
- `0005`: event callback. `g_pollable_input_stream_create_source()` calls its callback as `GPollableSourceFunc (stream, user_data)`, but the MR's `crfpmoc_read_bytes()` took only `user_data`, so it treated the stream as its state machine and crashed fprintd on the first MKBP event (`FP_IS_DEVICE` and `G_IS_POLLABLE_INPUT_STREAM` assertions). The replay tests never deliver an MKBP event, so they could not catch it.

The package provides and conflicts with `libfprint` (soname `libfprint-2.so=2-64`), so fprintd links against it unchanged and `pacman -Syu` does not swap it back. The source is pinned to the v1.94.100 tag commit and every source has a sha256; the source's blake2 sum matches Arch's signature-checked one. Tests: the full libfprint suite passes (133 tests). It runs serially, because upstream's `egis_etu905` replay times out when tests run in parallel, also without these patches. The suite includes the MR's `crfpmoc` replay, a `crfpmoc-v3` replay (v3 responses, `FP_SEED` denied) and unit tests for the v3 parser and the seed file.

Build it as your user. Only installing the build dependencies needs root:

```sh
sudo pacman -S --needed --asdeps git glib2-devel gobject-introspection gtk-doc meson python-cairo python-gobject umockdev
cd ~/googlebook-ruby/assets/packages/libfprint-crfpmoc
PKGDEST=~/googlebook-ruby-build/libfprint-crfpmoc makepkg -fc
```

Install it, install fprintd and load the driver:

```sh
sudo ~/googlebook-ruby/tools/install_fingerprint_userspace.sh
```

The script runs `pacman -U` (answer `y` to removing `libfprint`) and `pacman -S --needed fprintd`, loads `cros_ec_usb`, and stops fprintd so it rediscovers devices. It then prints the next steps. Run these as your user, not root:

```sh
fprintd-enroll
fprintd-verify
sudo journalctl -b -u fprintd
```

For driver debug output, run `sudo systemctl stop fprintd`, then `sudo env G_MESSAGES_DEBUG=all /usr/lib/fprintd --no-timeout` in one terminal and the commands above in another. fprintd finds `/dev/cros_fp` only if the device exists when fprintd starts, so the driver has to be loaded first: by hand, or at boot with `install_fingerprint_driver.sh --autoload`.

**Security trade-off.** ChromeOS derives the seed from the TPM. Here it is a plaintext root-only file. Templates are still encrypted with a key that never leaves the FPMCU, so they are useless on other hardware. Anyone who can read both the seed and `/var/lib/fprint` (root, or offline access to an unencrypted disk) can load them back into this FPMCU. Root can already fake a match on this reader, because there is no authenticated session (see the research notes). Sealing the seed with `systemd-creds --with-key=tpm2` would only add protection against offline disk access. Deleting the seed file invalidates every enrolled print, so re-enroll afterwards. If another OS set a different seed before a warm reboot, verify fails until the FPMCU is reset (`ectool --name=cros_fp reboot_ec`) or the machine is powered off.

Undo: `sudo pacman -S libfprint` (replaces the package), and optionally `sudo rm /var/lib/fprint/crfpmoc-seed`.

**Later: PAM.** Not wired yet. Once enroll/verify work, add `auth sufficient pam_fprintd.so` near the top of `/etc/pam.d/sudo`. Then do the same for the greetd greeter and the noctalia lock screen (whichever PAM service they use), keeping a password fallback.

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

If the new kernel does not boot: start the live USB, `cryptsetup open` the userdata partition (any mapper name works, for example `cachyos-root`), mount subvolume `@` on `/mnt`, then run `python3 /mnt/opt/lapis-kit/tools/kernel_update.py rollback --root /mnt` (typed confirmation). From a working system, `kernel_update.py rollback` restores the previous set. `prepare --no-upgrade` rebuilds the boot set for the installed kernel, for example after changing `/etc/asus-boot/mkinitcpio.conf`; `prepare` alone runs `pacman -S linux-cachyos linux-cachyos-headers` and lets the hooks stage it. Running `prepare`, `commit` or `install-hooks` from a checkout first copies its `lapis/` and `tools/` to `/opt/lapis-kit`, so the hooks and the rescue copy match the code you ran.

### Migrating from the pinned kernel

Systems installed before the hooks pin `linux-cachyos` with `IgnorePkg` and a blocking guard hook. With no update pending (`kernel_update.py status`), run once from the checkout:

```sh
sudo ~/googlebook-ruby/tools/kernel_update.py install-hooks
```

It refreshes `/opt/lapis-kit/{lapis,tools}` as root-owned copies, writes both hooks and the confirm service and timer, removes `00-asus-kernel-guard.hook` and `/usr/local/libexec/asus-boot-update-guard`, and removes only `linux-cachyos` and `linux-cachyos-headers` from `IgnorePkg` in `/etc/pacman.conf`, keeping a backup at `/etc/pacman.conf.kernel-pin.bak`.

First physical upgrade, 7.2.9-1 to 7.2.9-2-cachyos, completed on 10 October 2026: prepare, commit, reboot and confirm, with audio, Xe and speaker calibration working afterwards.

### Renaming the root mapper

Systems installed before 11 October 2026 open the disk as `omarchy_root`, with the labels `OMARCHY` and `Omarchy`. The tools accept either mapper name. To switch to `cachyos-root`, run from the checkout with no update pending. Only `/etc/kernel/cmdline` names the mapper (the `encrypt` hook reads it from `cryptdevice=`), and `fstab` mounts by UUID, so a rebuilt boot set is all that is needed. The labels are cosmetic and can be changed online.

```sh
sudo sed -i 's/omarchy_root/cachyos-root/g' /etc/kernel/cmdline
sudo cryptsetup config --label CACHYOS /dev/disk/by-partlabel/userdata
sudo btrfs filesystem label / CachyOS
sudo ~/googlebook-ruby/tools/kernel_update.py prepare --no-upgrade
sudo /opt/lapis-kit/tools/kernel_update.py commit
```

Reboot, type the disk password, and check that `findmnt /` shows `/dev/mapper/cachyos-root`. The confirm timer then settles the update as usual. Verified on the development laptop, 11 October 2026.
