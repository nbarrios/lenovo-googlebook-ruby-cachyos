#!/bin/sh
# Install the Ruby glowbar service: a user daemon that shows dictation, battery
# and an accent colour on the EC lightbar. Run as root via sudo; it adds the
# invoking user to the video group, which may write the lightbar.
set -eu
here=$(dirname "$(readlink -f "$0")")
assets=$here/../assets/installed/glowbar
[ "$(id -u)" = 0 ] || { echo 'Run with sudo' >&2; exit 1; }
[ "$(cat /sys/class/dmi/id/product_name)" = Ruby ] || { echo 'Not a Ruby board' >&2; exit 1; }
[ -e /sys/class/chromeos/cros_ec/lightbar/led_rgb ] || { echo 'No EC lightbar (cros_ec_lightbar not loaded?)' >&2; exit 1; }

install -Dm755 "$assets/ruby-glowbar.py" /usr/local/libexec/ruby-glowbar
install -Dm644 "$assets/ruby-glowbar.service" /etc/systemd/user/ruby-glowbar.service
install -Dm644 "$assets/90-ruby-glowbar.rules" /etc/udev/rules.d/90-ruby-glowbar.rules
install -Dm755 "$assets/ruby-glowbar.sleep" /usr/lib/systemd/system-sleep/ruby-glowbar
udevadm control --reload
udevadm trigger --action=add /sys/bus/platform/devices/cros-ec-lightbar.*
if [ -n "${SUDO_USER:-}" ]; then
    usermod -aG video "$SUDO_USER"
    echo "Added $SUDO_USER to the video group; reboot (a logout may not restart the systemd user manager), then run:"
else
    echo 'Add your user to the video group, reboot, then run:'
fi
echo '  systemctl --user daemon-reload && systemctl --user enable --now ruby-glowbar'
