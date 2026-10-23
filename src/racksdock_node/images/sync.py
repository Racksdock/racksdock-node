"""Catalog sync for hosts (docs/01 §6): the platform publishes a release as a directory
`<catalog_url>/{manifest.signed.json, catalog.pub, <os>_<release>.qcow2, <os>_<release>.container.tar}`; a host downloads the
signed manifest, verifies the signature against its pinned catalog public key (pinned on first contact), fetches the images of the
kinds it runs that it does not have yet (hash-checked, `docker load` for container archives, image id checked) and keeps the
manifest + public key in its image directory for the host-agent. Idempotent; safe to run while rentals are up (existing files are
replaced atomically and a running VM keeps its old backing file)."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.manifest import CatalogManifest, ImageEntry

log = logging.getLogger("racksdock.images.sync")
PIN_PATH = Path("/etc/racksdock/catalog.pub")


def _get(url: str, timeout: float = 60) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _download(url: str, dest: Path, expect_sha256: str | None, progress: Callable[[str], None] | None = None) -> str:
    """Stream url to dest (via dest.part), return the sha256. Raises on mismatch."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    done = 0
    with urllib.request.urlopen(url, timeout=120) as r, part.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        next_report = 0
        while True:
            chunk = r.read(8 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress and total and done >= next_report:
                progress(f"{dest.name}: {done / 2**30:.1f}/{total / 2**30:.1f} GiB")
                next_report = done + (2 << 30)
    digest = h.hexdigest()
    if expect_sha256 and digest != expect_sha256.removeprefix("sha256:"):
        part.unlink(missing_ok=True)
        raise ValueError(f"{dest.name}: digest {digest[:16]} != expected {expect_sha256[:16]}")
    os.replace(part, dest)
    return digest


def docker_image_id(tag: str) -> str:
    cp = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture_output=True, text=True, timeout=60)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def loaded_id_file(image_dir: Path, version: str) -> Path:
    """Docker image stores (classic graph driver vs containerd snapshotter) assign different image ids to the same archive.
    When a signed, hash-checked archive loads with an id other than the build host's, the id it got on THIS host is recorded
    here and accepted alongside the catalog's: the trust is in the signed archive bytes, not in the store's id."""
    return image_dir / f"{version}.container.loaded"


def container_id_ok(image_dir: Path, entry: ImageEntry, image_id: str) -> bool:
    if not image_id:
        return False
    if image_id == entry.sha256:
        return True
    f = loaded_id_file(image_dir, entry.version)
    return f.exists() and f.read_text().strip() == image_id


def local_images(image_dir: Path, kinds: tuple[str, ...] = ("vm", "container"), pub: str | None = None) -> list[str]:
    """'<version>:<kind>' for every manifest entry of the given kinds that is actually present on this host."""
    sm = image_dir / "manifest.signed.json"
    pubfile = image_dir / "catalog.pub"
    if not sm.exists() or (pub is None and not pubfile.exists()):
        return []
    try:
        env = SignedEnvelope.model_validate_json(sm.read_text())
        if not env.verify(expected_signer=pub or pubfile.read_text().strip()):
            return []
        m = CatalogManifest.model_validate(env.payload)
    except Exception:
        return []
    out = []
    for e in m.images:
        if e.kind not in kinds:
            continue
        if e.kind == "vm" and (image_dir / e.file).exists():
            out.append(f"{e.version}:vm")
        elif e.kind == "container" and shutil.which("docker") and container_id_ok(image_dir, e, docker_image_id(e.file)):
            out.append(f"{e.version}:container")
    return sorted(out)


def vm_image_path(image_dir: Path, version: str, os_name: str | None = None, pub: str | None = None) -> Path:
    """The qcow2 behind a catalog version. A patch release keeps an unchanged image under the new version with its old file
    name (`ImageEntry.file`), so the name comes from the host's verified manifest; <version>.qcow2 when there is no manifest."""
    sm = image_dir / "manifest.signed.json"
    try:
        env = SignedEnvelope.model_validate_json(sm.read_text())
        if env.verify(expected_signer=pub or (image_dir / "catalog.pub").read_text().strip()):
            for e in CatalogManifest.model_validate(env.payload).images:
                if e.kind == "vm" and e.version == version and (os_name is None or e.os == os_name):
                    return image_dir / e.file
    except Exception:
        pass
    return image_dir / f"{version}.qcow2"


def fetch_manifest(catalog_url: str, pin: Path | None = PIN_PATH) -> tuple[CatalogManifest, str, str]:
    """Download + verify the signed manifest. Returns (manifest, signed_json, pubkey_hex). The public key is pinned at `pin` on
    first contact; a later key change is refused (the operator replaces the pin deliberately)."""
    signed = _get(catalog_url.rstrip("/") + "/manifest.signed.json").decode()
    pub = _get(catalog_url.rstrip("/") + "/catalog.pub").decode().strip()
    if pin is not None:
        if pin.exists():
            pinned = pin.read_text().strip()
            if pinned != pub:
                raise ValueError(f"catalog public key changed ({pub[:12]} != pinned {pinned[:12]}); refusing. Replace {pin} to accept the new key")
        else:
            pin.parent.mkdir(parents=True, exist_ok=True)
            pin.write_text(pub + "\n")
    env = SignedEnvelope.model_validate_json(signed)
    if not env.verify(expected_signer=pub) or env.kind != "catalog_manifest":
        raise ValueError("catalog manifest signature invalid")
    return CatalogManifest.model_validate(env.payload), signed, pub


