# Turnkey host image

Bootable Ubuntu LTS image for miner servers (docs/01 §5). Built with mkosi; installed from ISO or PXE.

- `mkosi.conf`: base distribution, packages (qemu-kvm, libvirt, OVMF, nvme-cli, pciutils, dmidecode, chrony, python3, racksdock-host-agent wheel).
- `mkosi.extra/`: files overlaid on the image: kernel cmdline (IOMMU, hugepages), vfio-pci binding, systemd units, first-boot script.
- `scripts/firstboot.sh`: asks for miner hotkey + network, discovers GPU/NVSwitch/NVMe PCI functions, writes `/etc/racksdock/resources.json`, enables the agent.
- `scripts/bind-vfio.sh`: binds every NVIDIA GPU (class 0302) and NVSwitch (class 0680, vendor 10de) plus the data NVMe controllers to `vfio-pci` at boot; the NVIDIA host driver is never loaded for them.

Build: `mkosi -f build` (requires mkosi >= 24 on the build machine). Output: `racksdock-host_<version>.raw` + ISO installer. Sign with the catalog key and publish the digest in the catalog manifest.
