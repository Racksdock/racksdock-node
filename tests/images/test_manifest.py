import json

import pytest
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.manifest import load_signed_manifest, verify_image_file
from typer.testing import CliRunner

from racksdock_node.images.cli import app

runner = CliRunner()


def test_manifest_build_sign_verify(tmp_path):
    imgs = tmp_path / "imgs"
    imgs.mkdir()
    for name in ("ubuntu24.04_v2026.10.1.qcow2", "ubuntu24.04_cuda13.x_v2026.10.1.qcow2", "verify_v2026.10.1.qcow2", "racksdock-host_v2026.10.1.raw", "README.txt"):
        (imgs / name).write_bytes(name.encode() * 100)
    r = runner.invoke(app, ["manifest", "build", str(imgs), "v2026.10.1", "--out", str(tmp_path / "m.json"), "--driver", "595.58.03", "--cuda", "13.0", "--node-agent", "0.1.0"])
    assert r.exit_code == 0, r.output
    m = json.loads((tmp_path / "m.json").read_text())
    assert len(m["images"]) == 3 and m["host_image"]["os"] == "racksdock-host"
    cuda = [i for i in m["images"] if "cuda" in i["os"]][0]
    assert cuda["cuda"] == "13.0" and [i for i in m["images"] if i["os"] == "ubuntu24.04"][0]["cuda"] == ""
    assert [i for i in m["images"] if i["os"] == "verify"][0]["validator_only"] is True
    key = tmp_path / "catalog.seed"
    r = runner.invoke(app, ["manifest", "sign", str(tmp_path / "m.json"), str(key), "--out", str(tmp_path / "signed.json")])
    assert r.exit_code == 0
    pub = SigningKey.load_or_create(key).public.hex
    r = runner.invoke(app, ["manifest", "verify", str(tmp_path / "signed.json"), pub, "--image-dir", str(imgs)])
    assert r.exit_code == 0 and r.output.count("ok ") == 4
    with pytest.raises(ValueError):
        load_signed_manifest(tmp_path / "signed.json", SigningKey.generate().public.hex)
    man = load_signed_manifest(tmp_path / "signed.json", pub)
    (imgs / "verify_v2026.10.1.qcow2").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        verify_image_file(man, imgs / "verify_v2026.10.1.qcow2", "verify", "verify_v2026.10.1")
    r = runner.invoke(app, ["manifest", "os-options", str(tmp_path / "signed.json"), pub])
    assert r.exit_code == 0 and json.loads(r.output)["images"][0]["os"].startswith("ubuntu")
