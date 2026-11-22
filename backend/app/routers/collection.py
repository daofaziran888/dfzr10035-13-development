"""采集处理接口：批量提交补采申请、分时批次派发、回执入账与恢复核对。

路由固定挂在 ``/api/collection`` 下，必须先于通用资源路由 ``/api/{module}`` 注册。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from app.schemas import ActionResult
from app.services.collection import collection_service
from app.services.windows import parse_date

router = APIRouter(prefix="/api/collection", tags=["采集处理"])


class SubmitPayload(BaseModel):
    # 兼容多种写法，缺省按端点 + 申请列表处理
    端点: str | None = None
    关联端点: str | None = None
    endpoint: str | None = None
    日期: str | None = None
    补采日期: str | None = None
    date: str | None = None
    自动派发: bool | None = None
    dispatch: bool | None = None
    申请: list[Any] | None = None
    申请列表: list[Any] | None = None
    applications: list[Any] | None = None
    缺采时段: Any = None
    windows: Any = None

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ReceiptPayload(BaseModel):
    # 回执登记：结果必填，开始/结束可只报本次实际处理的子区间
    结果: str | None = None
    result: str | None = None
    status: str | None = None
    开始时间: str | None = None
    开始: str | None = None
    start: str | None = None
    结束时间: str | None = None
    结束: str | None = None
    end: str | None = None
    返回行数: int | None = None
    rows: int | None = None
    备注: str | None = None
    remark: str | None = None
    结果回执: str | None = None

    def as_payload(self) -> dict[str, Any]:
        payload = self.model_dump(exclude_none=True)
        if "结果回执" in payload and "结果" not in payload:
            payload["结果"] = payload.pop("结果回执")
        return payload


class DispatchPayload(BaseModel):
    remark: str | None = None


class ResumePayload(BaseModel):
    端点: str | None = None
    关联端点: str | None = None
    endpoint: str | None = None
    日期: str | None = None
    date: str | None = None
    自动派发: bool | None = None
    dispatch: bool | None = None

    def as_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


@router.get("")
def root(endpoint: str | None = Query(default=None)) -> dict[str, Any]:
    """补采处理总览（兼容前端按模块直接读取）。"""
    return collection_service.summary(endpoint)


@router.get("/summary")
def summary(endpoint: str | None = Query(default=None)) -> dict[str, Any]:
    """补采全景：申请/批次/回执/已入账覆盖/待重放片段。"""
    return collection_service.summary(endpoint)


@router.get("/report")
def report(endpoint: str | None = Query(default=None)) -> dict[str, Any]:
    """补采结果、各申请回执、恢复后应执行片段的一次性核对视图。"""
    return collection_service.report(endpoint)


@router.get("/applications")
def list_applications(
    endpoint: str | None = Query(default=None),
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """补采申请清单：每条申请都能回看实际补了哪些区间。"""
    return collection_service.list_applications(
        endpoint=endpoint, status=status, keyword=keyword, page=page, size=size
    )


@router.get("/applications/{entry_id}")
def get_application(entry_id: int) -> dict[str, Any]:
    entry = collection_service.get_application(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"补采申请 {entry_id} 不存在")
    return entry


@router.post("/applications/submit")
def submit_applications(payload: SubmitPayload) -> dict[str, Any]:
    """一次提交多条申请：按实际覆盖切分分时补采批次并同步请求补采清单。"""
    try:
        return collection_service.submit(payload.as_payload())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/submit")
def submit_shortcut(payload: SubmitPayload) -> dict[str, Any]:
    return submit_applications(payload)


@router.get("/batches")
def list_batches(
    endpoint: str | None = Query(default=None),
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return collection_service.list_batches(
        endpoint=endpoint, status=status, keyword=keyword, page=page, size=size
    )


@router.get("/batches/{entry_id}")
def get_batch(entry_id: int) -> dict[str, Any]:
    entry = collection_service.get_batch(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"补采批次 {entry_id} 不存在")
    return entry


@router.post("/batches/{entry_id}/dispatch", response_model=ActionResult)
def dispatch_batch(entry_id: int, _: DispatchPayload | None = Body(default=None)) -> ActionResult:
    entry, message = collection_service.dispatch(entry_id)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/batches/{entry_id}/receipt")
def register_receipt(entry_id: int, payload: ReceiptPayload) -> ActionResult:
    """登记单段回执：成功覆盖只入账一次，剩余范围自动进入重放队列。"""
    try:
        entry, message = collection_service.register_receipt(entry_id, payload.as_payload())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/resume")
def resume(payload: ResumePayload | None = Body(default=None)) -> dict[str, Any]:
    """中断恢复：先核对已提交回执，再派发尚未覆盖的片段。"""
    options = payload.as_payload() if payload else {}
    try:
        return collection_service.resume(
            endpoint=options.get("端点") or options.get("关联端点") or options.get("endpoint"),
            date_value=options.get("日期") or options.get("date"),
            dispatch=options.get("自动派发", options.get("dispatch", True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/coverage")
def coverage(endpoint: str | None = Query(default=None)) -> dict[str, Any]:
    """已入账的成功覆盖区间（每段只记一次）。"""
    return collection_service.list_coverage(endpoint=endpoint)


@router.get("/gaps")
def gaps(
    endpoint: str = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    # 可选补采日期，缺省用当天
    date_value: str | None = Query(default=None, alias="date"),
) -> dict[str, Any]:
    """查询端点窗口内尚未覆盖的缺采时段。"""
    try:
        return collection_service.gaps(
            endpoint, start, end, base_date=parse_date(date_value) if date_value else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
