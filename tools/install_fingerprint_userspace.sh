#!/bin/sh
# Install the fingerprint userspace on Ruby: libfprint-crfpmoc (libfprint with
# the ChromeOS FPMCU driver, replaces the repo libfprint) and fprintd, then
# load cros_ec_usb so /dev/cros_fp exists. PAM is not touched. Run as root via
# sudo after tools/install_fingerprint_driver.sh:
#
#   sudo tools/install_fingerprint_userspace.sh [path/to/libfprint-crfpmoc-*.pkg.tar.zst]
#
# Default package: ~/googlebook-ruby-build/libfprint-crfpmoc/ of the invoking
# user, built from assets/packages/libfprint-crfpmoc (docs/cachyos-ruby.md).
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run with sudo' >&2; exit 1; }
[ "$(cat /sys/class/dmi/id/product_name)" = Ruby ] || { echo 'Not a Ruby board' >&2; exit 1; }
user=${SUDO_USER:-root}
home=$(getent passwd "$user" | cut -d: -f6)

pkg=${1-}
if [ -z "$pkg" ]; then
    pkg=$(ls -1 "$home"/googlebook-ruby-build/libfprint-crfpmoc/libfprint-crfpmoc-*.pkg.tar.zst 2>/dev/null | sort -V | tail -n 1)
fi
[ -n "$pkg" ] && [ -f "$pkg" ] || {
    echo "No libfprint-crfpmoc package found (looked in $home/googlebook-ruby-build/libfprint-crfpmoc)." >&2
    echo 'Build it first: see docs/cachyos-ruby.md, Fingerprint.' >&2
    exit 1
}
modinfo -F filename cros_ec_usb >/dev/null 2>&1 || {
    echo 'cros_ec_usb is not installed: run tools/install_fingerprint_driver.sh first.' >&2
    exit 1
}

echo "==> Installing $pkg"
echo '    (if the repo libfprint is installed, pacman asks to remove it: answer y)'
pacman -U --needed "$pkg"
echo '==> Installing fprintd'
pacman -S --needed fprintd
pacman -Q libfprint-crfpmoc fprintd
# pacman never replaces this package; say when the repo libfprint moves ahead.
here=$(dirname "$(readlink -f "$0")")
install -Dm755 "$here/../assets/installed/pacman/libfprint-crfpmoc-check.sh" /usr/local/libexec/libfprint-crfpmoc-check
install -Dm644 "$here/../assets/installed/pacman/zz-libfprint-crfpmoc-check.hook" /etc/pacman.d/hooks/zz-libfprint-crfpmoc-check.hook

echo '==> Loading cros_ec_usb'
modprobe cros_ec_usb
for i in 1 2 3 4 5 6 7 8 9 10; do [ -e /dev/cros_fp ] && break; sleep 1; done
if [ ! -e /dev/cros_fp ]; then
    modprobe -a cros_ec_dev cros_ec_chardev
    sleep 3
fi
[ -e /dev/cros_fp ] || { echo 'No /dev/cros_fp; check dmesg' >&2; exit 1; }
ls -l /dev/cros_fp

# fprintd enumerates devices when it starts (D-Bus activated, exits when
# idle): stop a running instance so the next client sees the reader.
systemctl stop fprintd.service 2>/dev/null || true

cat <<EOF

Done. Next, as $user (not root), one command per line:

  fprintd-list $user
  fprintd-enroll
  fprintd-verify

fprintd-enroll uses the right index finger by default (-f left-index-finger
etc. for others) and asks for 5 touches. The first open creates the seed
/var/lib/fprint/crfpmoc-seed (root, 0600); prints are stored under
/var/lib/fprint/$user/crfpmoc/.

Logs:

  sudo journalctl -b -u fprintd
  sudo journalctl -b -u fprintd -o cat | grep -i crfpmoc

Full driver debug output (stop the service, run the daemon in a terminal,
then use fprintd-enroll/fprintd-verify in another terminal; Ctrl-C to end):

  sudo systemctl stop fprintd
  sudo env G_MESSAGES_DEBUG=all /usr/lib/fprintd --no-timeout

cros_ec_usb is loaded by hand until autoload is enabled
(sudo tools/install_fingerprint_driver.sh --autoload). PAM (sudo, greetd,
the noctalia lock screen) is not configured yet; see docs/cachyos-ruby.md.
EOF
