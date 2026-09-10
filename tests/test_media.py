"""照片库的测试。重点是"不穿帮"：不重复发、不发对不上时段的、不把旧照片说成刚拍的。"""

from __future__ import annotations

import logging
import random
from pathlib import Path

import yaml

from newperson.media import (
    CommandImageGenerator,
    MediaService,
    NullImageGenerator,
    PhotoLibrary,
)
from newperson.models import PhotoRequest

LUNCH = {
    "id": "lunch-001",
    "file": "lunch-001.jpg",
    "tags": ["food", "lunch"],
    "caption": "一碗牛肉面，公司楼下那家。",
    "taken_hint": "工作日中午",
}
BEACH = {
    "id": "beach-001",
    "file": "beach-001.jpg",
    "tags": ["view", "sea"],
    "caption": "海边。",
    "time_of_day": "day",
}
WINDOW = {
    "id": "window-001",
    "file": "window-001.jpg",
    "tags": ["view", "night"],
    "caption": "窗外。",
    "time_of_day": "night",
}
OLD_TRIP = {
    "id": "trip-2019",
    "file": "trip-2019.jpg",
    "tags": ["travel"],
    "caption": "以前去京都拍的。",
    "freshness": "dated",
}


def build_library(tmp_path: Path, entries: list[dict], *, missing: set[str] = frozenset()) -> PhotoLibrary:
    """造一个临时照片目录：假图片文件 + index.yaml，返回已经 load 过的库。

    ``missing`` 里的 id 只写进索引、不落盘，用来模拟"索引写了但图没了"。
    """
    for e in entries:
        if e.get("id") not in missing:
            (tmp_path / e["file"]).write_bytes(b"\x89PNG not really an image")
    index = tmp_path / "index.yaml"
    index.write_text(yaml.safe_dump({"photos": entries}, allow_unicode=True), encoding="utf-8")
    library = PhotoLibrary(index)
    library.load()
    return library


def want(*tags: str, photo_id: str | None = None, description: str = "") -> PhotoRequest:
    return PhotoRequest(photo_id=photo_id, tags=list(tags), description=description)


class FakeGenerator:
    """假生成器：记下 prompt，返回事先准备好的结果。用来确认"库里有就不该生成"。"""

    def __init__(self, result: Path | None) -> None:
        self.result = result
        self.prompts: list[str] = []

    async def generate(self, prompt: str, out_dir: Path) -> Path | None:
        self.prompts.append(prompt)
        return self.result


# -- 加载 -------------------------------------------------------------------


def test_a_missing_index_is_an_empty_library(tmp_path: Path) -> None:
    """还没配照片的部署也要能起来，不能因为少个文件就崩。"""
    library = PhotoLibrary(tmp_path / "index.yaml")
    library.load()
    assert library.photos == []
    assert library.available(set()) == []
    assert library.describe_available(set()) == ""


def test_an_empty_index_is_an_empty_library(tmp_path: Path) -> None:
    index = tmp_path / "index.yaml"
    index.write_text("", encoding="utf-8")
    library = PhotoLibrary(index)
    library.load()
    assert library.photos == []


def test_an_entry_without_a_file_on_disk_is_skipped(tmp_path: Path, caplog) -> None:
    """索引里登记了但图片没了：跳过并出警告，别等到要发的时候才发现。"""
    with caplog.at_level(logging.WARNING, logger="newperson.media"):
        library = build_library(tmp_path, [LUNCH, BEACH], missing={"beach-001"})
    assert [p.id for p in library.photos] == ["lunch-001"]
    assert "beach-001" in caplog.text


def test_a_malformed_entry_is_skipped(tmp_path: Path, caplog) -> None:
    """索引手写出错也只丢这一条，其余照片照常可用。"""
    with caplog.at_level(logging.WARNING, logger="newperson.media"):
        library = build_library(tmp_path, [LUNCH, {"file": "x.jpg"}])
    assert [p.id for p in library.photos] == ["lunch-001"]


def test_files_are_resolved_next_to_the_index(tmp_path: Path) -> None:
    """file 是相对索引目录的，整个照片目录搬走也不用改索引。"""
    nested = tmp_path / "photos"
    nested.mkdir()
    library = build_library(nested, [LUNCH])
    assert library.resolve_path(library.photos[0]) == nested / "lunch-001.jpg"


def test_all_tags_are_deduped_and_lowercased(tmp_path: Path) -> None:
    library = build_library(tmp_path, [LUNCH, dict(BEACH, tags=["View", "food"])])
    assert library.all_tags() == ["food", "lunch", "view"]


# -- 挑图 -------------------------------------------------------------------


def test_a_photo_id_is_matched_exactly(tmp_path: Path, rng: random.Random) -> None:
    """模型是直接从可用列表里抄 id 的，抄了就发这张，别再去猜标签。"""
    library = build_library(tmp_path, [LUNCH, BEACH, WINDOW])
    picked = library.pick(want("view", photo_id="lunch-001"), set(), rng)
    assert picked is not None and picked.id == "lunch-001"


