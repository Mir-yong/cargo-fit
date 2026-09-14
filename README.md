# CLP NSGA-II + DBLF 솔버

LCL 다중 하역지 컨테이너 적재 최적화. 논문(Elahi et al. 2025) 방법론 기반.

## 구조

```
clp_solver/
├── run.py                 # 실행 스크립트 (CSV in → JSON out)
├── modified_BR6_full.csv  # 샘플 데이터
└── clp/
    ├── model.py           # Box, Container, CSV 로더
    ├── dblf.py            # DBLF 디코더 (극점, feasibility, 지지면적)
    ├── objectives.py      # Z1/Z2/Z3 평가 + 박스 랭킹
    └── nsga2.py           # NSGA-II 엔진 (인코딩, 연산자, 정렬)
```

## 실행

VSCode에서 `clp_solver` 폴더를 열고 터미널에서:

```bash
# instance 목록 확인
python run.py --csv modified_BR6_full.csv --list

# 빠른 테스트 (작게)
python run.py --csv modified_BR6_full.csv --instance 1 --pop 20 --gen 30

# 논문 파라미터 (느림, 수십초~수분)
python run.py --csv modified_BR6_full.csv --instance 1 --pop 50 --gen 200

# 결과 JSON 경로 지정
python run.py --csv modified_BR6_full.csv --instance 1 --out result.json
```

의존성 없음 (순수 Python 3.8+, 표준 라이브러리만).

## 출력

- 콘솔: 진행률 + KPI(용적률/ULO/CG편차/미적재) + Pareto front 요약
- `result.json`: 3D 시뮬레이터용 좌표 (box별 x,y,z + dx,dy,dz + 회전)

## 설계 결정

`DECISIONS.md` 참조. 핵심:
- 전량 적재 원칙, 미적재는 화주별 리포트
- priority 블록 구조 유지, 블록 내부에서만 유전연산
- 회전: uniform crossover, 박스 단위 리셋
- 안정성 θ=0.70, stackable 해석 A, 6방향 자유
