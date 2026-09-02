"""
sync_engine.py — 실제 서버 반영을 담당하는 핵심 엔진.

설계 원칙: "계획(plan) → 확인 → 실행(apply)" 2단계 분리.
  - plan()   : 아무것도 바꾸지 않음. 무엇이 어디서 어떻게 바뀔지 전부 계산해서 보여줌.
  - apply()  : 백업 → 교체 → [로컬 검증] → push → [서버 재검증] 순서로 진행.

요청사항 5번 (확실한 교체 여부 리포팅) 해결:
  1. 교체 시 JSON 경로 단위로 몇 곳을 바꿨는지 기록 (replaced_paths)
  2. push 전에 수정본에서 pred를 다시 추출해 최신 로직과 100% 일치하는지 검증
  3. push 후 서버에서 **다시 fetch**해서 실제 반영됐는지 최종 검증 (Verified 상태)
  → 리포트의 결과 컬럼: Verified / PushedButMismatch / Failed / Skipped

요청사항 6번 (동명이인/보호 세그먼트) 해결:
  - SyncPolicy의 보호/제외 목록을 plan 단계에서 걸러내고, 걸러진 이유를 리포트에 남김
  - 이름이 중복인 자식은 repo.resolve()가 예외를 던지므로 실수로 잘못된
    세그먼트를 기준으로 삼는 사고 자체가 차단됨
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .integrity import (check_child_integrity, find_embedded_nodes,
                        normalize_context, similarity_pct, strip_leading_withouts,
                        summarize_pred_changes, wrap_without)
from .policy import SyncPolicy
from .repository import SegmentRepository

logger = logging.getLogger(__name__)

_FILENAME_SAFE = re.compile(r'[\\/:*?"<>|]')


def _safe_filename(name: str) -> str:
    return _FILENAME_SAFE.sub("_", name)


def sync_embedded_nodes(obj: Any, marker_name: str, new_pred: Any,
                        new_context: str, sync_context: bool = False,
                        path: str = "$") -> list[dict]:
    """
    description == marker_name 인 노드를 전부 찾아 pred를 자식의 최신 상태로 동기화.
    자식 최상위의 Exclude(without)는 pred 내부에 있으므로 pred 교체로 함께 반영됨.

    sync_context=False (기본): 버킷(context)은 절대 건드리지 않음.
      부모 안에서 의도적으로 다른 버킷(예: 자식은 Visit, 부모 안에서는 Hit)을
      쓰는 경우가 있으므로, 차이는 기록만 하고 유지.
    sync_context=True: 버킷도 자식 최신으로 강제 동기화 (정책 "sync").

    반환: 위치별 기록 [{"path", "pred_replaced", "context_note"}, ...]
      - context_note: 버킷이 다르면 "hits≠visits (유지)" 또는 "hits→visits (변경)"
    주의: description, 그리고 이 노드를 감싸는 부모 쪽 래퍼(부모가 자체적으로 건
          Exclude 등)는 건드리지 않음 → 부모의 의도는 보존.
    """
    from .integrity import normalize_context

    records: list[dict] = []
    if isinstance(obj, dict):
        if obj.get("description") == marker_name and "pred" in obj:
            rec = {"path": path, "pred_replaced": True, "context_note": None,
                   "exclude_note": None}
            # Include/Exclude(without 겹수)는 부모 소유 → 겹수 그대로 유지,
            # 자식 최신의 core만 삽입 (양방향 모두 극성 미전파 — 버킷과 동일 원칙)
            emb_n, _ = strip_leading_withouts(obj["pred"])
            _, latest_core = strip_leading_withouts(new_pred)
            obj["pred"] = wrap_without(copy.deepcopy(latest_core), emb_n)
            if emb_n:
                rec["exclude_note"] = f"Exclude 래퍼 {emb_n}겹 유지"
            cur_ctx = normalize_context(obj.get("context"))
            if cur_ctx != new_context:
                if sync_context:
                    obj["context"] = new_context
                    rec["context_note"] = f"{cur_ctx}→{new_context} (변경)"
                else:
                    rec["context_note"] = f"{cur_ctx}≠{new_context} (유지)"
            records.append(rec)
            # ⚠️ 여기서 반드시 종료: 교체한 새 pred 내부로 재귀 진입하면
            # 그 안의 동일 마커(자기 참조형 중첩)를 또 교체 → 무한 재귀(RecursionError)
            return records
        for k, v in obj.items():
            records.extend(sync_embedded_nodes(
                v, marker_name, new_pred, new_context, sync_context, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            records.extend(sync_embedded_nodes(
                item, marker_name, new_pred, new_context, sync_context, f"{path}[{i}]"))
    return records


@dataclass
class PlannedUpdate:
    parent_id: str
    parent_name: str
    similarity_before: float
    embed_count: int              # 부모 안에 자식이 박혀있는 위치 수
    context_notes: list[str] = field(default_factory=list)  # ["hits≠visits (유지)", ...]
    skipped_reason: str | None = None   # 정책에 의해 제외된 경우


@dataclass
class SyncPlan:
    child_id: str
    child_name: str
    comment: str
    latest_pred: Any
    latest_context: str = "hits"        # 자식의 최신 버킷 (Hit/Visit/Visitor)
    updates: list[PlannedUpdate] = field(default_factory=list)

    @property
    def actionable(self) -> list[PlannedUpdate]:
        return [u for u in self.updates if u.skipped_reason is None]

    def show(self) -> None:
        print(f"\n📋 [실행 계획] 자식: {self.child_name} ({self.child_id})")
        print(f"   커멘트: {self.comment or '(없음)'}")
        print("-" * 95)
        if not self.updates:
            print("✅ 업데이트가 필요한 부모가 없습니다.")
            return
        for u in self.updates:
            if u.skipped_reason:
                print(f"⏭️  SKIP  | {u.parent_name:<40} | {u.parent_id} | 사유: {u.skipped_reason}")
            else:
                ctx = f" | 🪣 버킷: {', '.join(u.context_notes)}" if u.context_notes else ""
                print(f"🛠️  UPDATE| {u.parent_name:<40} | {u.parent_id} "
                      f"| 유사도 {u.similarity_before}% | 교체 위치 {u.embed_count}곳{ctx}")
        print("-" * 95)
        print(f"→ 실행 대상 {len(self.actionable)}개 / 제외 {len(self.updates) - len(self.actionable)}개")
        print("   실제 반영: engine.apply(plan)  |  미리보기 diff: engine.preview_diff(plan)")


class BatchPlan:
    """여러 자식 세그먼트에 대한 SyncPlan 묶음."""
    def __init__(self, plans: list[SyncPlan], skipped: list[tuple[str, str]] | None = None):
        self.plans = plans
        self.skipped = skipped or []   # (이름/ID, 사유)

    def _ipython_display_(self):
        pass

    def __repr__(self):
        return f"<BatchPlan 자식 {len(self.plans)}개 / 대상 부모 {self.total_targets}개>"

    @property
    def total_targets(self) -> int:
        return sum(len(p.actionable) for p in self.plans)

    def show(self) -> None:
        print(f"\n📋 [일괄 실행 계획] 자식 {len(self.plans)}개 | "
              f"실행 대상 부모 총 {self.total_targets}개")
        print("=" * 108)
        print(f"{'No.':<4}| {'자식 세그먼트':<40} | {'자식 ID':<26}| {'대상':<6}| 제외")
        print("-" * 108)
        for i, p in enumerate(self.plans, 1):
            n_skip = len(p.updates) - len(p.actionable)
            print(f"{i:02d}  | {p.child_name:<40} | {p.child_id:<26}| "
                  f"{len(p.actionable)}개{'':<2}| {n_skip}개")
        if not self.plans:
            print("(업데이트가 필요한 자식이 없습니다)")
        if self.skipped:
            print("-" * 108)
            print(f"⏭️ 계획에서 제외된 자식 {len(self.skipped)}개:")
            for ref, reason in self.skipped[:5]:
                print(f"   · {ref} — {reason}")
            if len(self.skipped) > 5:
                print(f"   … 외 {len(self.skipped) - 5}개")
        print("-" * 108)
        print("   상세: batch.plans[i].show()  |  실제 반영: engine.apply_batch(batch)")


class SyncEngine:
    def __init__(self, client, repo: SegmentRepository, policy: SyncPolicy | None = None):
        self._client = client
        self._repo = repo
        self._policy = policy or SyncPolicy()

    # ------------------------------------------------------------------ plan

    def plan(self, child_ref: str, comment: str = "") -> SyncPlan:
        """서버를 건드리지 않고, 무엇이 바뀔지 계산만 한다."""
        child = self._repo.resolve(child_ref)  # 이름 중복이면 여기서 예외 발생 (안전장치)

        plan = SyncPlan(child_id=child.id, child_name=child.name,
                        comment=comment, latest_pred=child.root_pred,
                        latest_context=normalize_context(child.root_context))

        if child.root_pred is None:
            raise ValueError(f"자식 '{child.name}'의 기준 pred를 찾을 수 없어 계획을 만들 수 없습니다.")

        if self._policy.is_child_blocked(child.id):
            logger.warning("자식 %s(%s)는 정책상 동기화 제외 대상입니다.", child.name, child.id)
            return plan

        ctx_action = self._policy.context_mismatch_action
        for r in check_child_integrity(self._repo, child.id, verbose=False,
                                       context_policy=ctx_action):
            reason = None
            if self._policy.is_parent_blocked(r.parent_id):
                reason = "보호 세그먼트 (protected_parent_ids)"
            else:
                reason = self._policy.pair_block_reason(
                    r.parent_id, child.id, r.parent_name, child.name)
            verb = "동기화 예정" if ctx_action == "sync" else "유지"
            plan.updates.append(PlannedUpdate(
                parent_id=r.parent_id,
                parent_name=r.parent_name,
                similarity_before=r.worst_similarity,
                embed_count=len(r.matches),
                context_notes=[] if ctx_action == "ignore" else sorted(
                    {f"{m.context_current}≠{m.context_latest} ({verb})"
                     for m in r.matches if m.context_mismatch}),
                skipped_reason=reason,
            ))
        return plan

    def plan_batch(self, targets, comment: str = "",
                   comments: dict[str, str] | None = None) -> BatchPlan:
        """
        여러 자식에 대한 계획을 한 번에 생성. 서버 쓰기 없음.

        targets  : repo.find() 결과(Selection), ID 리스트, 또는 audit_all() 결과(dict)
        comment  : 전체 공통 커멘트
        comments : {child_id: 개별 커멘트} — 지정한 자식만 공통 커멘트 대신 사용

        업데이트가 필요 없는 자식은 계획에서 자동으로 빠지고,
        오류가 난 자식은 사유와 함께 skipped에 기록된다 (전체 중단 없음).
        """
        ids = list(targets.keys()) if isinstance(targets, dict) else list(targets)
        comments = comments or {}
        plans, skipped = [], []

        for cid in ids:
            try:
                p = self.plan(cid, comment=comments.get(cid, comment))
            except Exception as e:  # noqa: BLE001 — 한 건 실패로 전체를 멈추지 않음
                name = self._repo.segments[cid].name if cid in self._repo.segments else cid
                skipped.append((f"{name} ({cid})", str(e)))
                continue
            if p.actionable:
                plans.append(p)
        return BatchPlan(plans, skipped)

    def apply_batch(self, batch: BatchPlan, confirm: bool | None = None) -> list[dict]:
        """BatchPlan 일괄 실행. 확인은 배치 전체에 대해 한 번만 받는다."""
        if confirm is None:
            confirm = not self._policy.require_confirmation
        if not confirm:
            answer = input(f"⚠️ 자식 {len(batch.plans)}개 / 부모 {batch.total_targets}개를 "
                           f"실제로 수정합니다. 진행하려면 'yes' 입력: ").strip().lower()
            if answer != "yes":
                print("🚫 취소되었습니다.")
                return []

        all_reports: list[dict] = []
        for i, p in enumerate(batch.plans, 1):
            print(f"\n[{i}/{len(batch.plans)}] {p.child_name} ({p.child_id})")
            all_reports.extend(self.apply(p, confirm=True))

        ok = sum(1 for r in all_reports if r["결과"] == "Verified")
        print(f"\n✨ [일괄 완료] 총 {len(all_reports)}건 처리 — 성공(Verified) {ok}건")
        return all_reports

    def preview_diff(self, plan: SyncPlan, max_parents: int = 5) -> None:
        """계획된 각 부모에 대해 실제 바뀔 diff를 출력."""
        from .integrity import pred_diff_text
        for u in plan.actionable[:max_parents]:
            parent = self._repo.segments[u.parent_id]
            print(f"\n===== {u.parent_name} ({u.parent_id}) =====")
            for path, node in find_embedded_nodes(parent.definition, plan.child_name):
                cur_ctx = normalize_context(node.get("context"))
                print(f"\n-- 위치: {path}")
                if cur_ctx != plan.latest_context:
                    verb = ("→ " + plan.latest_context + " 변경 예정"
                            if self._policy.context_mismatch_action == "sync"
                            else f"≠ {plan.latest_context} — 유지됨 (의도적 차이 가능)")
                    print(f"   🪣 버킷: {cur_ctx} {verb}")
                summary = summarize_pred_changes(plan.latest_pred, node["pred"])
                if summary:
                    print(f"   📝 변경 요약: {summary}")
                print(pred_diff_text(plan.latest_pred, node["pred"]) or "(pred 일치)")

    # ----------------------------------------------------------------- apply

    def apply(self, plan: SyncPlan, confirm: bool | None = None) -> list[dict]:
        """
        계획 실행. 각 부모마다: 백업 → 교체 → 로컬검증 → push(재시도) → 서버 재검증.
        반환: 리포트 dict 목록 (엑셀 저장용).
        """
        if confirm is None:
            confirm = not self._policy.require_confirmation
        if not confirm:
            answer = input(f"⚠️ {len(plan.actionable)}개 부모 세그먼트를 실제로 수정합니다. "
                           f"진행하려면 'yes' 입력: ").strip().lower()
            if answer != "yes":
                print("🚫 취소되었습니다.")
                return []

        backup_dir = Path(self._policy.backup_root) / datetime.now().strftime("%Y%m%d")
        backup_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now()
        reports: list[dict] = []

        for u in plan.updates:
            base_row = {
                "날짜": today.strftime("%Y-%m-%d"),
                "세그먼트명": u.parent_name,
                "세그먼트ID": u.parent_id,
                "요인세그먼트명": plan.child_name,
                "요인세그먼트ID": plan.child_id,
                "추가커멘트": plan.comment,
                "교체위치수": u.embed_count,
            }

            if u.skipped_reason:
                reports.append({**base_row, "결과": f"Skipped ({u.skipped_reason})",
                                "백업파일": ""})
                continue

            seg = self._repo.segments[u.parent_id]

            # 0) 동시 편집 감지: 로드 이후 다른 사람이 이 부모를 수정했는지 확인.
            #    수정됐다면 우리가 가진 스냅샷은 낡은 것 → push하면 그 사람의
            #    변경을 덮어쓰므로 중단하고 재로드를 요구.
            if self._policy.check_concurrent_edit:
                conflict = self._check_concurrent_edit(seg)
                if conflict:
                    print(f"🛑 동시 편집 감지: {seg.name} — {conflict}")
                    reports.append({**base_row,
                                    "결과": f"Skipped (동시 편집 감지: {conflict} — 재로드 필요)",
                                    "백업파일": ""})
                    continue

            # 0-1) 변경 요약: 교체 '전' 부모 안 pred와 최신 pred의 구조적 차이 기록
            pre_nodes = find_embedded_nodes(seg.full_form.get("definition", {}), plan.child_name)
            change_summaries = []
            for pre_path, pre_node in pre_nodes:
                s = summarize_pred_changes(plan.latest_pred, pre_node["pred"])
                if s:
                    change_summaries.append(f"{pre_path}: {s}")

            # 1) 백업
            original = copy.deepcopy(seg.full_form)
            original.setdefault("id", seg.id)   # 복구 시 대상 식별 보장
            ts = datetime.now().strftime("%H%M%S")
            backup_path = (backup_dir /
                           f"FULL_BACKUP_{_safe_filename(seg.name)}_{seg.id}_{ts}.json")
            try:
                backup_path.write_text(
                    json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as e:
                logger.error("백업 실패 → 이 세그먼트는 건너뜀: %s", e)
                reports.append({**base_row, "결과": f"Failed (백업 실패: {e})", "백업파일": ""})
                continue

            # 2) 교체 (기본: pred만 동기화, 버킷은 정책이 "sync"일 때만 변경)
            sync_ctx = self._policy.context_mismatch_action == "sync"
            updated = copy.deepcopy(original)
            change_records = sync_embedded_nodes(
                updated.get("definition", {}), plan.child_name,
                plan.latest_pred, plan.latest_context, sync_context=sync_ctx)

            if not change_records:
                reports.append({**base_row, "결과": "Failed (교체 대상 미발견 — 구조 변경 의심)",
                                "백업파일": str(backup_path)})
                continue

            replaced_paths = [r["path"] for r in change_records]
            ctx_notes = [f"{r['path']}: {r['context_note']}"
                         for r in change_records if r["context_note"]]
            exc_notes = [f"{r['path']}: {r['exclude_note']}"
                         for r in change_records if r.get("exclude_note")]

            # 3) 로컬 검증: pred는 항상 100% 일치, 버킷은 "sync" 모드일 때만 검증
            post = find_embedded_nodes(updated.get("definition", {}), plan.child_name)
            _, latest_core = strip_leading_withouts(plan.latest_pred)
            def _core_ok(node):
                _, core = strip_leading_withouts(node["pred"])
                return similarity_pct(latest_core, core) == 100
            local_ok = post and all(
                _core_ok(node)
                and (not sync_ctx
                     or normalize_context(node.get("context")) == plan.latest_context)
                for _, node in post)
            if not local_ok:
                reports.append({**base_row, "결과": "Failed (로컬 교체 검증 실패 — push 중단)",
                                "백업파일": str(backup_path)})
                continue

            # 4) description 이력 추가
            if plan.comment:
                entry = f"{today.strftime('%y%m%d')} - {plan.comment}"
                cur = updated.get("description", "")
                updated["description"] = f"{cur}\n{entry}" if cur else entry

            # 5) push (재시도)
            status = self._push_with_retry(seg.id, updated)

            # 6) 서버 재검증: 성공 시 다시 받아와서 실제 반영 확인
            if status == "Pushed":
                status, fresh = self._verify_on_server(seg.id, plan)
                if status == "Verified":
                    # 서버 응답 원본(fresh)으로 갱신 — modified 타임스탬프까지
                    # 최신화되어 다음 push 때 동시 편집 오탐이 없음
                    seg.full_form = fresh
                    seg.definition = fresh.get("definition", {})
                    seg.description = fresh.get("description", "")
                    print(f"✅ 반영+검증 완료: {seg.name} (교체 {len(replaced_paths)}곳)")
                else:
                    print(f"⚠️ push는 됐으나 서버 검증 불일치: {seg.name} → 백업으로 확인 필요")
            else:
                print(f"❌ 반영 실패: {seg.name} → {status}")

            reports.append({**base_row, "결과": status,
                            "교체경로": "; ".join(replaced_paths),
                            "변경요약": " || ".join(change_summaries),
                            "Exclude보존": "; ".join(exc_notes) if exc_notes else "",
                            "버킷메모": "; ".join(ctx_notes) if ctx_notes else "",
                            "백업파일": str(backup_path)})

        return reports

    def _check_concurrent_edit(self, seg) -> str | None:
        """
        push 직전 서버의 modified 타임스탬프를 재확인.
        반환: 충돌 설명 문자열 (충돌 없으면 None).
        modified 필드가 없으면 확인 불가 → 경고만 남기고 통과.
        """
        try:
            fresh = self._client.getSegment(seg.id, True)
        except Exception as e:  # noqa: BLE001 — 확인 실패 시 안전한 쪽(중단)으로
            return f"서버 확인 실패({e})"
        server_mod = fresh.get("modified")
        local_mod = seg.modified
        if server_mod is None or local_mod is None:
            logger.warning("%s: modified 필드 없음 — 동시 편집 확인 생략", seg.id)
            return None
        if server_mod != local_mod:
            return f"로드 시점 {local_mod} → 서버 {server_mod}"
        return None

    def _push_with_retry(self, seg_id: str, payload: dict) -> str:
        last_err = None
        for attempt in range(1, self._policy.max_retries + 1):
            try:
                res = self._client.updateSegment(seg_id, payload)
                # aanalytics2는 실패 시 에러 dict를 반환하는 경우가 있음
                if isinstance(res, dict) and res.get("errorCode"):
                    raise RuntimeError(res)
                return "Pushed"
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(self._policy.retry_backoff_sec * attempt)
        return f"Failed ({last_err})"

    def _verify_on_server(self, seg_id: str, plan: SyncPlan) -> tuple[str, dict]:
        """반환: (상태, 서버에서 새로 받은 원본). 실패 시 원본은 빈 dict."""
        sync_ctx = self._policy.context_mismatch_action == "sync"
        try:
            fresh = self._client.getSegment(seg_id, True)
            hits = find_embedded_nodes(fresh.get("definition", {}), plan.child_name)
            _, latest_core = strip_leading_withouts(plan.latest_pred)
            def _core_ok(node):
                _, core = strip_leading_withouts(node["pred"])
                return similarity_pct(latest_core, core) == 100
            ok = hits and all(
                _core_ok(node)
                and (not sync_ctx
                     or normalize_context(node.get("context")) == plan.latest_context)
                for _, node in hits)
            return ("Verified" if ok else "PushedButMismatch"), fresh
        except Exception as e:  # noqa: BLE001
            return f"PushedButVerifyFailed ({e})", {}

    # -------------------------------------------------------------- rollback

    # -------------------------------------------------------------- snapshot

    def snapshot(self, ids: list[str] | None = None, label: str | None = None,
                 refresh: bool = False) -> str:
        """
        디렉토리 단위 일괄 백업 (스냅샷).

        ids     : 백업할 세그먼트 ID 목록 (기본: 로드된 전체)
        label   : 디렉토리명에 붙일 라벨 (예: "before_checkout_v2")
        refresh : True면 저장 직전 서버에서 다시 fetch (진짜 '현재 서버 상태' 스냅샷).
                  False(기본)면 로드 시점의 스냅샷 — 빠르고 API 호출 없음.

        생성 구조: {backup_root}/snapshot_YYYYMMDD_HHMMSS[_label]/
                     ├─ FULL_BACKUP_{이름}_{ID}.json  (세그먼트별)
                     └─ manifest.json                 (목록·시각 메타데이터)
        복원: engine.rollback_dir(스냅샷경로) 또는 rollback(개별 파일) 그대로 사용.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dir_name = f"snapshot_{ts}" + (f"_{_safe_filename(label)}" if label else "")
        snap_dir = Path(self._policy.backup_root) / dir_name
        snap_dir.mkdir(parents=True, exist_ok=True)

        targets = ids if ids is not None else list(self._repo.segments.keys())
        manifest = {"created_at": datetime.now().isoformat(),
                    "refresh": refresh, "count": 0, "failed": [], "segments": []}

        for sid in targets:
            seg = self._repo.segments.get(sid)
            if refresh or seg is None:
                try:
                    data = self._client.getSegment(sid, True)
                except Exception as e:  # noqa: BLE001
                    logger.error("스냅샷 fetch 실패 %s: %s", sid, e)
                    manifest["failed"].append({"id": sid, "reason": str(e)})
                    continue
                name = data.get("name", sid)
            else:
                data = copy.deepcopy(seg.full_form)
                name = seg.name
            data.setdefault("id", sid)

            path = snap_dir / f"FULL_BACKUP_{_safe_filename(name)}_{sid}.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            manifest["segments"].append({"id": sid, "name": name,
                                         "modified": data.get("modified")})
            manifest["count"] += 1

        (snap_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"📦 스냅샷 저장: {snap_dir} — {manifest['count']}개"
              + (f" (실패 {len(manifest['failed'])}개)" if manifest["failed"] else ""))
        return str(snap_dir)

    def rollback_dir(self, backup_dir: str | Path,
                     only_ids: set[str] | None = None) -> dict[str, str]:
        """백업 폴더의 모든 백업을 일괄 복원. only_ids로 특정 세그먼트만 선별 가능.
        같은 세그먼트의 백업이 여러 개면 가장 이른 시각(원본에 가장 가까운 것)을 사용."""
        chosen: dict[str, Path] = {}
        for p in sorted(Path(backup_dir).glob("FULL_BACKUP_*.json")):
            sid = json.loads(p.read_text(encoding="utf-8")).get("id")
            if sid and sid not in chosen:   # 정렬상 첫 파일 = 가장 이른 백업
                chosen[sid] = p
        results = {}
        for sid, p in chosen.items():
            if only_ids and sid not in only_ids:
                continue
            results[sid] = self.rollback(p)
        return results

    def rollback(self, backup_file: str | Path) -> str:
        """백업 JSON 파일 하나를 서버에 그대로 복원."""
        p = Path(backup_file)
        original = json.loads(p.read_text(encoding="utf-8"))
        seg_id = original.get("id") or p.stem.rsplit("_", 1)[-1]
        status = self._push_with_retry(seg_id, original)
        print(f"↩️ 롤백 {seg_id}: {status}")
        return status
