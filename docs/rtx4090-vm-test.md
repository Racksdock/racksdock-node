# 8x RTX 4090 bare-metal VM test (temporary)

This runbook exercises the **real** delivery path for the first time: the host-agent creates a KVM virtual machine with all eight
GPUs passed through (VFIO), the validator rents that VM over SSH, a tenant rents it with the CLI. The A100 test used the lab-only
`static` mode (no VM, no isolation); this one uses the production `real` mode with two substitutions:

| Substituted | Why | What replaces it |
|---|---|---|
| GPU attestation | Ada GPUs have no device attestation (no `nvattest`) | dev CA: the platform issues a simulated identity for the 8 GPUs; the real GPUs are still measured by the suite |
| Profile | GeForce has no NVLink, no PCIe P2P, no DCGM diag | `rtx4090x8-dev`: Q2, Q3, Q7, Q8, Q9–Q13 (Q4/Q5/Q6/Q14/Q15 excluded), lenient reference set |

Everything else is production code: libvirt domain, OVMF, VFIO binding including the cards' audio functions, cloud-init seed,
vsock node-agent, LUKS data disk, NAT port forwarding, signed image catalog, admission test (create / 8 GPUs / recover / clean),
qualification, canaries, in-rental checks, scoring.

The box: bare metal (`systemd-detect-virt: none`), EPYC 75F3 128 threads, 503 GB RAM, 1.7 TB free on the root NVMe, Ubuntu
22.04.5, kernel 6.8, AMD-Vi on with 159 IOMMU groups, 8x RTX 4090 (`10de:2684`) each with an audio function (`10de:22ba`), only a
private address (172.19.4.67). Because it has no public IP, **everything runs on the box** (platform, validator, miner, tenant CLI);
the VM is reached at `172.19.4.67:20022` through NAT. Topology B (platform + validator on another machine) works only if that
machine can reach `172.19.4.67:20022`; see §6.

## 1. What runs where (single box, all as root)

| Process | Role | Started by |
|---|---|---|
| `racksdock-marketplace` | platform (API, console, dev CA, lease/orchestration) | `run_vm_test.py` |
| `racksdock-host-agent` (mode `real`) | the miner's server software: creates/destroys the VM | `run_vm_test.py` |
| `racksdock-miner bootstrap` | miner neuron: registers the node from the bootstrap VM's evidence | `run_vm_test.py` |
| `racksdock-validator run` | validator: admission, qualification, canaries, in-rental checks, weights | `run_vm_test.py` |
| `racksdock` CLI | tenant journey (sign-up, key, create, attestation-verified ssh, delete) | `run_vm_test.py` |
| `iperf3 -s` | Q12 endpoint (host side of the virtual NIC; use `--iperf 62.83.43.83` for a real link if that server runs `iperf3 -s`) | `run_vm_test.py` |

Wallets: not needed. The chain is the mock JSON file; hotkeys are placeholder strings. Nothing talks to Bittensor.

## 2. Prepare the host (once; ~5 min)

Copy the three repositories to the box the same way you did for the A100 box, so that `/root/racksdock/` contains
`racksdock-subnet`, `racksdock-node`, `racksdock-platform` (any directory works; adjust `REPOS`).

**This step unloads the NVIDIA driver on the host.** From here on the GPUs belong to VMs; `nvidia-smi` on the host stops working.
Stop anything using the GPUs first (`docker ps`, `nvidia-smi`).

```bash
# on the 4090 box, as root
REPOS=/root/racksdock PROFILE=rtx4090x8-dev TRUST_MODE=dev bash /root/racksdock/racksdock-node/host-image/scripts/setup-host.sh
```

What it does: installs qemu-kvm, libvirt, OVMF, libguestfs, iptables, Python 3.11 (deadsnakes); loads `vfio-pci` and `vhost_vsock`;
blacklists the host NVIDIA driver; starts libvirt's `default` NAT network; records the GPU facts (PCI id, VRAM) while `nvidia-smi`
still works; unloads the NVIDIA modules; writes `/etc/racksdock/resources.json`; binds the 16 GPU/audio functions to `vfio-pci` now
and at every boot; installs the racksdock venv at `/opt/racksdock/venv`; creates the dev CA in `/etc/racksdock/devca` and the 8-GPU
simulated identity in `/etc/racksdock/node`; writes `/etc/racksdock/host-agent.env`.

