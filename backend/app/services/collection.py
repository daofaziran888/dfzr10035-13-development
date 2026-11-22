"""采集处理领域服务：把端点缺采时段按实际覆盖切分成"分时补采批次"。

业务链路：

1. 值班员一次提交多条补采申请（同一端点、可部分重叠）；
2. 服务先从已入账覆盖（collection_coverage）中扣掉已采到的区间，
   再按所有剩余区间的起止点切成互不重叠的分时片段；
3. 每个片段生成一条补采批次（collection_batch），并同步一条请求批次
   （request_batch）的"补采清单"，同一片段重复提交时复用原批次；
4. 回执按片段登记：成功区间只入账一次，重叠申请各自回看自己的实际补采时段；
5. 中断/失败/部分成功后的剩余区间进入重放队列；
6. 恢复时先核对已提交的成功回执（补记漏入账的覆盖），再派发仍未覆盖的片段。

所有时刻按左闭右开区间处理，业务状态只在本模块内流转。
"""
from __future__ import annotations

import copy
from datetime import date, datetime
from typing import Any, Iterable

from app.services.windows import (
    Interval,
    clock_text,
    interval_payload,
    intersect_intervals,
    iso_text,
    merge_intervals,
    minutes,
    parse_date,
    parse_moment,
    subtract_intervals,
    window_text,
)
from app.store import store

# 补采批次仍然可以继续派发的状态；已成功/已覆盖的片段不会被重复派发。
OPEN_BATCH_STATUS = {"待派发", "已派发", "已中断", "待重放", "部分成功"}
# 恢复时纳入核对的批次状态（已派发但没有回执的批次需要在核对后重新派发）。
RESUMABLE_BATCH_STATUS = {"待派发", "已派发", "已中断", "待重放", "部分成功"}
SUCCESS_WORDS = {"成功", "success", "succeeded", "ok", "完成"}
INTERRUPT_WORDS = {"中断", "interrupted", "interrupt", "超时", "timeout"}
FAIL_WORDS = {"失败", "failed", "fail", "error", "错误"}


def _result_of(value: Any) -> str:
    text = str(value or "成功").strip()
    lowered = text.casefold()
    if lowered in SUCCESS_WORDS or text in SUCCESS_WORDS:
        return "成功"
    if lowered in INTERRUPT_WORDS or text in INTERRUPT_WORDS:
        return "中断"
    if lowered in FAIL_WORDS or text in FAIL_WORDS:
        return "失败"
    raise ValueError(f"无法识别的回执结果：{value}")


def _texts(intervals: Iterable[Interval]) -> str:
    parts = [window_text(start, end) for start, end in intervals]
    return "、".join(parts) if parts else "—"


def _next_code(rows: list[dict[str, Any]], field: str, prefix: str) -> str:
    """按已有编号的最大序号递增，避免删除/复用后撞号。"""
    largest = 0
    for row in rows:
        value = str(row.get(field, ""))
        if value.startswith(prefix):
            suffix = value[len(prefix):].lstrip("-")
            if suffix.isdigit():
                largest = max(largest, int(suffix))
    return f"{prefix}{largest + 1:04d}"


def _status_counts(rows: list[dict[str, Any]], statuses: Iterable[str]) -> dict[str, int]:
    return {status: sum(1 for row in rows if row.get("状态") == status) for status in statuses}


