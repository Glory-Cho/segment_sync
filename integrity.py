"""
integrity.py — 자식의 최신 로직 vs 부모 내부에 박제된 로직 비교.

기존 코드와의 차이:
- pred 추출이 '첫 번째 매칭에서 멈추던' 문제 해결 → 한 부모 안에 같은 자식이
  여러 번 들어있으면 **전부** 찾아서 각각의 위치(JSON 경로)와 유사도를 보고.
  (요청사항 5번 "확실한 교체 여부 리포팅"의 전제 조건)
- 유사도와 별개로 unified diff 텍스트를 함께 제공 → 뭐가 다른지 눈으로 확인 가능.
"""

from __future__ import annotations

import difflib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .repository import SegmentRepository

logger = logging.getLogger(__name__)


class QuietList(list):
    """Jupyter 셀 마지막 줄에서 호출해도 반환값이 자동 출력되지 않는 리스트.
    데이터는 그대로 담겨 있어 변수에 받아 사용 가능."""
    def _ipython_display_(self):
        pass  # 셀 자동 출력 억제


class QuietDict(dict):
    """QuietList의 dict 버전 (audit_all 반환용)."""
    def _ipython_display_(self):
        pass


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def similarity_pct(obj1: Any, obj2: Any) -> float:
    return round(difflib.SequenceMatcher(None, _canon(obj1), _canon(obj2)).ratio() * 100, 2)


def strip_leading_withouts(pred: Any) -> tuple[int, Any]:
    """
    pred 최상위에서 연속된 without 래퍼를 벗겨 (겹수, core)를 반환.

    설계 원칙 (버킷과 동일): 임베디드 노드의 Include/Exclude 상태(without 겹수)는
    '부모의 소유'로 취급하여 동기화가 절대 변경하지 않는다. 비교와 교체는 모두
    양쪽에서 without을 벗긴 core끼리 수행한다.

    이유: 부모 안의 without(X)가 '부모가 건 Exclude'인지 '자식의 옛 최상위
    Exclude 잔재'인지는 JSON만으로 구분이 원리적으로 불가능하다. 방향에 따라
    전파 여부가 달라지는 비대칭(Incl→Excl은 전파, Excl→Incl은 미전파)보다,
    양방향 모두 전파하지 않고 차이를 경고로 알리는 것이 예측 가능하고 안전하다.
    """
    n = 0
    cur = pred
    while isinstance(cur, dict) and cur.get("func") == "without" and "pred" in cur:
        n += 1
        cur = cur["pred"]
    return n, cur


def wrap_without(pred: Any, wrappers: int) -> Any:
    """pred를 without 래퍼로 지정한 겹수만큼 감싼다."""
    for _ in range(wrappers):
        pred = {"func": "without", "pred": pred}
    return pred


def pred_diff_text(latest: Any, current: Any, n_lines: int = 40) -> str:
    """사람이 읽을 수 있는 diff (미리보기용, 최대 n_lines줄)."""
    a = json.dumps(current, sort_keys=True, ensure_ascii=False, indent=2).splitlines()
    b = json.dumps(latest, sort_keys=True, ensure_ascii=False, indent=2).splitlines()
    lines = list(difflib.unified_diff(a, b, fromfile="부모 내부(현재)", tofile="자식 최신", lineterm=""))
    if len(lines) > n_lines:
        lines = lines[:n_lines] + [f"... (이하 {len(lines) - n_lines}줄 생략)"]
    return "\n".join(lines)


def _fmt_path(deepdiff_path: str) -> str:
    """DeepDiff 경로 표기(root['a'][0]['b'])를 읽기 쉬운 형태($.a[0].b)로."""
    return (deepdiff_path.replace("root", "$")
            .replace("']['", ".").replace("['", ".").replace("']", ""))


