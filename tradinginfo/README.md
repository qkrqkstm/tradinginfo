# Stock Catalyst Alert

SEC EDGAR 실시간 공시(8-K / 6-K / 425 / SC TO-I)를 폴링해서 **긍정적 촉매제**를 탐지하고
Telegram으로 알림을 보내며, GitHub Pages에 대시보드를 띄웁니다.

탐지 대상: `자사주매입` `주식소각` `M&A` `파트너십` `임상통과` `탑라인결과` `FDA` `대형계약` `지수편입` `가이던스상향` `배당`

---

## ⚠️ 먼저 읽을 것

**1. 채팅으로 공유한 봇 토큰은 폐기하고 재발급하세요.**
`8782520335:AAE_...` 는 이미 평문으로 노출됐습니다. BotFather에서 `/revoke` → `/token` 으로 새로 받고,
**절대 코드에 하드코딩하지 말고** GitHub Secrets에만 넣으세요. 이 저장소가 public이면
GitHub Secret Scanning이 커밋된 토큰을 자동 무효화합니다.

**2. GitHub Actions는 30초 주기를 보장하지 못합니다.**
- `schedule` cron의 최소 단위는 **5분**이고, 러너가 붐비면 지연되거나 실행이 누락됩니다.
- 그래서 이 저장소는 **5분마다 잡을 띄우고 잡 내부에서 30초 루프를 4분간 도는** 구조입니다.
  실질 커버리지는 약 80~90%, 최악의 경우 몇 분의 공백이 생깁니다.
- **진짜 무중단 30초가 필요하면** `deploy/k8s.yaml`로 쿠버네티스에 올리세요. 이미 클러스터를
  운영하시니 이쪽이 정답입니다. 공백 없이 30초 주기 + 웹페이지까지 한 파드에서 해결됩니다.

**3. 저장소는 public으로 두세요.**
private이면 이 워크플로가 월 약 40,000분을 소비합니다(무료 한도 2,000분). 요금이 나옵니다.

**4. SEC Fair Access 정책**
연락 가능한 이메일이 포함된 User-Agent가 없으면 EDGAR가 403을 돌려줍니다. 초당 10회 제한이며
이 스크립트는 6회로 제한합니다.

**5. 이건 투자 자문이 아닙니다.** 키워드 매칭 기반 탐지라 오탐/누락이 있습니다. 반드시 원문을 확인하세요.

---

## 구조

```
tradinginfo/
├── scripts/catalyst_monitor.py   # 수집 · 분류 · 알림 본체
├── config.json                   # 민감도 설정 (커밋하면 즉시 반영)
├── docs/index.html               # 대시보드 (GitHub Pages)
├── docs/data/alerts.json         # 탐지 결과 피드
├── state/seen.json               # 중복 방지용 accession 기록
├── .github/workflows/monitor.yml # 5분 스케줄 + 30초 내부 루프
└── deploy/                       # Dockerfile, k8s 매니페스트
```

동작 흐름:

```
EDGAR getcurrent atom (8-K/6-K/425/SC TO-I)
   → 신규 accession 필터 (state/seen.json)
   → index.json으로 문서 목록 조회 → EX-99 보도자료 우선 본문 파싱
   → 정규식 촉매제 분류 + 네거티브 필터(CRL, 임상중단, 계약파기 등)
   → 신뢰도 ≥ min_confidence 이면 Telegram 전송
   → docs/data/alerts.json 갱신 → 커밋/푸시 → Pages 반영
```

---

## 설치

### 1) 저장소에 올리기

```bash
git clone https://github.com/qkrqkstm/tradinginfo.git
cd tradinginfo
# 이 폴더의 파일들을 복사해 넣은 뒤
git add .
git commit -m "feat: US stock catalyst monitor"
git push origin main
```

### 2) Secrets 등록

저장소 → Settings → Secrets and variables → Actions → **New repository secret**

| 이름 | 값 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather에서 **재발급한** 토큰 |
| `TELEGRAM_CHAT_ID` | `-1004403452866` |
| `SEC_USER_AGENT` | `hkpark hkpark@saraminhr.co.kr` 형식 (이메일 필수) |

### 3) 봇을 채널에 추가