class CollectionService:
    """补采申请 → 分时补采批次 → 回执入账 → 重放/恢复 的完整处理服务。"""

    # ---------------------------------------------------------------- 查询
    def list_applications(
        self,
        *,
        endpoint: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> dict[str, Any]:
        rows = list(store.rows("collection_task"))
        if endpoint:
            rows = [row for row in rows if row.get("关联端点") == endpoint]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("申请编号", "")).casefold()
                or needle in str(row.get("时段", "")).casefold()
                or needle in str(row.get("关联端点", "")).casefold()
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return {
            "items": [self.application_payload(row) for row in rows[start:start + size]],
            "total": total,
            "page": page,
            "size": size,
        }

    def get_application(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find("collection_task", entry_id)
        return self.application_payload(row) if row else None

    def list_batches(
        self,
        *,
        endpoint: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> dict[str, Any]:
        rows = list(store.rows("collection_batch"))
        if endpoint:
            rows = [row for row in rows if row.get("关联端点") == endpoint]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        if keyword:
            needle = keyword.casefold()
            rows = [
                row for row in rows
                if needle in str(row.get("批次编号", "")).casefold()
                or needle in str(row.get("时段", "")).casefold()
                or any(needle in str(code).casefold() for code in row.get("覆盖申请", []))
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        batch_page = rows[start:start + size]
        return {
            "items": [self.batch_payload(row) for row in batch_page],
            "total": total,
            "page": page,
            "size": size,
        }

    def get_batch(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find("collection_batch", entry_id)
        return self.batch_payload(row) if row else None

    def list_coverage(self, *, endpoint: str | None = None) -> dict[str, Any]:
        rows = list(store.rows("collection_coverage"))
        if endpoint:
            rows = [row for row in rows if row.get("关联端点") == endpoint]
        items = [
            {
                **interval_payload((self._moment(row["开始时间"]), self._moment(row["结束时间"]))),
                "覆盖编号": row.get("覆盖编号", ""),
                "关联端点": row.get("关联端点", ""),
                "来源批次": row.get("来源批次", ""),
                "来源回执": row.get("来源回执", ""),
                "入账时间": row.get("入账时间", ""),
            }
            for row in rows
        ]
        return {
            "items": items,
            "total": len(items),
            "覆盖分钟": sum(int(row.get("分钟", 0)) for row in rows),
        }

    def gaps(
        self,
        endpoint: str,
        start: Any,
        end: Any,
        *,
        base_date: date | None = None,
    ) -> dict[str, Any]:
        """返回端点指定窗口内尚未入账的缺采时段（按实际覆盖计算）。"""
        window = self._parse_window(start, end, base_date)
        ledger = self._ledger(endpoint)
        missing = subtract_intervals([window], ledger)
        covered = intersect_intervals([window], ledger)
        return {
            "关联端点": endpoint,
            "查询窗口": window_text(*window),
            "缺采时段": _texts(missing),
            "已覆盖时段": _texts(covered),
            "缺采区间": [interval_payload(part) for part in missing],
            "覆盖区间": [interval_payload(part) for part in covered],
            "缺采分钟": minutes(missing),
        }

    def summary(self, endpoint: str | None = None) -> dict[str, Any]:
        def scoped(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
            if not endpoint:
                return list(rows)
            return [row for row in rows if row.get(field) == endpoint]

        tasks = scoped(store.rows("collection_task"), "关联端点")
        batches = scoped(store.rows("collection_batch"), "关联端点")
        receipts = scoped(store.rows("collection_receipt"), "关联端点")
        ledger = scoped(store.rows("collection_coverage"), "关联端点")
        retries = [
            row for row in scoped(store.rows("retry_queue"), "失败端点")
            if str(row.get("队列状态", "")).startswith("补采")
        ]
        return {
            "申请总数": len(tasks),
            "申请状态": _status_counts(tasks, ["待处理", "已切分", "补采中", "已完成", "部分失败", "已中断"]),
            "批次总数": len(batches),
            "批次状态": _status_counts(
                batches, ["待派发", "已派发", "已成功", "已覆盖", "部分成功", "待重放"]
            ),
            "回执总数": len(receipts),
            "成功回执": sum(1 for row in receipts if row.get("结果") == "成功"),
            "覆盖区间数": len(ledger),
            "已入账覆盖分钟": sum(int(row.get("分钟", 0)) for row in ledger),
            "待重放片段": sum(1 for row in retries if row.get("status") in ("待重试", "重试中")),
            "覆盖明细": [
                {
                    **interval_payload((self._moment(row["开始时间"]), self._moment(row["结束时间"]))),
                    "关联端点": row.get("关联端点", ""),
                    "来源批次": row.get("来源批次", ""),
                    "来源回执": row.get("来源回执", ""),
                }
                for row in ledger
            ],
        }

    def report(self, endpoint: str | None = None) -> dict[str, Any]:
        """一次看清补采结果、各申请回执以及恢复后应执行的片段。"""
        tasks = [
            self.application_payload(row) for row in store.rows("collection_task")
            if not endpoint or row.get("关联端点") == endpoint
        ]
        batches = [
            self.batch_payload(row) for row in store.rows("collection_batch")
            if not endpoint or row.get("关联端点") == endpoint
        ]
        receipts = [
            copy.deepcopy(row) for row in store.rows("collection_receipt")
            if not endpoint or row.get("关联端点") == endpoint
        ]
        pending: list[dict[str, Any]] = []
        for batch in batches:
            ledger = self._ledger(batch.get("关联端点", ""))
            remaining = subtract_intervals([self._batch_interval(batch)], ledger)
            for part in remaining:
                pending.append({
                    "id": batch.get("id"),
                    "批次编号": batch["批次编号"],
                    "时段": window_text(*part),
                    "开始时间": iso_text(part[0]),
                    "结束时间": iso_text(part[1]),
                    "状态": batch.get("状态", ""),
                    "覆盖申请": list(batch.get("覆盖申请", [])),
                    "请求批次编号": batch.get("请求批次编号", ""),
                })
        request_batches = [
            copy.deepcopy(row) for row in store.rows("request_batch")
            if str(row.get("补采来源", "")) == "采集处理"
            and (not endpoint or row.get("关联端点") == endpoint)
        ]
        retries = [
            copy.deepcopy(row) for row in store.rows("retry_queue")
            if str(row.get("队列状态", "")).startswith("补采")
            and (not endpoint or row.get("失败端点") == endpoint)
        ]
        return {
            "关联端点": endpoint or "全部",
            "补采结果": self.summary(endpoint),
            "分时批次": batches,
            "各申请回执": [
                {
                    "申请编号": task["申请编号"],
                    "申请时段": task["时段"],
                    "状态": task["状态"],
                    "已覆盖时段": task["已覆盖时段"],
                    "未覆盖时段": task["未覆盖时段"],
                    "回执": task["回执"],
                    "实际补采时段": task["实际补采时段"],
                }
                for task in tasks
            ],
            "回执明细": receipts,
            "恢复后应执行片段": pending,
            "请求批次补采清单": request_batches,
            "重放队列": retries,
        }

    def application_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        """申请回看视图：实际补了哪些区间、对应哪些分时批次和请求批次。"""
        payload = copy.deepcopy(row)
        payload["实际补采时段"] = [
            dict(item) for item in payload.get("实际补采时段", [])
        ]
        payload["回执"] = [dict(item) for item in payload.get("回执", [])]
        batch_codes = set(payload.get("批次编号", []))
        batches = [
            item for item in store.rows("collection_batch")
            if item.get("批次编号") in batch_codes
        ]
        payload["分时批次"] = [
            {
                "批次编号": item.get("批次编号", ""),
                "时段": item.get("时段", ""),
                "状态": item.get("状态", ""),
                "结果": item.get("结果", ""),
                "已覆盖时段": item.get("已覆盖时段", "—"),
                "未覆盖时段": item.get("未覆盖时段", ""),
                "请求批次编号": item.get("请求批次编号", ""),
            }
            for item in batches
        ]
        payload["补采清单"] = [
            dict(entry)
            for item in batches
            for entry in self._request_batch_entries(item)
        ]
        return payload

    def _request_batch_entries(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        if not batch.get("请求批次ID"):
            return []
        row = store.find("request_batch", int(batch["请求批次ID"]))
        if row is None:
            return []
        return [
            entry for entry in row.get("补采清单", [])
            if entry.get("批次编号") == batch.get("批次编号")
        ]

    def batch_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(row)
        payload["回执明细"] = [
            copy.deepcopy(receipt)
            for receipt in store.rows("collection_receipt")
            if receipt.get("批次编号") == row.get("批次编号")
        ]
        return payload

    # ---------------------------------------------------------------- 提交
    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """一次提交多条补采申请：切分 → 建分时批次 → 同步补采清单 → 派发。"""
        endpoint = str(payload.get("端点") or payload.get("关联端点") or payload.get("endpoint") or "").strip()
        raw_date = payload.get("日期") or payload.get("补采日期") or payload.get("date")
        base_date = parse_date(raw_date) if raw_date not in (None, "") else None
        auto_dispatch = payload.get(
            "自动派发", payload.get("dispatch", payload.get("auto_dispatch", True))
        )
        specs = payload.get("申请") or payload.get("applications") or payload.get("申请列表")
        if specs is None:
            specs = payload.get("缺采时段") or payload.get("windows") or payload.get("时段")
        if isinstance(specs, dict):
            specs = [specs]
        if not specs:
            raise ValueError("请至少提交一条补采申请")

        tasks: list[dict[str, Any]] = []
        reused_applications: list[str] = []
        submitted_codes: set[str] = set()
        for spec in specs:
            if isinstance(spec, (int, str)):
                task = self._find_application(spec)
                if task is None:
                    raise ValueError(f"补采申请不存在：{spec}")
                tasks.append(task)
                continue
            if not isinstance(spec, dict):
                raise ValueError("补采申请格式应为对象或申请编号")
            if spec.get("id") or spec.get("申请编号"):
                task = self._find_application(spec.get("id") or spec.get("申请编号"))
                if task is None:
                    raise ValueError(f"补采申请不存在：{spec.get('申请编号') or spec.get('id')}")
                tasks.append(task)
                continue
            candidate = self._create_application(spec, endpoint, base_date)
            existing = self._find_equivalent_application(candidate)
            if existing is not None:
                # 同一端点同一时段重复提交：复用原申请，避免重复建单。
                reused_applications.append(existing["申请编号"])
                task = existing
            else:
                store.rows("collection_task").append(candidate)
                task = candidate
            tasks.append(task)
        # 同一次提交里重复勾选同一申请只处理一次。
        deduped: list[dict[str, Any]] = []
        for task in tasks:
            if task["申请编号"] in submitted_codes:
                continue
            submitted_codes.add(task["申请编号"])
            deduped.append(task)
        tasks = deduped

        if not tasks:
            raise ValueError("请至少提交一条补采申请")
        task_endpoints = {str(task.get("关联端点") or "") for task in tasks}
        if len(task_endpoints) > 1:
            raise ValueError("一次提交的补采申请必须属于同一端点")
        task_endpoint = next(iter(task_endpoints))
        if endpoint and task_endpoint and endpoint != task_endpoint:
            raise ValueError(f"申请端点 {task_endpoint} 与提交端点 {endpoint} 不一致")
        endpoint = endpoint or task_endpoint
        if not endpoint:
            raise ValueError("补采申请缺少关联端点")

        # 先核对既有成功回执，避免重复入账，也避免把已覆盖片段再次切出来。
        reconciled = self._reconcile_success_receipts(endpoint)
        plan = self._plan_segments(endpoint, tasks)
        created: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []
        for segment in plan:
            start, end, cover_codes = segment
            batch = self._find_open_batch(endpoint, start, end)
            if batch is None:
                batch = self._create_batch(endpoint, start, end, cover_codes)
                created.append(batch)
            else:
                added = [code for code in cover_codes if code not in batch.get("覆盖申请", [])]
                if added:
                    batch.setdefault("覆盖申请", []).extend(added)
                    batch.setdefault("覆盖申请ID", []).extend(
                        task["id"] for task in tasks if task.get("申请编号") in added
                    )
                reused.append(batch)
            for task in tasks:
                if task.get("申请编号") in cover_codes:
                    codes = task.setdefault("批次编号", [])
                    if batch["批次编号"] not in codes:
                        codes.append(batch["批次编号"])
            self._refresh_batch(batch)

        for task in tasks:
            self._refresh_task(task)

        dispatched: list[dict[str, Any]] = []
        if auto_dispatch:
            for batch in [*created, *reused]:
                # 重复提交时只补派尚未成功的片段；已成功/已覆盖的不再请求。
                if batch.get("状态") in ("待派发", "待重放", "已中断"):
                    self._dispatch(batch)
                    dispatched.append(batch)

        segments = [
            {
                "批次编号": batch["批次编号"],
                "时段": batch["时段"],
                "开始时间": batch["开始时间"],
                "结束时间": batch["结束时间"],
                "覆盖申请": list(batch.get("覆盖申请", [])),
                "状态": batch.get("状态", ""),
                "请求批次编号": batch.get("请求批次编号", ""),
                "新建": batch in created,
            }
            for batch in [*created, *reused]
        ]
        if not segments:
            message = "选中申请已全部覆盖，无需重复补采"
        else:
            message = (
                f"已切分{len(segments)}个分时补采批次"
                f"（新建{len(created)}个、复用{len(reused)}个）"
            )
            if dispatched:
                message += f"，已派发{len(dispatched)}个"
            if reconciled:
                message += f"，核对补记成功回执{reconciled}条"

        if reused_applications:
            message += f"，复用已有申请{len(reused_applications)}条"
        return {
            "ok": True,
            "message": message,
            "关联端点": endpoint,
            "reconciled_receipts": reconciled,
            "reused_applications": reused_applications,
            "applications": [self.application_payload(task) for task in tasks],
            "batches": [self.batch_payload(batch) for batch in [*created, *reused]],
            "segments": segments,
            "request_batches": [
                self._request_batch_payload(batch) for batch in [*created, *reused]
            ],
            "summary": self.summary(endpoint),
        }

    # ---------------------------------------------------------------- 派发
    def dispatch(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        batch = store.find("collection_batch", batch_id)
        if batch is None:
            return None, f"补采批次 {batch_id} 不存在"
        if batch.get("状态") == "已成功":
            return self.batch_payload(batch), f"批次{batch['批次编号']}已成功，无需重复派发"
        if batch.get("状态") in ("已覆盖", "已取消"):
            return self.batch_payload(batch), f"批次{batch['批次编号']}状态为{batch['状态']}，不派发"
        if batch.get("状态") == "已派发":
            return self.batch_payload(batch), f"批次{batch['批次编号']}已派发，等待回执"
        ledger = self._ledger(batch.get("关联端点", ""))
        remaining = subtract_intervals([self._batch_interval(batch)], ledger)
        if not remaining:
            self._refresh_batch(batch)
            self._refresh_request_batch(batch)
            self._reconcile_retries(batch.get("关联端点", ""))
            return self.batch_payload(batch), f"核对回执后批次{batch['批次编号']}已全部覆盖，跳过派发"
        if remaining == [self._batch_interval(batch)]:
            self._dispatch(batch)
            return self.batch_payload(batch), f"批次{batch['批次编号']}已派发"
        # 已有部分覆盖：只对未覆盖片段建/复用批次派发，避免重复请求已入账数据。
        children = [
            self._ensure_batch(batch.get("关联端点", ""), part, list(batch.get("覆盖申请", [])))
            for part in remaining
        ]
        for child in children:
            self._dispatch(child)
        self._refresh_batch(batch)
        self._refresh_request_batch(batch)
        self._reconcile_retries(batch.get("关联端点", ""))
        return self.batch_payload(batch), (
            "批次{}仅派发未覆盖片段{}".format(
                batch["批次编号"], "、".join(child["时段"] for child in children)
            )
        )

    def _dispatch(self, batch: dict[str, Any]) -> dict[str, Any]:
        if batch.get("状态") == "已成功":
            return batch
        batch["状态"] = "已派发"
        batch["派发时间"] = self._now()
        batch["在途"] = True
        batch["pending"] = True
        batch["abnormal"] = False
        for row in store.rows("retry_queue"):
            if (
                row.get("失败端点") == batch.get("关联端点")
                and str(row.get("队列状态", "")).startswith("补采")
                and row.get("开始时间") == batch.get("开始时间")
                and row.get("结束时间") == batch.get("结束时间")
            ):
                row["status"] = "重试中"
                row["最后重试"] = self._now()
        self._refresh_request_batch(batch)
        self._sync_request_batch_state(batch, "执行中")
        self._refresh_tasks_for_batch(batch)
        return batch

    # ---------------------------------------------------------------- 回执
    def register_receipt(
        self,
        batch_id: int,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        batch = store.find("collection_batch", batch_id)
        if batch is None:
            return None, f"补采批次 {batch_id} 不存在"
        result = _result_of(payload.get("结果") or payload.get("result") or payload.get("status"))
        batch_interval = self._batch_interval(batch)
        start = payload.get("开始时间") or payload.get("start") or payload.get("开始")
        end = payload.get("结束时间") or payload.get("end") or payload.get("结束")
        base_date = batch_interval[0].date()
        receipt_interval = (
            self._parse_window(start, end, base_date) if start or end else batch_interval
        )
        clipped = intersect_intervals([receipt_interval], [batch_interval])
        if not clipped:
            raise ValueError("回执时段与批次时段没有交集")
        receipt_interval = clipped[0]
        rows = payload.get("返回行数") or payload.get("rows") or 0
        remark = str(payload.get("备注") or payload.get("remark") or "")

        existing = next(
            (
                row for row in store.rows("collection_receipt")
                if row.get("批次编号") == batch["批次编号"]
                and row.get("开始时间") == iso_text(receipt_interval[0])
                and row.get("结束时间") == iso_text(receipt_interval[1])
                and row.get("结果") == result
            ),
            None,
        )
        if existing is not None:
            self._refresh_batch(batch)
            self._refresh_request_batch(batch)
            self._refresh_tasks_for_batch(batch)
            return self.batch_payload(batch), (
                f"回执{existing['回执编号']}已登记过，重复提交不重复入账"
            )

        code = _next_code(store.rows("collection_receipt"), "回执编号", "BFRC-")
        new_parts: list[Interval] = []
        if result == "成功":
            new_parts = self._book_coverage(
                batch.get("关联端点", ""), receipt_interval, batch, code
            )
        receipt = {
            "id": max((int(row.get("id", 0)) for row in store.rows("collection_receipt")), default=0) + 1,
            "回执编号": code,
            "批次编号": batch["批次编号"],
            "关联端点": batch.get("关联端点", ""),
            "开始时间": iso_text(receipt_interval[0]),
            "结束时间": iso_text(receipt_interval[1]),
            "时段": window_text(*receipt_interval),
            "结果": result,
            "返回行数": rows,
            "备注": remark,
            "覆盖申请": list(batch.get("覆盖申请", [])),
            "入账区间": [interval_payload(part) for part in new_parts],
            "入账分钟": minutes(new_parts),
            "提交时间": self._now(),
        }
        store.rows("collection_receipt").append(receipt)
        batch["回执编号"] = code
        batch["结果"] = result
        batch["在途"] = False
        batch["最后回执时间"] = receipt["提交时间"]

        self._record_attempts(batch, receipt, new_parts)
        self._refresh_batch(batch)
        self._refresh_tasks_for_batch(batch)
        self._refresh_request_batch(batch)
        self._sync_request_batch_state(batch)

        # 剩余未覆盖范围进重放队列；成功覆盖的部分关闭对应重放任务。
        remaining = subtract_intervals([batch_interval], self._ledger(batch.get("关联端点", "")))
        if remaining:
            for part in remaining:
                self._enqueue_retry(batch, part)
        self._reconcile_retries(batch.get("关联端点", ""))

        if result == "成功":
            if remaining:
                message = (
                    f"回执{code}成功入账{minutes(new_parts)}分钟，"
                    f"批次{batch['批次编号']}仍缺{_texts(remaining)}，已进入重放队列"
                )
            else:
                message = f"回执{code}成功，批次{batch['批次编号']}已全部覆盖"
        else:
            message = (
                f"回执{code}登记为{result}，批次{batch['批次编号']}未覆盖"
                f"{_texts(remaining)}，已进入重放队列"
            )
        return self.batch_payload(batch), message

    def _book_coverage(
        self,
        endpoint: str,
        interval: Interval,
        batch: dict[str, Any],
        receipt_code: str,
    ) -> list[Interval]:
        """只把尚未入账的部分写入覆盖账本，保证成功数据只计一次。"""
        ledger = self._ledger(endpoint)
        new_parts = subtract_intervals([interval], ledger)
        rows = store.rows("collection_coverage")
        for start, end in new_parts:
            rows.append({
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "覆盖编号": _next_code(rows, "覆盖编号", "BFCOV-"),
                "关联端点": endpoint,
                "开始时间": iso_text(start),
                "结束时间": iso_text(end),
                "时段": window_text(start, end),
                "来源批次": batch.get("批次编号", ""),
                "来源回执": receipt_code,
                "分钟": minutes([(start, end)]),
                "入账时间": self._now(),
            })
        return new_parts

    # ---------------------------------------------------------------- 恢复
    def resume(
        self,
        *,
        endpoint: str | None = None,
        date_value: Any = None,
        dispatch: bool = True,
    ) -> dict[str, Any]:
        """中断后续做：先核对已提交回执并补记覆盖，再派发未覆盖片段。"""
        target_date = parse_date(date_value) if date_value not in (None, "") else None
        reconciled = self._reconcile_success_receipts(endpoint)
        batches = [
            row for row in store.rows("collection_batch")
            if row.get("状态") in RESUMABLE_BATCH_STATUS
            and (not endpoint or row.get("关联端点") == endpoint)
            and (target_date is None or self._batch_interval(row)[0].date() == target_date)
        ]
        checked_receipts = sum(
            1 for row in store.rows("collection_receipt")
            if (not endpoint or row.get("关联端点") == endpoint)
            and (target_date is None or self._moment(row["开始时间"]).date() == target_date)
        )
        executed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        # 同一剩余区间可能同时被父批次和已建子批次命中，按区间去重后只派发一次。
        targets: dict[tuple[str, str, str], dict[str, Any]] = {}
        for batch in batches:
            ledger = self._ledger(batch.get("关联端点", ""))
            remaining = subtract_intervals([self._batch_interval(batch)], ledger)
            if not remaining:
                self._refresh_batch(batch)
                self._refresh_request_batch(batch)
                skipped.append({
                    "id": batch.get("id"),
                    "批次编号": batch["批次编号"],
                    "时段": batch["时段"],
                    "原因": "回执核对后已全部覆盖",
                })
                continue
            for part in remaining:
                key = (batch.get("关联端点", ""), iso_text(part[0]), iso_text(part[1]))
                existing = self._find_open_batch(batch.get("关联端点", ""), part[0], part[1])
                if existing is not None and (
                    key not in targets
                    or existing["批次编号"] > targets[key]["批次编号"]
                ):
                    targets[key] = existing
                elif key not in targets:
                    targets[key] = self._ensure_batch(
                        batch.get("关联端点", ""), part, list(batch.get("覆盖申请", []))
                    )
            self._refresh_batch(batch)
            self._refresh_request_batch(batch)
        for target in targets.values():
            if target.get("状态") in ("已成功", "已覆盖"):
                skipped.append({
                    "id": target.get("id"),
                    "批次编号": target["批次编号"],
                    "时段": target["时段"],
                    "原因": "已覆盖",
                })
                continue
            if target.get("状态") == "已派发":
                # 已经在途的片段不重复派发，等回执后再核对。
                skipped.append({
                    "id": target.get("id"),
                    "批次编号": target["批次编号"],
                    "时段": target["时段"],
                    "原因": "已派发待回执",
                })
                continue
            if dispatch:
                self._dispatch(target)
            executed.append({
                "id": target.get("id"),
                "批次编号": target["批次编号"],
                "时段": target["时段"],
                "开始时间": target["开始时间"],
                "结束时间": target["结束时间"],
                "覆盖申请": list(target.get("覆盖申请", [])),
                "状态": target.get("状态", ""),
                "请求批次编号": target.get("请求批次编号", ""),
            })
        endpoints = {endpoint} if endpoint else {
            row.get("关联端点") for row in store.rows("collection_batch")
        }
        for item in endpoints:
            if item:
                self._reconcile_retries(item)
        for task in store.rows("collection_task"):
            if not endpoint or task.get("关联端点") == endpoint:
                self._refresh_task(task)
        message = (
            f"核对回执{checked_receipts}条、补记覆盖{reconciled}段"
            f"，应执行{len(executed)}个片段、跳过{len(skipped)}个已覆盖片段"
        )
        return {
            "ok": True,
            "message": message,
            "核对回执": checked_receipts,
            "补记覆盖": reconciled,
            "应执行片段": executed,
            "跳过片段": skipped,
            "applications": [
                self.application_payload(task)
                for task in store.rows("collection_task")
                if not endpoint or task.get("关联端点") == endpoint
            ],
            "summary": self.summary(),
        }

    # ---------------------------------------------------------------- 内部：申请/批次
    def _create_application(
        self,
        spec: dict[str, Any],
        endpoint: str,
        base_date: date | None,
    ) -> dict[str, Any]:
        start = spec.get("开始时间") or spec.get("开始") or spec.get("start")
        end = spec.get("结束时间") or spec.get("结束") or spec.get("end")
        if not start or not end:
            raise ValueError("补采申请必须同时提供开始时间和结束时间")
        interval = self._parse_window(start, end, base_date)
        task_endpoint = str(
            spec.get("关联端点") or spec.get("端点") or endpoint or ""
        ).strip()
        if not task_endpoint:
            raise ValueError("补采申请缺少关联端点")
        if endpoint and task_endpoint != endpoint:
            raise ValueError("同一批补采申请必须属于同一端点")
        rows = store.rows("collection_task")
        code = _next_code(rows, "申请编号", "BFAP-")
        task = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "申请编号": code,
            "关联端点": task_endpoint,
            "补采日期": interval[0].date().isoformat(),
            "开始时间": iso_text(interval[0]),
            "结束时间": iso_text(interval[1]),
            "时段": window_text(*interval),
            "申请分钟数": minutes([interval]),
            "已覆盖时段": "—",
            "未覆盖时段": window_text(*interval),
            "覆盖分钟数": 0,
            "已覆盖区间": [],
            "未覆盖区间": [interval_payload(interval)],
            "实际补采时段": [],
            "回执": [],
            "批次编号": [],
            "状态": "待处理",
            "pending": True,
            "abnormal": False,
            "创建时间": self._now(),
            "最后更新": self._now(),
        }
        return task

    def _find_equivalent_application(
        self,
        candidate: dict[str, Any],
    ) -> dict[str, Any] | None:
        for row in store.rows("collection_task"):
            if (
                row.get("关联端点") == candidate.get("关联端点")
                and row.get("开始时间") == candidate.get("开始时间")
                and row.get("结束时间") == candidate.get("结束时间")
            ):
                return row
        return None

    def _find_application(self, key: Any) -> dict[str, Any] | None:
        text = str(key)
        for row in store.rows("collection_task"):
            if text in (str(row.get("id")), str(row.get("申请编号"))):
                return row
        return None

    def _create_batch(
        self,
        endpoint: str,
        start: datetime,
        end: datetime,
        cover_codes: list[str],
    ) -> dict[str, Any]:
        rows = store.rows("collection_batch")
        code = _next_code(rows, "批次编号", "BFB-")
        task_ids = [
            task["id"] for task in store.rows("collection_task")
            if task.get("申请编号") in cover_codes
        ]
        batch = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "批次编号": code,
            "关联端点": endpoint,
            "开始时间": iso_text(start),
            "结束时间": iso_text(end),
            "时段": window_text(start, end),
            "覆盖申请": list(dict.fromkeys(cover_codes)),
            "覆盖申请ID": task_ids,
            "状态": "待派发",
            "结果": "",
            "在途": False,
            "补采结论": "未开始",
            "回执编号": "",
            "回执数量": 0,
            "已覆盖时段": "—",
            "未覆盖时段": window_text(start, end),
            "入账分钟": 0,
            "派发时间": "",
            "创建时间": self._now(),
            "最后更新": self._now(),
            "pending": True,
            "abnormal": False,
            "请求批次ID": None,
            "请求批次编号": "",
        }
        rows.append(batch)
        self._create_request_batch(batch)
        return batch

    def _ensure_batch(
        self,
        endpoint: str,
        interval: Interval,
        cover_codes: list[str],
    ) -> dict[str, Any]:
        batch = self._find_open_batch(endpoint, interval[0], interval[1])
        if batch is None:
            batch = self._create_batch(endpoint, interval[0], interval[1], cover_codes)
        for code in cover_codes:
            if code not in batch.setdefault("覆盖申请", []):
                batch["覆盖申请"].append(code)
        for task in store.rows("collection_task"):
            if task.get("申请编号") not in batch.get("覆盖申请", []):
                continue
            codes = task.setdefault("批次编号", [])
            if batch["批次编号"] not in codes:
                codes.append(batch["批次编号"])
            self._refresh_task(task)
        self._refresh_batch(batch)
        self._refresh_request_batch(batch)
        return batch

    def _find_open_batch(
        self,
        endpoint: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any] | None:
        for row in store.rows("collection_batch"):
            if (
                row.get("关联端点") == endpoint
                and row.get("开始时间") == iso_text(start)
                and row.get("结束时间") == iso_text(end)
                and row.get("状态") in OPEN_BATCH_STATUS
            ):
                return row
        return None

    def _plan_segments(
        self,
        endpoint: str,
        tasks: list[dict[str, Any]],
    ) -> list[tuple[datetime, datetime, list[str]]]:
        """按实际覆盖切分：剩余区间起止点做边界，重叠申请合并到同一片段。"""
        ledger = self._ledger(endpoint)
        remaining: dict[str, list[Interval]] = {}
        boundaries: set[datetime] = set()
        for task in tasks:
            window = self._task_interval(task)
            parts = subtract_intervals([window], ledger)
            remaining[task["申请编号"]] = parts
            for start, end in parts:
                boundaries.add(start)
                boundaries.add(end)
        ordered = sorted(boundaries)
        segments: list[tuple[datetime, datetime, list[str]]] = []
        for start, end in zip(ordered, ordered[1:]):
            if end <= start:
                continue
            covers = [
                code for code, parts in remaining.items()
                if any(part_start <= start and part_end >= end for part_start, part_end in parts)
            ]
            if covers:
                segments.append((start, end, covers))
        return segments

    def _refresh_task(self, task: dict[str, Any]) -> None:
        window = self._task_interval(task)
        ledger = self._ledger(task.get("关联端点", ""))
        covered = intersect_intervals([window], ledger)
        remaining = subtract_intervals([window], ledger)
        task["已覆盖区间"] = [interval_payload(part) for part in covered]
        task["未覆盖区间"] = [interval_payload(part) for part in remaining]
        task["已覆盖时段"] = _texts(covered)
        task["未覆盖时段"] = _texts(remaining)
        task["覆盖分钟数"] = minutes(covered)
        task["最后更新"] = self._now()
        attempts = task.get("实际补采时段", [])
        if not remaining:
            task["状态"] = "已完成"
        elif covered:
            task["状态"] = "部分失败"
        elif attempts:
            task["状态"] = "已中断"
        elif task.get("批次编号"):
            codes = set(task.get("批次编号", []))
            batches = [
                row for row in store.rows("collection_batch")
                if row.get("批次编号") in codes
            ]
            task["状态"] = "补采中" if any(
                row.get("状态") in ("已派发", "部分成功", "待重放") for row in batches
            ) else "已切分"
        else:
            task["状态"] = "待处理"
        task["pending"] = task["状态"] not in ("已完成", "已取消")
        task["abnormal"] = task["状态"] in ("部分失败", "已中断")

    def _refresh_tasks_for_batch(self, batch: dict[str, Any]) -> None:
        codes = set(batch.get("覆盖申请", []))
        for task in store.rows("collection_task"):
            if task.get("关联端点") == batch.get("关联端点") or task.get("申请编号") in codes:
                self._refresh_task(task)

    def _refresh_batch(self, batch: dict[str, Any]) -> None:
        interval = self._batch_interval(batch)
        ledger = self._ledger(batch.get("关联端点", ""))
        covered = intersect_intervals([interval], ledger)
        remaining = subtract_intervals([interval], ledger)
        batch["已覆盖时段"] = _texts(covered)
        batch["未覆盖时段"] = _texts(remaining)
        batch["入账分钟"] = minutes(covered)
        batch["回执数量"] = sum(
            1 for row in store.rows("collection_receipt")
            if row.get("批次编号") == batch.get("批次编号")
        )
        if not remaining:
            if batch.get("结果") in ("成功", ""):
                batch["补采结论"] = "已成功"
            else:
                batch["补采结论"] = "已补全"
        elif covered:
            batch["补采结论"] = "部分成功"
        elif batch.get("回执编号"):
            batch["补采结论"] = f"未覆盖（{batch.get('结果')}）"
        else:
            batch["补采结论"] = "未开始"
        batch["最后更新"] = self._now()
        if not remaining:
            batch["状态"] = "已成功" if batch.get("回执编号") else "已覆盖"
            batch["在途"] = False
            batch["pending"] = False
            batch["abnormal"] = False
        elif batch.get("在途"):
            # 在途批次保持已派发，等回执回来再判定成功/重放。
            batch["状态"] = "已派发"
            batch["pending"] = True
            batch["abnormal"] = False
        elif covered:
            batch["状态"] = "部分成功"
            batch["pending"] = True
            batch["abnormal"] = True
        elif batch.get("回执编号") or batch.get("状态") in ("已中断", "待重放", "部分成功"):
            batch["状态"] = "待重放"
            batch["pending"] = True
            batch["abnormal"] = True
        elif batch.get("状态") not in ("已成功", "已覆盖"):
            batch["状态"] = "待派发"
        self._refresh_request_batch(batch)

    # ---------------------------------------------------------------- 内部：请求批次同步
    def _create_request_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows("request_batch")
        rb_code = _next_code(rows, "批次编号", "REQU-BF-")
        row = {
            "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
            "批次编号": rb_code,
            "关联端点": batch.get("关联端点", ""),
            "参数模板": f"backfill:{batch.get('时段', '')}",
            "请求数量": len(batch.get("覆盖申请", [])),
            "并发控制": "1",
            "超时上限": 30000,
            "完成数量": 0,
            "失败数量": 0,
            "补采批次": batch.get("批次编号", ""),
            "补采清单": [self._backfill_entry(batch)],
            "补采来源": "采集处理",
            "status": "待执行",
            "pending": True,
            "abnormal": False,
        }
        rows.append(row)
        batch["请求批次ID"] = row["id"]
        batch["请求批次编号"] = rb_code
        return row

    def _backfill_entry(self, batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "批次编号": batch.get("批次编号", ""),
            "时段": batch.get("时段", ""),
            "开始时间": batch.get("开始时间", ""),
            "结束时间": batch.get("结束时间", ""),
            "覆盖申请": list(batch.get("覆盖申请", [])),
            "状态": batch.get("状态", ""),
            "结果": batch.get("结果", ""),
            "回执编号": batch.get("回执编号", ""),
            "已覆盖时段": batch.get("已覆盖时段", "—"),
            "未覆盖时段": batch.get("未覆盖时段", ""),
            "入账分钟": batch.get("入账分钟", 0),
        }

    def _refresh_request_batch(self, batch: dict[str, Any]) -> None:
        request_batch = None
        if batch.get("请求批次ID"):
            request_batch = store.find("request_batch", int(batch["请求批次ID"]))
        if request_batch is None:
            request_batch = self._create_request_batch(batch)
        entries = request_batch.setdefault("补采清单", [])
        entry = next(
            (item for item in entries if item.get("批次编号") == batch.get("批次编号")), None
        )
        if entry is None:
            entry = self._backfill_entry(batch)
            entries.append(entry)
        else:
            entry.update(self._backfill_entry(batch))
        request_batch["补采批次"] = batch.get("批次编号", "")
        request_batch["关联端点"] = batch.get("关联端点", "")
        request_batch["请求数量"] = len(batch.get("覆盖申请", []))
        # 完成数量按"本片段内各申请是否已覆盖"逐段统计，不跨片段串账。
        segment_covered = intersect_intervals(
            [self._batch_interval(batch)], self._ledger(batch.get("关联端点", ""))
        )
        done = 0
        for code in batch.get("覆盖申请", []):
            task = next(
                (
                    row for row in store.rows("collection_task")
                    if row.get("申请编号") == code
                ),
                None,
            )
            if task is None:
                continue
            pieces = intersect_intervals([self._task_interval(task)], [self._batch_interval(batch)])
            if pieces and not subtract_intervals(pieces, segment_covered):
                done += 1
        request_batch["完成数量"] = done
        request_batch["失败数量"] = max(0, request_batch["请求数量"] - done)
        if batch.get("状态") in ("已成功", "已覆盖"):
            request_batch["status"] = "已完成"
            request_batch["pending"] = False
            request_batch["abnormal"] = False
        elif batch.get("状态") in ("待重放", "已中断", "部分成功"):
            request_batch["status"] = "部分失败"
            request_batch["pending"] = True
            request_batch["abnormal"] = True
        elif batch.get("状态") == "已派发":
            request_batch["status"] = "执行中"
            request_batch["pending"] = True
            request_batch["abnormal"] = False
        else:
            request_batch["status"] = "待执行"
            request_batch["pending"] = True
            request_batch["abnormal"] = False
        for key in ("pending", "abnormal", "完成数量", "失败数量", "补采清单"):
            batch[key] = request_batch[key]

    def _sync_request_batch_state(self, batch: dict[str, Any], fallback: str = "执行中") -> None:
        if batch.get("请求批次ID"):
            row = store.find("request_batch", int(batch["请求批次ID"]))
            if row is not None and not row.get("补采清单"):
                row["status"] = fallback

    def _request_batch_payload(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        if batch.get("请求批次ID"):
            row = store.find("request_batch", int(batch["请求批次ID"]))
            if row is not None:
                return copy.deepcopy(row)
        return None

    # ---------------------------------------------------------------- 内部：回执/重放
    def _record_attempts(
        self,
        batch: dict[str, Any],
        receipt: dict[str, Any],
        new_parts: list[Interval],
    ) -> None:
        receipt_interval = (self._moment(receipt["开始时间"]), self._moment(receipt["结束时间"]))
        codes = set(batch.get("覆盖申请", []))
        for task in store.rows("collection_task"):
            if task.get("申请编号") not in codes:
                continue
            pieces = intersect_intervals([self._task_interval(task)], [receipt_interval])
            attempts = task.setdefault("实际补采时段", [])
            for start, end in pieces:
                already = any(
                    item.get("回执编号") == receipt["回执编号"]
                    and item.get("开始时间") == iso_text(start)
                    and item.get("结束时间") == iso_text(end)
                    for item in attempts
                )
                if already:
                    continue
                ledger = self._ledger(task.get("关联端点", ""))
                fully_covered = not subtract_intervals([(start, end)], ledger)
                booked = minutes(intersect_intervals([(start, end)], new_parts))
                attempts.append({
                    "回执编号": receipt["回执编号"],
                    "批次编号": batch["批次编号"],
                    "时段": window_text(start, end),
                    "开始时间": iso_text(start),
                    "结束时间": iso_text(end),
                    "结果": receipt["结果"],
                    "入账": booked > 0,
                    "已覆盖": fully_covered,
                    "入账分钟": booked,
                    "返回行数": receipt.get("返回行数", 0),
                    "提交时间": receipt["提交时间"],
                })
                attempts.sort(key=lambda item: item.get("开始时间", ""))
            receipts = task.setdefault("回执", [])
            if not any(item.get("回执编号") == receipt["回执编号"] for item in receipts):
                receipts.append({
                    "回执编号": receipt["回执编号"],
                    "批次编号": batch["批次编号"],
                    "时段": receipt["时段"],
                    "结果": receipt["结果"],
                    "入账分钟": receipt.get("入账分钟", 0),
                    "提交时间": receipt["提交时间"],
                })

    def _reconcile_success_receipts(self, endpoint: str | None = None) -> int:
        """补记成功回执里尚未入账的覆盖（用于中断后恢复核对）。"""
        added = 0
        for receipt in list(store.rows("collection_receipt")):
            if receipt.get("结果") != "成功":
                continue
            if endpoint and receipt.get("关联端点") != endpoint:
                continue
            batch = next(
                (
                    row for row in store.rows("collection_batch")
                    if row.get("批次编号") == receipt.get("批次编号")
                ),
                None,
            )
            if batch is None:
                batch = {
                    "批次编号": receipt.get("批次编号", ""),
                    "关联端点": receipt.get("关联端点", ""),
                }
            interval = (self._moment(receipt["开始时间"]), self._moment(receipt["结束时间"]))
            new_parts = self._book_coverage(
                receipt.get("关联端点", ""), interval, batch, receipt.get("回执编号", "")
            )
            added += len(new_parts)
            if new_parts:
                self._record_attempts(
                    {
                        "批次编号": receipt.get("批次编号", ""),
                        "覆盖申请": receipt.get("覆盖申请", []),
                    },
                    receipt,
                    new_parts,
                )
        if added:
            for batch in store.rows("collection_batch"):
                if not endpoint or batch.get("关联端点") == endpoint:
                    self._refresh_batch(batch)
            for task in store.rows("collection_task"):
                if not endpoint or task.get("关联端点") == endpoint:
                    self._refresh_task(task)
        return added

    def _enqueue_retry(self, batch: dict[str, Any], interval: Interval) -> dict[str, Any]:
        endpoint = batch.get("关联端点", "")
        start_text, end_text = iso_text(interval[0]), iso_text(interval[1])
        rows = store.rows("retry_queue")
        for row in rows:
            if (
                row.get("失败端点") == endpoint
                and row.get("开始时间") == start_text
                and row.get("结束时间") == end_text
                and row.get("status") in ("待重试", "重试中")
            ):
                return row
        code = _next_code(rows, "任务编号", "RETR-BF-")
        row = {
            "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
            "任务编号": code,
            "失败端点": endpoint,
            "失败原因": f"补采批次{batch.get('批次编号', '')}未覆盖{window_text(*interval)}",
            "重试次数": 0,
            "最大重试": 3,
            "退避策略": "exponential:5s/baseline",
            "最后重试": "",
            "队列状态": "补采待重放",
            "status": "待重试",
            "pending": True,
            "abnormal": True,
            "开始时间": start_text,
            "结束时间": end_text,
            "时段": window_text(*interval),
            "来源批次": batch.get("批次编号", ""),
            "覆盖申请": list(batch.get("覆盖申请", [])),
            "创建时间": self._now(),
        }
        rows.append(row)
        return row

    def _reconcile_retries(self, endpoint: str) -> None:
        """按最新覆盖账本收缩/关闭补采产生的重放任务。"""
        ledger = self._ledger(endpoint)
        rows = store.rows("retry_queue")
        seen: set[tuple[str, str]] = set()
        for row in list(rows):
            if row.get("失败端点") != endpoint:
                continue
            if not str(row.get("队列状态", "")).startswith("补采"):
                continue
            if row.get("status") not in ("待重试", "重试中"):
                continue
            interval = (self._moment(row["开始时间"]), self._moment(row["结束时间"]))
            remaining = subtract_intervals([interval], ledger)
            if not remaining:
                row["status"] = "已成功"
                row["队列状态"] = "补采已回填"
                row["pending"] = False
                continue
            first, *extra = remaining
            if remaining != [interval]:
                # 原片段只成功了一部分：剩余部分需要重新派发，回到待重试。
                row["status"] = "待重试"
            row["开始时间"] = iso_text(first[0])
            row["结束时间"] = iso_text(first[1])
            row["时段"] = window_text(*first)
            for part in extra:
                clone = dict(row)
                clone["id"] = max((int(item.get("id", 0)) for item in rows), default=0) + 1
                clone["任务编号"] = _next_code(rows, "任务编号", "RETR-BF-")
                clone["开始时间"] = iso_text(part[0])
                clone["结束时间"] = iso_text(part[1])
                clone["时段"] = window_text(*part)
                clone["status"] = "待重试"
                rows.append(clone)
        # 同一端点的补采重放任务按区间去重，重复提交不会累积。
        for row in list(rows):
            if row.get("失败端点") != endpoint:
                continue
            if not str(row.get("队列状态", "")).startswith("补采"):
                continue
            if row.get("status") not in ("待重试", "重试中"):
                continue
            key = (row.get("开始时间", ""), row.get("结束时间", ""))
            if key in seen:
                rows.remove(row)
            else:
                seen.add(key)

    # ---------------------------------------------------------------- 工具
    def _ledger(self, endpoint: str) -> list[Interval]:
        return merge_intervals(
            (
                self._moment(row["开始时间"]),
                self._moment(row["结束时间"]),
            )
            for row in store.rows("collection_coverage")
            if row.get("关联端点") == endpoint
        )

    def _task_interval(self, task: dict[str, Any]) -> Interval:
        return self._moment(task["开始时间"]), self._moment(task["结束时间"])

    def _batch_interval(self, batch: dict[str, Any]) -> Interval:
        return self._moment(batch["开始时间"]), self._moment(batch["结束时间"])

    def _parse_window(self, start: Any, end: Any, base_date: date | None) -> Interval:
        from app.services.windows import parse_window

        return parse_window(start, end, base_date)

    @staticmethod
    def _moment(value: Any) -> datetime:
        return parse_moment(value)

    @staticmethod
    def _now() -> str:
        return datetime.now().replace(microsecond=0).isoformat(sep=" ")


collection_service = CollectionService()