Check the printed `resources.json`: 8 entries in `gpu_pci`, 8 in `extra_pci` (the `.1` audio functions), `nvme_pci` **empty**
(the two spare NVMe drives are left alone; the VM gets a 500 GB virtio data disk instead, because passed-through NVMe is
crypto-erased after every rental), `vcpus` 120, `memory_gb` 439, `hugepage_kib` 0 (no hugepages reserved on this box).
Every line under step 7 must end in `-> vfio-pci`.

If the NVIDIA modules cannot be unloaded the script stops: reboot (the blacklist is installed) and run it again.

## 3. Build the two images on the box (once; 30–60 min, mostly NVIDIA downloads)

```bash
bash /root/racksdock/racksdock-platform/dev/vmtest/build-images.sh verify ubuntu24.04
```

Builds `verify_v2026.10.1.qcow2` (validator image: driver, DCGM if available, CUDA 13, nccl-tests, nvbandwidth, fio, iperf3,
sysbench) and `ubuntu24.04_v2026.10.1.qcow2` (tenant image: driver, Docker), signs the catalog manifest with a key created at
`/etc/racksdock/catalog.key`, and writes `manifest.signed.json`, `catalog.pub` and `catalog.json` to `/var/lib/racksdock/images`.
The marketplace is pointed at that `catalog.json`, so it only offers images that exist on this host. The default driver is the
580 open-kernel-module server branch; `DRIVER=<version>` overrides it. If an exact version is missing, the build falls back to the
branch and prints a WARNING.

## 4. Run the workflow (~45–75 min)

```bash
/opt/racksdock/venv/bin/python /root/racksdock/racksdock-platform/dev/vmtest/run_vm_test.py
```

Expected sequence and timings on this box:

1. marketplace + host-agent start. The host-agent destroys any stale `racksdock-*` VM and logs `preflight:` lines if something is
   missing (OVMF, vfio binding, libvirt network). No preflight lines = ready.
2. `racksdock-miner bootstrap`: the host boots a `verify` VM (OVMF + 439 GB pinned + 8 GPUs: 1–2 min to the login prompt,
   cloud-init and driver load 1–2 min), the node-agent connects over vsock, sends evidence, the platform verifies it against the dev
   CA, the miner commits and registers. 3–6 min. The runner prints `virsh list` every 15 s.
3. validator: admission test (create a verify VM → 8 GPU ids match → stop node-agent and drop SSH, host must recover via
   `virsh reboot` → delete → fresh VM boots clean), then the qualification suite: Q2 SKU, Q3 memory (`racksdock-memtest` on all
   8 GPUs), Q7 host↔device bandwidth (nvbandwidth), Q8 NCCL all_reduce/all_gather/reduce_scatter/alltoall 1 KB–8 GB over host memory
   (slow on GeForce, ~10 min), Q9 CPU (STREAM, sysbench), Q10 RAM, Q11 NVMe (fio on the LUKS data disk, 2 min sustained), Q12 network
   (iperf3 60 s each way), Q13 power/clock profile (3 min under load). 20–35 min in total.
4. tenant journey: sign-up, key, `instances create --os ubuntu24.04`, wait for `active`, `racksdock ssh --check-only` (attestation
   + pinned host key), then a real `ssh` that prints `nvidia-smi`, `nproc`, `free`, `lsblk` **from inside the VM**: expect 8 GPUs,
   120 CPUs, ~430 GB RAM, `vda` 200 GB and `vdb` 500 GB (the data disk, mounted encrypted at `/data`).
5. 10 min of in-rental checks (attestation + allowlisted health every 60 s), delete, readiness canary.

While it runs, from another shell on the box:

```bash
virsh list --all                                # the VM: racksdock-<instance id>
virsh console racksdock-<id>                    # guest console (Ctrl+] to leave)
iptables -t nat -S | grep racksdock-nat         # the host:20022 -> VM:22 forward
tail -f /var/lib/racksdock-vmtest/logs/host-agent.log
tail -f /var/lib/racksdock-vmtest/logs/validator.log
curl -s http://127.0.0.1:<port>/v1/evidence | head   # port is printed by the runner
```

Isolation check you asked for after the A100 test: SSH into the tenant VM, `sudo -s`, `df -h`, `lsblk`, `ip a`. You see the VM's
disks (`vda`, `vdb`) and the VM's 192.168.122.x address only. The host's root filesystem, the other NVMe drives, Docker and the
host's network are not reachable. The remaining gap is the one documented in docs/05: the **miner** controls the hypervisor and
could read VM memory; no CC in this design.

