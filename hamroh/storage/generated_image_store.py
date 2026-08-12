"""Read-only access to raster files produced by Codex ``image_gen``.

Codex writes generated images below ``CODEX_HOME/generated_images`` and
returns their absolute paths to the model.  This store accepts only those
absolute paths and securely snapshots the file through directory file
descriptors.  No pathname is reopened after validation, so a concurrent
rename or symlink swap cannot redirect Telegram to adjacent Codex secrets.
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

# Keep a conservative cap so an unexpected output cannot consume unbounded
# memory while Hamroh prepares it for Telegram.
MAX_GENERATED_IMAGE_BYTES = 10 * 1024 * 1024


class GeneratedImagePathError(ValueError):
    """Raised when an image-gen output path fails the safety checks."""


@dataclass(frozen=True)
class GeneratedImage:
    """A validated, immutable snapshot ready for Telegram upload."""

    filename: str
    content: bytes
    size_bytes: int


def _is_supported_raster(header: bytes) -> bool:
    """Recognize the photo formats accepted from ``image_gen``."""

    return header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")


class GeneratedImageStore:
    """Securely snapshot image-gen outputs under one fixed lexical root."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = MAX_GENERATED_IMAGE_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("generated image size limit must be positive")
        # abspath is deliberately lexical: Path.resolve() would follow a
        # hostile generated_images symlink and silently broaden the allowlist.
        self._root = Path(os.path.abspath(root))
        self._max_bytes = max_bytes

    @property
    def root(self) -> Path:
        return self._root

    def read(self, absolute: str) -> GeneratedImage:
        """Return a pinned snapshot of one absolute image-gen output path."""

        parts = self._relative_parts(absolute)
        root_fd = self._open_root()
        try:
            return self._read_from_root(root_fd, parts, absolute)
        finally:
            os.close(root_fd)

    def _relative_parts(self, absolute: str) -> tuple[str, ...]:
        if not absolute:
            raise GeneratedImagePathError(
                "generated image path must be a non-empty string"
            )
        candidate = Path(absolute)
        if not candidate.is_absolute():
            raise GeneratedImagePathError(
                "generated image path must be the absolute path returned by image_gen"
            )
        try:
            relative = candidate.relative_to(self._root)
        except ValueError as exc:
            raise GeneratedImagePathError(
                f"generated image path must be under {self._root}, got {absolute!r}"
            ) from exc
        parts = relative.parts
        if not parts:
            raise GeneratedImagePathError("generated image path must name a file")
        if any(part in {"", ".", ".."} for part in parts):
            raise GeneratedImagePathError(
                f"generated image path may not contain '.' or '..': {absolute!r}"
            )
        return parts

    @staticmethod
    def _directory_flags() -> int:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def _open_root(self) -> int:
        # O_NOFOLLOW protects only the final component of one os.open call.
        # Walk from / with pinned directory descriptors so a symlink in any
        # root component is rejected and concurrent renames cannot redirect us.
        directory_fd = os.open("/", self._directory_flags())
        try:
            for part in self._root.parts[1:]:
                next_fd = os.open(
                    part,
                    self._directory_flags(),
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            return directory_fd
        except OSError as exc:
            os.close(directory_fd)
            raise GeneratedImagePathError(
                f"generated image root is unavailable or unsafe: {self._root}"
            ) from exc

    def _read_from_root(
        self,
        root_fd: int,
        parts: tuple[str, ...],
        original: str,
    ) -> GeneratedImage:
        directory_fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                next_fd = self._open_directory(part, directory_fd, original)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = self._open_file(parts[-1], directory_fd, original)
        finally:
            os.close(directory_fd)

        try:
            return self._snapshot(file_fd, parts[-1], original)
        finally:
            os.close(file_fd)

    def _open_directory(self, name: str, parent_fd: int, original: str) -> int:
        try:
            return os.open(name, self._directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            self._raise_open_error(original, exc)

    @staticmethod
    def _file_flags() -> int:
        # O_NONBLOCK prevents a hostile FIFO from hanging before fstat rejects
        # it. It has no effect on ordinary files.
        return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

    def _open_file(self, name: str, parent_fd: int, original: str) -> int:
        try:
            return os.open(name, self._file_flags(), dir_fd=parent_fd)
        except OSError as exc:
            self._raise_open_error(original, exc)

    @staticmethod
    def _raise_open_error(original: str, exc: OSError) -> NoReturn:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            detail = "contains a symlink or non-directory component"
        elif exc.errno == errno.ENOENT:
            detail = "was not found"
        else:
            detail = "could not be opened safely"
        raise GeneratedImagePathError(f"generated image {original!r} {detail}") from exc

    def _snapshot(self, file_fd: int, filename: str, original: str) -> GeneratedImage:
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise GeneratedImagePathError(
                f"generated image is not a regular file: {original}"
            )
        if info.st_nlink != 1:
            raise GeneratedImagePathError(
                f"generated image may not be hard-linked: {original}"
            )
        if info.st_size <= 0:
            raise GeneratedImagePathError(f"generated image is empty: {original}")
        if info.st_size > self._max_bytes:
            raise GeneratedImagePathError(
                f"generated image exceeds {self._max_bytes} bytes: {original}"
            )

        with os.fdopen(os.dup(file_fd), "rb") as stream:
            content = stream.read(self._max_bytes + 1)
        if len(content) > self._max_bytes:
            raise GeneratedImagePathError(
                f"generated image exceeds {self._max_bytes} bytes: {original}"
            )
        after = os.fstat(file_fd)
        if len(content) != info.st_size or after.st_size != info.st_size:
            raise GeneratedImagePathError(
                f"generated image changed while being read: {original}"
            )
        if not _is_supported_raster(content[:12]):
            raise GeneratedImagePathError(
                f"generated image is not a supported PNG or JPEG: {original}"
            )
        return GeneratedImage(
            filename=filename,
            content=content,
            size_bytes=len(content),
        )
