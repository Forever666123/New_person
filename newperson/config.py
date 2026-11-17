"""运行配置：从环境变量 / ``.env`` 读取。

人物本身的设定不在这里，见 ``persona.py``。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator


def _split_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


class Settings(BaseModel):
    discord_bot_token: str = ""
    owner_user_id: int = 0
    allowed_user_ids: list[int] = Field(default_factory=list)
    """允许和人物聊天的用户；总是包含 owner。"""
    proactive_channel_id: int | None = None
    """主动消息发到哪个频道；None 表示私聊 owner。"""

    model: str = "claude-opus-5-5"
    """回复和主动消息用的模型。这两件事直接决定她像不像人。"""
    fallback_model: str = "claude-opus-5-5"
    """回复被主模型的安全分类器拒了，交给它再试一次。写 none 关掉。

    拒了不换的话，那批消息一直挂在未读里，之后每一次回复都带着它、每一次都被拒——
    一条消息就能让她从此不说话。跟主模型一样时等于没有。
    """
    utility_model_override: str = ""
    """日程生成和记忆整理用的模型。留空就跟主模型一样。

    这两件事不面向对话，只要格式对、意思在就行，用便宜的那档能省不少。
    """
    effort: str = "medium"
    max_tokens: int = 8000

    persona_path: Path = Path("persona/persona.yaml")
    photos_index: Path = Path("persona/photos/index.yaml")
    db_path: Path = Path("data/newperson.db")
    downloads_dir: Path = Path("data/downloads")
    generated_dir: Path = Path("data/generated")

    max_calls_per_day: int = 200
    """每天最多调多少次模型。超了就当"今天没怎么看手机"，任务顺延，不会崩。"""
    allow_placeholders: bool = False
    """人设里还有【待填】时是否允许启动。"""

    delay_scale: float = 1.0
    """把所有等待时间按比例缩放，调试用。1.0 = 真实节奏。"""
    force_awake: bool = False
    """调试用：让她一直醒着。

    第一次跑起来常常是半夜，她按作息正在睡觉，于是你发什么都要等到早上，
    看不到任何效果。这个开关只影响作息判定，不改她说话的方式。
    """
    log_level: str = "INFO"
    image_gen_command: str | None = None
    """外部图片生成命令模板，含 {prompt} 与 {out} 占位符。留空就只用本地照片库。"""
    study_calendar_ics_url: SecretStr | None = None
    """他学习日历的私密 iCal 地址。**这是密钥**：谁拿到都能看他的日历。

    用 SecretStr 包着：打印 Settings、异常里带出配置的时候只会显示星号。
    没配就整个催学不动。
    """

    @property
    def has_study_calendar(self) -> bool:
        return bool(self.study_calendar_ics_url and self.study_calendar_ics_url.get_secret_value().strip())

    @field_validator("effort")
    @classmethod
    def _check_effort(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if v not in allowed:
            raise ValueError(f"NEWPERSON_EFFORT 必须是 {sorted(allowed)} 之一，收到 {v!r}")
        return v

    @field_validator("delay_scale")
    @classmethod
    def _check_scale(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("DELAY_SCALE 必须大于 0")
        return v

    @property
    def reply_fallback(self) -> str:
        """真正会用的备用模型。关掉了或者跟主模型一样就是空串。"""
        chosen = self.fallback_model.strip()
        if chosen.lower() in {"", "none", "off"} or chosen == self.model:
            return ""
        return chosen

    @property
    def utility_model(self) -> str:
        return self.utility_model_override or self.model

    @property
    def all_allowed_user_ids(self) -> set[int]:
        ids = set(self.allowed_user_ids)
        if self.owner_user_id:
            ids.add(self.owner_user_id)
        return ids

    def missing_required(self) -> list[str]:
        """返回缺失的必填项名称，供 ``check`` 命令使用。"""
        missing = []
        if not self.discord_bot_token:
            missing.append("DISCORD_BOT_TOKEN")
        if not self.owner_user_id:
            missing.append("OWNER_DISCORD_USER_ID")
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            missing.append("ANTHROPIC_API_KEY")
        return missing


def load_settings(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    """读取 ``.env``（若存在）与环境变量，构造 :class:`Settings`。"""
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)
    env = os.environ
    return Settings(
        discord_bot_token=env.get("DISCORD_BOT_TOKEN", ""),
        owner_user_id=int(env.get("OWNER_DISCORD_USER_ID") or 0),
        allowed_user_ids=_split_ids(env.get("ALLOWED_USER_IDS")),
        proactive_channel_id=int(env["PROACTIVE_CHANNEL_ID"]) if env.get("PROACTIVE_CHANNEL_ID") else None,
        model=env.get("NEWPERSON_MODEL", "claude-opus-5-5"),
        fallback_model=env.get("NEWPERSON_FALLBACK_MODEL", "claude-opus-5-5"),
        utility_model_override=env.get("NEWPERSON_UTILITY_MODEL", ""),
        effort=env.get("NEWPERSON_EFFORT", "medium"),
        max_tokens=int(env.get("NEWPERSON_MAX_TOKENS", "8000")),
        max_calls_per_day=int(env.get("MAX_CALLS_PER_DAY", "200")),
        persona_path=Path(env.get("PERSONA_PATH", "persona/persona.yaml")),
        photos_index=Path(env.get("PHOTOS_INDEX", "persona/photos/index.yaml")),
        db_path=Path(env.get("DB_PATH", "data/newperson.db")),
        downloads_dir=Path(env.get("DOWNLOADS_DIR", "data/downloads")),
        generated_dir=Path(env.get("GENERATED_DIR", "data/generated")),
        delay_scale=float(env.get("DELAY_SCALE", "1.0")),
        force_awake=env.get("DEBUG_FORCE_AWAKE", "").strip().lower() in {"1", "true", "yes"},
        log_level=env.get("LOG_LEVEL", "INFO"),
        image_gen_command=env.get("IMAGE_GEN_COMMAND") or None,
        study_calendar_ics_url=SecretStr(env["STUDY_CALENDAR_ICS_URL"].strip())
        if env.get("STUDY_CALENDAR_ICS_URL", "").strip()
        else None,
    )
