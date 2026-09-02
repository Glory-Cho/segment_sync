"""
cli.py — 터미널/스케줄러에서 실행하기 위한 진입점.

사용 예:
  python -m segment_sync.cli audit --segments seglist.txt
  python -m segment_sync.cli plan  --segments seglist.txt --child s1234_abc
  python -m segment_sync.cli sync  --segments seglist.txt --child s1234_abc \
      --comment "조건 변경" --policy sync_policy.yaml --execute

--execute 를 붙이지 않으면 항상 dry-run(계획만 출력)입니다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("segment_sync.log", encoding="utf-8"),
        ],
    )


def _make_client():
    """aanalytics2 클라이언트 초기화. config.json 경로는 환경에 맞게 수정."""
    import aanalytics2 as api2

    api2.importConfigFile("config_analytics.json")
    logger_obj = api2.Login()
    company = logger_obj.getCompanyId()[0]["globalCompanyId"]
    return api2.Analytics(company)


def _load_ids(path: str) -> list[str]:
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def main() -> None:
    parser = argparse.ArgumentParser(prog="segment_sync")
    parser.add_argument("command", choices=["audit", "plan", "sync"])
    parser.add_argument("--segments", required=True, help="세그먼트 ID 목록 파일 (한 줄에 하나)")
    parser.add_argument("--child", help="기준 자식 세그먼트 (ID 권장)")
    parser.add_argument("--comment", default="", help="description에 남길 변경 이력")
    parser.add_argument("--policy", default="sync_policy.yaml", help="정책 파일 경로")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--execute", action="store_true", help="실제 push (없으면 dry-run)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    from .integrity import audit_all
    from .policy import load_policy
    from .reporting import save_reports_to_excel
    from .repository import SegmentRepository
    from .sync_engine import SyncEngine

    client = _make_client()
    policy = load_policy(args.policy)
    repo = SegmentRepository(client, policy.max_retries, policy.retry_backoff_sec)
    result = repo.load(_load_ids(args.segments), max_workers=args.workers)
    if result.failed:
        print(f"⚠️ 로드 실패 {len(result.failed)}건: {result.failed}")

    if args.command == "audit":
        audit_all(repo)
        return

    if not args.child:
        parser.error("plan/sync 명령에는 --child 가 필요합니다.")

    engine = SyncEngine(client, repo, policy)
    plan = engine.plan(args.child, comment=args.comment)
    plan.show()

    if args.command == "sync":
        if not args.execute:
            print("\n(dry-run 모드 — 실제 반영하려면 --execute 를 추가하세요)")
            return
        reports = engine.apply(plan, confirm=None)  # 정책에 따라 yes 확인
        save_reports_to_excel(reports)


if __name__ == "__main__":
    main()
