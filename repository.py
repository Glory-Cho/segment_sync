"""
repository.py — 세그먼트 로딩 + 의존성 그래프.

기존 코드와의 핵심 차이:
1. name_to_id 딕셔너리가 이름 중복 시 **조용히 덮어쓰던 버그** 제거.
   → ids_by_name: {이름: [id, id, ...]} 로 변경, 중복 발견 시 경고 로그.
2. 의존성 그래프를 이름이 아닌 **ID 기준**으로 저장 (parents_of / children_of).
3. fetch 실패 시 재시도(지수 백오프) + 실패 목록을 load 결과로 반환 → 몇 개가
   왜 빠졌는지 항상 알 수 있음.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class AmbiguousNameError(Exception):
    """같은 이름의 세그먼트가 2개 이상이라 이름만으로 특정할 수 없을 때."""


@dataclass
class Segment:
    id: str
    name: str
    definition: dict
    description: str
    full_form: dict  # 서버 응답 원본 (updateSegment 시 그대로 사용)

    @property
    def modified(self) -> str | None:
        """서버가 알려주는 마지막 수정 시각 — 동시 편집 감지의 기준."""
        return self.full_form.get("modified")

    @property
    def root_container(self) -> dict | None:
        """최상위 container 노드 (context + pred를 모두 가진 의미 단위)."""
        try:
            c = self.definition["container"]
            return c if isinstance(c, dict) else None
        except (KeyError, TypeError):
            return None

    @property
    def root_pred(self) -> Any:
        """이 세그먼트의 '기준 로직'. 구조가 다르면 None (예외 대신)."""
        return (self.root_container or {}).get("pred")

    @property
    def root_context(self) -> str | None:
        """Hit/Visit/Visitor 버킷. Adobe 기본값은 'hits' (필드 생략 가능)."""
        return (self.root_container or {}).get("context")


@dataclass
class LoadResult:
    loaded: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (id, 사유)
    duplicated_names: dict[str, list[str]] = field(default_factory=dict)


class Selection(list):
    """
    repo.find()의 결과. 세그먼트 ID 리스트이면서 show()로 매칭 내역을 확인할 수 있다.
    Jupyter 셀 마지막 줄에 두어도 자동 출력되지 않음 (show()로 명시적 확인).
    """
    def __init__(self, ids, repo, criteria: str = "", filtered_out: list | None = None):
        super().__init__(ids)
        self._repo = repo
        self._criteria = criteria
        self._filtered_out = filtered_out or []   # 자식이 아니라 제외된 (name, id)

    def _ipython_display_(self):
        pass

    def __repr__(self):
        return f"<Selection {len(self)}개 ({self._criteria})>"

    def show(self) -> None:
        print(f"\n🔍 검색 조건: {self._criteria}")
        print(f"📌 매칭된 자식 세그먼트 {len(self)}개")
        print("=" * 88)
        if not self:
            print("(매칭 없음)")
        else:
            print(f"{'No.':<4}| {'세그먼트명':<44} | {'세그먼트 ID':<26}| 부모 수")
            print("-" * 88)
            for i, sid in enumerate(self, 1):
                seg = self._repo.segments[sid]
                n_parents = len(self._repo.parents_of.get(sid, ()))
                print(f"{i:02d}  | {seg.name:<44} | {sid:<26}| {n_parents}개")
        if self._filtered_out:
            print("-" * 88)
            print(f"⏭️ 이름은 맞지만 자식으로 쓰이지 않아 제외된 세그먼트 "
                  f"{len(self._filtered_out)}개:")
            for name, sid in self._filtered_out[:5]:
                print(f"   · {name} ({sid})")
            if len(self._filtered_out) > 5:
                print(f"   … 외 {len(self._filtered_out) - 5}개")


class SegmentRepository:
    def __init__(self, client, max_retries: int = 3, retry_backoff_sec: float = 2.0):
        """
        client: aanalytics2의 Analytics 객체 (ags). getSegment / updateSegment 를 가진 객체.
        """
        self._client = client
        self._max_retries = max_retries
        self._backoff = retry_backoff_sec

        self.segments: dict[str, Segment] = {}          # {id: Segment}
        self.ids_by_name: dict[str, list[str]] = {}     # {name: [id, ...]}
        self.children_of: dict[str, set[str]] = {}      # {parent_id: {child_id}}
        self.parents_of: dict[str, set[str]] = {}       # {child_id: {parent_id}}
        # 이름은 알지만 로드된 세그먼트 중 매칭이 모호했던 참조 기록
        self.unresolved_refs: list[tuple[str, str]] = []  # (parent_id, child_name)

    # ------------------------------------------------------------------ load

    def _fetch_one(self, segment_id: str) -> Segment | None:
        last_err: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                raw = self._client.getSegment(segment_id, True)
                if not raw or "name" not in raw:
                    raise ValueError(f"비정상 응답: {raw!r:.200}")
                return Segment(
                    id=segment_id,
                    name=raw["name"],
                    definition=raw.get("definition", {}),
                    description=raw.get("description", ""),
                    full_form=raw,
                )
            except Exception as e:  # noqa: BLE001 — API 예외 종류가 불명확
                last_err = e
                wait = self._backoff * attempt
                logger.warning("fetch 실패 %s (시도 %d/%d): %s → %.1fs 후 재시도",
                               segment_id, attempt, self._max_retries, e, wait)
                time.sleep(wait)
        logger.error("fetch 최종 실패 %s: %s", segment_id, last_err)
        self._last_error = str(last_err)
        return None

    def load(self, id_list: list[str], max_workers: int = 10,
             progress: Callable[[int, int], None] | None = None) -> LoadResult:
        """세그먼트 병렬 로드. 실패/이름중복을 LoadResult로 리포팅."""
        result = LoadResult()
        total = len(id_list)
        done = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(self._fetch_one, sid): sid for sid in id_list}
            for fut in concurrent.futures.as_completed(futures):
                sid = futures[fut]
                seg = fut.result()
                done += 1
                if progress:
                    progress(done, total)
                if seg is None:
                    result.failed.append((sid, getattr(self, "_last_error", "unknown")))
                    continue
                self.segments[seg.id] = seg
                self.ids_by_name.setdefault(seg.name, []).append(seg.id)
                result.loaded += 1

        # 이름 중복 감지 — 요청사항 6번의 근본 원인 가시화
        for name, ids in self.ids_by_name.items():
            if len(ids) > 1:
                result.duplicated_names[name] = ids
                logger.warning("⚠️ 이름 중복: '%s' → %s (이름 기반 조회 시 명시적 ID 필요)",
                               name, ids)

        self._build_graph()
        logger.info("로드 완료: %d개 성공, %d개 실패, 중복 이름 %d건",
                    result.loaded, len(result.failed), len(result.duplicated_names))
        return result

    # ------------------------------------------------------------- resolution

    def resolve(self, ref: str) -> Segment:
        """
        ID 또는 이름으로 세그먼트 특정.
        - ID면 바로 반환
        - 이름이면 유일할 때만 반환, 중복이면 AmbiguousNameError (ID 목록 안내)
        """
        if ref in self.segments:
            return self.segments[ref]
        ids = self.ids_by_name.get(ref, [])
        if len(ids) == 1:
            return self.segments[ids[0]]
        if len(ids) > 1:
            raise AmbiguousNameError(
                f"'{ref}' 이름을 가진 세그먼트가 {len(ids)}개입니다: {ids}. "
                f"ID로 지정해 주세요."
            )
        raise KeyError(f"'{ref}' 를 찾을 수 없습니다 (로드되지 않았을 수 있음).")

    # ------------------------------------------------------------------ graph

    def _extract_child_names(self, obj: Any, valid_names: set[str],
                             self_name: str) -> list[str]:
        found: list[str] = []
        if isinstance(obj, dict):
            desc = obj.get("description")
            if desc in valid_names and desc != self_name:
                found.append(desc)
            for v in obj.values():
                found.extend(self._extract_child_names(v, valid_names, self_name))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(self._extract_child_names(item, valid_names, self_name))
        return found

    def _build_graph(self) -> None:
        self.children_of.clear()
        self.parents_of.clear()
        self.unresolved_refs.clear()
        all_names = set(self.ids_by_name.keys())

        for parent_id, seg in self.segments.items():
            child_names = set(self._extract_child_names(seg.definition, all_names, seg.name))
            for cname in child_names:
                ids = self.ids_by_name.get(cname, [])
                if len(ids) != 1:
                    # 이름이 중복이라 어느 자식인지 특정 불가 → 자동 동기화 금지, 기록만
                    self.unresolved_refs.append((parent_id, cname))
                    continue
                child_id = ids[0]
                self.children_of.setdefault(parent_id, set()).add(child_id)
                self.parents_of.setdefault(child_id, set()).add(parent_id)

        if self.unresolved_refs:
            logger.warning("이름 중복으로 매핑을 보류한 참조 %d건 (repo.unresolved_refs 확인)",
                           len(self.unresolved_refs))

    def get_affected_parent_ids(self, child_id: str) -> list[str]:
        return sorted(self.parents_of.get(child_id, set()))

    # ------------------------------------------------------------------ stats

    def find(self, contains: str | list[str] | None = None,
             regex: str | None = None,
             exclude: str | list[str] | None = None,
             children_only: bool = True,
             min_keyword_len: int = 2,
             all_segments: bool = False) -> "Selection":
        """
        세그먼트 '이름'으로 대상을 검색해 ID 목록(Selection)을 반환. 서버 호출 없음.

        contains : 포함할 키워드. 리스트면 OR 조건. **대소문자를 구분**한다.
        regex    : 정규식 패턴 (contains 대신 사용)
        exclude  : 이 키워드가 이름에 있으면 제외 (리스트면 OR)
        children_only : True(기본)면 다른 세그먼트에 포함된 적 있는 것(자식 역할)만
                        남기고, 나머지는 show()에서 '제외됨'으로 안내
        min_keyword_len : 키워드 최소 길이 (기본 2). 공란·1글자는 광범위 매칭
                          사고로 이어지므로 차단한다.
        all_segments : True면 조건 없이 전체 선택 (contains/regex 불필요).
                       의도적으로 전부를 대상 삼을 때만 명시적으로 사용.

        예) repo.find(contains="Cart")
            repo.find(contains=["Cart", "Checkout"], exclude="Legacy")
            repo.find(regex=r"^\\[Global\\].*2026Q3")
            repo.find(all_segments=True)
        """
        import re as _re

        def _as_list(v):
            if v is None:
                return []
            return [v] if isinstance(v, str) else list(v)

        includes = _as_list(contains)
        excludes = _as_list(exclude)

        if not all_segments and not includes and not regex:
            raise ValueError(
                "검색 조건이 없습니다. contains 또는 regex를 지정하거나, "
                "정말 전체를 대상으로 하려면 all_segments=True 를 명시하세요.")

        for kw in includes + excludes:
            if len(kw.strip()) < min_keyword_len:
                raise ValueError(
                    f"키워드 '{kw}'가 너무 짧습니다 (최소 {min_keyword_len}자). "
                    f"광범위 매칭을 막기 위한 제한입니다. "
                    f"전체를 대상으로 하려면 all_segments=True 를 사용하세요.")

        pattern = _re.compile(regex) if regex else None   # 대소문자 구분

        matched, filtered_out = [], []
        for sid, seg in self.segments.items():
            name = seg.name
            if all_segments:
                ok = True
            elif pattern is not None:
                ok = bool(pattern.search(name))
            else:
                ok = any(kw in name for kw in includes)     # 대소문자 구분
            if not ok:
                continue
            if any(kw in name for kw in excludes):
                continue
            if children_only and sid not in self.parents_of:
                filtered_out.append((name, sid))
                continue
            matched.append(sid)

        matched.sort(key=lambda i: self.segments[i].name)

        if all_segments:
            criteria = "전체 세그먼트(all_segments=True)"
        elif pattern is not None:
            criteria = f"regex={regex!r}"
        else:
            criteria = f"contains={includes!r} (대소문자 구분)"
        if excludes:
            criteria += f", exclude={excludes!r}"
        if children_only:
            criteria += ", 자식 세그먼트만"

        logger.info("find: %s → %d개 매칭", criteria, len(matched))
        return Selection(matched, self, criteria, filtered_out)

    def stats(self) -> dict:
        all_children = set().union(*self.children_of.values()) if self.children_of else set()
        return {
            "총 세그먼트": len(self.segments),
            "고유 이름": len(self.ids_by_name),
            "중복 이름": sum(1 for v in self.ids_by_name.values() if len(v) > 1),
            "부모(자식 포함) 세그먼트": len(self.children_of),
            "부품으로 쓰인 자식": len(all_children),
            "모호한 참조(보류)": len(self.unresolved_refs),
        }

    def show_tree(self) -> None:
        print("\n[전체 세그먼트 의존성 계층도]")
        for parent_id, child_ids in sorted(self.children_of.items(),
                                           key=lambda kv: self.segments[kv[0]].name):
            p = self.segments[parent_id]
            print(f"\n📂 {p.name} ({p.id})")
            for cid in sorted(child_ids, key=lambda i: self.segments[i].name):
                c = self.segments[cid]
                print(f"   └── 🧩 {c.name} ({c.id})")
