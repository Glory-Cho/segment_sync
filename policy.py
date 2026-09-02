"""
policy.py — 어떤 세그먼트를 건드려도 되는지에 대한 '규칙'을 코드 밖(YAML)으로 분리.

요청사항 6번 해결:
  - 이름이 같아도 절대 업데이트하면 안 되는 세그먼트 → protected_parent_ids
  - 특정 자식은 동기화 대상에서 제외 → excluded_child_ids
  - "이 부모 안의 이 자식만" 콕 집어 제외 → skip_pairs
모든 규칙은 이름이 아니라 **세그먼트 ID** 기준. 이름은 중복될 수 있지만 ID는 유일하기 때문.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SkipRule:
    """
    "특정 부모 안에서, 특정 조건에 맞는 자식만 갱신하지 마라"를 표현하는 규칙.

    용도 예: 부모 A가 Flagship 제품군 세그먼트들의 로직을 '일부만' 의도적으로
    발췌해 담고 있어 자동 동기화되면 안 되지만, 같은 부모 안의 다른 자식(B)은
    정상적으로 동기화되어야 하는 경우.

    부모 조건과 자식 조건이 **둘 다 매칭될 때만** 차단한다.
    각 조건은 ids / name_contains / name_regex 중 아무거나 조합 가능하며,
    같은 쪽 안에서는 OR로 평가된다. 이름 매칭은 **대소문자를 구분**한다.

    안전장치: 부모 쪽·자식 쪽 각각 최소 1개의 조건이 있어야 한다.
    (한쪽을 비우면 '모든 부모' 또는 '모든 자식'이 되어 광범위 차단 사고로 이어짐.
     전체 차단이 목적이면 protected_parent_ids / excluded_child_ids를 쓸 것.)
    """
    name: str = ""                       # 규칙 설명 (리포트에 표시됨)
    parent_ids: set[str] = field(default_factory=set)
    parent_name_contains: list[str] = field(default_factory=list)
    parent_name_regex: str | None = None
    child_ids: set[str] = field(default_factory=set)
    child_name_contains: list[str] = field(default_factory=list)
    child_name_regex: str | None = None

    def __post_init__(self):
        has_parent = bool(self.parent_ids or self.parent_name_contains
                          or self.parent_name_regex)
        has_child = bool(self.child_ids or self.child_name_contains
                         or self.child_name_regex)
        if not has_parent or not has_child:
            raise ValueError(
                f"skip_rule '{self.name or '(이름없음)'}': 부모 조건과 자식 조건이 "
                f"각각 최소 1개씩 필요합니다. 한쪽만 지정하면 광범위 차단이 되므로, "
                f"그 경우 protected_parent_ids 또는 excluded_child_ids를 사용하세요.")

    @staticmethod
    def _side_matches(seg_id: str, seg_name: str, ids: set[str],
                      contains: list[str], regex: str | None) -> bool:
        if seg_id in ids:
            return True
        if any(kw in seg_name for kw in contains):      # 대소문자 구분
            return True
        if regex:
            import re
            if re.search(regex, seg_name):
                return True
        return False

    def matches(self, parent_id: str, parent_name: str,
                child_id: str, child_name: str) -> bool:
        return (self._side_matches(parent_id, parent_name, self.parent_ids,
                                   self.parent_name_contains, self.parent_name_regex)
                and self._side_matches(child_id, child_name, self.child_ids,
                                       self.child_name_contains, self.child_name_regex))


@dataclass
class SyncPolicy:
    # 이 ID를 가진 부모 세그먼트는 어떤 경우에도 수정하지 않음
    protected_parent_ids: set[str] = field(default_factory=set)
    # 이 ID를 가진 자식은 동기화 소스로 사용하지 않음
    excluded_child_ids: set[str] = field(default_factory=set)
    # (parent_id, child_id) 쌍 단위로 제외 — "이 부모 안에서는 이 자식을 갱신하지 마라"
    skip_pairs: set[tuple[str, str]] = field(default_factory=set)
    # 이름 패턴 기반 쌍 제외 규칙 (skip_pairs의 확장 — 새 세그먼트도 자동 적용)
    skip_rules: list[SkipRule] = field(default_factory=list)
    # 부모 안 자식의 버킷(context)이 자식 최신과 다를 때 처리 방식
    #   "warn"  : 수정하지 않고 리포트에만 표시 (기본값 — 의도적 차이일 수 있음)
    #   "sync"  : 자식의 최신 버킷으로 강제 동기화
    #   "ignore": 아예 표시하지 않음
    context_mismatch_action: str = "warn"
    # push 직전 서버의 modified 타임스탬프를 재확인 — 로드 이후 다른 사람이
    # 수정했으면 push를 중단 (덮어쓰기 사고 방지). 부모당 fetch 1회 추가 비용.
    check_concurrent_edit: bool = True
    # True면 plan만 만들고 push는 명시적으로 apply(confirm=True)를 요구
    require_confirmation: bool = True
    # API 재시도 횟수 / 대기(초)
    max_retries: int = 3
    retry_backoff_sec: float = 2.0
    # 백업 루트 디렉토리
    backup_root: str = "backups"

    def is_parent_blocked(self, parent_id: str) -> bool:
        return parent_id in self.protected_parent_ids

    def is_child_blocked(self, child_id: str) -> bool:
        return child_id in self.excluded_child_ids

    def is_pair_blocked(self, parent_id: str, child_id: str) -> bool:
        """ID 기반 쌍 제외만 판정 (하위 호환). 이름 규칙까지 보려면 pair_block_reason 사용."""
        return (parent_id, child_id) in self.skip_pairs

    def pair_block_reason(self, parent_id: str, child_id: str,
                          parent_name: str = "", child_name: str = "") -> str | None:
        """이 (부모, 자식) 조합이 차단되면 사유 문자열, 아니면 None."""
        if (parent_id, child_id) in self.skip_pairs:
            return "쌍 단위 제외 (skip_pairs)"
        for rule in self.skip_rules:
            if rule.matches(parent_id, parent_name, child_id, child_name):
                return f"규칙 제외: {rule.name or '(이름없는 skip_rule)'}"
        return None


def load_policy(path: str | Path) -> SyncPolicy:
    """YAML 또는 JSON 정책 파일 로드. 파일이 없으면 기본 정책(전부 허용) 반환."""
    p = Path(path)
    if not p.exists():
        logger.warning("정책 파일 %s 없음 → 기본 정책 사용(제외 목록 없음)", p)
        return SyncPolicy()

    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        import yaml  # pip install pyyaml
        raw = yaml.safe_load(text) or {}
    else:
        import json
        raw = json.loads(text)

    def _as_list(v):
        if v is None:
            return []
        return [v] if isinstance(v, str) else list(v)

    rules = []
    for raw_rule in raw.get("skip_rules", []) or []:
        rules.append(SkipRule(
            name=raw_rule.get("name", ""),
            parent_ids=set(raw_rule.get("parent_ids", []) or []),
            parent_name_contains=_as_list(raw_rule.get("parent_name_contains")),
            parent_name_regex=raw_rule.get("parent_name_regex"),
            child_ids=set(raw_rule.get("child_ids", []) or []),
            child_name_contains=_as_list(raw_rule.get("child_name_contains")),
            child_name_regex=raw_rule.get("child_name_regex"),
        ))

    return SyncPolicy(
        protected_parent_ids=set(raw.get("protected_parent_ids", [])),
        excluded_child_ids=set(raw.get("excluded_child_ids", [])),
        skip_pairs={tuple(pair) for pair in raw.get("skip_pairs", [])},
        skip_rules=rules,
        context_mismatch_action=raw.get("context_mismatch_action", "warn"),
        check_concurrent_edit=raw.get("check_concurrent_edit", True),
        require_confirmation=raw.get("require_confirmation", True),
        max_retries=raw.get("max_retries", 3),
        retry_backoff_sec=raw.get("retry_backoff_sec", 2.0),
        backup_root=raw.get("backup_root", "backups"),
    )
