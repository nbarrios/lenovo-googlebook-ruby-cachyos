#!/bin/sh
# Suspend/resume check for Ruby: suspends (s2idle by default) with an RTC
# wake alarm, then reports S0ix residency, resume time and driver errors.
# Run as root via sudo from the desktop; the log goes to the invoking user's
# ~/suspend-test.log. If the RTC alarm does not wake the laptop, press a key.
#
#   sudo tools/test_suspend.sh [seconds] [s2idle|deep]
set -u
secs=${1:-30}
mode=${2:-s2idle}
[ "$(id -u)" = 0 ] || { echo 'Run with sudo' >&2; exit 1; }
user=${SUDO_USER:-root}
home=$(getent passwd "$user" | cut -d: -f6)
log=$home/suspend-test.log
: >"$log"; chown "$user:" "$log"
pmc=/sys/kernel/debug/pmc_core
say() { printf '%s\n' "$*" | tee -a "$log"; }
res() { cat "$pmc/slp_s0_residency_usec" 2>/dev/null || echo 0; }

say "== $(date -Is) kernel $(uname -r), mode $mode, alarm ${secs}s"
grep -q "$mode" /sys/power/mem_sleep || { say "mode $mode not offered: $(cat /sys/power/mem_sleep)"; exit 1; }
old_mode=$(sed 's/.*\[\(.*\)\].*/\1/' /sys/power/mem_sleep)
echo "$mode" > /sys/power/mem_sleep
say "power: $(cat /sys/class/power_supply/*/online 2>/dev/null | tr '\n' ' ')battery $(cat /sys/class/power_supply/BAT*/capacity 2>/dev/null)%"
r0=$(res); fail0=$(cat /sys/power/suspend_stats/fail)
cursor=$(journalctl -n 0 --show-cursor -q | sed -n 's/^-- cursor: //p')
sync

rtcwake -m no -s "$secs" >/dev/null 2>&1 || say 'rtcwake alarm not set: wake with a key'
t0=$(date +%s)
systemctl suspend
# systemctl returns before the sleep happens; wait for the resume to be logged.
for i in $(seq 1 $((secs + 120))); do
    journalctl --after-cursor="$cursor" -q -o cat | grep -q 'PM: suspend exit' && break
    sleep 1
done
sleep 5
t1=$(date +%s)
echo "$old_mode" > /sys/power/mem_sleep

r1=$(res)
say "== resumed after $((t1 - t0)) s wall clock"
say "suspend_stats: success $(cat /sys/power/suspend_stats/success) fail $(cat /sys/power/suspend_stats/fail) (fail before: $fail0)"
say "S0ix residency during sleep: $(( (r1 - r0) / 1000 )) ms (0 means the SoC never reached S0ix)"
say "== kernel log for the sleep"
journalctl --after-cursor="$cursor" -k -q -o short-monotonic --no-pager \
    | grep -i -E 'PM:|suspend|resume|wake|xe 0000|sof|snd|tas2781|iwlwifi|cros|error|fail|timeout|warn|call trace' \
    | grep -v -E 'audit' | tee -a "$log"
say "== after resume"
say "wifi: $(nmcli -t -f STATE general 2>/dev/null)"
say "cros_fp: $(ls /dev/cros_fp 2>&1)"
say "sound cards: $(cat /proc/asound/cards | grep -c '^ *[0-9]')"
say "failed units: $(systemctl --failed --no-legend | wc -l)"
echo "Log: $log"