## 4a. Result (2026-10-20)

Passed end to end on the box, in `real` mode: registration from a bootstrap VM, admission test (all four demonstrations incl. forced
recovery via `virsh reboot`), full qualification (Q2, Q3, Q7, Q8, Q9–Q13) in 53 min, tenant rental with verified attestation and
SSH login, 10/10 in-rental checks, delete. Measured inside the VM: 8× RTX 4090 (21.0 GiB usable each, VBIOS 95.02.3C), EPYC 75F3
120 vCPU (STREAM 45 GB/s, sysbench 299k), 431 GB RAM, virtio data disk 5.5/2.1 GB/s seq, 157k/27k IOPS, 1.78 GB/s sustained,
network 16.6/32.5 Gbps (VM↔host). The tenant VM showed exactly its own devices (`vda` 200G, `vdb` 500G, 8 GPUs, 120 CPUs).

Bugs found and fixed on the way (all in production code): cloud image root filesystem not grown before customizing; copy-in target
missing; `pipefail` + `head -1` aborting the setup script; iperf3 missing on the host; the node-agent and launch units ordered after
`cloud-init.target` (systemd ordering cycle; the agent was silently skipped on most boots); image path doubled the OS prefix;
`manifest verify` argument form; Python stdout buffering through `tee`; stale test state across runs; the 4090 memory floor set
too tight (22 GiB vs 21.0 measured).

## 4b. Rent the node yourself

The scripted run exercises the tenant journey automatically. To use the node like a customer, keep the platform, host-agent and
validator up and drive the CLI or the web console by hand:

```bash
/opt/racksdock/venv/bin/python -u /root/racksdock/racksdock-platform/dev/vmtest/run_vm_test.py --manual --keep-state --port 8000 --bind 0.0.0.0
```

`--keep-state` reuses the previous run's database, so the node is already registered and qualified (no bootstrap, no 53-minute
suite). `--port 8000 --bind 0.0.0.0` puts the console at `http://172.19.4.67:8000/` for machines on that network (or tunnel:
`ssh -L 8000:127.0.0.1:8000 root@<box>`). The runner prints the full CLI sequence (sign-up, key, credits, create, `racksdock ssh`,
delete) and stays up until Ctrl+C. Inside the VM, Docker with `--gpus all` works; `/data` is the encrypted data disk.

If `racksdock nodes list` shows `avail no` with no VM running, a previous run left an instance in `deleting`: the platform now
closes such an instance (and releases the node's lease) about a minute after the host-agent's heartbeat reports no VM. Before
that fix, delete it by hand under its owner (`RACKSDOCK_HOME=<that user's home> racksdock instances delete <id>`). A CLI config from
an earlier run may still point at the old random port: `racksdock auth login --email ... --password ... --api-url http://127.0.0.1:8000`.

Found during the manual rental (2026-10-20 evening): the agent's `/data` mount was invisible to the tenant because the unit had
`ProtectHome=read-only` (a private mount namespace). Fixed in the unit; built images need `dev/vmtest/patch-images.sh` once no
rental is running. Workaround inside an affected VM: `sudo mount /dev/mapper/racksdockdata /data`.

## 4c. Tenant benchmark findings (2026-10-21) and what they mean

A full benchmark inside a tenant VM (Qwen3-8B LoRA/QLoRA/full fine-tune, burns, VRAM tests) reported: healthy GPUs
(164 TFLOPS bf16, 920 GB/s, 0 VRAM errors, 35 min at 445 W each with 0 errors, peak 77 °C), 8-GPU LoRA at 92% scaling, but slow
GPU-to-GPU traffic (~1.3 GB/s NCCL), GPU 0 on a PCIe x8 link, and one 12-minute freeze of the whole VM.

- **The freeze was not the platform.** Host journal: at 00:44 UTC a root shell on the host ran
  `virsh dump <vm> --memory-only --live` (470 GB memory dump, `/var/tmp/vm-forensics/racksdock-memory.elf`, finished 00:56).
  QEMU's memory dump stops the vCPUs for the duration; the guest clock jumps when it resumes. Exactly the "miner/host operator can
  read VM memory" capability docs/05 describes. Delete the dump when done (470 GB).
- **The validator could not see the freeze**: its in-rental task died on the platform's `{"error": "node-agent unreachable"}`
  answer (pydantic error, unretrieved) and wrote no record for 12 minutes. Fixed: every failed challenge is now an in-rental record
  with `attestation_ok=false` and the error; test in `tests/validator/test_inrental_and_q7.py`.
- **Bogus strikes**: the in-rental check treated NVML throttle bit 0x4 (SW power cap, i.e. "running at the power limit") as a
  fault and raised a power_profile incident with a strike every minute of the tenant's burn (35 strikes, instance marked
  degraded). Fixed: only HW/thermal/power-brake slowdowns count, and they raise an incident without a strike; the 35 strikes and
  incidents were removed from the test database and the instance restored to active.