def summarize_pred_changes(latest: Any, current: Any, max_items: int = 8) -> str:
    """
    구조적 diff 요약 — "무엇이 어떻게 달라졌는지"를 문장으로.
    예: 값 변경 $.pred.val: 'cart' → 'cart_v2' | 조건 추가 1건
    deepdiff 미설치 시 빈 문자열 반환 (unified diff로 대체됨).
    """
    try:
        from deepdiff import DeepDiff
    except ImportError:
        logger.debug("deepdiff 미설치 — 구조 요약 생략 (pip install deepdiff)")
        return ""

    def _paths_of(items):
        """deepdiff 결과가 dict / list / SetOrdered 등 무엇이든 경로 목록으로 변환."""
        if hasattr(items, "keys"):
            return list(items.keys())
        return list(items)

    try:
        dd = DeepDiff(current, latest, ignore_order=True)
        parts: list[str] = []

        for path, chg in (dd.get("values_changed") or {}).items():
            parts.append(f"값 변경 {_fmt_path(path)}: "
                         f"{chg['old_value']!r} → {chg['new_value']!r}")
        for path, chg in (dd.get("type_changes") or {}).items():
            parts.append(f"타입 변경 {_fmt_path(path)}: "
                         f"{chg['old_value']!r} → {chg['new_value']!r}")
        for label, key in [("항목 추가", "dictionary_item_added"),
                           ("항목 삭제", "dictionary_item_removed"),
                           ("조건 추가", "iterable_item_added"),
                           ("조건 삭제", "iterable_item_removed")]:
            items = dd.get(key)
            if items:
                paths = _paths_of(items)
                parts.append(f"{label} {len(paths)}건: "
                             + ", ".join(_fmt_path(str(p)) for p in paths[:3])
                             + ("…" if len(paths) > 3 else ""))
    except Exception as e:  # noqa: BLE001 — 요약은 보조 정보, 실패해도 작업은 계속
        logger.warning("변경 요약 생성 실패(무시하고 진행): %s", e)
        return ""

    if not parts:
        return ""
    if len(parts) > max_items:
        parts = parts[:max_items] + [f"…외 {len(parts) - max_items}건"]
    return " | ".join(parts)


def normalize_context(ctx: str | None) -> str:
    """Adobe는 context 필드 생략 시 'hits'로 동작 → 비교 전 정규화."""
    return ctx or "hits"


