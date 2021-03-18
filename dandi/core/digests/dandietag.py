# Derived from <https://github.com/girder/django-s3-file-field/blob/master/
# s3_file_field/_multipart.py>, copyright Kitware, Inc. <kitware@kitware.com>
# under the Apache 2.0 license

from dataclasses import dataclass
from hashlib import md5
import math
import os
from typing import Iterator, List, NamedTuple, Optional, Union


def mb(bytes_size: int) -> int:
    return bytes_size * 2 ** 20


def gb(bytes_size: int) -> int:
    return bytes_size * 2 ** 30


def tb(bytes_size: int) -> int:
    return bytes_size * 2 ** 40


class Part(NamedTuple):
    number: int
    offset: int
    size: int


@dataclass
class PartGenerator:
    part_qty: int
    initial_part_size: int
    final_part_size: int

    # S3 multipart limits: https://docs.aws.amazon.com/AmazonS3/latest/dev/qfacts.html
    # 10k is the maximum number of allowed parts allowed by S3
    MAX_PARTS = 10_000
    # 5MB is the minimum part size allowed by S3
    MIN_PART_SIZE = mb(5)
    # 5GB is the maximum part size allowed by S3
    MAX_PART_SIZE = gb(5)

    @classmethod
    def for_file_size(cls, file_size: int) -> "PartGenerator":
        """Method to calculate sequential part sizes given a file size"""
        part_size = mb(64)

        if file_size > tb(5):
            raise ValueError("File is larger than the S3 maximum object size.")

        if math.ceil(file_size / part_size) >= cls.MAX_PARTS:
            part_size = math.ceil(file_size / cls.MAX_PARTS)

        if part_size < cls.MIN_PART_SIZE:
            part_size = cls.MIN_PART_SIZE

        if part_size > cls.MAX_PART_SIZE:
            part_size = cls.MAX_PART_SIZE

        part_qty, final_part_size = divmod(file_size, part_size)
        if final_part_size == 0:
            final_part_size = part_size
        else:
            part_qty += 1
        if part_qty == 1:
            part_size = final_part_size
        return cls(part_qty, part_size, final_part_size)

    def __len__(self) -> int:
        return self.part_qty

    def __getitem__(self, index: int) -> Part:
        if 1 <= index < self.part_qty:
            return Part(
                index, self.initial_part_size * (index - 1), self.initial_part_size
            )
        elif index == self.part_qty:
            return Part(
                index, self.initial_part_size * (index - 1), self.final_part_size
            )
        else:
            raise IndexError(index)

    def __iter__(self) -> Iterator[Part]:
        offset = 0
        for number in range(1, self.part_qty):
            yield Part(number, offset, self.initial_part_size)
            offset += self.initial_part_size
        yield Part(self.part_qty, offset, self.final_part_size)


class DandiETag:
    REGEX = r"[0-9a-f]{32}-\d{1,5}"
    MAX_STR_LENGTH = 38

    def __init__(self, file_size: int):
        self._part_gen: PartGenerator = PartGenerator.for_file_size(file_size)
        self._md5_digests: List[Optional[bytes]] = [None] * len(self._part_gen)
        self._next_index: int = 0

    @property
    def part_qty(self) -> int:
        return len(self._part_gen)

    @property
    def complete(self) -> bool:
        return self._next_index == self.part_qty

    def get_parts(self) -> Iterator[Part]:
        return iter(self._part_gen)

    def get_next_part(self) -> Optional[Part]:
        if self._next_index < self.part_qty:
            return self._part_gen[self._next_index + 1]
        else:
            return None

    def as_str(self) -> str:
        if not self.complete:
            raise ValueError("Not all part hashes submitted")
        blob = b""
        for d in self._md5_digests:
            assert d is not None
            blob += d
        parts_digest = md5(blob).hexdigest()
        return f"{parts_digest}-{len(self._md5_digests)}"

    @classmethod
    def from_file(
        cls, path: Union[str, bytes, "os.PathLike[str]", "os.PathLike[bytes]"]
    ) -> "DandiETag":
        etag = cls(file_size=os.path.getsize(path))
        with open(path, "rb") as f:
            for part in etag.get_parts():
                etag.update(f.read(part.size))
        return etag

    def add_digest(self, p: Part, part_digest: bytes) -> None:
        i = p.number - 1
        if self._md5_digests[i] is not None:
            raise RuntimeError(f"Digest for part {p.number} submitted more than once")
        self._md5_digests[i] = part_digest
        self._update_index()

    def add_next_digest(self, part_digest: bytes) -> None:
        if self.complete:
            raise RuntimeError(
                "Trying to update DandiETag with a new digest having already"
                f" processed all {self.part_qty} parts"
            )
        self._md5_digests[self._next_index] = part_digest
        self._update_index()

    def _update_index(self) -> None:
        while (
            self._next_index < self.part_qty
            and self._md5_digests[self._next_index] is not None
        ):
            self._next_index += 1

    def update(self, block):
        """Update etag with the new block of data"""
        if self.complete:
            raise RuntimeError(
                "Trying to update DandiETag with a new block having already"
                f" processed all {self.part_qty} parts"
            )
        self.add_next_digest(md5(block).digest())
