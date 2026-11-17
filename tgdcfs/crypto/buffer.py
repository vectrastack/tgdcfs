"""Buffer-level encryption for the metadata blob.

The metadata repositories that keep the whole tree as one document (the
pinned message, the GitLab file) used to reach the encryption layer
through the file-content repository. They now talk to their store
directly, so the same at-rest scheme is exposed here: a file header
followed by AES-GCM chunks, and a reader that passes legacy plaintext
through unchanged.
"""

from __future__ import annotations

from tgdcfs.crypto.cipher import CHUNK_OVERHEAD, ChunkedAESGCM
from tgdcfs.crypto.header import HEADER_SIZE, MAGIC, FileHeader
from tgdcfs.crypto.kdf import derive_file_key


def encrypt_buffer(master_key: bytes, chunk_size: int, buffer: bytes) -> bytes:
    """Encrypt ``buffer`` the way an uploaded file is encrypted."""
    header = FileHeader.new(chunk_size=chunk_size)
    file_key = derive_file_key(master_key, header.file_salt)
    cipher = ChunkedAESGCM(
        file_key=file_key,
        file_salt=header.file_salt,
        chunk_size=header.chunk_size,
    )
    parts = [header.serialize(file_key)]
    for index, start in enumerate(range(0, len(buffer), header.chunk_size)):
        chunk = buffer[start : start + header.chunk_size]
        parts.append(cipher.encrypt_chunk(chunk, index))
    return b"".join(parts)


def decrypt_buffer(master_key: bytes, data: bytes) -> bytes:
    """Decrypt a blob written by :func:`encrypt_buffer`.

    Data that does not start with the TGDCFS magic is returned as it is:
    a store written before encryption was turned on stays readable.
    """
    if not data.startswith(MAGIC):
        return data
    if len(data) < HEADER_SIZE:
        raise ValueError("encrypted blob is shorter than its header")
    header = FileHeader.parse(data[:HEADER_SIZE])
    file_key = derive_file_key(master_key, header.file_salt)
    header.verify(data[:HEADER_SIZE], file_key)
    cipher = ChunkedAESGCM(
        file_key=file_key,
        file_salt=header.file_salt,
        chunk_size=header.chunk_size,
    )
    body = data[HEADER_SIZE:]
    step = header.chunk_size + CHUNK_OVERHEAD
    parts: list[bytes] = []
    for index, start in enumerate(range(0, len(body), step)):
        parts.append(cipher.decrypt_chunk(body[start : start + step], index))
    return b"".join(parts)


def is_encrypted_buffer(data: bytes) -> bool:
    return data.startswith(MAGIC)


class MetadataCrypto:
    """The encrypt/decrypt pair a metadata repository needs, when
    at-rest encryption is enabled."""

    def __init__(self, master_key: bytes, chunk_size: int) -> None:
        self._master_key = master_key
        self._chunk_size = chunk_size

    def encrypt(self, buffer: bytes) -> bytes:
        return encrypt_buffer(self._master_key, self._chunk_size, buffer)

    def decrypt(self, data: bytes) -> bytes:
        return decrypt_buffer(self._master_key, data)
