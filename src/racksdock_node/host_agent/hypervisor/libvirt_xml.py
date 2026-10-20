"""libvirt domain XML for the whole-node VM (docs/01 §4). Pure function, unit-tested without libvirt."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from racksdock_subnet.core.schemas import VmResources

OVMF_CODE = "/usr/share/OVMF/OVMF_CODE_4M.fd"
OVMF_VARS = "/usr/share/OVMF/OVMF_VARS_4M.fd"
QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"
DATA_DISK_SERIAL = "racksdock-data"  # the guest sees /dev/disk/by-id/virtio-racksdock-data
ET.register_namespace("qemu", QEMU_NS)


def parse_pci(addr: str) -> tuple[str, str, str, str]:
    """'0000:17:00.0' -> ('0x0000','0x17','0x00','0x0')"""
    dom, bus, rest = addr.split(":")
    slot, func = rest.split(".")
    return f"0x{dom}", f"0x{bus}", f"0x{slot}", f"0x{int(func, 16):x}"


def numa_plan(vcpus: int, host_cores: list[list[int]] | None) -> list[tuple[int, int]]:
    """Return [(vcpu, host_cpu)] pinning. host_cores: per-NUMA-node list of host CPU ids available to the VM."""
    if not host_cores:
        return []
    flat: list[int] = []
    # interleave nodes so each NUMA node gets an equal share of vcpus
    per_node = [list(c) for c in host_cores]
    while any(per_node) and len(flat) < vcpus:
        for n in per_node:
            if n and len(flat) < vcpus:
                flat.append(n.pop(0))
    return [(i, c) for i, c in enumerate(flat)]


def build_domain_xml(name: str, resources: VmResources, os_disk: Path, seed_iso: Path, nvram: Path,
                     host_cores: list[list[int]] | None = None, bridge: str | None = "br0", vsock_cid: int | None = None,
                     hugepage_kib: int | None = None, nat_network: str | None = None, data_disk: Path | None = None,
                     console_log: Path | None = None) -> str:
    """`bridge` attaches a bridged NIC (one public IP per VM); `nat_network` attaches a libvirt NAT network instead (single-IP
    hosts; the backend forwards a host port). A passed-through NIC VF replaces both. `hugepage_kib` defaults to the profile's
    resources setting; 0 disables hugepage backing."""
    if hugepage_kib is None:
        hugepage_kib = resources.hugepage_kib
    dom = ET.Element("domain", type="kvm")
    ET.SubElement(dom, "name").text = name
    ET.SubElement(dom, "memory", unit="GiB").text = str(resources.memory_gb)
    ET.SubElement(dom, "currentMemory", unit="GiB").text = str(resources.memory_gb)
    mb = ET.SubElement(dom, "memoryBacking")
    if hugepage_kib:
        hp = ET.SubElement(mb, "hugepages")
        ET.SubElement(hp, "page", size=str(hugepage_kib), unit="KiB")
        ET.SubElement(mb, "locked")
    ET.SubElement(mb, "nosharepages")
    ET.SubElement(dom, "vcpu", placement="static").text = str(resources.vcpus)
    pins = numa_plan(resources.vcpus, host_cores)
    if pins:
        ct = ET.SubElement(dom, "cputune")
        for v, c in pins:
            ET.SubElement(ct, "vcpupin", vcpu=str(v), cpuset=str(c))
    os_el = ET.SubElement(dom, "os")
    ET.SubElement(os_el, "type", arch="x86_64", machine="q35").text = "hvm"
    ET.SubElement(os_el, "loader", readonly="yes", type="pflash").text = OVMF_CODE
    ET.SubElement(os_el, "nvram", template=OVMF_VARS).text = str(nvram)
    feats = ET.SubElement(dom, "features")
    ET.SubElement(feats, "acpi")
    ET.SubElement(feats, "apic")
    cpu = ET.SubElement(dom, "cpu", mode="host-passthrough", check="none", migratable="off")
    ET.SubElement(cpu, "cache", mode="passthrough")
    if host_cores and len(host_cores) > 1 and pins:
        numa = ET.SubElement(cpu, "numa")
        per = resources.vcpus // len(host_cores)
        mem_per = resources.memory_gb // len(host_cores)
        for i in range(len(host_cores)):
            lo = i * per
            hi = resources.vcpus - 1 if i == len(host_cores) - 1 else lo + per - 1
            ET.SubElement(numa, "cell", id=str(i), cpus=f"{lo}-{hi}", memory=str(mem_per), unit="GiB")
    ET.SubElement(dom, "on_poweroff").text = "destroy"
    ET.SubElement(dom, "on_reboot").text = "restart"
    ET.SubElement(dom, "on_crash").text = "destroy"
    dev = ET.SubElement(dom, "devices")
    ET.SubElement(dev, "emulator").text = "/usr/bin/qemu-system-x86_64"
    d = ET.SubElement(dev, "disk", type="file", device="disk")
    ET.SubElement(d, "driver", name="qemu", type="qcow2", cache="none", io="native", discard="unmap")
    ET.SubElement(d, "source", file=str(os_disk))
    ET.SubElement(d, "target", dev="vda", bus="virtio")
    ET.SubElement(d, "boot", order="1")
    if data_disk is not None:
        dd = ET.SubElement(dev, "disk", type="file", device="disk")
        ET.SubElement(dd, "driver", name="qemu", type="qcow2", cache="none", io="native", discard="unmap")
        ET.SubElement(dd, "source", file=str(data_disk))
        ET.SubElement(dd, "target", dev="vdb", bus="virtio")
        ET.SubElement(dd, "serial").text = DATA_DISK_SERIAL
    cd = ET.SubElement(dev, "disk", type="file", device="cdrom")
    ET.SubElement(cd, "driver", name="qemu", type="raw")
    ET.SubElement(cd, "source", file=str(seed_iso))
    ET.SubElement(cd, "target", dev="sda", bus="sata")
    ET.SubElement(cd, "readonly")
    for addr in passthrough_addresses(resources):
        hd = ET.SubElement(dev, "hostdev", mode="subsystem", type="pci", managed="yes")
        src = ET.SubElement(hd, "source")
        dmn, bus, slot, func = parse_pci(addr)
        ET.SubElement(src, "address", domain=dmn, bus=bus, slot=slot, function=func)
    if not resources.nic_vf_pci and nat_network:
        iface = ET.SubElement(dev, "interface", type="network")
        ET.SubElement(iface, "source", network=nat_network)
        ET.SubElement(iface, "model", type="virtio")
    elif not resources.nic_vf_pci and bridge:
        iface = ET.SubElement(dev, "interface", type="bridge")
        ET.SubElement(iface, "source", bridge=bridge)
        ET.SubElement(iface, "model", type="virtio")
    vs = ET.SubElement(dev, "vsock", model="virtio")
    if vsock_cid:
        ET.SubElement(vs, "cid", auto="no", address=str(vsock_cid))
    else:
        ET.SubElement(vs, "cid", auto="yes")
    rng = ET.SubElement(dev, "rng", model="virtio")
    ET.SubElement(rng, "backend", model="random").text = "/dev/urandom"
    ser = ET.SubElement(dev, "serial", type="pty")
    ET.SubElement(ser, "target", port="0")
    if console_log is not None:
        ET.SubElement(ser, "log", file=str(console_log), append="off")  # boot output survives for diagnostics (virtlogd)
    con = ET.SubElement(dev, "console", type="pty")
    ET.SubElement(con, "target", type="serial", port="0")
    ET.SubElement(dev, "video").append(ET.Element("model", type="none"))
    ET.SubElement(dev, "memballoon", model="none")
    if resources.pci_mmio64_mb:
        # OVMF sizes its 64-bit MMIO aperture for ordinary guests; eight datacenter GPUs need it widened.
        cl = ET.SubElement(dom, f"{{{QEMU_NS}}}commandline")
        ET.SubElement(cl, f"{{{QEMU_NS}}}arg", value="-fw_cfg")
        ET.SubElement(cl, f"{{{QEMU_NS}}}arg", value=f"name=opt/ovmf/X-PciMmio64Mb,string={resources.pci_mmio64_mb}")
    ET.indent(dom)
    return ET.tostring(dom, encoding="unicode")


def passthrough_addresses(resources: VmResources) -> list[str]:
    """Every PCI function the VM receives, in a stable order."""
    out: list[str] = []
    for addr in list(resources.gpu_pci) + list(resources.extra_pci) + list(resources.nvswitch_pci) + list(resources.nvme_pci) + ([resources.nic_vf_pci] if resources.nic_vf_pci else []):
        if addr and addr not in out:
            out.append(addr)
    return out