def test_more_overlapping_tags_wins(tmp_path: Path, rng: random.Random) -> None:
    library = build_library(tmp_path, [LUNCH, dict(BEACH, tags=["food"])])
    picked = library.pick(want("food", "lunch"), set(), rng)
    assert picked is not None and picked.id == "lunch-001"


def test_tags_are_matched_case_insensitively(tmp_path: Path, rng: random.Random) -> None:
    """标签是模型现写的，大小写不稳定，不该因此挑不到图。"""
    library = build_library(tmp_path, [LUNCH])
    picked = library.pick(want("Food", " LUNCH "), set(), rng)
    assert picked is not None and picked.id == "lunch-001"


def test_a_photo_with_no_overlapping_tag_is_not_picked(tmp_path: Path, rng: random.Random) -> None:
    """一张都不沾边就宁可不发，发了就是答非所问。"""
    library = build_library(tmp_path, [LUNCH])
    assert library.pick(want("cat"), set(), rng) is None


def test_a_photo_on_cooldown_is_not_picked(tmp_path: Path, rng: random.Random) -> None:
    """刚发过的不能再发一遍，否则一眼假。"""
    library = build_library(tmp_path, [LUNCH, dict(BEACH, tags=["food"])])
    picked = library.pick(want("food"), {"lunch-001"}, rng)
    assert picked is not None and picked.id == "beach-001"


def test_a_photo_id_on_cooldown_falls_back_to_tags(tmp_path: Path, rng: random.Random) -> None:
    """模型要的那张正在冷却里：退回按标签挑，而不是硬发。"""
    library = build_library(tmp_path, [LUNCH, dict(BEACH, tags=["food"])])
    picked = library.pick(want("food", photo_id="lunch-001"), {"lunch-001"}, rng)
    assert picked is not None and picked.id == "beach-001"


def test_everything_on_cooldown_returns_none(tmp_path: Path, rng: random.Random) -> None:
    library = build_library(tmp_path, [LUNCH])
    assert library.pick(want("food"), {"lunch-001"}, rng) is None


def test_a_daytime_photo_is_not_picked_at_night(tmp_path: Path, rng: random.Random) -> None:
    """半夜发一张大中午的沙滩照就穿帮了。"""
    library = build_library(tmp_path, [BEACH, WINDOW])
    picked = library.pick(want("view"), set(), rng, time_of_day="night")
    assert picked is not None and picked.id == "window-001"


def test_a_photo_for_any_time_survives_the_time_filter(tmp_path: Path, rng: random.Random) -> None:
    """没标时段的照片什么时候都能发，不然半夜基本没图可发。"""
    library = build_library(tmp_path, [LUNCH])
    picked = library.pick(want("food"), set(), rng, time_of_day="night")
    assert picked is not None and picked.id == "lunch-001"


def test_no_photo_fits_the_time_returns_none(tmp_path: Path, rng: random.Random) -> None:
    library = build_library(tmp_path, [BEACH])
    assert library.pick(want("view"), set(), rng, time_of_day="night") is None


def test_an_empty_request_picks_nothing(tmp_path: Path, rng: random.Random) -> None:
    """既没给 id 也没给标签，说明模型没想好，别乱发。"""
    library = build_library(tmp_path, [LUNCH])
    assert library.pick(want(), set(), rng) is None


def test_ties_are_broken_by_the_given_rng(tmp_path: Path) -> None:
    """同分随机，但随机源是传进来的：同一个种子必须挑出同一张。"""
    twins = [dict(LUNCH, id=f"food-{i}", file=f"food-{i}.jpg", tags=["food"]) for i in range(4)]
    library = build_library(tmp_path, twins)
    request = want("food")
    picked = {library.pick(request, set(), random.Random(seed)).id for seed in range(20)}
    assert len(picked) > 1, "同分永远挑同一张，看起来就像照着表发"
    first = library.pick(request, set(), random.Random(7))
    again = library.pick(request, set(), random.Random(7))
    assert first == again


# -- 给模型看的候选列表 -------------------------------------------------------


def test_describe_available_renders_one_line_per_photo(tmp_path: Path) -> None:
    """这段文本会原样进上下文，模型照着抄 id，格式必须稳定。"""
    library = build_library(tmp_path, [LUNCH])
    assert library.describe_available(set()) == (
        "- lunch-001 [food, lunch] 一碗牛肉面，公司楼下那家。（工作日中午）"
    )


def test_describe_available_hides_photos_that_cannot_be_sent(tmp_path: Path) -> None:
    """列表里出现的每一张都必须真能发，否则模型会挑一张然后发不出去。"""
    library = build_library(tmp_path, [LUNCH, BEACH, WINDOW])
    text = library.describe_available({"lunch-001"}, time_of_day="night")
    assert text == "- window-001 [view, night] 窗外。"


# -- MediaService ------------------------------------------------------------


async def test_resolve_returns_the_path_on_disk(tmp_path: Path, rng: random.Random) -> None:
    library = build_library(tmp_path, [LUNCH])
    service = MediaService(library, NullImageGenerator(), tmp_path / "generated")
    resolved = await service.resolve(want("food"), set(), rng)
    assert resolved is not None
    assert Path(resolved.path) == tmp_path / "lunch-001.jpg"
    assert resolved.photo_id == "lunch-001"
    assert resolved.caption == "一碗牛肉面，公司楼下那家。"
    assert resolved.is_fresh is True


