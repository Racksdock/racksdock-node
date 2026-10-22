from __future__ import annotations

import json
import re
from pathlib import Path

import typer
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.manifest import CatalogManifest, ImageEntry, load_signed_manifest, sha256_file, sign_manifest, template_meta, verify_image_file

app = typer.Typer(help="racksdock image catalog tools")
man = typer.Typer(help="catalog manifest")
app.add_typer(man, name="manifest")

NAME_RE = re.compile(r"^(?P<os>[a-z0-9.-]+(?:_cuda[0-9.x]+)?|verify|racksdock-host)_(?P<version>v\d{4}\.\d{2}\.\d+)\.(?:qcow2|raw)$")


def container_entries(release: str, repo: str = "racksdock") -> list[ImageEntry]:
    """Catalog entries for the docker images `repo/<os>:<release>` present on this host (container tier)."""
    import json as _json
    import subprocess

    out = subprocess.run(["docker", "images", "--format", "{{json .}}", f"{repo}/*:{release}"], capture_output=True, text=True, timeout=60).stdout
    entries = []
    for line in out.splitlines():
        d = _json.loads(line)
        os_name = d["Repository"].split("/", 1)[1]
        ref = f"{d['Repository']}:{d['Tag']}"
        image_id = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], capture_output=True, text=True, timeout=30).stdout.strip()
        size = subprocess.run(["docker", "image", "inspect", "--format", "{{.Size}}", ref], capture_output=True, text=True, timeout=30).stdout.strip()
        entries.append(ImageEntry(os=os_name, version=f"{os_name}_{release}", file=ref, sha256=image_id, size_bytes=int(size or 0), kind="container",
                                  validator_only=(os_name == "verify"), **template_meta(os_name)))
    return sorted(entries, key=lambda e: e.os)


@man.command("build")
def build(image_dir: Path, release: str, out: Path = Path("manifest.json"), driver: str = "", fabric_manager: str = "", cuda: str = "", node_agent: str = "",
          containers: bool = typer.Option(False, "--containers", help="also list the docker images racksdock/<os>:<release> on this host (container tier)"),
          container_repo: str = "racksdock",
          archive: bool = typer.Option(False, "--archive", help="docker-save the container images into image_dir so the platform store can serve them")):
    from .sync import archive_container_images

    images, host = [], None
    if containers:
        images += container_entries(release, container_repo)
        if archive:
            images = archive_container_images(images, image_dir, progress=typer.echo)
    for f in sorted(image_dir.iterdir()):
        m = NAME_RE.match(f.name)
        if not m:
            continue
        version = f"{m['os']}_{m['version']}"
        meta = template_meta(m["os"])
        entry = ImageEntry(os=m["os"], version=version, file=f.name, sha256=sha256_file(f), size_bytes=f.stat().st_size, driver=driver,
                           fabric_manager=fabric_manager or driver, cuda=meta["cuda"] or (cuda if "cuda" in m["os"] else ""), node_agent=node_agent,
                           validator_only=(m["os"] == "verify"), label=meta["label"], base_os=meta["base_os"], pytorch=meta["pytorch"])
        if m["os"] == "racksdock-host":
            host = entry
        else:
            images.append(entry)
    manifest = CatalogManifest(release=release, images=images, host_image=host)
    out.write_text(manifest.model_dump_json(by_alias=True, indent=1))
    typer.echo(f"wrote {out} with {len(images)} images" + (" + host image" if host else "") + (f" ({sum(1 for e in images if e.kind == 'container')} container)" if containers else ""))


@man.command("sign")
def sign(manifest: Path, key: Path, out: Path = Path("manifest.signed.json"), pub_out: Path | None = None):
    """Sign a manifest with the catalog key (created if missing). --pub-out writes the public key (hex) the host-agent needs as
    catalog.pub next to the images."""
    m = CatalogManifest.model_validate_json(manifest.read_text())
    sk = SigningKey.load_or_create(key)
    env = sign_manifest(m, sk)
    out.write_text(env.model_dump_json(indent=1))
    if pub_out is not None:
        pub_out.write_text(sk.public.hex + "\n")
    typer.echo(f"signed by {env.signer}")


@man.command("verify")
def verify(signed_manifest: Path, catalog_pubkey: str, image_dir: Path | None = None):
    m = load_signed_manifest(signed_manifest, catalog_pubkey)
    typer.echo(f"manifest release {m.release}: signature ok")
    if image_dir:
        for e in m.images + ([m.host_image] if m.host_image else []):
            p = image_dir / e.file
            if e.kind == "container":
                typer.echo(f"  container {e.file} = {e.sha256[:19]} (checked by the host-agent against `docker image inspect`)")
                continue
            if not p.exists():
                typer.echo(f"  missing {e.file}")
                continue
            verify_image_file(m, p, e.os, e.version)
            typer.echo(f"  ok {e.file}")


@app.command("fetch")
def fetch(catalog_url: str, dest: Path = Path("/var/lib/racksdock/images"), kinds: str = "vm,container",
          pin: Path = Path("/etc/racksdock/catalog.pub"), no_pin: bool = typer.Option(False, "--no-pin", help="do not pin/check the catalog key (tests only)")):
    """Download the platform's signed catalog release into dest: images of the given kinds this host lacks, hash-checked
    (container archives are docker-loaded). The catalog public key is pinned at --pin on first contact."""
    from .sync import sync_catalog

    r = sync_catalog(catalog_url, dest, tuple(k.strip() for k in kinds.split(",") if k.strip()), None if no_pin else pin, progress=typer.echo)
    typer.echo(f"release {r['release']}: fetched {len(r['fetched'])}, present {len(r['present'])}, skipped {len(r['skipped'])}")


@app.command("present")
def present(image_dir: Path = Path("/var/lib/racksdock/images"), kinds: str = "vm,container"):
    """List the catalog images actually present on this host ('<version>:<kind>')."""
    from .sync import local_images

    for i in local_images(image_dir, tuple(k.strip() for k in kinds.split(",") if k.strip())):
        typer.echo(i)


@man.command("os-options")
def os_options(signed_manifest: Path, catalog_pubkey: str):
    """Emit the marketplace catalog.json from a signed manifest."""
    m = load_signed_manifest(signed_manifest, catalog_pubkey)
    seen: set[str] = set()
    imgs = []
    for e in m.images:  # a VM and a container build of the same os/version are one catalog entry; each host serves the kind it runs
        if e.version in seen:
            continue
        seen.add(e.version)
        imgs.append({"os": e.os, "version": e.version, "description": f"{e.os} driver {e.driver}" + (f" CUDA {e.cuda}" if e.cuda else ""),
                     "label": e.label or template_meta(e.os)["label"], **({"validator_only": True} if e.validator_only else {})})
    cat = {"schema": "racksdock.catalog.v1", "release": m.release, "default_os": "ubuntu24.04", "images": imgs}
    typer.echo(json.dumps(cat, indent=1))
