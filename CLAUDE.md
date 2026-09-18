# CLAUDE.md

SEC EDGAR 공시를 폴링해 긍정적 촉매제를 탐지하고 Telegram 알림 + GitHub Pages 대시보드를
갱신하는 모니터. 운영 중인 시스템이므로 변경 시 아래 제약을 먼저 확인할 것.

## 구조

```
scripts/catalyst_monitor.py   수집·분류·알림 전부. 단일 파일, 외부 의존성은 requests 하나
config.json                   민감도 설정. 코드 수정 없이 바꿀 수 있는 것은 전부 여기
docs/index.html               대시보드. docs/data/alerts.json 을 30초마다 fetch
docs/data/alerts.json         탐지 결과 피드 (최근 300건)
state/seen.json               중복 방지용 accession 기록 (최근 2500건)
.github/workflows/monitor.yml 5분 스케줄 + 잡 내부 30초 루프
deploy/                       Dockerfile, k8s 매니페스트
```

데이터 흐름: EDGAR getcurrent atom → 신규 accession 필터 → index.json → EX-99 우선 본문 파싱
→ 정규식 분류 + 네거티브 필터 → Telegram → alerts.json 갱신 → 커밋 → Pages 반영

## 절대 어기면 안 되는 것

- **토큰을 코드·설정·커밋에 넣지 않는다.** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `SEC_USER_AGENT`는 GitHub Secrets 또는 k8s Secret에서만 온다. 테스트 목적이어도 하드코딩 금지.
- **동시 실행 금지.** 워크플로의 `concurrency` 블록과 k8s `replicas: 1`은 중복 알림을 막는
  장치다. 제거하거나 늘리지 말 것.
- **`state/seen.json`을 임의로 비우지 않는다.** 비우면 다음 실행에서 기존 공시 수십 건이
  한꺼번에 알림으로 나간다. 초기화가 필요하면 반드시 `--bootstrap`을 먼저 돌린다.
- **운영 중 `dry-run` 모드를 Actions에서 실행하지 않는다.** 전송만 건너뛸 뿐 탐지한 공시를
  seen에 기록하고 커밋까지 하므로, 그 사이 나온 촉매제가 알림 없이 소모된다. 테스트는 로컬에서.
- **`--bootstrap`은 알림을 보내지 않는다**는 전제가 코드에 박혀 있다(`Telegram(dry_run=...)`).
  이 연결을 끊지 말 것.

## EDGAR 제약

- User-Agent에 연락 가능한 이메일이 없으면 전부 403. `SEC_USER_AGENT="이름 email@도메인"` 형식.
- 초당 10회 제한. `RateLimiter`가 6회로 제한하고 있으며 이 값을 올리지 말 것.
- `getcurrent` 피드는 폼당 최근 100건만 반환한다. 그보다 오래된 공시는 구조적으로 탐지 불가.
- 공백이 들어간 폼 타입(`SC TO-I`)은 타임아웃이 잦다. `config.json`의 `form_types`에서 제외된
  상태이며, 되살릴 때는 사이클 지연을 감수해야 한다.
- 공시 접수 후 피드 반영까지 1~3분 걸린다. 30초 폴링의 실효 지연은 그만큼.

## 자주 하는 변경

**오탐이 보일 때** — 해당 보도자료의 핵심 문구를 `HARD_NEGATIVE`에 추가하는 것이 가장 효과적이다.
카테고리 패턴을 좁히는 것보다 우선 고려할 것.

**알림이 너무 많을 때** — `config.json`의 `min_confidence`를 올린다(55 → 70). 코드를 건드리지 않는다.

**촉매제 카테고리 추가** — `CATALYSTS` 리스트에 dict 추가. `key`는 영문 소문자,
`docs/index.html`의 `TYPE_CLASS` 매핑과 배지 CSS 클래스도 같이 추가해야 UI에 색이 나온다.

**신뢰도 계산** — `45 + (카테고리 weight 합) + 4×(추가 매칭 수) - 5×(소프트 네거티브 수)`,
35~97로 클램프. weight를 바꾸면 기존 `min_confidence` 기준이 흔들리니 함께 검토할 것.

## 테스트

네트워크 없이 분류기만 검증하는 것이 기본이다. 실제 EDGAR를 두드리는 테스트는 만들지 말 것.

```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
import catalyst_monitor as m
print(m.classify('<검증할 공시 본문>'))
"
```

파이프라인 전체를 볼 때는 `Edgar.get`을 몽키패치해 고정 응답을 돌려주는 방식을 쓴다.
긍정 샘플과 **반드시 차단돼야 하는 악재 샘플**(did not meet primary endpoint, complete response
letter, 계약 파기)을 함께 확인할 것.

로컬 실행:

```bash
export SEC_USER_AGENT="박형규 qkrqkstm@gmail.com"
python scripts/catalyst_monitor.py --dry-run
```

## 운영 특성

- `monitor.yml`에는 `schedule` 트리거가 없다. GitHub 자체 cron은 부하가 높을 때 실측 기준
  몇 시간씩 지연되는 것을 확인해 제거했고, 대신 cron-job.org 같은 외부 서비스가 5분마다
  `workflow_dispatch` API(`POST /repos/{owner}/{repo}/actions/workflows/monitor.yml/dispatches`)를
  호출해 실행을 트리거한다. 인증용 fine-grained PAT는 해당 저장소의 `Actions: Read and write`
  권한만 부여해 외부 cron 서비스에만 저장되어 있고, 이 저장소에는 존재하지 않는다.
  30초는 잡 내부 루프로 근사한 것이지 보장되지 않는다. 진짜 30초가 필요하면 `deploy/k8s.yaml`.
- 외부 cron 트리거가 끊기면(토큰 만료 등) 이 워크플로는 더 이상 자동 실행되지 않는다.
  `workflow_dispatch`를 Actions 탭에서 수동 실행하거나 PAT를 재발급해 cron 서비스에 갱신할 것.
- 저장소는 public이어야 한다. private이면 Actions 사용량이 월 4만 분에 달한다.
- 알림이 0건이어도 `updated_at` 타임스탬프 때문에 매 실행 커밋이 발생한다. 하루 약 288커밋.
  줄이려면 `persist()`에서 내용 변화가 있을 때만 쓰도록 수정.
- 시세(Yahoo)는 비공식 엔드포인트이며 지연 데이터다. 실패해도 `price: null`로 넘어가게 되어
  있으니 여기에 재시도 로직을 넣지 말 것.

## 하지 말아야 할 제안

- 투자 판단·매매 추천·종목 평가는 이 저장소의 범위가 아니다. 탐지와 전달까지만.
- 신뢰도 점수를 "매수 신호 강도"처럼 해석하거나 그런 UI 문구를 넣지 않는다.
- 유료 시세·공시 API 도입은 먼저 묻는다. 무료 소스로 동작하는 것이 현재 설계 전제다.

## 언어 규칙
- 응답 및 설명: 한국어
- 계획(plan) 문서, TODO 항목: 한국어
- 커밋 메시지: 한국어, 변경 이유 중심
- 코드 주석: 한국어
- 변수명/함수명/파일명: 영어
- 스클비트가 출력하는 로그 메시지: 영어 (서버 로케일 및 grep 호환)
