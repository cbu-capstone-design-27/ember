#!/usr/bin/env bash
# Build Hyper-V worker disks from the Ubuntu 24.04 cloud image with Tailscale preinstalled.
# Each hostname gets its own VHDX with its hostname and auth key baked in, so a VM
# created from it joins the tailnet on first boot with no console or SSH.
#
#   sudo TS_AUTHKEY=tskey-auth-... ./worker-image/build.sh hv-worker-1 hv-worker-2
#
# Output: worker-image/out/<hostname>.vhdx (attach as an existing disk to a Gen 2 VM)
# TS_AUTHKEY is optional; without it the VM boots but does not join the tailnet.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/out
RELEASE=${RELEASE:-noble}
BASE_URL=${BASE_URL:-https://cloud-images.ubuntu.com/$RELEASE/current}
IMG_NAME=$RELEASE-server-cloudimg-amd64.img
DISK_SIZE=${DISK_SIZE:-40G}   # virtual size; the VHDX is dynamic and cloud-init grows root on first boot

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ $# -ge 1 ] || { echo "usage: $0 <hostname>..." >&2; exit 1; }
command -v qemu-img >/dev/null || { echo "qemu-img not found (dnf install qemu-img)" >&2; exit 1; }
command -v e2fsck >/dev/null || { echo "e2fsck not found (dnf install e2fsprogs)" >&2; exit 1; }
[ -f "$HERE/user-data" ] || { echo "missing $HERE/user-data (copy user-data.example and fill it in)" >&2; exit 1; }

mkdir -p "$OUT"
if [ ! -f "$OUT/$IMG_NAME" ]; then
  curl -fL "$BASE_URL/$IMG_NAME" -o "$OUT/$IMG_NAME.part"
  expected=$(curl -fsSL "$BASE_URL/SHA256SUMS" | awk -v f="*$IMG_NAME" '$2 == f {print $1}')
  echo "$expected  $OUT/$IMG_NAME.part" | sha256sum -c -
  mv "$OUT/$IMG_NAME.part" "$OUT/$IMG_NAME"
fi

RAW=$OUT/base-tailscale.raw
MNT=$(mktemp -d)
LOOP=

# Kill anything still running inside the chroot; it would keep the filesystem busy
kill_chroot_procs() {
  local p
  for p in /proc/[0-9]*; do
    [ "$(readlink "$p/root" 2>/dev/null)" = "$MNT" ] && kill -9 "${p#/proc/}" 2>/dev/null
  done
  true
}

# Error path only: best effort, never lazy. A lazy unmount returns before the data is
# written back, and copying the image after one produces a corrupt filesystem.
cleanup() {
  kill_chroot_procs
  umount -R "$MNT" 2>/dev/null || true
  [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null || true
  LOOP=
}
trap 'cleanup; rmdir "$MNT" 2>/dev/null || true' EXIT

# Normal path: flush, unmount, detach; any failure stops the build
unmount_disk() {
  kill_chroot_procs
  sync
  umount -R "$MNT"
  losetup -d "$LOOP"
  LOOP=
}

# Refuse to continue with a filesystem the kernel flagged or e2fsck finds damaged
fsck_disk() {
  local loop d
  loop=$(losetup -fP --show -r "$1")
  udevadm settle 2>/dev/null || sleep 1
  for d in "$loop"p*; do
    [ "$(blkid -s TYPE -o value "$d")" = ext4 ] || continue
    e2fsck -fn "$d" >/dev/null 2>&1 || { losetup -d "$loop"; echo "filesystem errors on $d of $1" >&2; exit 1; }
  done
  losetup -d "$loop"
}

# Attach a raw disk and mount its root, /boot and ESP (found by the cloud image's labels)
mount_disk() {
  LOOP=$(losetup -fP --show "$1")
  udevadm settle 2>/dev/null || sleep 1
  part() { for d in "$LOOP"p*; do [ "$(blkid -s LABEL -o value "$d")" = "$1" ] && echo "$d"; done; true; }
  mount "$(part cloudimg-rootfs)" "$MNT"
  if [ -n "$(part BOOT)" ]; then mount "$(part BOOT)" "$MNT/boot"; fi
  mount "$(part UEFI)" "$MNT/boot/efi"
}

# ---- Stage 1: shared base with Tailscale installed ----
qemu-img convert -f qcow2 -O raw "$OUT/$IMG_NAME" "$RAW"
qemu-img resize -f raw "$RAW" "$DISK_SIZE"
mount_disk "$RAW"
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
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$VERSION_CODENAME.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$VERSION_CODENAME.tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tailscale

  # Hyper-V storage/network drivers must be in the initramfs or the VM cannot find its root disk
  KVER=$(ls /lib/modules | sort -V | tail -1)
  for m in hv_vmbus hv_storvsc hv_netvsc; do
    modinfo -k "$KVER" "$m" >/dev/null 2>&1 || NEED_EXTRA=1
  done
  if [ -n "${NEED_EXTRA:-}" ]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y "linux-modules-extra-$KVER"
  fi
  for m in hv_vmbus hv_storvsc hv_netvsc; do
    modinfo -k "$KVER" "$m" >/dev/null
    grep -qx "$m" /etc/initramfs-tools/modules || echo "$m" >> /etc/initramfs-tools/modules
  done
  update-initramfs -u -k "$KVER"

  # Make tty1 (the Hyper-V console window) the primary console. The cloud image puts
  # ttyS0 last, which sends boot messages, emergency mode and cloud-init to a serial port.
  # The grub.d file keeps it that way when a kernel update regenerates grub.cfg.
  for f in /boot/grub/grub.cfg /etc/default/grub.d/*.cfg /etc/default/grub; do
    if [ -f "$f" ]; then sed -i "s/console=tty1 console=ttyS0/console=ttyS0 console=tty1/" "$f"; fi
  done
  grep -q "console=ttyS0 console=tty1" /boot/grub/grub.cfg

  apt-get clean
  systemctl enable tailscaled.service tailscale-firstboot.service
  tailscale version
  help=$(tailscale up --help 2>&1 || true)
  grep -q "file:" <<<"$help"                    # --auth-key=file:... must be supported
  test ! -e /var/lib/tailscale/tailscaled.state # no identity baked in; every VM is a new node
'

rm -f "$MNT/usr/sbin/policy-rc.d"
rm -f "$RESOLV"
if [ -e "$RESOLV.build-bak" ] || [ -L "$RESOLV.build-bak" ]; then mv "$RESOLV.build-bak" "$RESOLV"; fi
# Per-machine identity is regenerated on first boot
: > "$MNT/etc/machine-id"
rm -f "$MNT"/etc/ssh/ssh_host_*
unmount_disk
fsck_disk "$RAW"

# ---- Stage 2: one VHDX per hostname, with its own cloud-init seed and key ----
for host in "$@"; do
  disk=$OUT/$host.raw
  cp --sparse=always "$RAW" "$disk"
  mount_disk "$disk"

  seed=$MNT/var/lib/cloud/seed/nocloud
  mkdir -p "$seed"
  install -m 600 "$HERE/user-data" "$seed/user-data"
  printf 'instance-id: %s-%s\nlocal-hostname: %s\n' "$host" "$(date +%Y%m%d%H%M%S)" "$host" > "$seed/meta-data"
  chmod 600 "$seed/meta-data"

  if [ -n "${TS_AUTHKEY:-}" ]; then
    install -d -m 700 "$MNT/etc/tailscale"
    ( umask 077; printf '%s\n' "$TS_AUTHKEY" > "$MNT/etc/tailscale/authkey" )
  fi

  unmount_disk
  fsck_disk "$disk"
  qemu-img convert -f raw -O vhdx -o subformat=dynamic "$disk" "$OUT/$host.vhdx"
  rm -f "$disk"
  [ -n "${SUDO_UID:-}" ] && chown "$SUDO_UID:$SUDO_GID" "$OUT/$host.vhdx"
  echo "Built $OUT/$host.vhdx"
done

rm -f "$RAW"
[ -n "${TS_AUTHKEY:-}" ] && echo "Auth key is baked into each VHDX. Treat them as secrets."
echo "Done."
