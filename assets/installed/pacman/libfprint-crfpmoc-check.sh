#!/bin/sh
# Pacman hook helper: say when the repo libfprint is newer than the installed
# libfprint-crfpmoc (which pacman never replaces on its own), so the Ruby
# fingerprint package can be rebuilt from assets/packages/libfprint-crfpmoc.
ours=$(pacman -Q libfprint-crfpmoc 2>/dev/null | awk '{print $2}') || exit 0
repo=$(pacman -Si libfprint 2>/dev/null | awk '/^Version/ {print $3; exit}')
[ -n "$ours" ] && [ -n "$repo" ] || exit 0
# Compare upstream versions only; CachyOS rebuilds (-1.1) are not news.
if [ "$(vercmp "${repo%-*}" "${ours%-*}")" -gt 0 ]; then
    echo "==> libfprint $repo is in the repos; libfprint-crfpmoc is $ours."
    echo "    Fingerprint still works. Rebuild libfprint-crfpmoc when convenient (googlebook-ruby, Fingerprint)."
fi
exit 0
