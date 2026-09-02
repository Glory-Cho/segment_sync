"""
segment_sync — Adobe Analytics 세그먼트 의존성 관리 & 동기화 툴킷

사용 예 (Jupyter):
    from segment_sync import SegmentRepository, SyncEngine, SyncPolicy, load_policy

    repo = SegmentRepository(ags)
    repo.load(seglist, max_workers=10)

    policy = load_policy("sync_policy.yaml")
    engine = SyncEngine(ags, repo, policy)

    plan = engine.plan("s1234_child_segment_id", comment="조건 변경")
    plan.show()                 # 무엇이 어떻게 바뀌는지 미리보기 (dry-run)
    engine.apply(plan)          # 실제 반영 (백업 + 검증 + 리포트)
"""

from .repository import SegmentRepository, Segment, AmbiguousNameError, Selection
from .policy import SyncPolicy, load_policy
from .integrity import check_child_integrity, audit_all
from .sync_engine import SyncEngine, SyncPlan, BatchPlan
from .reporting import save_reports_to_excel

__all__ = [
    "SegmentRepository", "Segment", "AmbiguousNameError", "Selection",
    "SyncPolicy", "load_policy",
    "check_child_integrity", "audit_all",
    "SyncEngine", "SyncPlan", "BatchPlan",
    "save_reports_to_excel",
]
