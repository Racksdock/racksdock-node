"""Catalog sync: a signed release served over HTTP is fetched, verified against a pinned key, and images land in the image dir."""
import hashlib
import http.server
import json
import threading

import pytest
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.manifest import CatalogManifest, ImageEntry, sign_manifest, template_meta

from racksdock_node.images import sync as sy


@pytest.fixture
def store(tmp_path):
    """A release directory with one small 'qcow2' and one container entry whose archive we never load (no docker in tests)."""
    rel = tmp_path / "store" / "v2026.10.9"
    rel.mkdir(parents=True)
    img = rel / "ubuntu24.04_v2026.10.9.qcow2"
    img.write_bytes(b"not really a disk image" * 1000)
    digest = hashlib.sha256(img.read_bytes()).hexdigest()
    entries = [ImageEntry(os="ubuntu24.04", version="ubuntu24.04_v2026.10.9", file=img.name, sha256=digest, size_bytes=img.stat().st_size, kind="vm", **template_meta("ubuntu24.04")),
               ImageEntry(os="pytorch-2.9-cuda13.0", version="pytorch-2.9-cuda13.0_v2026.10.9", file="racksdock/pytorch-2.9-cuda13.0:v2026.10.9", sha256="sha256:" + "a" * 64,
                          size_bytes=5, kind="container", archive="pytorch-2.9-cuda13.0_v2026.10.9.container.tar", archive_sha256="b" * 64, **template_meta("pytorch-2.9-cuda13.0"))]
    key = SigningKey.generate()
    env = sign_manifest(CatalogManifest(release="v2026.10.9", images=entries), key)
    (rel / "manifest.signed.json").write_text(env.model_dump_json())
    (rel / "catalog.pub").write_text(key.public.hex + "\n")
    (tmp_path / "store" / "current").symlink_to("v2026.10.9")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp_path / "store"), **k)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/current", key, rel
    srv.shutdown()


def test_fetch_pins_the_key_and_downloads_vm_images(store, tmp_path, monkeypatch):
    url, key, rel = store
    pin = tmp_path / "pin" / "catalog.pub"
    dest = tmp_path / "images"
    r = sy.sync_catalog(url, dest, kinds=("vm",), pin=pin, progress=lambda s: None)
    assert r["release"] == "v2026.10.9" and r["fetched"] == ["ubuntu24.04_v2026.10.9:vm"] and r["skipped"] == ["pytorch-2.9-cuda13.0_v2026.10.9"]
    assert pin.read_text().strip() == key.public.hex
    assert (dest / "ubuntu24.04_v2026.10.9.qcow2").read_bytes() == (rel / "ubuntu24.04_v2026.10.9.qcow2").read_bytes()
    assert (dest / "manifest.signed.json").exists() and (dest / "catalog.pub").read_text().strip() == key.public.hex
    # second run: nothing to fetch; inventory lists the vm image (docker is not consulted for vm kinds)
    r2 = sy.sync_catalog(url, dest, kinds=("vm",), pin=pin, progress=lambda s: None)
    assert r2["fetched"] == [] and r2["present"] == ["ubuntu24.04_v2026.10.9:vm"]
    assert sy.local_images(dest, ("vm",)) == ["ubuntu24.04_v2026.10.9:vm"]
    # a different signing key is refused once pinned
    other = SigningKey.generate()
    (rel / "catalog.pub").write_text(other.public.hex + "\n")
    with pytest.raises(ValueError, match="catalog public key changed"):
        sy.sync_catalog(url, dest, kinds=("vm",), pin=pin, progress=lambda s: None)


def test_corrupt_download_is_rejected(store, tmp_path):
    url, key, rel = store
    (rel / "ubuntu24.04_v2026.10.9.qcow2").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest"):
        sy.sync_catalog(url, tmp_path / "images2", kinds=("vm",), pin=tmp_path / "p" / "catalog.pub", progress=lambda s: None)
    assert not (tmp_path / "images2" / "manifest.signed.json").exists()  # a failed sync advertises nothing