def find_embedded_nodes(obj: Any, marker_name: str, path: str = "$") -> list[tuple[str, dict]]:
    """
    definition 안에서 description == marker_name 이면서 pred를 가진 **노드 자체**를
    찾아 (JSON경로, 노드dict) 목록으로 반환.

    ⚠️ 최상위(outermost) 매칭만 수집: 매칭된 노드의 내부로는 더 들어가지 않음.
    자식 세그먼트 정의 안에 같은 이름의 마커가 중첩된 경우(자기 참조형 구조),
    내부 마커는 자식 최신 로직의 '일부'이므로 별도 교체/검증 대상이 아님.
    (내부까지 들어가면 교체 시 무한 재귀 → RecursionError 발생)
    """
    hits: list[tuple[str, dict]] = []
    if isinstance(obj, dict):
        if obj.get("description") == marker_name and "pred" in obj:
            hits.append((path, obj))
            return hits  # 매칭 노드 내부로는 진입하지 않음
        for k, v in obj.items():
            hits.extend(find_embedded_nodes(v, marker_name, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            hits.extend(find_embedded_nodes(item, marker_name, f"{path}[{i}]"))
    return hits


def find_embedded_preds(obj: Any, marker_name: str, path: str = "$") -> list[tuple[str, Any]]:
    """(하위 호환) pred만 필요한 경우."""
    return [(p, node["pred"]) for p, node in find_embedded_nodes(obj, marker_name, path)]


@dataclass
class EmbeddedMatch:
    json_path: str
    similarity: float          # core(without 제거 후) 유사도
    diff_text: str
    context_current: str = "hits"   # 부모 안에 박제된 버킷
    context_latest: str = "hits"    # 자식의 최신 버킷
    exclude_wrappers: int = 0       # 부모 안 임베디드의 without 겹수 (보존 대상)
    exclude_latest: int = 0         # 자식 최신 최상위 without 겹수

    @property
    def polarity_warning(self) -> str | None:
        """Include/Exclude 상태가 자식과 어긋난 것으로 추정될 때 경고 문구.
        emb == latest   : 일반 Include 포함 → 정상
        emb == latest+1 : 일반 부모 Exclude 포함 → 정상 (보존)
        emb <  latest   : 자식이 최상위 Exclude를 추가한 듯 → 자동 반영 안 함
        emb >  latest+1 : 자식이 최상위 Exclude를 제거한 듯 → 수동 확인 필요"""
        if self.exclude_wrappers < self.exclude_latest:
            return ("자식 최상위 Exclude가 부모에 없음 "
                    "(자식 Include→Exclude 변경 추정 — 자동 반영 안 함, 수동 확인)")
        if self.exclude_wrappers > self.exclude_latest + 1:
            return ("부모 Exclude 겹수 과다 "
                    "(자식 Exclude→Include 변경 추정 — 수동 확인)")
        return None

    def __repr__(self):
        ctx = (f", 버킷 {self.context_current}≠{self.context_latest}"
               if self.context_mismatch else "")
        exc = f", Exclude x{self.exclude_wrappers}" if self.exclude_wrappers else ""
        warn = " ⚠️극성" if self.polarity_warning else ""
        return f"<Match {self.json_path} {self.similarity}%{ctx}{exc}{warn}>"

    @property
    def context_mismatch(self) -> bool:
        return self.context_current != self.context_latest

    @property
    def pred_outdated(self) -> bool:
        return self.similarity < 100


@dataclass
class ParentIntegrity:
    parent_id: str
    parent_name: str
    matches: list[EmbeddedMatch] = field(default_factory=list)

    def __repr__(self):
        ctx = " 🪣버킷차이" if self.has_context_mismatch else ""
        return (f"<{self.parent_name} ({self.parent_id}) "
                f"{self.worst_similarity}% x{len(self.matches)}곳{ctx}>")

    @property
    def worst_similarity(self) -> float:
        return min((m.similarity for m in self.matches), default=100.0)

    @property
    def has_context_mismatch(self) -> bool:
        return any(m.context_mismatch for m in self.matches)

    @property
    def pred_outdated(self) -> bool:
        return any(m.pred_outdated for m in self.matches)

    @property
    def polarity_warnings(self) -> list[str]:
        return [m.polarity_warning for m in self.matches if m.polarity_warning]


def check_child_integrity(repo: SegmentRepository, child_ref: str,
                          verbose: bool = True,
                          context_policy: str = "warn",
                          policy=None) -> list[ParentIntegrity]:
    """
    특정 자식 기준으로 모든 부모의 동기화 상태 점검. 업데이트 대상 부모만 반환.

    policy (SyncPolicy, 선택):
      전달하면 정책상 업데이트하지 않는 부모(protected_parent_ids, skip_pairs)를
      결과와 표에서 제외하고, 제외 건수만 한 줄로 요약. 자식이 excluded_child_ids면
      빈 결과 반환. 의존성 그래프 자체는 건드리지 않음 — 영향도 분석은 온전히 유지.

    context_policy (버킷 차이 처리):
      "warn"  : pred 불일치만 업데이트 대상. 버킷 차이는 ℹ️ 참고 정보로만 출력 (기본)
                → 부모 안에서 의도적으로 다른 버킷을 쓰는 케이스를 존중
      "sync"  : 버킷 차이도 불일치로 취급 (업데이트 대상에 포함)
      "ignore": 버킷 차이를 계산·표시하지 않음
    """
    child = repo.resolve(child_ref)
    latest = child.root_pred
    if latest is None:
        raise ValueError(f"'{child.name}'({child.id})의 definition.container.pred 를 찾을 수 없습니다.")
    latest_ctx = normalize_context(child.root_context)
    latest_n, latest_core = strip_leading_withouts(latest)

    # 정책: 자식 자체가 동기화 소스 제외 대상이면 점검 생략
    if policy is not None and policy.is_child_blocked(child.id):
        if verbose:
            print(f"\n⏭️ '{child.name}' ({child.id})는 정책상 동기화 제외 대상 "
                  f"(excluded_child_ids) — 점검 생략")
        return QuietList([])

    results: list[ParentIntegrity] = []
    context_only: list[ParentIntegrity] = []   # pred는 일치, 버킷만 다른 부모 (참고용)
    polarity_only: list[ParentIntegrity] = []  # core는 일치, Incl/Excl 상태만 어긋난 추정
    policy_excluded: list[str] = []            # 정책으로 걸러진 부모명 (요약 표시용)
    parent_ids = repo.get_affected_parent_ids(child.id)

    for pid in parent_ids:
        # 정책: 보호 부모 / 제외 쌍은 계산 자체를 건너뜀 (리포트 노이즈 제거)
        if policy is not None and (
                policy.is_parent_blocked(pid)
                or policy.pair_block_reason(pid, child.id,
                                            repo.segments[pid].name, child.name)):
            policy_excluded.append(repo.segments[pid].name)
            continue
        parent = repo.segments[pid]
        report = ParentIntegrity(parent_id=pid, parent_name=parent.name)
        for path, node in find_embedded_nodes(parent.definition, child.name):
            # Include/Exclude(without 겹수)는 부모 소유 — 양쪽 core끼리만 비교
            emb_n, emb_core = strip_leading_withouts(node["pred"])
            sim = similarity_pct(latest_core, emb_core)
            diff = "" if sim == 100 else pred_diff_text(latest_core, emb_core)
            report.matches.append(EmbeddedMatch(
                json_path=path, similarity=sim, diff_text=diff,
                context_current=normalize_context(node.get("context")),
                context_latest=latest_ctx,
                exclude_wrappers=emb_n,
                exclude_latest=latest_n,
            ))
        if report.pred_outdated or (context_policy == "sync" and report.has_context_mismatch):
            results.append(report)
        elif context_policy == "warn" and report.has_context_mismatch:
            context_only.append(report)
        elif report.polarity_warnings:
            polarity_only.append(report)

    results.sort(key=lambda r: r.worst_similarity)

    if verbose:
        print(f"\n🎯 대상 자식: {child.name} ({child.id}) | 최신 버킷: {latest_ctx}")
        print(f"📊 부모 {len(parent_ids)}개 중 {len(results)}개 불일치"
              + (f" (정책 제외 {len(policy_excluded)}개)" if policy_excluded else ""))
        print("=" * 110)
        print(f"{'No.':<4}| {'일치율':<9}| {'부모 세그먼트':<38} | {'부모 ID':<28} | {'포함 위치':<8}| 비고")
        print("-" * 110)
        for i, r in enumerate(results, 1):
            icon = "🔴" if r.worst_similarity < 60 else "🟡"
            notes = []
            exc = sum(m.exclude_wrappers for m in r.matches)
            if exc:
                notes.append(f"🚫Exclude x{exc} 유지")
            if r.polarity_warnings:
                notes.append("⚠️ " + r.polarity_warnings[0])
            if context_policy != "ignore" and r.has_context_mismatch:
                pairs = {f"{m.context_current}≠{m.context_latest}"
                         for m in r.matches if m.context_mismatch}
                verb = "동기화 예정" if context_policy == "sync" else "유지"
                notes.append(f"🪣 버킷 차이({verb}): {', '.join(sorted(pairs))}")
            print(f"{i:02d}  | {r.worst_similarity:>6.2f}% {icon}| {r.parent_name:<38} "
                  f"| {r.parent_id:<28} | {len(r.matches)}곳{'':<5}| {', '.join(notes)}")
        if not results:
            print("✅ 업데이트가 필요한 부모가 없습니다.")
        if polarity_only:
            print("-" * 110)
            print(f"⚠️ 조건(core)은 일치하지만 Include/Exclude 상태가 어긋난 것으로 추정되는 "
                  f"부모 {len(polarity_only)}개 (자동 변경 안 함 — 수동 확인 권장):")
            for r in polarity_only:
                print(f"   · {r.parent_name} ({r.parent_id}) | {r.polarity_warnings[0]}")
        if policy_excluded:
            print("-" * 110)
            print(f"⏭️ 정책 제외로 표시하지 않은 부모 {len(policy_excluded)}개: "
                  + ", ".join(policy_excluded[:5])
                  + (" …" if len(policy_excluded) > 5 else ""))
        if context_only:
            print("-" * 110)
            print(f"ℹ️ pred는 일치하지만 버킷이 다른 부모 {len(context_only)}개 "
                  f"(의도적일 수 있어 수정하지 않음):")
            for r in context_only:
                pairs = {f"{m.context_current}≠{m.context_latest}"
                         for m in r.matches if m.context_mismatch}
                print(f"   · {r.parent_name} ({r.parent_id}) | {', '.join(sorted(pairs))}")
    return QuietList(results)


def audit_all(repo: SegmentRepository, verbose: bool = True,
              context_policy: str = "warn",
              policy=None) -> dict[str, list[ParentIntegrity]]:
    """
    모든 자식 전수 점검. {child_id: [불일치 부모...]} 반환.

    policy (SyncPolicy, 선택): 전달하면 정책 제외 대상(excluded_child_ids,
    protected_parent_ids, skip_pairs)을 점검·표에서 제외 → 조치 가능한 항목만 표시.
    """
    report: dict[str, list[ParentIntegrity]] = {}
    skipped_children = 0
    for child_id in sorted(repo.parents_of.keys(),
                           key=lambda i: repo.segments[i].name):
        child = repo.segments[child_id]
        if child.root_pred is None:
            continue
        if policy is not None and policy.is_child_blocked(child_id):
            skipped_children += 1
            continue
        outdated = check_child_integrity(repo, child_id, verbose=False,
                                         context_policy=context_policy,
                                         policy=policy)
        if outdated:
            report[child_id] = outdated

    if verbose:
        total_children = len(repo.parents_of)
        print("\n" + "=" * 130)
        print(f"📊 [종합 점검] 자식 {total_children}개 중 {len(report)}개에서 불일치 감지"
              + (f" (정책 제외 자식 {skipped_children}개)" if skipped_children else ""))
        print("=" * 130)
        print(f"{'일치율':<10}| {'자식 세그먼트':<32} | {'자식 ID':<26} "
              f"| {'부모 세그먼트':<32} | {'부모 ID':<26} | 비고")
        print("-" * 130)
        for child_id, parents in report.items():
            cname = repo.segments[child_id].name
            for r in parents:
                icon = "🔴" if r.worst_similarity < 60 else "🟡"
                note = "🪣버킷차이" if r.has_context_mismatch and context_policy != "ignore" else ""
                print(f"{r.worst_similarity:>6.2f}% {icon}| {cname:<32} | {child_id:<26} "
                      f"| {r.parent_name:<32} | {r.parent_id:<26} | {note}")
        if not report:
            print("✅ 모든 세그먼트가 100% 동기화 상태입니다.")
    return QuietDict(report)