`-100...` 으로 시작하는 ID는 채널/슈퍼그룹입니다. 해당 채널에 봇을 **관리자로 초대**해야
메시지가 전송됩니다. 확인:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d chat_id=-1004403452866 -d text=테스트
```

### 4) 최초 부트스트랩 (필수)

그냥 켜면 이미 나와 있는 공시 수십 건이 한꺼번에 날아옵니다.
Actions 탭 → `catalyst-monitor` → **Run workflow** → mode: `bootstrap` 실행.
현재 공시들을 "이미 본 것"으로 표시만 하고 알림은 보내지 않습니다.

### 5) GitHub Pages 켜기

Settings → Pages → Source: `Deploy from a branch` → Branch: `main` / 폴더: `/docs` → Save.
1~2분 뒤 `https://qkrqkstm.github.io/tradinginfo/` 에서 열립니다.

### 6) 동작 확인

Run workflow → mode: `dry-run` 으로 실행하면 텔레그램 전송 없이 로그로만 탐지 결과를 볼 수 있습니다.

---

## 로컬 실행

```bash
pip install -r scripts/requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... SEC_USER_AGENT="hkpark you@example.com"

python scripts/catalyst_monitor.py --dry-run          # 1회, 전송 없음
python scripts/catalyst_monitor.py --loop --interval 30 --max-runtime 0
```

## 쿠버네티스 배포 (권장)

```bash
docker build -f deploy/Dockerfile -t harbor.example.com/tools/catalyst-monitor:0.1.0 .
docker push harbor.example.com/tools/catalyst-monitor:0.1.0

kubectl create ns catalyst
kubectl -n catalyst create secret generic catalyst-secrets \
  --from-literal=TELEGRAM_BOT_TOKEN='...' \
  --from-literal=TELEGRAM_CHAT_ID='-1004403452866' \
  --from-literal=SEC_USER_AGENT='hkpark hkpark@saraminhr.co.kr'
kubectl -n catalyst create configmap catalyst-ui --from-file=docs/index.html

kubectl apply -f deploy/k8s.yaml
```

`replicas: 1` 고정입니다. 2개 이상 뜨면 같은 공시를 중복 알림합니다.
UI를 수정하면 ConfigMap을 다시 만들고 (`kubectl create cm ... --dry-run=client -o yaml | kubectl apply -f -`)
파드를 롤아웃하세요.

---

## 튜닝

`config.json`:

| 키 | 기본값 | 설명 |
|---|---|---|
| `form_types` | `["8-K","6-K","425","SC TO-I"]` | 감시할 공시 폼 |
| `fetch_count` | `100` | 폼당 조회 건수 |
| `max_filings_per_cycle` | `40` | 사이클당 본문 분석 상한 (레이트리밋 보호) |
| `min_confidence` | `55` | 이 값 미만은 알림 안 함. **알림이 많으면 70으로 올리세요** |
| `fetch_quotes` | `true` | 시세 조회 (비공식 엔드포인트, 실패해도 무시) |

키워드 자체를 손보려면 `scripts/catalyst_monitor.py`의 `CATALYSTS` / `HARD_NEGATIVE` 리스트를 수정하세요.
`HARD_NEGATIVE`에 걸리면 무조건 제외되므로, 악재 오탐이 보이면 여기에 패턴을 추가하는 게 가장 효과적입니다.

---

## 알려진 한계

- **공시 시점 ≠ 발표 시점.** 기업이 보도자료를 먼저 내고 8-K는 몇 시간~하루 뒤에 올리는 경우가 흔합니다.
  속도가 중요하면 `scripts/catalyst_monitor.py`에 PR Newswire / GlobeNewswire RSS 소스를 추가해야 합니다.
- **커밋이 계속 쌓입니다.** 5분마다 최대 1커밋 → 하루 최대 288개. 주기적으로
  `git checkout --orphan`으로 히스토리를 정리하거나, k8s 배포로 전환하세요.
- 시세는 지연 데이터입니다. 실시간 체결가가 아닙니다.
- EDGAR `getcurrent` 피드는 접수 후 반영까지 보통 1~3분 걸립니다. 30초 폴링의 실효 지연은 그만큼입니다.
