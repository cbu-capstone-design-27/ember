# k3s over Tailscale: Step-by-Step Setup

Pi 5 control plane + 2 Hyper-V workers, with every node joined to Tailscale so addressing does not depend on DHCP reservations.

| Node | Hardware | Role | Tailscale name |
|---|---|---|---|
| `pi-cp` | Raspberry Pi 5 (arm64) | k3s server (control plane) | `pi-cp` |
| `hv-worker-1` | Ubuntu VM on Hyper-V (amd64) | k3s agent | `hv-worker-1` |
| `hv-worker-2` | Ubuntu VM on Hyper-V (amd64) | k3s agent | `hv-worker-2` |

The Ansible project (`infra/ansible/`) runs from WSL2 on the Windows host.

---

## Prerequisites

- Tailscale account with admin access to the tailnet
- Tailscale installed and logged in on the Windows 11 host (WSL2 reaches the tailnet through it)
- Raspberry Pi Imager 2.0 or newer
- `qemu-user-static` in WSL with the aarch64 binfmt handler enabled (`/proc/sys/fs/binfmt_misc/qemu-aarch64` exists), to build the Pi image
- Pi 5 with an SD card or NVMe drive (NVMe preferred, SD cards wear out under k3s writes)
- `qemu-img` and `e2fsprogs` in WSL (`sudo dnf install qemu-img e2fsprogs`) to build the worker disks
- This repo cloned inside the WSL filesystem (not `/mnt/c`) and `python3` in WSL. `make deps` installs Ansible and `kubectl` into `infra/ansible/.venv`, so nothing else is needed

---

## Part 1: Prepare the tailnet

### Step 1.1: Add the `tag:k3s` tag

Admin console > **Access controls**. Add a `tagOwners` entry:

```json
"tagOwners": {
  "tag:k3s": ["autogroup:admin"]
}
```

If your policy is still the default allow-all, that is all you need. If you have restricted it, also add rules so the nodes can talk to each other and you can reach them:

```json
"acls": [
  { "action": "accept", "src": ["tag:k3s"],          "dst": ["tag:k3s:*"] },
  { "action": "accept", "src": ["autogroup:member"], "dst": ["tag:k3s:22,6443"] }
]
```

### Step 1.2: Confirm MagicDNS is on

Admin console > **DNS** > MagicDNS enabled. This gives you `ssh user@pi-cp` from any tailnet device.

### Step 1.3: Generate an auth key

Admin console > **Settings** > **Keys** > **Generate auth key**:

| Setting | Value | Why |
|---|---|---|
| Reusable | On | Same key joins all three nodes |
| Expiration | 1 day | Limits damage if the key leaks |
| Ephemeral | Off | Ephemeral nodes get removed when offline |
| Pre-approved | On | No manual approval click needed |
| Tags | `tag:k3s` | Tagged devices have node key expiry disabled by default |

Copy the `tskey-auth-...` value somewhere temporary. Do not commit it anywhere.

---

## Part 2: Build the Pi image

The Pi image is stock Raspberry Pi OS Lite (64-bit) with Tailscale preinstalled and a first-boot unit that joins the tailnet. Nothing is downloaded or installed on first boot, so the Pi only needs Wi-Fi: no cable to a laptop, no monitor. Everything lives in `pi-image/`:

| File | What it is |
|---|---|
| `build.sh` | Downloads the latest Pi OS Lite arm64, installs Tailscale in an arm64 chroot, bakes in the cloud-init seed |
| `tailscale-firstboot.service` | Runs `tailscale up --auth-key=file:/boot/firmware/tailscale-authkey`, retries every 30 s until it joins, then deletes the key |
| `user-data.example` | Hostname, user, SSH key, passwordless sudo |
| `network-config.example` | Wi-Fi, plus Ethernet DHCP if a cable happens to be plugged in |

### Step 2.1: Fill in the cloud-init files

```bash
cd pi-image
cp user-data.example user-data
cp network-config.example network-config
```

Fill in the placeholders. The username must match `ansible_user` in `ansible/group_vars/k3s_cluster.yml` (`control`). Get the password hash from `mkpasswd -m yescrypt` and the Wi-Fi PSK from `wpa_passphrase <SSID> <password>` (the 64-hex line). Both files are gitignored because they hold the Wi-Fi PSK and password hash.

### Step 2.2: Build

With the auth key from Step 1.3 (fish: `sudo TS_AUTHKEY=(cat .env) ./pi-image/build.sh`):

```bash
sudo TS_AUTHKEY=tskey-auth-... ./pi-image/build.sh
```

