#!/bin/sh
# Ruby display experiment, run from the console-only live USB as root.
# Loads the Xe driver once (no unload/reload) and logs what happens next to
# the directory this script lives on, so results survive a black screen.
# Usage: sh /mnt/xe-test.sh [plain]   ("plain" = no ASUS power-feature params)
set -u
log=$(dirname "$(readlink -f "$0")")/xe-test-$(date +%H%M%S)
mkdir -p "$log"
params='gsc_firmware_path= enable_panel_replay=0 enable_psr=0 enable_fbc=0 enable_dsb=0 enable_sagv=0 enable_dc=0'
[ "${1:-}" = plain ] && params=
echo "mode=${1:-asus-params} params=$params" > "$log/mode.txt"
dmesg > "$log/dmesg-before.txt"
sync
modprobe xe $params > "$log/modprobe.txt" 2>&1
echo "modprobe exit $?" >> "$log/modprobe.txt"
for t in 2 5 10 20; do
    sleep 5
    dmesg > "$log/dmesg-after.txt"
    for c in /sys/class/drm/card*-*; do
        echo "$c $(cat "$c/status") $(cat "$c/enabled" 2>/dev/null) $(head -3 "$c/modes" 2>/dev/null | tr '\n' ' ')"
    done > "$log/drm-status.txt"
    cat /sys/class/vtconsole/vtcon*/name > "$log/vtconsole.txt" 2>&1
    for e in /sys/class/drm/card*-eDP*/edid; do [ -s "$e" ] && cp "$e" "$log/edid.bin"; done
    for p in /sys/module/xe/parameters/*; do echo "$(basename "$p")=$(cat "$p")"; done > "$log/xe-params.txt" 2>&1
    sync
done
echo "Done; results in $log" | tee "$log/done.txt"
sync
