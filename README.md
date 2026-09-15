# Cargo Fit

FCL 다중 목적지 컨테이너 적재 최적화 시뮬레이터.
NSGA-II로 **용적률 · 리핸들링 · 무게중심** 세 목적을 동시에 고려한 적재안을 만들고,
3D로 비교 · 검토합니다. (논문: Elahi et al. 2025 방법론 기반)

**데모** — <https://mir-yong.github.io/cargo-fit/> (설치 없이 결과 확인)
**실행 방법** — [START.md](START.md)

---

## 구조

```
├── server.py              # FastAPI 서버 (업로드 → 최적화 → 결과 · 명세서)
├── run.py                 # CLI 솔버 (CSV in → JSON out)
├── batch.py               # 여러 instance 일괄 실행
├── requirements.txt
├── Dockerfile             # 배포용 (wkhtmltopdf · 한글 폰트 포함)
│
├── clp/                   # 최적화 엔진
│   ├── model.py           #   Box · Container · Placement, CSV 로더
│   ├── dblf.py            #   DBLF 디코더 (극점 · feasibility · 지지면적 · 적재 단수)
│   ├── objectives.py      #   Z1/Z2/Z3 평가 + 박스 랭킹
│   ├── nsga2.py           #   NSGA-II 엔진
│   ├── heuristic.py       #   휴리스틱 솔버 (시간 예산 기반)
│   ├── load_sequence.py   #   적재 순서 (지지 · 접근 의존성 위상정렬)
│   ├── solutions.py       #   해 선택 (3종 프리셋 · 가중치 · 그래프 점)
│   ├── pallet.py          #   팔레타이징
│   └── manifest.py        #   작업명세서 PDF
│
├── web/                   # 프론트엔드 (원본)
│   ├── index.html         #   단일 페이지 앱 (Three.js 3D 뷰어)
│   ├── demo-result.json   #   정적 데모용 사전 계산 결과
│   └── brand/             #   아이콘
│
├── docs/                  # GitHub Pages 배포본 (build_static.py가 생성)
└── tools/build_static.py  # web/ → docs/ 정적 데모 빌드
```

## 실행

```bash
pip install -r requirements.txt   # Python 3.10 이상
python3 server.py                 # → http://localhost:8000
```

CLI만 쓸 때:

```bash
python3 run.py --csv modified_BR6_full.csv --list
python3 run.py --csv modified_BR6_full.csv --instance 1 --pop 30 --gen 30
```

## 최적화

**목적함수 3종** (파레토 최적화)

| | 지표 | 방향 |
|---|---|---|
| Z1 | 용적률 | 최대화 |
| Z2 | ULO (리핸들링 유발 선행쌍) | 최소화 |
| Z3 | 무게중심 편차 | 최소화 |

**물리 제약**

| 제약 | 설명 | 기본값 |
|---|---|---|
| 지지면적 | 바닥이 아니면 아래 화물이 밑면을 받쳐야 하는 비율 | 80% |
| 적재 가능 | `stackable=0` 또는 `fragile=1`인 화물 위에는 적재 금지 | — |
| 최대 적재 단수 | 몇 단까지 쌓을지 | 제한 없음 |
| 무게중심 허용오차 | 초과 시 목적함수에 페널티 | 5% |
| 중량 한도 | 컨테이너 최대 적재중량 | 규격별 |
| 하역지 구역 | 먼저 내릴 화물을 문 쪽에 배치 (해제 가능) | 적용 |

지지면적 · 최대 적재 단수 · 무게중심 허용오차는 화면의 **고급 설정** 또는
`POST /api/optimize`의 `min_support_ratio` · `max_stack_levels` · `cg_tolerance`로 조정합니다.

**해 선택** — 파레토 프론트에서 3종(최대 용적률 / 균형 / 최소 리핸들링)을 제시하고,
가중치 슬라이더로 그 사이 임의 지점을 고를 수 있습니다.
선형 가중합은 볼록 껍질 꼭짓점만 고를 수 있어 도달 가능한 해가 몇 개로 제한되므로,
**증강 가중 체비셰프 스칼라화**를 씁니다.

## API

| 엔드포인트 | 설명 |
|---|---|
| `POST /api/upload` | CSV 업로드 → 화물 요약 + 컨테이너 추천 |
| `POST /api/optimize` | 계산 시작 (백그라운드) → `job_id` |
| `GET /api/status/{job_id}` | 진행률 |
| `GET /api/result/{job_id}` | 적재안 3종 + 좌표 + 파레토 프론트 |
| `GET /api/reweight/{job_id}` | 가중치로 다른 해 선택 |
| `GET /api/pick_point/{job_id}` | 파레토 그래프의 점으로 해 선택 |
| `GET /api/manifest/{job_id}` | 작업명세서 PDF |
| `POST /api/pallet-preview` | 팔레트 규격 비교 |

## 정적 데모 갱신

`web/`을 고친 뒤:

```bash
python3 tools/build_static.py    # web/ → docs/
git add docs web && git commit && git push
```

GitHub Pages가 `docs/`를 서빙하므로 푸시하면 자동 반영됩니다.
`web/`이 항상 원본이며, 두 벌을 손으로 맞출 필요는 없습니다.

## 의존성

- Python 3.10+ / fastapi · uvicorn · python-multipart · pydantic
- 3D 라이브러리(Three.js)와 폰트를 CDN에서 받으므로 **인터넷 연결 필요**
- 명세서 PDF는 **wkhtmltopdf** 필요 (없으면 그 기능만 비활성)

## 문서

- [START.md](START.md) — 설치 · 실행 · 화면 조작 · CSV 형식
- [CargoFit_변경사항_설명서.pdf](CargoFit_변경사항_설명서.pdf) — 기존 버전 대비 변경 내역
- [DECISIONS.md](DECISIONS.md) — 알고리즘 설계 결정 기록
- `확정사안_정리.md` — 요구사항 정리
