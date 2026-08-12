"""Security contract for Codex-generated image paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from hamroh.storage.generated_image_store import (
    GeneratedImagePathError,
    GeneratedImageStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> GeneratedImageStore:
    root = tmp_path / "codex" / "generated_images"
    root.mkdir(parents=True)
    return GeneratedImageStore(root)


def test_resolves_nested_image_gen_output(store: GeneratedImageStore) -> None:
    image = store.root / "thread-id" / "generated.png"
    image.parent.mkdir()
    content = b"\x89PNG\r\n\x1a\nimage"
    image.write_bytes(content)

    result = store.read(str(image))

    assert result.filename == "generated.png"
    assert result.content == content
    assert result.size_bytes == len(content)


def test_rejects_relative_path(store: GeneratedImageStore) -> None:
    with pytest.raises(GeneratedImagePathError, match="absolute path"):
        store.read("thread-id/generated.png")


def test_rejects_codex_auth_sibling(store: GeneratedImageStore) -> None:
    auth = store.root.parent / "auth.json"
    auth.write_text("secret", encoding="utf-8")

    with pytest.raises(GeneratedImagePathError, match="must be under"):
        store.read(str(auth))


def test_rejects_traversal_out_of_generated_root(store: GeneratedImageStore) -> None:
    hostile = f"{store.root}/thread-id/../../auth.json"

    with pytest.raises(GeneratedImagePathError, match="may not contain"):
        store.read(hostile)


def test_rejects_symlink_to_file_outside_root(store: GeneratedImageStore) -> None:
    secret = store.root.parent / "secret.png"
    secret.write_bytes(b"secret")
    link = store.root / "thread-id" / "generated.png"
    link.parent.mkdir()
    link.symlink_to(secret)

    with pytest.raises(GeneratedImagePathError, match="symlink"):
        store.read(str(link))


def test_rejects_symlinked_generated_images_root(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    auth = codex_home / "auth.json"
    auth.write_text("secret", encoding="utf-8")
    root = codex_home / "generated_images"
    root.symlink_to(codex_home, target_is_directory=True)
    store = GeneratedImageStore(root)

    # The allowlist stays lexical; construction must not resolve the symlink
    # and silently broaden it to all of CODEX_HOME.
    assert store.root == root
    with pytest.raises(GeneratedImagePathError, match="root is unavailable or unsafe"):
        store.read(str(root / "auth.json"))
    with pytest.raises(GeneratedImagePathError, match="must be under"):
        store.read(str(auth))


def test_rejects_symlinked_root_ancestor(tmp_path: Path) -> None:
    actual_home = tmp_path / "actual-codex"
    root = actual_home / "generated_images"
    root.mkdir(parents=True)
    linked_home = tmp_path / "linked-codex"
    linked_home.symlink_to(actual_home, target_is_directory=True)
    store = GeneratedImageStore(linked_home / "generated_images")
    image = root / "generated.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    with pytest.raises(GeneratedImagePathError, match="root is unavailable or unsafe"):
        store.read(str(linked_home / "generated_images" / image.name))


def test_rejects_symlinked_intermediate_directory(store: GeneratedImageStore) -> None:
    outside = store.root.parent / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"\x89PNG\r\n\x1a\nsecret")
    (store.root / "thread-id").symlink_to(outside, target_is_directory=True)

    with pytest.raises(GeneratedImagePathError, match="symlink"):
        store.read(str(store.root / "thread-id" / "secret.png"))


def test_rejects_hard_link_to_file_outside_root(store: GeneratedImageStore) -> None:
    secret = store.root.parent / "secret.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\nsecret")
    linked = store.root / "generated.png"
    linked.hardlink_to(secret)

    with pytest.raises(GeneratedImagePathError, match="hard-linked"):
        store.read(str(linked))


def test_rejects_non_image_file(store: GeneratedImageStore) -> None:
    payload = store.root / "metadata.json"
    payload.write_text('{"not":"an image"}', encoding="utf-8")

    with pytest.raises(GeneratedImagePathError, match="supported PNG or JPEG"):
        store.read(str(payload))


def test_rejects_oversized_file(tmp_path: Path) -> None:
    root = tmp_path / "generated_images"
    root.mkdir()
    store = GeneratedImageStore(root, max_bytes=8)
    image = root / "large.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nextra")

    with pytest.raises(GeneratedImagePathError, match="exceeds 8 bytes"):
        store.read(str(image))


@pytest.mark.parametrize(
    "name, content",
    [
        ("photo.jpg", b"\xff\xd8\xff\xe0jpeg"),
    ],
)
def test_accepts_supported_raster_signatures(
    store: GeneratedImageStore,
    name: str,
    content: bytes,
) -> None:
    image = store.root / name
    image.write_bytes(content)

    assert store.read(str(image)).content == content


@pytest.mark.parametrize(
    "name, content",
    [
        ("animation.gif", b"GIF89aimage"),
        ("photo.webp", b"RIFF\x04\x00\x00\x00WEBPdata"),
    ],
)
def test_rejects_non_photo_raster_formats(
    store: GeneratedImageStore,
    name: str,
    content: bytes,
) -> None:
    image = store.root / name
    image.write_bytes(content)

    with pytest.raises(GeneratedImagePathError, match="supported PNG or JPEG"):
        store.read(str(image))
