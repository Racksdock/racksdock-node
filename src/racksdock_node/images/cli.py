from __future__ import annotations

import json
import re
from pathlib import Path

import typer
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.manifest import CatalogManifest, ImageEntry, load_signed_manifest, sha256_file, sign_manifest, verify_image_file

app = typer.Typer(help="racksdock image catalog tools")
man = typer.Typer(help="catalog manifest")
app.add_typer(man, name="manifest")

NAME_RE = re.compile(r"^(?P<os>[a-z0-9.]+(?:_cuda[0-9.x]+)?|verify|racksdock-host)_(?P<version>v\d{4}\.\d{2}\.\d+)\.(?:qcow2|raw)$")


@man.command("build")
def build(image_dir: Path, release: str, out: Path = Path("manifest.json"), driver: str = "", fabric_manager: str = "", cuda: str = "", node_agent: str = ""):
    images, host = [], None
    for f in sorted(image_dir.iterdir()):
        m = NAME_RE.match(f.name)
        if not m:
            continue
        version = f"{m['os']}_{m['version']}"
        entry = ImageEntry(os=m["os"], version=version, file=f.name, sha256=sha256_file(f), size_bytes=f.stat().st_size, driver=driver,
                           fabric_manager=fabric_manager or driver, cuda=cuda if "cuda" in m["os"] else "", node_agent=node_agent, validator_only=(m["os"] == "verify"))
        if m["os"] == "racksdock-host":
            host = entry
        else:
            images.append(entry)
    manifest = CatalogManifest(release=release, images=images, host_image=host)
    out.write_text(manifest.model_dump_json(by_alias=True, indent=1))
    typer.echo(f"wrote {out} with {len(images)} images" + (" + host image" if host else ""))


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
            if not p.exists():
                typer.echo(f"  missing {e.file}")
                continue
            verify_image_file(m, p, e.os, e.version)
            typer.echo(f"  ok {e.file}")


@man.command("os-options")
def os_options(signed_manifest: Path, catalog_pubkey: str):
    """Emit the marketplace catalog.json from a signed manifest."""
    m = load_signed_manifest(signed_manifest, catalog_pubkey)
    cat = {"schema": "racksdock.catalog.v1", "release": m.release, "default_os": "ubuntu24.04",
           "images": [{"os": e.os, "version": e.version, "description": f"{e.os} driver {e.driver}" + (f" CUDA {e.cuda}" if e.cuda else ""), **({"validator_only": True} if e.validator_only else {})} for e in m.images]}
    typer.echo(json.dumps(cat, indent=1))
