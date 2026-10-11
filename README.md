# CachyOS on Lenovo Googlebook 15

An experimental route to native **CachyOS on the Lenovo Googlebook 15 (Google Ruby)**, using the factory firmware, an encrypted internal drive and no firmware replacement. It is a fork of [Omarchy on ASUS Googlebook](https://github.com/jmklex/asus-googlebook-omarchy), whose boot path this port reuses.

**Early experimental release, tested on one laptop.** Everything below was verified on a single Ruby (512 GB, firmware `Google_Ruby.16650.143.0`) in October 2026. Other capacities, SKUs or firmware versions are untested, and the installer refuses partition layouts it has not seen. This is a community project, unaffiliated with Lenovo, Google, ASUS, CachyOS or Omarchy.

> **Built with AI assistance.** The Ruby port (code, patches and documentation) was developed with substantial help from an AI assistant, Anthropic's Claude, working under the direction of the maintainer, and tested on that one laptop. Read the scripts before running them as root.

## What works

| Area | Status on Ruby |
| --- | --- |
| Install and boot | LUKS2 + Btrfs on the internal NVMe; typed disk password at boot |
| Display | 2880×1800 OLED at 120 Hz; Xe display power saving on (DC6, Panel Replay verified active) |
| Input | Keyboard, keyboard backlight, touchpad, touchscreen |
| Wi-Fi | Intel CNVi |
| Speakers | SOF + 4× TAS2563 with Lenovo's tuning and **factory per-speaker calibration** from VPD; Dolby DAX |
| Headphones and microphones | Headset auto-switch; internal mics with a +36 dB PipeWire boost |
| Fingerprint reader | USB FPMCU via an out-of-tree `cros_ec_usb` DKMS driver and a patched libfprint; works for `sudo` and the Noctalia lock screen |
| Kernel updates | `pacman -Syu` stages and validates the new signed boot images; one command writes them |
| Suspend | s2idle reaches S0ix; about 0.35 %/hour overnight; power key and lid suspend |
| Camera and Bluetooth | UVC camera; Bluetooth audio with AAC and headset microphone |
| External display | USB-C DisplayPort up to 3840×2160 at 240 Hz (hotplug after resume verified) and the built-in HDMI port |
| Thunderbolt | Thunderbolt 3 dock (USB, charging) authorized by `bolt`, including after resume and replug |
| Not tested | Displays through a Thunderbolt dock, multiple external displays, deep sleep (S3) |

## Start here

1. Read [CachyOS on Ruby](docs/cachyos-ruby.md) end to end. It lists every difference from the upstream Lapis procedure.
2. Back up your files and make a separate [Googlebook recovery USB](docs/recovery.md). **Unlocking the bootloader erases the device.**
3. [Build and test the live USB](docs/live-usb.md) with `tools/build_live.py --console-only`. This runs in RAM and does not touch the internal drive.
4. Install with `tools/native.py` and `tools/configure_cachyos.py` ([procedure](docs/cachyos-ruby.md#procedure-root-console-only-live-session)).
5. After the first boot, run the audio and fingerprint installers. Kernel updates then go through `pacman -Syu` and `kernel_update.py commit`.

You need an advanced Linux workflow, a second computer, a dedicated live USB (16 GB or larger), separate recovery media, and an ext4 drive for private backups.

## How it boots

```text
Factory Depthcharge firmware, developer mode
  → AVB-signed Android v4 boot / init_boot / vendor_boot / vbmeta, slot A
  → linux-cachyos + small initramfs + typed LUKS password
  → encrypted Btrfs root → Xe graphics, then SOF audio → niri/Noctalia or any desktop
```

Installation formats `userdata`, replaces the four slot-A boot images, renames the two `pvmfw` GPT labels and clears only the pending Android recovery command. Partition offsets and sizes stay intact. The factory `super` partition is left untouched; the audio installer reads Lenovo's firmware from it. Slot B is backed up and unmodified, but **is not a working factory-OS fallback once userdata is reformatted**. Background: [technical notes](docs/architecture.md).

## What this fork adds

| Component | Purpose |
| --- | --- |
| `tools/configure_cachyos.py` | Network install of CachyOS `x86-64-v3` packages and the Ruby configuration |
| `tools/kernel_update.py` | Pacman hooks that preserve modules and build, sign and loader-validate new boot images; `commit`, `confirm` and `rollback` (also from the live USB) |
| `tools/install_ruby_audio.sh` | SOF, TAS2563 tuning, Dolby DAX firmware and UCM, extracted from the laptop's own factory partition |
| `assets/installed/ruby-speaker-calibration.*` | Loads the factory speaker calibration from coreboot VPD at boot |
| `assets/installed/pipewire/ruby-mic.conf` | Internal-microphone gain |
| `assets/installed/cros-ec-usb/`, `tools/install_fingerprint_driver.sh` | DKMS package of Google's USB transport for the fingerprint MCU |
| `assets/packages/libfprint-crfpmoc/`, `tools/install_fingerprint_userspace.sh` | libfprint with the ChromeOS match-on-chip driver and Ruby fixes |
| `docs/research/fingerprint.md` | Sources and security model for the fingerprint work |

The upstream Lapis/Omarchy tools (`tools/configure_target.py`, the Hyprland live desktop) remain and are documented in [install.md](docs/install.md), [support.md](docs/support.md) and [maintenance.md](docs/maintenance.md).

## What is not shipped

No OS image, Lenovo/Google/ASUS firmware, disk passwords, Wi-Fi profiles, SSH keys, AVB private keys, device backups, disk GUIDs or private logs. Each installation generates its own AVB signing key. The fingerprint seed is generated on the device and stays in `/var/lib/fprint`. See [NOTICE.md](NOTICE.md) for third-party code and licenses.

## Contributing

Reports from other Ruby units are very welcome: exact SKU, firmware version, storage capacity, the stage you reached and what you saw. Keep serial numbers, network names, addresses, disk UUIDs, passwords and backups out of issues. Do not remove the layout guards to force an install. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [validation record](docs/validation.md).

## License and attribution

Project code and documentation are under the [MIT License](LICENSE), as upstream. Kernel code in `assets/installed/cros-ec-usb/` is GPL-2.0 and the libfprint patches are LGPL-2.1-or-later; see [NOTICE.md](NOTICE.md).

The Lapis boot path, toolkit and recovery procedure are the work of **Jonathan Kleiman — [Omarchy on ASUS Googlebook](https://github.com/jmklex/asus-googlebook-omarchy)**. Please credit the upstream project as well as this fork.
