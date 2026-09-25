"""中断续算：按指标分步落库，续算跳过已完成步骤，结果与一次跑完一致。"""
import unittest

from helpers import (
    ADMIN,
    SH_DAY,
    capacity_snapshot,
    field_event,
    info_latency_def,
    make_service,
    queue_def,
    rescue_count_def,
    rescue_record,
    utilization_def,
    wait_event,
)

from service_09251_010.domain.errors import ComputationInterrupted
from service_09251_010.domain.models import RUN_INTERRUPTED

EVIDENCE = [
    wait_event("w1", "2026-10-01T10:05:00+08:00", 30),
    wait_event("w2", "2026-10-01T10:35:00+08:00", 50),
    capacity_snapshot("c1", "2026-10-01T10:10:00+08:00", 100, 60),
    field_event("i1", "2026-10-01T10:20:00+08:00", "info_published", latency_minutes=8),
    rescue_record("r1", "2026-10-01T11:00:00+08:00"),
]

DEFS = [queue_def(), utilization_def(), info_latency_def(), rescue_count_def()]


def build_review(service, def_ids):
    return service.create_review(
        ADMIN, region="沪", name="国庆复盘", window=SH_DAY, metric_def_ids=def_ids
    )["id"]


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.service, self.store = make_service()
        self.def_ids = [
            self.service.create_metric_definition(ADMIN, definition)["id"] for definition in DEFS
        ]

    def test_interrupted_run_resumes_without_recomputing_done_steps(self):
        review_a = build_review(self.service, self.def_ids)
        self.service.add_evidence(ADMIN, review_a, EVIDENCE)

        calls: list[str] = []

        def flaky_hook(run_id, metric_key):
            calls.append(metric_key)
            if len(calls) == 3:
                raise RuntimeError("模拟进程崩溃")

        with self.assertRaises(ComputationInterrupted) as ctx:
            self.service.compute(ADMIN, review_a, step_hook=flaky_hook)
        run_id = ctx.exception.details["run_id"]

        run = self.service.get_run(ADMIN, run_id)
        self.assertEqual(run["status"], RUN_INTERRUPTED)
        self.assertEqual(len(run["steps"]), 2)  # 第三步崩溃，前两步已落库

        # 对照组：同样输入一次跑完
        review_b = build_review(self.service, self.def_ids)
        self.service.add_evidence(ADMIN, review_b, EVIDENCE)
        clean = self.service.compute(ADMIN, review_b)

        # 续算：只补算剩余两步
        outcome = self.service.resume(ADMIN, run_id, step_hook=lambda *_: calls.append("resumed"))
        self.assertTrue(outcome["resumed"])
        self.assertEqual(len(calls), 5)  # 3 次失败前调用 + 2 次续算调用

        version_a = self.service.get_version(ADMIN, review_a, outcome["version_no"])
        version_b = self.service.get_version(ADMIN, review_b, clean["version_no"])
        self.assertEqual(version_a["results"], version_b["results"])
        self.assertEqual(version_a["fingerprint"], version_b["fingerprint"])

    def test_resume_is_idempotent_after_done(self):
        review_id = build_review(self.service, self.def_ids)
        self.service.add_evidence(ADMIN, review_id, EVIDENCE)
        outcome = self.service.compute(ADMIN, review_id)
        again = self.service.resume(ADMIN, outcome["run_id"])
        self.assertFalse(again["resumed"])
        self.assertEqual(again["version_no"], outcome["version_no"])

    def test_startup_recovery_marks_running_runs_resumable(self):
        review_id = build_review(self.service, self.def_ids)
        self.service.add_evidence(ADMIN, review_id, EVIDENCE)

        def crash_hook(run_id, metric_key):
            raise RuntimeError("断电")

        with self.assertRaises(ComputationInterrupted) as ctx:
            self.service.compute(ADMIN, review_id, step_hook=crash_hook)
        run_id = ctx.exception.details["run_id"]

        # 模拟进程崩溃遗留的 running 状态，重启后应被标记为可续算
        self.store.update_run_status(run_id, "running")
        self.assertEqual(self.service.recover_interrupted(), 1)
        self.assertEqual(self.service.get_run(ADMIN, run_id)["status"], RUN_INTERRUPTED)

        outcome = self.service.resume(ADMIN, run_id)
        self.assertTrue(outcome["resumed"])
        self.assertEqual(self.service.get_run(ADMIN, run_id)["status"], "done")


if __name__ == "__main__":
    unittest.main()