- **GPU 0 at x8** is a hardware fact of the box (`lspci` on the host: `Width x8 (downgraded)`), a riser or slot issue. Q7 now
  fails a node whose per-GPU host bandwidth or PCIe link width is an outlier, so a validator catches this at qualification.
- **Slow GPU-to-GPU**: GeForce has no P2P, so NCCL goes through host memory. On this host the guest RAM is 4 KiB-page backed
  (no hugepages reserved) and `iommu=pt` is off, which makes every GPU DMA to host memory go through small IOMMU entries. The
  production host image reserves 1 GiB hugepages; on this box: `HUGEPAGES_GB=440 bash setup-host.sh`, reboot, then
  `HUGEPAGE_KIB=1048576` in the setup (or `hugepage_kib` in resources.json). Expect a large NCCL/H2D improvement and
  near-instant VM starts. Not done yet (needs a reboot of the box).
- The `NCCL_SHM_USE_CUDA_MEMCPY=1` deadlock is an NCCL/driver matter inside the guest, not something the platform sets.

## 4c. Tenant benchmark on the rental (2026-10-21) and what it taught the validator

The user's own benchmark of the rented VM (Qwen3-8B fine-tuning, burns, memory tests) found: GPUs healthy (164 TFLOPS bf16,
920 GB/s, 0 VRAM errors, 35 min at 445 W each); 8-GPU LoRA 15k tokens/s; full fine-tuning slow because NCCL over host memory
reaches only ~1.3 GB/s (GeForce has no P2P; the VM's guest RAM is 4 KiB-page backed, so GPU DMA to host memory pays IOMMU
translation, which 1 GiB hugepages would avoid); GPU 0 on a PCIe x8 link (host-side `lspci` confirms: slot/riser); and one
12-minute VM freeze. The freeze was `virsh dump --memory-only --live` of the 470 GB VM, run on the host by the benchmark's own
forensics at 00:44-00:56 UTC (host journal), not the platform. Validator fixes that came out of it (all with tests):
`inrental.py` treated the normal SW power cap (0x4) as throttling and filed a strike + incident every minute of the burn (35 of
each, instance marked degraded) -> mask is now HW/thermal/power-brake slowdowns only, throttle incidents carry no strike; the
in-rental task died on a `{"error": ...}` challenge reply instead of recording a failed check (the freeze was invisible for 12 min);
Q7 always passed -> now flags per-GPU host-link outliers (< 60 % of the node median) and PCIe links narrower than their maximum.

## 5. If something fails

| Symptom | Where | Fix |
|---|---|---|
| `preflight: ... OVMF ... missing` | host-agent.log | `apt-get install ovmf`; rerun |
| `preflight: PCI device ... bound to nvidia` | host-agent.log | GPUs still on the host driver: `rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia; /usr/local/bin/bind-vfio.sh` or reboot |
| `virsh start ... failed: ... vfio ... group is not viable` | host-agent.log | a device in a GPU's IOMMU group is not bound: `racksdock-discover-resources` prints `PROBLEM` lines; bind it or move the card |
| `virsh start ... failed: ... apparmor` / `Permission denied` on the qcow2 | host-agent.log | `echo 'security_driver = "none"' >> /etc/libvirt/qemu.conf; systemctl restart libvirtd` (lab fallback) |
| `no DHCP lease for racksdock-...` | host-agent.log | guest did not get an address: `virsh console <name>`; `virsh net-dhcp-leases default`; check `virsh net-info default` is active |
| VM `running` + `forwarding` logged, but no `node-agent hello` within 2 min | host-agent.log | look at `/var/lib/racksdock/vms/<instance>/console.log`: `Ordering cycle found, skipping racksdock-node-agent.service` means the image carries a unit ordered after `cloud-init.target` (fixed 2026-10-20; `dev/vmtest/patch-images.sh` refreshes the units in built images). Otherwise log into the VM (validator VMs: `ssh -i /var/lib/racksdock-vmtest/validator-state/ssh_ed25519 -p 20022 ubuntu@172.19.4.67`) and read `journalctl -u racksdock-node-agent -b`; on the host `lsmod | grep vhost_vsock` |
| `user-data hash mismatch` | marketplace / instance `error` | the VM's cloud-init copy differs from the platform's string; compare `/var/lib/cloud/instance/user-data.txt` in the VM with the request |
| Q8 fails or times out | validator.log, evidence `qualification` | GeForce NCCL over host memory is slow; drop `Q8` from `checks` in `rtx4090x8-dev.json`, restart the runner |
| Q11 fails: `/data` missing | validator.log | the data disk was not set up: inside the VM `lsblk`, `journalctl -u racksdock-node-agent` (cryptsetup errors) |
| image build: `supermin: failed to find a suitable kernel` | build-images.sh | `chmod 0644 /boot/vmlinuz-*` (the script does this; rerun) |
| image build: driver package not found | build-images.sh | pass `DRIVER=<version listed by apt-cache search nvidia-driver- | grep server-open>` |

