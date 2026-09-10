"""照片：本地照片库 + 可选的外部图片生成。见 DESIGN.md 2.9。"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Protocol

from .models import Photo, PhotoRequest, ResolvedPhoto


class PhotoLibrary:
    def __init__(self, index_path: str | Path) -> None:
        """index 不存在时视为空库（不报错），``check`` 命令会另外提示。"""
        self.index_path = Path(index_path)
        self.photos: list[Photo] = []

    def load(self) -> None:
        """读取 index.yaml；file 相对 index 所在目录解析；文件不存在的条目跳过并记录警告。"""
        raise NotImplementedError

    def all_tags(self) -> list[str]:
        raise NotImplementedError

    def pick(self, tags: list[str], exclude_ids: set[str], rng: random.Random) -> Photo | None:
        """按标签重合数排序（大小写不敏感），优先未发过；同分随机。没有任何重合返回 None。"""
        raise NotImplementedError

    def resolve_path(self, photo: Photo) -> Path:
        raise NotImplementedError


class ImageGenerator(Protocol):
    async def generate(self, prompt: str, out_dir: Path) -> Path | None: ...


class NullImageGenerator:
    async def generate(self, prompt: str, out_dir: Path) -> Path | None:
        return None


class CommandImageGenerator:
    """调用外部命令生成图片。command 模板含 {prompt} 与 {out}；超时 timeout 秒；失败返回 None。"""

    def __init__(self, command: str, timeout: float = 120.0) -> None:
        self.command = command
        self.timeout = timeout

    async def generate(self, prompt: str, out_dir: Path) -> Path | None:
        raise NotImplementedError


class MediaService:
    def __init__(self, library: PhotoLibrary, generator: ImageGenerator, generated_dir: Path) -> None:
        self.library = library
        self.generator = generator
        self.generated_dir = generated_dir

    async def resolve(self, request: PhotoRequest, exclude_ids: set[str], rng: random.Random) -> ResolvedPhoto | None:
        """先查库，再尝试生成；都没有返回 None。"""
        raise NotImplementedError