def test_container_inventory_uses_docker_image_ids(store, tmp_path, monkeypatch):
    url, key, rel = store
    dest = tmp_path / "images3"
    dest.mkdir()
    (dest / "manifest.signed.json").write_text((rel / "manifest.signed.json").read_text())
    (dest / "catalog.pub").write_text(key.public.hex + "\n")
    monkeypatch.setattr(sy.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(sy, "docker_image_id", lambda tag: "sha256:" + "a" * 64 if "pytorch" in tag else "")
    assert sy.local_images(dest) == ["pytorch-2.9-cuda13.0_v2026.10.9:container"]
    monkeypatch.setattr(sy, "docker_image_id", lambda tag: "sha256:" + "f" * 64)
    assert sy.local_images(dest) == []


def test_host_agent_image_kinds_and_catalog_url():
    from racksdock_node.host_agent.config import HostAgentConfig

    cfg = HostAgentConfig(mode="dual", platform_ws="ws://62.83.43.83:8000/v1/hosts/ws")
    assert cfg.image_kinds == ("vm", "container") and cfg.effective_catalog_url == "http://62.83.43.83:8000/catalog/current"
    assert HostAgentConfig(mode="docker").image_kinds == ("container",) and HostAgentConfig(mode="real").image_kinds == ("vm",)
    assert HostAgentConfig(mode="real", catalog_url="").effective_catalog_url is None
    assert HostAgentConfig(mode="real", platform_ws="wss://p.example/v1/hosts/ws").effective_catalog_url == "https://p.example/catalog/current"


def test_template_meta_and_manifest_json_roundtrip():
    e = ImageEntry(os="pytorch-2.9-cuda13.0", version="pytorch-2.9-cuda13.0_v1", file="x.qcow2", sha256="0" * 64, size_bytes=1, **template_meta("pytorch-2.9-cuda13.0"))
    d = json.loads(e.model_dump_json())
    assert d["label"] == "PyTorch 2.9, CUDA 13.0, Ubuntu 24.04" and d["cuda"] == "13.0" and d["pytorch"] == "2.9" and d["archive"] == ""


def test_vm_image_digest_is_cached_in_a_sidecar(store, tmp_path, monkeypatch):
    """After one verification the periodic sync trusts the sidecar (digest + size) instead of re-hashing multi-GiB images."""
    url, key, rel = store
    dest = tmp_path / "images4"
    sy.sync_catalog(url, dest, kinds=("vm",), pin=tmp_path / "p4" / "catalog.pub", progress=lambda s: None)
    side = dest / "ubuntu24.04_v2026.10.9.qcow2.sha256"
    assert side.exists() and len(side.read_text().split()) == 3
    calls = []
    orig = sy._sha256
    monkeypatch.setattr(sy, "_sha256", lambda p: (calls.append(p), orig(p))[1])
    r = sy.sync_catalog(url, dest, kinds=("vm",), pin=tmp_path / "p4" / "catalog.pub", progress=lambda s: None)
    assert r["present"] == ["ubuntu24.04_v2026.10.9:vm"] and calls == []  # no hashing
    (dest / "ubuntu24.04_v2026.10.9.qcow2").write_bytes(b"x")  # size changed: sidecar no longer trusted, image re-fetched
    r = sy.sync_catalog(url, dest, kinds=("vm",), pin=tmp_path / "p4" / "catalog.pub", progress=lambda s: None)
    assert r["fetched"] == ["ubuntu24.04_v2026.10.9:vm"]


def test_store_specific_image_id_is_accepted_for_a_verified_archive(store, tmp_path, monkeypatch):
    """docker load may yield a different image id on another storage driver; after a hash-checked archive the id this host got
    is recorded and counts as present (without the archive hash it would be refused)."""
    import hashlib

    url, key, rel = store
    tar = rel / "pytorch-2.9-cuda13.0_v2026.10.9.container.tar"
    tar.write_bytes(b"fake archive")
    # re-sign the manifest with the real archive digest
    from racksdock_subnet.core.envelope import SignedEnvelope
    from racksdock_subnet.core.manifest import CatalogManifest, sign_manifest

    env = SignedEnvelope.model_validate_json((rel / "manifest.signed.json").read_text())
    m = CatalogManifest.model_validate(env.payload)
    for e in m.images:
        if e.kind == "container":
            e.archive_sha256 = hashlib.sha256(b"fake archive").hexdigest()
    (rel / "manifest.signed.json").write_text(sign_manifest(m, key).model_dump_json())
    monkeypatch.setattr(sy.shutil, "which", lambda name: "/usr/bin/docker")
    ids = {"racksdock/pytorch-2.9-cuda13.0:v2026.10.9": ""}
    monkeypatch.setattr(sy, "docker_image_id", lambda tag: ids.get(tag, ""))

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "load"]:
            ids["racksdock/pytorch-2.9-cuda13.0:v2026.10.9"] = "sha256:" + "e" * 64  # store-specific id
            return subprocess.CompletedProcess(cmd, 0, "Loaded image\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    import subprocess

    monkeypatch.setattr(sy.subprocess, "run", fake_run)
    dest = tmp_path / "images5"
    r = sy.sync_catalog(url, dest, kinds=("container",), pin=tmp_path / "p5" / "catalog.pub", progress=lambda s: None)
    assert r["fetched"] == ["pytorch-2.9-cuda13.0_v2026.10.9:container"]
    assert sy.loaded_id_file(dest, "pytorch-2.9-cuda13.0_v2026.10.9").read_text().strip() == "sha256:" + "e" * 64
    assert sy.local_images(dest, ("container",)) == ["pytorch-2.9-cuda13.0_v2026.10.9:container"]
    entry = [e for e in m.images if e.kind == "container"][0]
    assert sy.container_id_ok(dest, entry, "sha256:" + "e" * 64) and not sy.container_id_ok(dest, entry, "sha256:" + "f" * 64)


def test_vm_image_path_follows_the_manifest_file_name(tmp_path):
    """A patch release keeps an unchanged qcow2 under the new version with its old file name (publish-patch-release.sh); the
    host-agent must open that file, not <version>.qcow2 (seen: every verify launch failed with 'verify_v2026.10.2.qcow2 not found')."""
    img_dir = tmp_path / "images"
    img_dir.mkdir()
    entries = [ImageEntry(os="verify", version="verify_v2026.10.2", file="verify_v2026.10.1.qcow2", sha256="c" * 64, size_bytes=1, kind="vm", validator_only=True),
               ImageEntry(os="verify", version="verify_v2026.10.2", file="racksdock/verify:v2026.10.2", sha256="sha256:" + "d" * 64, size_bytes=1, kind="container")]
    key = SigningKey.generate()
    (img_dir / "manifest.signed.json").write_text(sign_manifest(CatalogManifest(release="v2026.10.2", images=entries), key).model_dump_json())
    (img_dir / "catalog.pub").write_text(key.public.hex + "\n")
    assert sy.vm_image_path(img_dir, "verify_v2026.10.2", "verify") == img_dir / "verify_v2026.10.1.qcow2"
    assert sy.vm_image_path(img_dir, "ubuntu24.04_v2026.10.1", "ubuntu24.04") == img_dir / "ubuntu24.04_v2026.10.1.qcow2"  # not in the manifest: the plain name
    (img_dir / "catalog.pub").write_text(SigningKey.generate().public.hex + "\n")  # a manifest that does not verify is not trusted for file names
    assert sy.vm_image_path(img_dir, "verify_v2026.10.2", "verify") == img_dir / "verify_v2026.10.2.qcow2"
    assert sy.vm_image_path(tmp_path / "nowhere", "verify_v1") == tmp_path / "nowhere" / "verify_v1.qcow2"
