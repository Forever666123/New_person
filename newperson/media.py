"""照片：本地照片库 + 可选的外部图片生成。见 DESIGN.md 2.9。

为什么照片要从一个**登记过的库**里挑，而不是随口生成：她发的图得和她说的话对得上。
所以每张照片都带 ``caption`` 和 ``taken_hint``，候选列表原样进模型上下文
（:meth:`PhotoLibrary.describe_available`），由模型直接挑 ``photo_id``，这里再核对一次。

挑图有三条硬约束，都是为了不穿帮：

- **冷却**：``exclude_ids`` 是最近发过的（由 memory 按 30 天算），同一张不连着发第二遍。
- **时段**：``time_of_day`` 对不上的不要，免得凌晨三点发一张大中午的沙滩照。
- **新鲜度**：``freshness == "dated"`` 的旧照片不能说成"刚拍的"，
  所以 :class:`ResolvedPhoto` 带一个 ``is_fresh`` 交给下游措辞。

图片生成只是兜底，而且允许完全没有（:class:`NullImageGenerator`）。
外部命令的 prompt 来自模型输出，属于不可信输入，拼进 shell 之前一律 ``shlex.quote``。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import shlex
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import ValidationError

from .models import Photo, PhotoRequest, ResolvedPhoto, TimeOfDay

log = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{(prompt|out)\}")
"""命令模板里的占位符。一次扫描全部替换，替换进去的内容不再被二次扫描。"""


def _normalize(tags: list[str]) -> set[str]:
    """标签归一化。标签是模型现写的英文词，大小写和空白都不稳定，比较前统一。"""
    return {t.strip().lower() for t in tags if t.strip()}


class PhotoLibrary:
    def __init__(self, index_path: str | Path) -> None:
        """index 不存在时视为空库（不报错），``check`` 命令会另外提示。"""
        self.index_path = Path(index_path)
        self.photos: list[Photo] = []

    # -- 加载 ---------------------------------------------------------------

    def load(self) -> None:
        """读取 index.yaml；file 相对 index 所在目录解析；文件不存在的条目跳过并记录警告。

        整个加载过程不抛异常：照片是锦上添花的功能，缺图不该拖垮机器人启动。
        """
        self.photos = []
        if not self.index_path.is_file():
            log.info("照片索引 %s 不存在，按空库处理", self.index_path)
            return
        with self.index_path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for entry in raw.get("photos") or []:
            try:
                photo = Photo(**entry)
            except (ValidationError, TypeError) as exc:
                log.warning("照片索引里有一条读不了，跳过：%s（%s）", entry, exc)
                continue
            if not self.resolve_path(photo).is_file():
                log.warning("照片 %s 的文件 %s 不存在，跳过", photo.id, photo.file)
                continue
            self.photos.append(photo)

    def resolve_path(self, photo: Photo) -> Path:
        """条目里的 file 写的是相对 index 所在目录的路径，这样整个照片目录可以整体搬走。"""
        return self.index_path.parent / photo.file

    def all_tags(self) -> list[str]:
        """库里出现过的标签，去重排序。用来告诉模型有哪些题材可选。"""
        return sorted({t for photo in self.photos for t in _normalize(photo.tags)})

    # -- 候选 ---------------------------------------------------------------

    def available(
        self, exclude_ids: set[str], time_of_day: TimeOfDay | None = None
    ) -> list[Photo]:
        """当前能发的照片：去掉冷却期内的，去掉与此刻时段对不上的。

        ``time_of_day`` 为 None 表示不限时段（比如模型明确要一张旧照片时）。
        """
        return [
            photo
            for photo in self.photos
            if photo.id not in exclude_ids and self._fits_time(photo, time_of_day)
        ]

    @staticmethod
    def _fits_time(photo: Photo, time_of_day: TimeOfDay | None) -> bool:
        """``any`` 的照片什么时候都能发；其余必须和当前时段一致。"""
        return time_of_day is None or photo.time_of_day in ("any", time_of_day)

    def describe_available(
        self, exclude_ids: set[str], time_of_day: TimeOfDay | None = None
    ) -> str:
        """渲染成给模型看的多行文本，一行一张，模型照着挑 id。"""
        return "\n".join(
            self._describe(photo) for photo in self.available(exclude_ids, time_of_day)
        )

    @staticmethod
    def _describe(photo: Photo) -> str:
        """``- lunch-001 [food, lunch] 一碗牛肉面，公司楼下那家。（工作日中午）``

        caption 让模型知道图里有什么，taken_hint 让它别把中午的照片说成晚上拍的。
        """
        segments = [f"- {photo.id}"]
        if photo.tags:
            segments.append("[" + ", ".join(photo.tags) + "]")
        if photo.caption:
            segments.append(photo.caption)
        line = " ".join(segments)
        if photo.taken_hint:
            line += f"（{photo.taken_hint}）"
        return line

    # -- 挑图 ---------------------------------------------------------------

    def pick(
        self,
        request: PhotoRequest,
        exclude_ids: set[str],
        rng: random.Random,
        time_of_day: TimeOfDay | None = None,
    ) -> Photo | None:
        """按 id → 按标签挑一张。挑不到返回 None，上层会把 ``{photo}`` 占位去掉。

        随机源由外面传进来，测试才能复现"同分随机"的结果。
        """
        candidates = self.available(exclude_ids, time_of_day)
        if not candidates:
            return None

        if request.photo_id:
            for photo in candidates:
                if photo.id == request.photo_id:
                    return photo
            # 模型挑的 id 可能刚好在冷却里或时段不符：不硬发，退回按标签挑。
            log.info("模型要的照片 %s 现在不可用，改按标签挑", request.photo_id)

        wanted = _normalize(request.tags)
        if not wanted:
            return None
        scored = [(len(_normalize(photo.tags) & wanted), photo) for photo in candidates]
        best = max(score for score, _ in scored)
        if best == 0:
            # 一个标签都不沾的照片宁可不发，发了就是答非所问。
            return None
        top = sorted((photo for score, photo in scored if score == best), key=lambda p: p.id)
        return rng.choice(top)


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
        out_dir.mkdir(parents=True, exist_ok=True)
        # 用 prompt 的哈希做文件名：同一句话重复生成会落在同一个文件上，不会堆垃圾，
        # 也不引入时间/随机数这类让结果不可复现的东西。
        digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
        out = out_dir / f"gen-{digest}.png"
        # prompt 是模型输出的不可信文本，必须转义后再进 shell，否则等于把命令行交给模型。
        values = {"prompt": prompt, "out": str(out)}
        command = _PLACEHOLDER.sub(lambda m: shlex.quote(values[m.group(1)]), self.command)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            log.warning("图片生成命令起不来：%s", exc)
            return None

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError:
            log.warning("图片生成超过 %.0f 秒，放弃", self.timeout)
            # 不杀掉的话进程会一直挂在那儿占资源。
            await self._kill(proc)
            return None

        if proc.returncode != 0:
            log.warning(
                "图片生成命令返回 %s：%s",
                proc.returncode,
                stderr.decode("utf-8", "replace").strip()[:500],
            )
            return None
        if not out.is_file():
            log.warning("图片生成命令说成功了，但 %s 不存在", out)
            return None
        return out

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


class MediaService:
    def __init__(self, library: PhotoLibrary, generator: ImageGenerator, generated_dir: Path) -> None:
        self.library = library
        self.generator = generator
        self.generated_dir = generated_dir

    async def resolve(
        self,
        request: PhotoRequest,
        exclude_ids: set[str],
        rng: random.Random,
        time_of_day: TimeOfDay | None = None,
    ) -> ResolvedPhoto | None:
        """先查库，再尝试生成；都没有返回 None。"""
        photo = self.library.pick(request, exclude_ids, rng, time_of_day)
        if photo is not None:
            return ResolvedPhoto(
                path=str(self.library.resolve_path(photo)),
                photo_id=photo.id,
                caption=photo.caption,
                # 标了 dated 的是有日期痕迹的旧照片，只能当"翻出来的"，不能说成刚拍的。
                is_fresh=photo.freshness != "dated",
            )

        prompt = request.description.strip() or " ".join(request.tags).strip()
        if not prompt:
            # 连想发什么都说不清楚，就别生成了。
            return None
        path = await self.generator.generate(prompt, self.generated_dir)
        if path is None:
            return None
        return ResolvedPhoto(path=str(path), photo_id=None, caption=request.description, is_fresh=True)
