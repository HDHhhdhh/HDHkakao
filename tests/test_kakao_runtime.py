import hashlib
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from kakao_runtime import ACTIVE_RUNTIME_MANIFEST, RuntimeValidationError, VerifiedSpoilerRuntime, parse_runtime_manifest
from kakao_tablet_reauth import SpoilerTabletAuthenticator, discover_spoiler_auth_runtime
from kakao_direct import DirectBridgeError


def published(root: Path, suffix=b"first"):
    payloads = (("node.exe", "Spoiler.Runtime.Node", b"node" + suffix),
                ("spoiler-bridge.mjs", "Spoiler.Runtime.Bridge.Listener", b"bridge" + suffix),
                ("spoiler-auth.mjs", "Spoiler.Runtime.Bridge.Auth", b"auth" + suffix))
    lines = [f"{filename}|{resource}|{len(data)}|{hashlib.sha256(data).hexdigest()}"
             for filename, resource, data in payloads]
    seed = ("\n".join(lines) + "\n").encode()
    version = "v1-" + hashlib.sha256(seed).hexdigest()[:16]
    directory = root / version
    directory.mkdir(parents=True)
    for filename, _, data in payloads:
        (directory / filename).write_bytes(data)
    manifest = ("SpoilerRuntimeManifest/1\nversion=" + version + "\n" + "\n".join(lines) + "\n").encode()
    (root / ACTIVE_RUNTIME_MANIFEST).write_bytes(manifest)
    return directory, manifest


def test_publication_selects_coherent_runtime_regardless_of_modification_time(tmp_path):
    root = tmp_path / "runtime"
    older, first = published(root)
    newer, _ = published(root, b"new")
    os.utime(older, (2147483000, 2147483000))
    assert discover_spoiler_auth_runtime(root) == (newer / "node.exe", newer / "spoiler-auth.mjs")
    # An explicit publication switch is respected without heuristics.
    (root / ACTIVE_RUNTIME_MANIFEST).write_bytes(first)
    assert discover_spoiler_auth_runtime(root)[0].parent == older


def test_unpublished_cache_is_not_used_and_child_is_not_started(tmp_path):
    root = tmp_path / "runtime"
    published(root)
    (root / ACTIVE_RUNTIME_MANIFEST).unlink()
    with pytest.raises(DirectBridgeError) as error:
        discover_spoiler_auth_runtime(root)
    assert error.value.code == "spoiler_auth_missing"
    spawn = Mock()
    with pytest.raises(DirectBridgeError):
        SpoilerTabletAuthenticator(runtime_root=root, popen_factory=spawn).authenticate(
            {"email": "fixture@example.com", "password": "synthetic"})
    spawn.assert_not_called()


@pytest.mark.parametrize("filename", ["node.exe", "spoiler-bridge.mjs", "spoiler-auth.mjs"])
@pytest.mark.parametrize("mode", ["changed", "truncated", "missing"])
def test_any_payload_tampering_or_partial_extraction_rejects_entire_runtime(tmp_path, filename, mode):
    directory, _ = published(tmp_path / "runtime")
    file = directory / filename
    if mode == "missing":
        file.unlink()
    elif mode == "truncated":
        file.write_bytes(b"x")
    else:
        file.write_bytes(bytes(value ^ 1 for value in file.read_bytes()))
    with pytest.raises(RuntimeValidationError):
        VerifiedSpoilerRuntime(directory.parent)


@pytest.mark.parametrize("corrupt", [
    lambda raw: raw.replace(b"\n", b"\r\n"),
    lambda raw: b"\xef\xbb\xbf" + raw,
    lambda raw: raw.replace(b"node.exe|", b"../node.exe|"),
    lambda raw: raw.replace(b"version=v1-", b"version=../v1-"),
    lambda raw: raw[:-1],
    lambda raw: raw + b"unexpected\n",
    lambda raw: raw.replace(b"|9|", b"|09|", 1),
    lambda raw: b"x" * 4097,
])
def test_noncanonical_or_traversing_manifest_is_rejected(tmp_path, corrupt):
    _, manifest = published(tmp_path / "runtime")
    with pytest.raises(RuntimeValidationError):
        parse_runtime_manifest(corrupt(manifest))


def test_unexpected_files_are_rejected(tmp_path):
    directory, _ = published(tmp_path / "runtime")
    (directory / "unexpected.mjs").write_text("// fixture", encoding="utf-8")
    with pytest.raises(RuntimeValidationError):
        VerifiedSpoilerRuntime(directory.parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime execution lease")
def test_verified_runtime_cannot_be_changed_until_lease_closes(tmp_path):
    directory, _ = published(tmp_path / "runtime")
    with VerifiedSpoilerRuntime(directory.parent):
        with pytest.raises(OSError):
            (directory / "spoiler-auth.mjs").write_bytes(b"modified")
        with pytest.raises(OSError):
            (directory / "node.exe").unlink()
    (directory / "spoiler-auth.mjs").write_bytes(b"modified")