async def test_a_dated_photo_is_not_fresh(tmp_path: Path, rng: random.Random) -> None:
    """有日期痕迹的旧照片只能当"翻出来的"，不能说成刚拍的。"""
    library = build_library(tmp_path, [OLD_TRIP])
    service = MediaService(library, NullImageGenerator(), tmp_path / "generated")
    resolved = await service.resolve(want("travel"), set(), rng)
    assert resolved is not None and resolved.is_fresh is False


async def test_the_generator_is_only_used_when_the_library_has_nothing(
    tmp_path: Path, rng: random.Random
) -> None:
    made = tmp_path / "made.png"
    made.write_bytes(b"x")
    generator = FakeGenerator(made)
    library = build_library(tmp_path, [LUNCH])
    service = MediaService(library, generator, tmp_path / "generated")

    from_library = await service.resolve(want("food"), set(), rng)
    assert from_library is not None and from_library.photo_id == "lunch-001"
    assert generator.prompts == [], "库里有现成的就不该去生成"

    generated = await service.resolve(want("cat", description="窝在沙发上的猫"), set(), rng)
    assert generated is not None
    assert generated.path == str(made)
    assert generated.photo_id is None
    assert generator.prompts == ["窝在沙发上的猫"]


async def test_the_generator_falls_back_to_the_tags_as_prompt(
    tmp_path: Path, rng: random.Random
) -> None:
    made = tmp_path / "made.png"
    made.write_bytes(b"x")
    generator = FakeGenerator(made)
    service = MediaService(build_library(tmp_path, []), generator, tmp_path / "generated")
    await service.resolve(want("cat", "home"), set(), rng)
    assert generator.prompts == ["cat home"]


async def test_a_null_generator_means_no_photo(tmp_path: Path, rng: random.Random) -> None:
    """没有生成器是常态：挑不到就返回 None，上层去掉 {photo} 占位。"""
    service = MediaService(build_library(tmp_path, [LUNCH]), NullImageGenerator(), tmp_path / "g")
    assert await service.resolve(want("cat", description="猫"), set(), rng) is None


async def test_nothing_to_describe_means_no_generation(tmp_path: Path, rng: random.Random) -> None:
    generator = FakeGenerator(tmp_path / "never.png")
    service = MediaService(build_library(tmp_path, []), generator, tmp_path / "generated")
    assert await service.resolve(want(), set(), rng) is None
    assert generator.prompts == []


# -- 外部命令生成 -------------------------------------------------------------


async def test_the_command_generator_returns_the_written_file(tmp_path: Path) -> None:
    generator = CommandImageGenerator("printf '%s' {prompt} > {out}", timeout=10)
    out = await generator.generate("一只猫", tmp_path / "gen")
    assert out is not None
    assert out.parent == tmp_path / "gen"
    assert out.read_text(encoding="utf-8") == "一只猫"


async def test_the_prompt_cannot_run_a_second_command(tmp_path: Path) -> None:
    """prompt 是模型输出的不可信文本：拼进 shell 前必须转义，否则等于把命令行交出去。"""
    generator = CommandImageGenerator("printf '%s' {prompt} > {out}", timeout=10)
    out = await generator.generate(f"猫; touch {tmp_path / 'pwned'}", tmp_path / "gen")
    assert out is not None
    assert not (tmp_path / "pwned").exists()
    assert "touch" in out.read_text(encoding="utf-8"), "转义后 prompt 应该被原样当成文本"


async def test_the_same_prompt_always_lands_on_the_same_file(tmp_path: Path) -> None:
    """文件名取 prompt 的哈希：重复生成不会堆一地临时文件，也没有时间/随机数掺进来。"""
    generator = CommandImageGenerator("printf '%s' {prompt} > {out}", timeout=10)
    first = await generator.generate("一只猫", tmp_path / "gen")
    again = await generator.generate("一只猫", tmp_path / "gen")
    other = await generator.generate("另一只猫", tmp_path / "gen")
    assert first == again != other


async def test_a_failing_command_generates_nothing(tmp_path: Path, caplog) -> None:
    generator = CommandImageGenerator("exit 3", timeout=10)
    with caplog.at_level(logging.WARNING, logger="newperson.media"):
        assert await generator.generate("一只猫", tmp_path / "gen") is None
    assert caplog.text


async def test_a_command_that_writes_nothing_generates_nothing(tmp_path: Path) -> None:
    """命令说成功了但没落盘，一样当失败处理。"""
    generator = CommandImageGenerator("true", timeout=10)
    assert await generator.generate("一只猫", tmp_path / "gen") is None


async def test_a_slow_command_is_given_up_on(tmp_path: Path, caplog) -> None:
    """生成卡住不能把整个回复流程拖死。"""
    generator = CommandImageGenerator("sleep 5", timeout=0.2)
    with caplog.at_level(logging.WARNING, logger="newperson.media"):
        assert await generator.generate("一只猫", tmp_path / "gen") is None
    assert "超过" in caplog.text
