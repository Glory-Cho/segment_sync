# segment_sync

Adobe Analytics 세그먼트가 몇 백 개로 불어나면 "이 자식 세그먼트, 부모 바뀐 거 반영됐나?"를
사람 눈으로 확인하는 게 불가능해진다. 이 패키지는 그 확인·정리를 대신 해준다 — 세그먼트 족보
(부모-자식) 무결성 점검 + 정책 기반 동기화 + 변경 리포트까지.

> 실행용 노트북이 아니라 **엔진 자체**다. 세그먼트 ID, 정책 파일(`sync_policy.yaml`)은 회사
> 내부 정보라 여기 올라오지 않는다 — import해서 쓰는 라이브러리만 있다.

## 뭘 하는 도구인가

- **로드(`SegmentRepository`)**: 세그먼트 ID 목록을 병렬로 긁어와 이름 중복/보류 상태까지 진단
- **점검(`check_child_integrity`, `audit_all`)**: 부모-자식 세그먼트 트리를 그려주고, 정책상
  제외 대상을 뺀 나머지가 실제로 부모와 일치하는지 표로 보여줌
- **동기화(`SyncEngine`)**: 계획(`plan`) → 미리보기(dry-run) → 실제 반영(`apply`)의 3단계.
  적용 전에 백업 + 검증 + 리포트까지 자동
- **리포트(`save_reports_to_excel`)**: 변경 건수만큼 xlsx로 기록

## 구성

```
segment_sync/
├── __init__.py       # 공개 API (아래 "사용법" 참고)
├── repository.py      # SegmentRepository — 로드/이름 중복 진단/조회
├── integrity.py         # 부모-자식 무결성 점검 (check_child_integrity, audit_all)
├── policy.py               # SyncPolicy — sync_policy.yaml 로더 (보호 대상, 예외 규칙 등)
├── sync_engine.py             # SyncEngine — plan → preview → apply
├── reporting.py                  # 결과를 엑셀 리포트로 저장
└── cli.py                            # 커맨드라인 진입점
```

## 사용법

```python
from segment_sync import SegmentRepository, SyncEngine, load_policy

repo = SegmentRepository(ags)
repo.load(seglist, max_workers=10)

policy = load_policy("sync_policy.yaml")   # 로컬 정책 파일 — 저장소에는 없음
engine = SyncEngine(ags, repo, policy)

plan = engine.plan("<child_segment_id>", comment="조건 변경")
plan.show()                 # 무엇이 어떻게 바뀌는지 미리보기 (dry-run)
engine.apply(plan)          # 실제 반영 (백업 + 검증 + 리포트)
```

## 필요한 것

```bash
pip install aanalytics2 pandas openpyxl pyyaml
pip install deepdiff   # 구조 diff 요약용, 선택
```

## 주의

- `sync_policy.yaml`(보호 세그먼트 ID, 제외 규칙)과 실제 세그먼트 ID가 들어가는 노트북/스크립트는
  **절대 이 저장소에 올리지 않는다** — `.gitignore`로도 막아둠.
- 이 저장소는 라이브러리 코드만 담는다. 실행은 사내 노트북에서, `sync_policy.yaml`은 로컬에만
  둘 것.
