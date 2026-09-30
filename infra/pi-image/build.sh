#!/usr/bin/env bash
# Build a Raspberry Pi OS Lite (arm64) image with Tailscale preinstalled, so a
# freshly flashed Pi joins the tailnet on first boot with no cable or console.
#
#   sudo TS_AUTHKEY=tskey-auth-... ./pi-image/build.sh
#
# Runs on x86 Linux/WSL via qemu-user binfmt (aarch64). Output: pi-image/out/pi-tailscale.img
# TS_AUTHKEY is optional; without it, drop a tailscale-authkey file on bootfs after flashing.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/out
BASE_URL=${BASE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}
IMG=$OUT/pi-tailscale.img

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ] || { echo "no aarch64 binfmt; install qemu-user-static" >&2; exit 1; }
for f in user-data network-config; do
  [ -f "$HERE/$f" ] || { echo "missing $HERE/$f (copy $f.example and fill it in)" >&2; exit 1; }
done

mkdir -p "$OUT"
if [ ! -f "$OUT/base.img" ]; then
  curl -fL "$BASE_URL" -o "$OUT/base.img.xz"
  xz -d "$OUT/base.img.xz"
fi
cp --sparse=always "$OUT/base.img" "$IMG"

LOOP=$(losetup -fP --show "$IMG")
MNT=$(mktemp -d)
cleanup() {
  umount -R "$MNT" 2>/dev/null || umount -lR "$MNT" 2>/dev/null || true
  losetup -d "$LOOP" 2>/dev/null || true
  rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT

mount "${LOOP}p2" "$MNT"
mount "${LOOP}p1" "$MNT/boot/firmware"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"
mount --rbind /dev "$MNT/dev"
mount --make-rslave "$MNT/dev"

# DNS for apt inside the chroot, and keep package scripts from starting services
RESOLV=$MNT/etc/resolv.conf
if [ -e "$RESOLV" ] || [ -L "$RESOLV" ]; then mv "$RESOLV" "$RESOLV.build-bak"; fi
cp -L /etc/resolv.conf "$RESOLV"
printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d"
chmod 755 "$MNT/usr/sbin/policy-rc.d"

install -m 644 "$HERE/tailscale-firstboot.service" "$MNT/etc/systemd/system/"

chroot "$MNT" /bin/bash -euxo pipefail -c '
  . /etc/os-release
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/$VERSION_CODENAME.tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tailscale
  apt-get clean
  systemctl enable tailscaled.service tailscale-firstboot.service
  tailscale version
  help=$(tailscale up --help 2>&1 || true)
  grep -q "file:" <<<"$help"                    # --auth-key=file:... must be supported
  test ! -e /var/lib/tailscale/tailscaled.state # no identity baked in; every flash is a new node
'

rm -f "$MNT/usr/sbin/policy-rc.d"
rm -f "$RESOLV"
if [ -e "$RESOLV.build-bak" ] || [ -L "$RESOLV.build-bak" ]; then mv "$RESOLV.build-bak" "$RESOLV"; fi

# cloud-init seed, written the same way Raspberry Pi Imager writes it
BOOT=$MNT/boot/firmware
ID=pi-tailscale-$(date +%Y%m%d%H%M%S)
install -m 644 "$HERE/user-data" "$BOOT/user-data"
install -m 644 "$HERE/network-config" "$BOOT/network-config"
printf 'instance-id: %s\n' "$ID" > "$BOOT/meta-data"
sed -i -E 's/ ?ds=nocloud[^ ]*//; s/$/ ds=nocloud;i='"$ID"'/' "$BOOT/cmdline.txt"

if [ -n "${TS_AUTHKEY:-}" ]; then
  printf '%s\n' "$TS_AUTHKEY" > "$BOOT/tailscale-authkey"
  echo "Auth key written to bootfs. Treat $IMG as a secret."
fi

cleanup
trap - EXIT
[ -n "${SUDO_UID:-}" ] && chown "$SUDO_UID:$SUDO_GID" "$IMG"
echo "Built $IMG (instance-id $ID)"
