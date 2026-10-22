# images (`racksdock_node.images` + `image-build/`)

Catalog images (docs/01 §6) and the tools baked into them.

- `image-build/build.sh <os>`: downloads the Ubuntu cloud image, applies `image-build/packages/<os>.txt` and `image-build/overlay/` with `virt-customize`, installs the pinned NVIDIA driver + Fabric Manager (+ CUDA for `_cuda` variants), installs `racksdock-node-agent` into `/opt/racksdock/venv`, compiles STREAM, writes `/etc/racksdock/image-version`, and produces `<os>_<release>.qcow2`.
- `image-build/overlay/usr/local/bin/`: in-image tools used by cloud-init and the validator suite:
  - `racksdock-launch` runs the tenant's startup script or Docker container from `/etc/racksdock/launch.json` and records `/run/racksdock/launch.status`.
  - `racksdock-memtest` allocates and verifies GPU memory on all GPUs through the CUDA driver API (ctypes, no toolkit needed) and measures write bandwidth; `--quick` checks for a leftover marker pattern.
  - `racksdock-ramtest`, `racksdock-marker`, `racksdock-train-bench`, `racksdock-infer-bench` (wrappers around the pinned workload containers `racksdock/train-bench:pinned` and `racksdock/infer-bench:pinned`; the containers print `tokens_per_s` and step-time percentiles).
- `racksdock-images manifest build|sign|verify`: the catalog manifest lists image digests, versions and pins; it is signed with the catalog key and the host-agent refuses images that do not match it.

Image builds require a Linux build host with libguestfs (`virt-customize`) and network access; they are not run in CI here. The manifest tooling is tested.

Publishing a catalog on a host (also done by `racksdock-platform/dev/vmtest/build-images.sh`):

```
OUT_DIR=/var/lib/racksdock/images PYTHON=/opt/racksdock/venv/bin/python image-build/build.sh verify v2026.10.1 580.82.07
racksdock-images manifest build /var/lib/racksdock/images v2026.10.1 --out /var/lib/racksdock/images/manifest.json --driver 580.82.07
racksdock-images manifest sign /var/lib/racksdock/images/manifest.json /etc/racksdock/catalog.key \
    --out /var/lib/racksdock/images/manifest.signed.json --pub-out /var/lib/racksdock/images/catalog.pub
racksdock-images manifest os-options /var/lib/racksdock/images/manifest.signed.json "$(cat /var/lib/racksdock/images/catalog.pub)" > /var/lib/racksdock/images/catalog.json
```

`build.sh` fetches the node-agent's dependencies for the **guest's** Python (3.12 on 24.04 images, 3.11 from deadsnakes on 22.04
images), installs the open-kernel-module server driver branch with fallbacks (exact pin → branch → proprietary → newest), and for
`verify` installs DCGM (skipped with a warning where unavailable), the CUDA toolkit, nccl-tests and nvbandwidth. The node-agent runs
as root in the VM (docs/node-agent-privileges.md).

## Container-tier images (`container-image/`)

`container-image/build.sh <os> <release>` builds `racksdock/<os>:<release>` with docker: `ubuntu24.04` (tenant: sshd, nested Docker,
node-agent) from `ubuntu:24.04`, and `verify` (DCGM 4, fio, iperf3, sysbench, memtester, nccl-tests, nvbandwidth, STREAM) from the
CUDA devel image. The host's NVIDIA driver is injected at run time by the container toolkit, so images carry no driver. The same
in-image tools (`racksdock-memtest`, `racksdock-launch`, ...) are copied from `image-build/overlay`. `racksdock-images manifest build
<dir> <release> --containers` adds entries of kind `container` pinned to the docker image id; the host-agent refuses an image whose
id differs. `dev/vmtest/build-container-images.sh verify ubuntu24.04` does build, manifest, sign and catalog in one go.
