"""采集处理回归测试：分时切分、重叠入账、重复提交、中断恢复。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.seed import SEED_ROWS
from app.services.collection import collection_service
from app.services.windows import parse_window, subtract_intervals
from app.store import store

COLLECTION_TABLES = (
    "collection_task",
    "collection_batch",
    "collection_receipt",
    "collection_coverage",
)


class CollectionCase(unittest.TestCase):
    def setUp(self) -> None:
        for module in COLLECTION_TABLES:
            store.rows(module).clear()
        for module in ("retry_queue", "request_batch", "call_log"):
            store.rows(module).clear()
            store.rows(module).extend(dict(row) for row in SEED_ROWS[module])

    def submit_pair(self, endpoint: str = "ENDP-CSV-0001") -> dict:
        return collection_service.submit({
            "端点": endpoint,
            "日期": "2026-11-22",
            "申请": [
                {"开始": "08:00", "结束": "10:00"},
                {"开始": "09:00", "结束": "11:00"},
            ],
        })

    def test_overlapping_applications_split_into_three_segments(self) -> None:
        result = self.submit_pair()
        self.assertEqual(
            [(item["时段"], item["覆盖申请"]) for item in result["segments"]],
            [
                ("08:00-09:00", ["BFAP-0001"]),
                ("09:00-10:00", ["BFAP-0001", "BFAP-0002"]),
                ("10:00-11:00", ["BFAP-0002"]),
            ],
        )
        self.assertEqual(result["reconciled_receipts"], 0)
        self.assertTrue(all(item["状态"] == "已派发" for item in result["segments"]))

    def test_overlapping_success_is_booked_once(self) -> None:
        result = self.submit_pair()
        batches = {item["时段"]: item for item in result["batches"]}
        collection_service.register_receipt(batches["09:00-10:00"]["id"], {"结果": "成功"})
        collection_service.register_receipt(batches["08:00-09:00"]["id"], {"结果": "成功"})
        collection_service.register_receipt(batches["09:00-10:00"]["id"], {"结果": "成功"})
        coverage = collection_service.list_coverage(endpoint="ENDP-CSV-0001")
        self.assertEqual(sorted(item["时段"] for item in coverage["items"]),
                         ["08:00-09:00", "09:00-10:00"])
        self.assertEqual(coverage["覆盖分钟"], 120)
        # 重叠段对两份申请都回看得到，但账本里只入账一次。
        apps = collection_service.list_applications()["items"]
        self.assertEqual(apps[0]["已覆盖区间"][0]["开始"], "08:00")
        self.assertEqual(apps[0]["已覆盖区间"][0]["结束"], "10:00")
        self.assertEqual(apps[1]["已覆盖时段"], "09:00-10:00")
        for app in apps:
            overlap = [
                item for item in app["实际补采时段"]
                if item["时段"] == "09:00-10:00"
            ]
            self.assertEqual(len(overlap), 1)
            self.assertTrue(overlap[0]["入账"])
        shared = [
            receipt for receipt in store.rows("collection_receipt")
            if receipt["时段"] == "09:00-10:00"
        ]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["覆盖申请"], ["BFAP-0001", "BFAP-0002"])

    def test_repeat_submit_keeps_single_booking(self) -> None:
        result = self.submit_pair()
        batches = {item["时段"]: item for item in result["batches"]}
        collection_service.register_receipt(batches["09:00-10:00"]["id"], {"结果": "成功"})
        collection_service.register_receipt(batches["08:00-09:00"]["id"], {"结果": "成功"})
        repeat = collection_service.submit({
            "端点": "ENDP-CSV-0001",
            "日期": "2026-11-22",
            "申请": [
                {"开始": "08:00", "结束": "10:00"},
                {"开始": "09:00", "结束": "11:00"},
            ],
        })
        self.assertEqual(len(collection_service.list_coverage(endpoint="ENDP-CSV-0001")["items"]), 2)
        self.assertEqual([item["时段"] for item in repeat["segments"]], ["10:00-11:00"])
        self.assertEqual(len(repeat["applications"]), 2)
        self.assertEqual(collection_service.list_applications()["total"], 2)

    def test_interrupt_queues_remaining_and_request_batch_sync(self) -> None:
        result = self.submit_pair()
        batches = {item["时段"]: item for item in result["batches"]}
        collection_service.register_receipt(batches["09:00-10:00"]["id"], {"结果": "成功"})
        message_batch = collection_service.register_receipt(
            batches["10:00-11:00"]["id"],
            {"结果": "成功", "开始": "10:00", "结束": "10:20"},
        )[0]
        collection_service.register_receipt(
            batches["10:00-11:00"]["id"],
            {"结果": "中断", "开始": "10:20", "结束": "11:00"},
        )
        retries = [
            row for row in store.rows("retry_queue")
            if row.get("失败端点") == "ENDP-CSV-0001"
            and str(row.get("队列状态", "")).startswith("补采")
        ]
        self.assertEqual([row["时段"] for row in retries], ["10:20-11:00"])
        self.assertEqual(retries[0]["status"], "待重试")
        request_batch = store.find("request_batch", int(message_batch["请求批次ID"]))
        entry = request_batch["补采清单"][0]
        self.assertEqual(entry["时段"], "10:00-11:00")
        self.assertEqual(entry["已覆盖时段"], "10:00-10:20")
        self.assertEqual(entry["未覆盖时段"], "10:20-11:00")
        self.assertEqual(request_batch["status"], "部分失败")

    def test_resume_reconciles_receipts_then_dispatches_remaining(self) -> None:
        result = self.submit_pair()
        batches = {item["时段"]: item for item in result["batches"]}
        collection_service.register_receipt(batches["09:00-10:00"]["id"], {"结果": "成功"})
        collection_service.register_receipt(batches["08:00-09:00"]["id"], {"结果": "成功"})
        collection_service.register_receipt(batches["10:00-11:00"]["id"], {"结果": "中断"})
        resumed = collection_service.resume(endpoint="ENDP-CSV-0001")
        self.assertEqual(
            [(item["时段"], item["状态"]) for item in resumed["应执行片段"]],
            [("10:00-11:00", "已派发")],
        )
        self.assertEqual(resumed["跳过片段"], [])
        again = collection_service.resume(endpoint="ENDP-CSV-0001")
        self.assertEqual(again["应执行片段"], [])
        self.assertEqual(again["跳过片段"][0]["原因"], "已派发待回执")
        self.assertEqual(
            len([row for row in store.rows("collection_batch")
                 if row.get("时段") == "10:00-11:00"]), 1
        )

    def test_resume_books_success_receipt_missing_from_ledger(self) -> None:
        result = self.submit_pair()
        batch = next(item for item in result["batches"] if item["时段"] == "09:00-10:00")
        # 模拟回执已落库但覆盖账本丢失（中断写坏）的情形。
        store.rows("collection_receipt").append({
            "id": 9001,
            "回执编号": "BFRC-9001",
            "批次编号": batch["批次编号"],
            "关联端点": "ENDP-CSV-0001",
            "开始时间": "2026-11-22T09:00:00",
            "结束时间": "2026-11-22T10:00:00",
            "时段": "09:00-10:00",
            "结果": "成功",
            "覆盖申请": list(batch["覆盖申请"]),
            "入账区间": [],
            "入账分钟": 0,
            "提交时间": "2026-11-22 09:30:00",
        })
        resumed = collection_service.resume(endpoint="ENDP-CSV-0001")
        self.assertEqual(resumed["补记覆盖"], 1)
        coverage = collection_service.list_coverage(endpoint="ENDP-CSV-0001")
        self.assertEqual([item["时段"] for item in coverage["items"]], ["09:00-10:00"])
        self.assertNotIn("09:00-10:00", [item["时段"] for item in resumed["应执行片段"]])

    def test_invalid_window_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_window("10:00", "09:00")
        with self.assertRaises(ValueError):
            collection_service.submit({
                "端点": "ENDP-CSV-0001",
                "申请": [{"开始": "10:00", "结束": "09:00"}],
            })

    def test_subtract_intervals(self) -> None:
        base = [parse_window("08:00", "11:00")]
        covers = [parse_window("09:00", "10:00"), parse_window("10:30", "11:00")]
        self.assertEqual(
            [tuple(item.strftime("%H:%M") for item in part) for part in subtract_intervals(base, covers)],
            [("08:00", "09:00"), ("10:00", "10:30")],
        )


if __name__ == "__main__":
    unittest.main()
