"""FastAPI 入口：功能开关判定服务。

路由：
- POST /api/v1/flags              创建/更新开关配置（已存在则 version+1）
- GET  /api/v1/flags              列出全部开关
- GET  /api/v1/flags/{flag_key}   查看单个开关
- DELETE /api/v1/flags/{flag_key} 删除开关
- POST /api/v1/flags/{flag_key}/evaluate  判定（按路径中的 flag_key）
- POST /api/v1/evaluate           判定（flag_key 在请求体中）
- GET  /api/v1/decisions/{request_id}     按 request_id 查询判定快照
- GET  /healthz                   健康检查
"""

import json
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import crud
from app.database import Base, engine, get_db
from app.engine import RuleEvaluationError, decide
from app.models import FlagConfig
from app.schemas import EvaluateRequest, EvaluateResponse, FlagResponse, FlagUpsertRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _seed_demo_flags()
    yield


app = FastAPI(
    title="Feature Flag Decision API",
    version="1.0.0",
    description="有序属性规则 + 加权变体 + 稳定哈希分桶的功能开关判定服务",
    lifespan=lifespan,
)


def _seed_demo_flags() -> None:
    """首次启动时写入一个演示开关，已存在则不覆盖。"""
    db = next(get_db())
    try:
        if crud.get_flag_or_none(db, "new_checkout") is not None:
            return
        demo = FlagUpsertRequest(
            flag_key="new_checkout",
            description="新版结账页灰度：内部员工与白名单地区走规则，其余用户按 90/10 分桶",
            variants=[
                {"key": "on", "weight": 10, "description": "新版结账页"},
                {"key": "off", "weight": 90, "description": "旧版结账页"},
            ],
            rules=[
                {
                    "priority": 1,
                    "attribute": "employee",
                    "operator": "eq",
                    "values": [True],
                    "variant_key": "on",
                },
                {
                    "priority": 2,
                    "attribute": "region",
                    "operator": "in",
                    "values": ["cn-beta", "sg-beta"],
                    "variant_key": "on",
                },
            ],
        )
        crud.upsert_flag(db, demo)
    finally:
        db.close()


def _snapshot_to_response(snap, *, request_context: dict | None = None) -> dict:
    context = request_context if request_context is not None else json.loads(
        snap.request_context_json
    )
    return {
        "request_id": snap.request_id,
        "flag_key": snap.flag_key,
        "user_id": snap.user_id,
        "config_version": snap.config_version,
        "matched_rule": None,
        "bucket_value": snap.bucket_value,
        "variant_key": snap.variant_key,
        "decision_path": "rule" if snap.matched_rule_index is not None else "bucket",
        "explanation": json.loads(snap.explanation_json),
        # SnapshotResponse 扩展字段
        "request_context": context,
        "matched_rule_index": snap.matched_rule_index,
        "decided_at": snap.decided_at,
    }


def _replay_response(snap, flag: FlagConfig, req: EvaluateRequest) -> EvaluateResponse:
    """从快照重建判定响应（幂等重试场景）。"""
    body = _snapshot_to_response(snap, request_context=req.context)
    matched_rule = None
    if snap.matched_rule_index is not None:
        rules = crud.parse_rules(flag.rules_json)
        r = rules[snap.matched_rule_index]
        matched_rule = {
            "rule_index": snap.matched_rule_index,
            "priority": r.priority,
            "attribute": r.attribute,
            "operator": r.operator,
            "values": r.values,
            "variant_key": r.variant_key,
        }
    body["matched_rule"] = matched_rule
    allowed = EvaluateResponse.model_fields
    return EvaluateResponse(**{k: v for k, v in body.items() if k in allowed})


