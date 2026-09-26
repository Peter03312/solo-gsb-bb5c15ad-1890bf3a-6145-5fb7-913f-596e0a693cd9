"""Pydantic 请求 / 响应模型与输入校验。

时间约定：
- 客户端可提交带时区的 ISO 8601 时间；
- 不带时区的时间一律视为 UTC；
- 服务端内部统一以 UTC 存储和返回（形如 ``2026-03-01T08:00:00Z``）。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class Sensitivity(str, Enum):
    """展品敏感度分级（CIE 157 常用控制经验值，可被自定义上限覆盖）。"""

    HIGH = "high"          # 高敏感：丝织品、纸本、水彩等
    MEDIUM = "medium"      # 中等敏感：油画、木器、骨角器等
    LOW = "low"            # 低敏 / 不敏感：石材、金属、陶瓷等


# 各级敏感度的默认年度曝光上限（lux·hour/年）
STANDARD_ANNUAL_LIMITS: dict[Sensitivity, Optional[float]] = {
    Sensitivity.HIGH: 15000.0,
    Sensitivity.MEDIUM: 150000.0,
    Sensitivity.LOW: None,   # None 表示不设上限（insensitive）
}

# 允许的自定义上限范围：负数被业务显式拒绝
MIN_LUX = 0.0
MAX_LUX = 100000.0


def to_utc(value: datetime) -> datetime:
    """把任意 datetime 归一化为 UTC；naive datetime 视为 UTC。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ShowcaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="展柜名称")
    location: Optional[str] = Field(default=None, max_length=256)


class ShowcaseOut(BaseModel):
    id: int
    name: str
    location: Optional[str] = None


class ExhibitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="展品名称")
    sensitivity: Sensitivity
    annual_limit_lux_hours: Optional[float] = Field(
        default=None,
        ge=0,
        description="年度 lux·hour 上限；留空时按敏感度取标准值，低敏展品可显式传 0 表示不限",
    )

    @field_validator("annual_limit_lux_hours")
    @classmethod
    def _check_finite(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and (v != v or v in (float("inf"), float("-inf"))):
            raise ValueError("年度上限必须是有限数值")
        return v


class ExhibitUpdate(BaseModel):
    """把展品重新布置到另一个展柜。"""

    showcase_id: int


class ExhibitOut(BaseModel):
    id: int
    showcase_id: int
    name: str
    sensitivity: Sensitivity
    annual_limit_lux_hours: Optional[float] = None


class LightIntervalIn(BaseModel):
    """一条带起止时间的照明区间（闭开区间 [start, end)）。"""

    start: datetime
    end: datetime
    lux: float = Field(ge=MIN_LUX, le=MAX_LUX, description="区间内照度（lux），不允许负数")

    @field_validator("lux")
    @classmethod
    def _check_lux_finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("照度必须是有限数值")
        return v

    @model_validator(mode="after")
    def _check_range(self) -> "LightIntervalIn":
        start, end = to_utc(self.start), to_utc(self.end)
        if end <= start:
            raise ValueError("照明区间结束时间必须晚于开始时间（不接受反向或零长度区间）")
        self.start, self.end = start, end
        return self


class LightIntervalBulk(BaseModel):
    """批量提交照明区间，整体校验、整体入库。"""

    intervals: list[LightIntervalIn] = Field(min_length=1)


class LightIntervalOut(LightIntervalIn):
    id: int
    showcase_id: int


class MergedSegmentOut(BaseModel):
    """时间线合并后的一个恒照度片段。"""

    start: datetime
    end: datetime
    lux: float


class SafeLuxLimitIn(BaseModel):
    """安全照度上限试算请求：计划照明的起止时间。

    归一化到 UTC 后要求起点早于终点，且二者处于同一公历年。
    """

    start: datetime = Field(description="计划照明开始时间；不带时区视为 UTC")
    end: datetime = Field(description="计划照明结束时间；不带时区视为 UTC")

    @model_validator(mode="after")
    def _check_range(self) -> "SafeLuxLimitIn":
        start, end = to_utc(self.start), to_utc(self.end)
        if end <= start:
            raise ValueError("计划照明结束时间必须晚于开始时间（不接受反向或零长度区间）")
        if start.year != end.year:
            raise ValueError("计划照明起止时间必须处于同一公历年（UTC 归一化后）")
        self.start, self.end = start, end
        return self


class SafeLuxExhibitOut(BaseModel):
    """安全照度上限核算涉及的有限额展品及其年度剩余额度。"""

    exhibit_id: int
    exhibit_name: str
    annual_limit_lux_hours: float
    remaining_lux_hours: float = Field(
        description="年度剩余额度 = 年度上限 − 年度曝光；负值表示已超限"
    )


class SafeLuxLimitOut(BaseModel):
    showcase_id: int
    showcase_name: str
    year: int
    start: datetime = Field(description="归一化到 UTC 的计划开始时间")
    end: datetime = Field(description="归一化到 UTC 的计划结束时间")
    feasible: bool = Field(description="现状是否未超限、允许追加照明")
    max_safe_lux: Optional[int] = Field(
        default=None,
        description="计划时段内可追加的最大整数照度（0～100000 lux）；现状已超限时为 null",
    )
    binding_exhibits: list[SafeLuxExhibitOut] = Field(
        default_factory=list,
        description="安全上限处年度剩余额度最少的展品（可并列）；无有限额展品或不可行时为空",
    )
    exceeded_exhibits: list[SafeLuxExhibitOut] = Field(
        default_factory=list,
        description="现状已超出各自年度上限的展品；feasible=true 时为空",
    )


class LightingPreviewIn(BaseModel):
    """照明调整试算请求：撤去 / 新增仅在内存中演算，不写入数据库。"""

    day: Optional[date] = Field(
        default=None,
        description="查询日（UTC），默认今天；试算范围为该日所属公历年",
    )
    remove_interval_ids: list[int] = Field(
        default_factory=list,
        description="要暂时撤去的既有照明区间 ID（必须属于该展柜，否则 404）",
    )
    add_intervals: list[LightIntervalIn] = Field(
        default_factory=list,
        description="拟新增的照明区间（沿用现有时间与照度校验）",
    )


class LightingPreviewExhibitOut(BaseModel):
    """单件展品在 现状 / 调整后 两种时间线下的年度曝光对比。"""

    exhibit_id: int
    exhibit_name: str
    sensitivity: Sensitivity
    annual_limit_lux_hours: Optional[float]
    year: int
    current_total_lux_hours: float = Field(description="现状年度曝光（lux·hour）")
    preview_total_lux_hours: float = Field(description="调整后年度曝光（lux·hour）")
    delta_lux_hours: float = Field(description="试算差额 = 调整后 − 现状")
    current_first_exceed_at: Optional[datetime] = Field(
        default=None, description="现状首次超出年度上限的时刻；不限量展品为 null"
    )
    preview_first_exceed_at: Optional[datetime] = Field(
        default=None, description="调整后首次超出年度上限的时刻；不限量展品为 null"
    )


class LightingPreviewOut(BaseModel):
    showcase_id: int
    showcase_name: str
    query_date: datetime
    year: int
    removed_interval_ids: list[int]
    added_interval_count: int
    exhibits: list[LightingPreviewExhibitOut]


class ExhibitBudgetOut(BaseModel):
    exhibit_id: int
    exhibit_name: str
    sensitivity: Sensitivity
    annual_limit_lux_hours: Optional[float]
    year: int
    total_lux_hours: float
    remaining_lux_hours: Optional[float] = Field(
        default=None, description="本年度剩余额度；不限量展品为 null"
    )
    first_exceed_at: Optional[datetime] = Field(
        default=None, description="累计曝光首次越过年度上限的时刻"
    )
    exceeded_lux_hours: float = Field(
        default=0.0, description="截至年末区间累计超出量（total - limit）"
    )
    query_date: datetime
    daily_quota_lux_hours: Optional[float] = Field(
        default=None, description="查询日均匀日额度 = 年度上限 / 当年天数"
    )
    daily_used_lux_hours: float = Field(default=0.0, description="查询日当天已累计曝光")
    daily_remaining_lux_hours: Optional[float] = Field(
        default=None, description="查询日剩余额度 = 日额度 - 当日已用"
    )
    annual_remaining_at_date: Optional[float] = Field(
        default=None, description="截至查询日 24:00 的年度剩余额度"
    )


class ShowcaseBudgetOut(BaseModel):
    showcase_id: int
    showcase_name: str
    query_date: datetime
    exhibits: list[ExhibitBudgetOut]


class HealthOut(BaseModel):
    status: str
    database: str