The output is `pi-image/out/pi-tailscale.img`. It holds the auth key, so keep it out of git (it is ignored) and delete it once the key is revoked. Without `TS_AUTHKEY`, the image has no key and the first-boot unit simply does not run; see Step 3.2.

The build also checks that `tailscaled.state` is absent, so every Pi flashed from the image gets its own Tailscale identity.

---

## Part 3: Flash and boot

### Step 3.1: Flash

In Raspberry Pi Imager: **Device** Raspberry Pi 5, **OS** > **Use custom** > `pi-tailscale.img`, **Storage** your SD card or NVMe. When Imager offers OS customisation, choose **No**: the image already carries its `user-data`, `network-config`, and instance-id, and Imager's settings would replace them.

### Step 3.2: Only if you built without `TS_AUTHKEY`

Reinsert the card, open `bootfs`, and create a file named `tailscale-authkey` containing just the key.

### Step 3.3: Boot

Move the card or drive to the Pi and power it on. No other cables are needed.

---

## Part 4: First boot and verify the Pi

### Step 4.1: Boot

Power on and wait 3 to 5 minutes. First boot resizes the filesystem, runs cloud-init, and may reboot once.

### Step 4.2: Check the admin console

**Machines** page should list `pi-cp` with `tag:k3s` and **Expiry disabled**.

### Step 4.3: Check from WSL

```bash
ssh <user>@pi-cp            # or use the 100.x address if MagicDNS does not resolve in WSL
tailscale ip -4             # run on the Pi; write this IP down for Part 7
ls /boot/firmware/tailscale-authkey   # should fail: the key is deleted after the join
```

### Step 4.4: If it did not join

Plug in a monitor and keyboard, or an Ethernet cable to a router (`eth0` uses DHCP), then:

```bash
journalctl -u tailscale-firstboot -b    # each failed attempt and its error; it retries every 30 s
nmcli device status                     # is wlan0 connected?
timedatectl                             # a wrong clock can fail the TLS handshake until NTP syncs
cloud-init status --long                # did user-data and network-config apply?
```

If the key was wrong or expired, write a fresh one to `/boot/firmware/tailscale-authkey` and run `sudo systemctl restart tailscale-firstboot`.

---

## Part 5: Build the worker disks

Same idea as the Pi image, built from Ubuntu's official 24.04 **cloud image** instead of an installer ISO. Each worker gets its own VHDX with Tailscale installed and its hostname and auth key baked in, so the VM joins the tailnet on first boot. Files are in `worker-image/`:

| File | What it is |
|---|---|
| `build.sh` | Downloads and checksums the cloud image, installs Tailscale and the Hyper-V drivers in a chroot, then writes one VHDX per hostname |
| `tailscale-firstboot.service` | Runs `tailscale up --auth-key=file:/etc/tailscale/authkey`, retries every 30 s until it joins, then deletes the key |
| `user-data.example` | User, SSH key, passwordless sudo (the hostname comes from the build argument) |

### Step 5.1: Virtual switch

An **External** switch bound to your wired NIC is preferred because nodes can find direct LAN paths to each other. The Default Switch also works now that traffic goes over Tailscale, but connections are more likely to fall back to slower DERP relays.

```powershell
New-VMSwitch -Name LAN-External -NetAdapterName 'Ethernet' -AllowManagementOS $true
```

### Step 5.2: Fill in `user-data`

```bash
cd worker-image
cp user-data.example user-data
```

Use the same username, password hash, and SSH key as the Pi (you can copy them from `pi-image/user-data`). The file is gitignored.

### Step 5.3: Build

Needs `qemu-img` and `e2fsprogs` in WSL (`sudo dnf install qemu-img e2fsprogs`). From the repo root (fish: `sudo TS_AUTHKEY=(cat .env) ./worker-image/build.sh hv-worker-1 hv-worker-2`):

```bash
sudo TS_AUTHKEY=tskey-auth-... ./worker-image/build.sh hv-worker-1 hv-worker-2
```

Output is `worker-image/out/hv-worker-1.vhdx` and `hv-worker-2.vhdx`. Each holds the auth key, so treat them as secrets until the VMs have joined. The disks are 40 GB dynamic (override with `DISK_SIZE=80G`), and cloud-init grows the root filesystem on first boot. The downloaded cloud image is cached in `out/`; delete it to pick up a newer one.

### Step 5.4: Copy the disks to Windows

```bash
mkdir -p /mnt/d/HyperV/Disks
cp worker-image/out/hv-worker-*.vhdx /mnt/d/HyperV/Disks/
```

---