Inside a VM: in dev trust mode the host-agent adds the key named by `RACKSDOCK_DEV_SSH_PUBKEY` (set by `setup-host.sh` to the
key you use for the box) to the **bootstrap** VM (empty user-data). Validator and tenant VMs define their own user list, which makes
cloud-init ignore extra keys; use the validator's key (`/var/lib/racksdock-vmtest/validator-state/ssh_ed25519`) or the tenant key
(`/var/lib/racksdock-vmtest/demo_key`) for those. The port always points at the newest VM. There:
`sudo journalctl -u racksdock-node-agent -b --no-pager`, `cloud-init status --long`, `ls -R /etc/racksdock`, `lsmod | grep vsock`,
`nvidia-smi`. The VM's boot console is kept at `/var/lib/racksdock/vms/<instance>/console.log` on the host.

Logs: `/var/lib/racksdock-vmtest/logs/`. Reset between runs: `virsh destroy <name>; virsh undefine --nvram <name>` (the next
host-agent start does this itself), `rm -rf /var/lib/racksdock-vmtest`.

## 6. Topology B (validator + platform on another machine)

Only if that machine can reach the box: forward a public `IP:PORT` to `172.19.4.67:20022` (router/firewall) and run
`setup-host.sh` with `PLATFORM_URL=http://<platform>:8000 PUBLIC_IP=<that public IP> NAT_SSH_PORT=<that port>`; then start the
host-agent as a service (`systemctl enable --now racksdock-host-agent`) and run the miner/validator steps from the A100 runbook §4
with `RACKSDOCK_PROFILE=rtx4090x8-dev`. The platform and validator never need SSH into the box itself; they only reach the VM's
forwarded port, which is what production looks like.

## 7. Not covered by this test

Real GPU attestation (`nvattest`, RIMs), NVLink/NVSwitch checks (Q5/Q6), DCGM diagnostics (Q4), workload containers (Q14/Q15),
1 GiB hugepage backing, bridged networking with a public IP per VM, the widened OVMF MMIO window for datacenter-sized BARs,
NVMe controller passthrough and `nvme sanitize` (the box's spare drives are deliberately not used), Fabric Manager. These need
the B300 reference node.

## 8. Remove after the test

Code (temporary 4090 parts only; the libvirt/NAT/setup-host changes are production code and stay):

- `racksdock-subnet/src/racksdock_subnet/core/spec/profiles/rtx4090x8-dev.json`
- `racksdock-subnet/src/racksdock_subnet/core/spec/reference-sets/ref_rtx4090_dev_v0.json`
- the `test_rtx4090_profile_...` test at the end of `racksdock-subnet/tests/core/test_profile.py`
- this file and its mention in the root `README.md`
- `racksdock-platform/dev/vmtest/` may stay: it is the generic single-box bare-metal test and is what the B300 reference node runs
  with `PROFILE=b300x8 TRUST_MODE=prod`

Box (to give the GPUs back to the host OS): `systemctl disable --now racksdock-bind-vfio racksdock-host-agent; rm
/etc/modprobe.d/racksdock-vfio.conf /etc/modules-load.d/racksdock.conf; update-initramfs -u; reboot`, then remove
`/etc/racksdock`, `/opt/racksdock`, `/var/lib/racksdock`, `/var/lib/racksdock-vmtest`, `/var/lib/racksdock-host-agent`, and the
racksdock NAT rules (`iptables-save | grep racksdock-nat` should print nothing after a host-agent cleanup).
