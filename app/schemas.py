"""请求 / 响应数据模型（Pydantic v2）。"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Operator = Literal["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "prefix", "suffix", "contains"]


class RuleIn(BaseModel):
    """有序属性规则。priority 越小优先级越高。"""

    priority: int = Field(ge=1, description="优先级，数字越小越先判定")
    attribute: str = Field(min_length=1, description="要检查的用户属性名，如 region / age")
    operator: Operator = "eq"
    values: list[Any] = Field(default_factory=list, description="比较值；eq/ne/gt 等取第一个")
    variant_key: str = Field(min_length=1, description="命中后返回的变体 key")

    @field_validator("values")
    @classmethod
    def _values_not_empty(cls, v: list[Any]) -> list[Any]:
        if not v:
            raise ValueError("规则必须提供至少一个比较值 values")
        return v


class VariantIn(BaseModel):
    """加权变体。所有权重之和必须等于 100。"""

    key: str = Field(min_length=1)
    weight: int = Field(ge=0, le=100, description="桶权重，全部变体之和必须为 100")
    description: str = ""


class FlagUpsertRequest(BaseModel):
    flag_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    description: str = ""
    variants: list[VariantIn] = Field(min_length=2, description="至少配置两个加权变体")
    rules: list[RuleIn] = Field(default_factory=list)

    @field_validator("variants")
    @classmethod
    def _weights_sum_to_100(cls, v: list[VariantIn]) -> list[VariantIn]:
        total = sum(item.weight for item in v)
        if total != 100:
            raise ValueError(f"变体权重之和必须为 100，当前为 {total}")
        keys = [item.key for item in v]
        if len(set(keys)) != len(keys):
            raise ValueError("变体 key 不允许重复")
        return v

    @model_validator(mode="after")
    def _rules_consistent(self) -> "FlagUpsertRequest":
        priorities = [r.priority for r in self.rules]
        if len(set(priorities)) != len(priorities):
            raise ValueError("规则 priority 不允许重复")
        variant_keys = {v.key for v in self.variants}
        for r in self.rules:
            if r.variant_key not in variant_keys:
                raise ValueError(f"规则(priority={r.priority})引用了不存在的变体 {r.variant_key!r}")
        return self


class FlagResponse(BaseModel):
    flag_key: str
    description: str
    version: int
    variants: list[VariantIn]
    rules: list[RuleIn]
    created_at: Any
    updated_at: Any


class FlagVersionResponse(BaseModel):
    """某一历史版本的完整不可变配置。"""

    flag_key: str
    version: int
    description: str
    variants: list[VariantIn]
    rules: list[RuleIn]
    created_at: Any


class EvaluateRequest(BaseModel):
    flag_key: str | None = Field(default=None, max_length=128, description="路径式调用可省略")
    user_id: str = Field(min_length=1, max_length=128)
    context: dict[str, Any] = Field(
        default_factory=dict, description="用户属性，规则按此匹配，如 {\"region\": \"cn\", \"age\": 20}"
    )
    request_id: str | None = Field(default=None, max_length=64, description="可选，幂等用；缺省自动生成")
    config_version: int | None = Field(
        default=None, ge=1, description="可选，指定使用的历史配置版本；缺省使用最新版"
    )


class ExplanationStep(BaseModel):
    step: str
    detail: dict[str, Any] = Field(default_factory=dict)


class EvaluateResponse(BaseModel):
    request_id: str
    flag_key: str
    user_id: str
    config_version: int
    matched_rule: dict[str, Any] | None = Field(
        description="命中的规则；无规则命中时为 null"
    )
    bucket_value: int | None = Field(description="0-99 的稳定哈希分桶值；规则命中时为 null")
    variant_key: str
    decision_path: Literal["rule", "bucket"]
    explanation: list[ExplanationStep]


class SnapshotResponse(BaseModel):
    request_id: str
    flag_key: str
    user_id: str
    request_context: dict[str, Any]
    config_version: int
    bucket_value: int | None
    matched_rule_index: int | None
    variant_key: str
    explanation: list[ExplanationStep]
    decided_at: Any