## Part 6: Create and join the workers

### Step 6.1: Create each VM in Hyper-V Manager

**Action** > **New** > **Virtual Machine**, once per worker:

| Page | Setting |
|---|---|
| Name | `hv-worker-1` (match the disk name) |
| Generation | **Generation 2** |
| Memory | 4096 MB or more; uncheck **Use Dynamic Memory** (`make prep` refuses an agent under 3500 MB) |
| Networking | `LAN-External` |
| Connect Virtual Hard Disk | **Use an existing virtual hard disk** > `D:\HyperV\Disks\hv-worker-1.vhdx` |

Before first start, open the VM's **Settings**:

- **Security**: keep Secure Boot on, template **Microsoft UEFI Certificate Authority** (the default Windows template will not boot Linux)
- **Processor**: 2 or more virtual processors

Start the VM. Nothing else is needed; after a minute or two it appears in the Tailscale admin console with `tag:k3s`. To watch it boot, use **Connect**; the login prompt shows up once cloud-init has run.

### Step 6.2: Record the Tailscale IPs

From WSL, for Part 7:

```bash
tailscale status | grep hv-worker     # or: ssh control@hv-worker-1 tailscale ip -4
```

### Step 6.3: If a worker did not join

Open the VM console in Hyper-V Manager, log in as your user (password from `user-data`), then:

```bash
journalctl -u tailscale-firstboot -b     # each attempt and its error; it retries every 30 s
ip -br addr                              # did eth0 get an address from the switch?
cloud-init status --long
```

If the key expired, write a new one to `/etc/tailscale/authkey` (mode 600) and run `sudo systemctl restart tailscale-firstboot`.

### Step 6.4: Verify the mesh

From any node:

```bash
tailscale status
tailscale ping pi-cp     # "via <LAN ip>" is direct; "via DERP" means relayed
```

### Step 6.5: Revoke the auth key

Admin console > **Settings** > **Keys** > revoke it. Existing nodes stay connected. Generate a fresh key the next time you add a node.

---

## Part 7: Configure the cluster with Ansible