def _decision_response(
    request_id: str, flag: FlagConfig, req: EvaluateRequest, decision
) -> EvaluateResponse:
    matched_rule = None
    if decision.matched_rule is not None:
        r = decision.matched_rule
        matched_rule = {
            "rule_index": decision.matched_rule_index,
            "priority": r.priority,
            "attribute": r.attribute,
            "operator": r.operator,
            "values": r.values,
            "variant_key": r.variant_key,
        }
    return EvaluateResponse(
        request_id=request_id,
        flag_key=flag.flag_key,
        user_id=req.user_id,
        config_version=flag.version,
        matched_rule=matched_rule,
        bucket_value=decision.bucket_value,
        variant_key=decision.variant_key,
        decision_path=decision.decision_path,
        explanation=decision.explanation,
    )


@app.exception_handler(RuleEvaluationError)
async def rule_error_handler(request: Request, exc: RuleEvaluationError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> dict:
    return {"status": "ok", "snapshots": crud.count_snapshots(db)}


@app.post("/api/v1/flags", response_model=FlagResponse, status_code=status.HTTP_201_CREATED)
def create_or_update_flag(payload: FlagUpsertRequest, db: Session = Depends(get_db)):
    try:
        flag = crud.upsert_flag(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return crud.flag_to_response(flag)


@app.get("/api/v1/flags", response_model=list[FlagResponse])
def list_flags(db: Session = Depends(get_db)):
    return [crud.flag_to_response(f) for f in crud.list_flags(db)]


@app.get("/api/v1/flags/{flag_key}", response_model=FlagResponse)
def get_flag(flag_key: str, db: Session = Depends(get_db)):
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")
    return crud.flag_to_response(flag)


@app.delete("/api/v1/flags/{flag_key}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flag(flag_key: str, db: Session = Depends(get_db)) -> None:
    if not crud.delete_flag(db, flag_key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在")


def _evaluate(req: EvaluateRequest, db: Session, flag_key: str) -> EvaluateResponse:
    flag = crud.get_flag_or_none(db, flag_key)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"开关 {flag_key!r} 不存在"
        )

    request_id = req.request_id or uuid.uuid4().hex

    # 幂等：相同 request_id 直接返回首次判定快照，保证重试结果一致
    existing = crud.get_snapshot(db, request_id)
    if existing is not None:
        if existing.flag_key != flag_key or existing.user_id != req.user_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"request_id={request_id} 已用于其他判定（flag_key/user_id 不一致）",
            )
        return _replay_response(existing, flag, req)

    rules = crud.parse_rules(flag.rules_json)
    variants = crud.parse_variants(flag.variants_json)
    decision = decide(flag.flag_key, req.user_id, req.context, variants, rules)

    try:
        crud.save_snapshot(
            db,
            request_id=request_id,
            flag=flag,
            user_id=req.user_id,
            context=req.context,
            decision=decision,
        )
    except IntegrityError:
        # 并发下 request_id 唯一键冲突：回退到已存在的快照
        db.rollback()
        existing = crud.get_snapshot(db, request_id)
        if existing is not None:
            return _replay_response(existing, flag, req)
        raise

    return _decision_response(request_id, flag, req, decision)


@app.post("/api/v1/evaluate", response_model=EvaluateResponse)
def evaluate_by_body(req: EvaluateRequest, db: Session = Depends(get_db)):
    if not req.flag_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="请求体必须提供 flag_key"
        )
    return _evaluate(req, db, req.flag_key)


@app.post("/api/v1/flags/{flag_key}/evaluate", response_model=EvaluateResponse)
def evaluate_by_path(flag_key: str, req: EvaluateRequest, db: Session = Depends(get_db)):
    if req.flag_key is not None and req.flag_key != flag_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径 flag_key 与请求体 flag_key 不一致",
        )
    return _evaluate(req, db, flag_key)


@app.get("/api/v1/decisions/{request_id}")
def get_decision(request_id: str, db: Session = Depends(get_db)):
    snap = crud.get_snapshot(db, request_id)
    if snap is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"未找到 request_id={request_id} 的判定快照"
        )
    return _snapshot_to_response(snap)
