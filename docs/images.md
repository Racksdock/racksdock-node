# images (`racksdock_node.images` + `image-build/`)

Catalog images (docs/01 §6) and the tools baked into them.

- `image-build/build.sh <os>`: downloads the Ubuntu cloud image, applies `image-build/packages/<os>.txt` and `image-build/overlay/` with `virt-customize`, installs the pinned NVIDIA driver + Fabric Manager (+ CUDA for `_cuda` variants), installs `racksdock-node-agent` into `/opt/racksdock/venv`, compiles STREAM, writes `/etc/racksdock/image-version`, and produces `<os>_<release>.qcow2`.
- `image-build/overlay/usr/local/bin/`: in-image tools used by cloud-init and the validator suite:
  - `racksdock-launch` runs the tenant's startup script or Docker container from `/etc/racksdock/launch.json` and records `/run/racksdock/launch.status`.
  - `racksdock-memtest` allocates and verifies GPU memory on all GPUs through the CUDA driver API (ctypes, no toolkit needed) and measures write bandwidth; `--quick` checks for a leftover marker pattern.
  - `racksdock-ramtest`, `racksdock-marker`, `racksdock-train-bench`, `racksdock-infer-bench` (wrappers around the pinned workload containers `racksdock/train-bench:pinned` and `racksdock/infer-bench:pinned`; the containers print `tokens_per_s` and step-time percentiles).
- `racksdock-images manifest build|sign|verify`: the catalog manifest lists image digests, versions and pins; it is signed with the catalog key and the host-agent refuses images that do not match it.

Image builds require a Linux build host with libguestfs (`virt-customize`) and network access; they are not run in CI here. The manifest tooling is tested.