Everything after first boot is in `infra/ansible/`. It wraps the upstream [k3s-io/k3s-ansible](https://github.com/k3s-io/k3s-ansible) collection (`k3s.orchestration`, pinned to a commit in `requirements.yml`) with our own prep and smoke-test playbooks.

| File | What it is |
|---|---|
| `inventory.yml` | Hosts only: `server` (pi-cp) and `agent` (the workers) |
| `host_vars/<node>.yml` | `ts_ip`: the node's Tailscale IP. Ansible connects to it, and k3s advertises it |
| `group_vars/k3s_cluster.yml` | SSH user (`control`), `ansible_host: "{{ ts_ip }}"`, minimum memory per role |
| `group_vars/all/main.yml` | k3s version, `api_endpoint`, server/agent args (tailscale0, the Pi taint), `cluster_context: homelab`, extra manifests |
| `group_vars/all/vault.yml` | Encrypted k3s join token (`vault_k3s_token`). Committed; `.vault-pass` is not |
| `playbooks/prep.yml` | Checks tailnet, hostname and memory; apt upgrade; time sync; reboots only if needed |
| `playbooks/post.yml` | Makes the server's `/etc/rancher/k3s/config.yaml` (which holds the token) `0640 root:adm`, so `control` can run `k3s`/`kubectl` on the Pi without permission warnings |
| `playbooks/smoke.yml` | Nodes Ready on 100.x IPs, Pi taint, pod DNS, PVC data survives a pod restart |
| `manifests/local-path-retain.yaml` | Extra StorageClass (see Storage below) |
| `site.yml` | prep, then `k3s.orchestration.site`, then post, then smoke |

Run `make` with no target to list them all.

### Step 7.1: Install the tooling (once per machine)

```bash
cd infra/ansible
make deps          # .venv with ansible-core + netaddr, pinned collections, kubectl matching k3s_version
```

### Step 7.2: The join token (once per cluster)

`group_vars/all/vault.yml` is already committed. You only need `.vault-pass`, which is not in git: get it from whoever ran `make vault-init` (it belongs in a password manager). For a brand-new cluster, delete both and run `make vault-init`, which generates a random token and encrypts it. Without the token you cannot add nodes to the cluster.

### Step 7.3: Record the nodes

`inventory.yml` lists hosts by name. Each node gets `host_vars/<name>.yml` with its IP from Parts 4 and 6:

```yaml
# host_vars/hv-worker-2.yml
ts_ip: 100.x.x.3
```

Tailscale IPs are stable for the life of the device, so these replace DHCP reservations.

---

## Part 8: Build the cluster

From WSL, in `infra/ansible`:

```bash
make ping      # every node answers over 100.x
make prep      # optional; make up runs it first
make up        # prep, install k3s, merge the kubeconfig as context "homelab", smoke tests
make nodes
```

**Expected:** every node `Ready`, the `INTERNAL-IP` column shows `100.x` addresses, and the smoke play passes. `make smoke` re-runs just the checks. `make up` is safe to re-run, but k3s-ansible restarts k3s on every run, so expect "changed" tasks and a short API blip.

`kubectl` from `.venv/bin` works directly: `ansible/.venv/bin/kubectl --context homelab get pods -A` (or put `.venv/bin` on your PATH).

### Storage

- k3s's bundled `local-path` provisioner stores volumes under `/var/lib/rancher/k3s/storage` on the node that runs the pod. The Pi carries a `CriticalAddonsOnly` taint, so volumes only land on the workers. Workers carry the label `ember.io/storage=true` for nodeSelectors.
- Use StorageClass **`local-path-retain`** for anything stateful (Neo4j, Postgres). Deleting the PVC leaves the data on disk, and you remove it yourself with `kubectl delete pv` plus the directory on the node. The default `local-path` class deletes the data along with the PVC.
- A local-path volume is tied to one worker, so a stateful pod cannot fail over to another node. Worker disks are 40 GB unless built with `DISK_SIZE=80G`.

### Day-2 targets

| Target | What it does |
|---|---|
| `make upgrade` | Bump `k3s_version` in `group_vars/all/main.yml` first, then run it (and `make deps` for a matching kubectl) |
| `make reboot` | Rolling reboot of every node |
| `make kubeconfig` | Re-fetch the kubeconfig into `~/.kube/config` |
| `make reset` | Uninstall k3s everywhere (asks you to type `reset homelab`). Destroys all volumes |

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Node shows a LAN IP instead of `100.x` | `--node-ip` did not apply. Inspect `/etc/systemd/system/k3s*.service` on that node, then `make up` again |
| `make prep` fails "needs 3500 MB" | The worker VM has Dynamic Memory on or too little RAM. Shut it down, VM **Settings** > **Memory**: 4096 MB, uncheck **Enable Dynamic Memory**, start it |
| `make` fails with "The vault password file ... .vault-pass was not found" | `.vault-pass` is missing (Step 7.2) |
| Agents never join | From a worker: `curl -k https://<pi ts_ip>:6443/ping` should return `pong`. If not, check ACLs |
| Pods on different nodes cannot reach each other | `tailscale ping` between nodes; confirm ACL allows `tag:k3s` to `tag:k3s:*` (VXLAN uses UDP 8472) |
| Everything is slow | `tailscale ping` shows `via DERP`. Use the External switch and make sure UDP 41641 is not blocked outbound |
| Edited `user-data` on an already-booted card, but nothing reran | cloud-init only reruns for a new instance-id, and it reads the one on the kernel command line (`ds=nocloud;i=...` in `cmdline.txt`) over `meta-data`. Change the `i=` value (keep it one line) or run `sudo cloud-init clean --reboot` |
| Pi never shows up in the admin console | See Step 4.4; most often Wi-Fi (wrong SSID/PSK in `network-config`) or an expired key |
| Hyper-V: "virtual hard disk files must be uncompressed and unencrypted and must not be sparse" | In an admin PowerShell: `fsutil sparse setflag D:\HyperV\Disks\hv-worker-1.vhdx 0`, or copy the file with `cp` from WSL instead of Explorer |
| Hyper-V VM stops at a "No operating system was loaded" or Secure Boot error | VM **Settings** > **Security** > template **Microsoft UEFI Certificate Authority** |
| Pi missing after a reboot | `systemctl is-enabled tailscaled` should be `enabled` |
| `make ping` fails from WSL | Confirm Tailscale is running on the Windows host and `ping 100.x.x.1` works from PowerShell first |

---

## Adding a node later

1. Generate a new short-lived tagged auth key
2. For a worker: `sudo TS_AUTHKEY=... ./worker-image/build.sh <name>` and create the VM (Parts 5 and 6). For a Pi: change `hostname` in `pi-image/user-data`, rebuild with the new key, and flash (Parts 2 and 3)
3. Boot it; it joins by itself
4. Add it under `agent` in `ansible/inventory.yml` and create `ansible/host_vars/<name>.yml` with its `ts_ip`
5. `make up` (existing nodes keep their data, but k3s restarts on them)
6. Revoke the key