def sync_catalog(catalog_url: str, image_dir: Path, kinds: tuple[str, ...] = ("vm", "container"), pin: Path | None = PIN_PATH,
                 progress: Callable[[str], None] | None = None) -> dict:
    """Bring image_dir up to date with the platform's catalog for the given kinds. Returns a summary dict."""
    progress = progress or (lambda s: log.info("%s", s))
    m, signed, pub = fetch_manifest(catalog_url, pin)
    image_dir.mkdir(parents=True, exist_ok=True)
    fetched, present, skipped = [], [], []
    # the validators' verify image first: it is what the bootstrap and every verification session need, so a host becomes
    # testable as early as possible while the tenant templates are still downloading
    for e in sorted(m.images, key=lambda x: (not x.validator_only, x.os)):
        if e.kind not in kinds:
            skipped.append(e.version)
            continue
        if e.kind == "vm":
            dest = image_dir / e.file
            if dest.exists() and _verified(dest, e.sha256, e.size_bytes):
                present.append(f"{e.version}:vm")
                continue
            progress(f"downloading {e.file} ({e.size_bytes / 2**30:.1f} GiB)")
            _download(catalog_url.rstrip("/") + "/" + e.file, dest, e.sha256, progress)
            (image_dir / (e.file + ".sha256")).write_text(f"{e.sha256}  {e.file} {dest.stat().st_size}\n")
            fetched.append(f"{e.version}:vm")
        else:
            if container_id_ok(image_dir, e, docker_image_id(e.file)):
                present.append(f"{e.version}:container")
                continue
            if not e.archive:
                skipped.append(e.version)
                progress(f"{e.file}: no archive in the catalog; cannot fetch")
                continue
            with tempfile.TemporaryDirectory(dir=str(image_dir)) as td:
                tar = Path(td) / e.archive
                progress(f"downloading {e.archive} ({e.size_bytes / 2**30:.1f} GiB)")
                _download(catalog_url.rstrip("/") + "/" + e.archive, tar, e.archive_sha256 or None, progress)
                progress(f"docker load {e.archive}")
                cp = subprocess.run(["docker", "load", "-i", str(tar)], capture_output=True, text=True, timeout=3600)
                if cp.returncode != 0:
                    raise RuntimeError(f"docker load {e.archive} failed: {cp.stderr.strip()[-300:]}")
            got = docker_image_id(e.file)
            if got != e.sha256:
                if not e.archive_sha256 or not got:
                    raise ValueError(f"{e.file}: loaded image id {got[:19]} != catalog {e.sha256[:19]} and the archive is not hash-pinned")
                # the archive bytes were verified against the signed manifest; the id difference comes from the docker store
                loaded_id_file(image_dir, e.version).write_text(got + "\n")
                progress(f"{e.file}: this docker store reports id {got[:19]} for the verified archive (catalog {e.sha256[:19]}); recorded")
            fetched.append(f"{e.version}:container")
    # the manifest + key last, so a half-finished sync never advertises images it does not have
    (image_dir / "manifest.signed.json").write_text(signed)
    (image_dir / "catalog.pub").write_text(pub + "\n")
    return {"release": m.release, "fetched": fetched, "present": present, "skipped": skipped}


def _verified(path: Path, expect_sha256: str, size_bytes: int) -> bool:
    """True when the file matches the manifest. The digest of a multi-GiB image is cached in `<file>.sha256` next to it (digest
    + size), so the periodic sync hashes each image once, not every ten minutes."""
    side = path.with_name(path.name + ".sha256")
    try:
        if size_bytes and path.stat().st_size != size_bytes:
            return False
        if side.exists():
            tok = side.read_text().split()
            if tok and tok[0] == expect_sha256 and (len(tok) < 3 or tok[2] == str(path.stat().st_size)):
                return True
    except OSError:
        return False
    ok = _sha256(path) == expect_sha256
    if ok:
        side.write_text(f"{expect_sha256}  {path.name} {path.stat().st_size}\n")
    return ok


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_container_images(entries: list[ImageEntry], out_dir: Path, progress: Callable[[str], None] | None = None) -> list[ImageEntry]:
    """`docker save` each container entry to out_dir/<version>.container.tar (reused when the image id is unchanged) and fill
    archive/archive_sha256. Used by the release build on the build host."""
    progress = progress or (lambda s: log.info("%s", s))
    out = []
    for e in entries:
        if e.kind != "container":
            out.append(e)
            continue
        tar = out_dir / f"{e.version}.container.tar"
        idfile = out_dir / f"{e.version}.container.id"
        shafile = out_dir / f"{e.version}.container.sha256"
        if tar.exists() and idfile.exists() and idfile.read_text().strip() == e.sha256 and shafile.exists():
            digest = shafile.read_text().split()[0]
        else:
            progress(f"docker save {e.file} -> {tar.name}")
            with tar.open("wb") as f:
                subprocess.run(["docker", "save", e.file], stdout=f, check=True, timeout=7200)
            digest = _sha256(tar)
            idfile.write_text(e.sha256 + "\n")
            shafile.write_text(f"{digest}  {tar.name}\n")
        out.append(e.model_copy(update={"archive": tar.name, "archive_sha256": digest}))
    return out
