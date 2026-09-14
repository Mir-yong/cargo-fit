"""
빠른 휴리스틱 솔버.

용도가 둘이다.
  1) NSGA-II 성능 비교의 기준선(baseline)
  2) 현장 변동(화물 미입고/롤오버/실측 불일치) 발생 시 즉시 재계산

GA 없이 정해진 순서로 디코더를 1회 통과시키므로 1초 내외로 끝난다.
디코더는 NSGA-II와 동일한 것을 쓰므로 "탐색 여부"만 다른 공정한 비교가 된다.

전략:
  ffd_volume  : 부피 내림차순 (First Fit Decreasing, 가장 표준적)
  ffd_base    : 밑면적 내림차순
  ffd_height  : 높이 내림차순
  ffd_maxdim  : 최대변 내림차순
  wall        : 벽 쌓기 (같은 타입끼리 묶어 면을 이루도록)
모두 priority(하역 순서) 블록을 먼저 지키고, 블록 내부에서만 정렬한다.
"""
import time
import random
from typing import List, Dict, Tuple
from collections import defaultdict

from .model import Box, Container, Placement
from .dblf import decode, retry_unplaced
from .objectives import utilization, ulo_scan, cg_deviation

STRATEGIES = ["ffd_volume", "ffd_base", "ffd_height", "ffd_maxdim", "wall"]
# [수정됨] 3번 항목: ffd_base/ffd_height/ffd_maxdim은 원래 _order_by_strategy()에
# 구현까지 다 돼 있었는데 STRATEGIES 목록에 안 들어가서 실제로는 한 번도 안 쓰이고
# 있었다. solve()의 "1. 결정론적 기본 전략 탐색" 단계에 공짜로 후보를 3개 더
# 추가하는 것과 같아서(시간 예산은 그대로, 시작 후보만 다양해짐), 손해 볼 게 없다.


def _order_by_strategy(boxes: List[Box], strategy: str) -> List[Box]:
    """priority 블록을 지키면서 블록 내부를 전략에 따라 정렬."""
    blocks: Dict[int, List[Box]] = defaultdict(list)
    for b in boxes:
        blocks[b.priority].append(b)

    ordered: List[Box] = []
    for pr in sorted(blocks.keys()):
        grp = list(blocks[pr])
        if strategy == "ffd_volume":
            grp.sort(key=lambda b: -b.volume)
        elif strategy == "ffd_base":
            grp.sort(key=lambda b: -(b.l * b.w))
        elif strategy == "ffd_height":
            grp.sort(key=lambda b: -b.h)
        elif strategy == "ffd_maxdim":
            grp.sort(key=lambda b: -max(b.l, b.w, b.h))
        elif strategy == "wall":
            # 같은 타입끼리 인접시켜 면을 이루게. 타입 그룹은 총부피 큰 순.
            by_type: Dict[int, List[Box]] = defaultdict(list)
            for b in grp:
                by_type[b.type_id].append(b)
            tkeys = sorted(by_type.keys(),
                           key=lambda t: -sum(x.volume for x in by_type[t]))
            grp = [b for t in tkeys for b in by_type[t]]
        else:
            raise ValueError(f"알 수 없는 전략: {strategy}")
        ordered.extend(grp)
    return ordered


def _best_rotation(box: Box) -> int:
    """
    높이가 가장 낮아지는 회전을 기본값으로. 낮게 눕히면 위에 더 쌓을 수 있다.
    (디코더가 이 회전으로 안 되면 다른 회전을 자동 시도하므로 시작값 역할)
    """
    return min(range(6), key=lambda o: box.dims_for_orientation(o)[2])


def solve_one(boxes: List[Box], cont: Container,
              strategy: str = "ffd_volume",
              use_zones: bool = True) -> Tuple[List[Placement], List[Box]]:
    """단일 전략으로 1회 배치."""
    order = _order_by_strategy(boxes, strategy)
    rot = [_best_rotation(b) for b in order]
    return decode(order, rot, cont, use_zones=use_zones)


def solve(boxes: List[Box], cont: Container,
          strategies: List[str] = None,
          objective: str = "balanced",
          iterations: int = 30,
          time_budget: float = None,
          use_zones: bool = True,
          progress=None) -> dict:
    """
    현장 변동 대응용 보조 알고리즘.

    time_budget(초)을 주면 그 시간 동안 계속 무작위 탐색을 반복한다
    (iterations는 이때 안전판 역할만 — 데이터가 아주 작아 1세대가
    순식간에 끝나도 무한히 돌지 않도록 상한을 둔다).
    time_budget이 없으면 예전처럼 iterations(세대) 수만큼만 반복한다.

    use_zones: 하역지별 x구역 할당을 쓸지 여부 (dblf.decode 참고).
        True(기본)  : 리핸들링을 줄이기 위해 구역을 우선 시도 (실무 기본값)
        False       : 구역 제약 없이 순수 공간만 채움 — 용적률 최대화 우선,
                      대신 리핸들링(재작업)이 늘어날 수 있다.
    """
    if strategies is None:
        strategies = STRATEGIES

    results = []
    total_boxes = len(boxes)

    # 1. 결정론적 기본 전략 탐색
    for s in strategies:
        placed, unplaced = solve_one(boxes, cont, s, use_zones=use_zones)
        # [수정됨] 4번 항목: ULO 개수와 리핸들링 박스 수를 각각 count_ulo()/
        # count_blocking_boxes()로 따로 구하면 완전히 같은 O(n²) 순회를 두 번
        # 돈다. ulo_scan() 한 번으로 둘 다 얻어서 절반으로 줄였다.
        _ulo, _blockers = ulo_scan(placed)
        results.append({
            "strategy": s,
            "placed": placed,
            "unplaced": unplaced,
            "util": utilization(placed, cont),
            "ulo": _ulo,
            "reloc": len(_blockers),
            "cg": cg_deviation(placed, cont),
        })

    # [핵심 로직] 심사 기준: use_zones에 따라 완전히 다르다.
    #
    # use_zones=True (기본, 실무 기본값) — 무게중심 5% 이하 방어 + 리핸들링 억제 +
    # 용적률 극대화를 함께 고려한 균형 점수.
    # [버그 수정 1] cg_deviation()은 0~1 사이의 "비율"을 반환하는데(예: 12.47% -> 0.1247),
    # 예전 코드는 임계값을 5.0(퍼센트 포인트로 착각)으로 비교해서
    # r["cg"] - 5.0 이 항상 음수가 되어 cg_penalty가 절대 0보다 커지지 않았다.
    # 즉 무게중심 페널티가 사실상 완전히 꺼져 있었던 것 — "완화"가 아니라 "무력화"였다.
    # objectives.py의 cg_tolerance(=0.05, NSGA-II 쪽)와 단위를 맞추고, 가중치도
    # 실제로 5%를 넘는 해를 걸러낼 만큼(2.0) 올렸다.
    # [버그 수정 2] ULO는 "선행쌍 개수"라 박스 수가 늘면 수백~수천까지 커진다
    # (실측: ffd_volume 한 번에 ULO 1204). 이걸 그대로 * 0.001만 해도 용적률(0~1)
    # 스케일을 통째로 압도해서, 탐색이 사실상 "용적률 최대화"가 아니라
    # "ULO 최소화"가 되어버렸다 — 매 실행 용적률이 55~60%에서 멈춘 원인.
    # 실무 지표이자 값의 범위가 안정적인 "실제 리핸들링 박스 수"를 전체 박스 대비
    # 비율로 정규화해서 용적률과 같은 0~1 스케일에서 비교되게 고쳤다.
    #
    # use_zones=False ("하역지 구역 제약 해제") — 순수 최대 용적률 모드.
    # [수정됨] 원래는 zones를 꺼도 평가 기준(evaluate)이 그대로라서, 무게중심을
    # 5% 밑으로 낮추려고 용적률을 몇 %p 포기한 해가 승자로 뽑히는 경우가 있었다.
    # zones 체크박스 설명("리핸들링보다 용적률을 우선")과 실제 동작이 안 맞았던
    # 원인이 이것 — "zones를 껐다"는 것과 "심사 기준을 바꿨다"는 것은 별개인데
    # 심사 기준은 항상 그대로였다. 이제 zones=False일 때는 무게중심/리핸들링
    # 페널티를 아예 빼고 용적률만으로 승자를 고른다 — dblf.py의 use_zones=False
    # 설명("네이버지도의 환승 제약 없는 최단시간 경로")과 같은 논리를 심사 기준에도
    # 일관되게 적용한 것. 대신 이 모드에서는 무게중심 편차나 리핸들링이 커질 수
    # 있다는 걸 UI에서 분명히 안내해야 한다.
    if use_zones:
        # [수정됨] 하드코딩 0.05 → cont.cg_tolerance. objectives.evaluate()와
        # 동일하게 요청별 CoG 허용오차 설정을 fast(휴리스틱) 모드도 따르게 한다.
        def evaluate(r):
            cg_penalty = max(0.0, r["cg"] - cont.cg_tolerance) * 2.0
            reloc_ratio = (r["reloc"] / total_boxes) if total_boxes else 0.0
            reloc_penalty = reloc_ratio * 0.15
            return r["util"] - cg_penalty - reloc_penalty
    else:
        def evaluate(r):
            return r["util"]

    # 현재까지 1등 기록 초기화
    best_res = max(results, key=evaluate)

    rng = random.Random()
    searches_per_gen = 20  # 1세대당 20번의 배치 시뮬레이션 수행

    # 2. 시간 예산(또는 지정된 세대 수)만큼 심층 무작위 반복
    t0 = time.time()
    g = 0
    while True:
        if time_budget is not None:
            if time.time() - t0 >= time_budget:
                break
        elif g >= iterations:
            break
        g += 1

        for _ in range(searches_per_gen):
            blocks = defaultdict(list)
            for b in boxes:
                blocks[b.priority].append(b)

            rand_order = []
            for pr in sorted(blocks.keys()):
                grp = list(blocks[pr])
                rng.shuffle(grp)
                rand_order.extend(grp)

            rot = [rng.choice([_best_rotation(b), _best_rotation(b), rng.randint(0, 5)]) for b in rand_order]

            placed, unplaced = decode(rand_order, rot, cont, use_zones=use_zones)

            # [수정됨] 4번 항목: 여기가 실제 핫패스 — 60초 예산 동안 수백~수천 번
            # 호출된다. ulo_scan() 한 번으로 ULO/리핸들링을 동시에 얻는다.
            _ulo, _blockers = ulo_scan(placed)
            r = {
                "strategy": f"random_{g}",
                "placed": placed,
                "unplaced": unplaced,
                "util": utilization(placed, cont),
                "ulo": _ulo,
                "reloc": len(_blockers),
                "cg": cg_deviation(placed, cont),
            }

            # 새로 찾은 해가 기존 1등보다 점수가 높으면 즉시 갱신
            if evaluate(r) > evaluate(best_res):
                best_res = r

        # 화면 UI에 현재 1등 해의 정확한 스펙 보고
        if progress:
            progress(g, best_res["util"], best_res["ulo"], best_res["cg"])
            time.sleep(0.01) # UI가 부드럽게 갱신되도록 아주 짧은 대기

    # [추가됨] 2차 삽입 재시도 — decode()는 한 번 훑고 지나가면 되돌아가지
    # 않는 구조라, 어떤 박스를 검토하던 시점엔 자리가 없어서 unplaced로
    # 넘어갔는데 그 뒤 다른 박스들이 자리 잡으며 새로 생긴 빈틈은 다시
    # 확인하지 않는다(정밀 탐색 쪽과 동일한 문제 — retry_unplaced() 참고).
    # 여기서도 decode() 호출마다(searches_per_gen × 세대 수만큼) 재시도를
    # 돌리면 시간 예산 안에 시도할 수 있는 후보 수가 크게 줄어드니, 시간
    # 예산이 다 끝난 뒤 최종 1등 해에 대해서만 한 번 적용한다. 이미 놓인
    # 박스는 그대로 두고 "더 채우기"만 하므로 결과가 나빠질 일은 없다.
    if best_res["unplaced"]:
        p2, u2 = retry_unplaced(best_res["placed"], best_res["unplaced"], cont, use_zones=use_zones)
        if len(p2) > len(best_res["placed"]):
            best_res = dict(best_res)
            best_res["placed"] = p2
            best_res["unplaced"] = u2
            _ulo, _blockers = ulo_scan(p2)
            best_res["util"] = utilization(p2, cont)
            best_res["ulo"] = _ulo
            best_res["reloc"] = len(_blockers)
            best_res["cg"] = cg_deviation(p2, cont)

    # [추가됨] 정밀 탐색(server.py의 _run_precise) 쪽과 같은 이유로 —
    # 위 2차 삽입 재시도가 util/ulo/cg를 바꿨을 수 있는데, 마지막
    # progress() 호출은 재시도 전(while 루프 안) 값을 보여준 채로 끝나
    # 있다. "계산 중" 화면에 뜨는 숫자와 실제 결과가 달라 보이지 않도록
    # 최종(재시도 반영) 값으로 진행률을 한 번 더 갱신한다.
    if progress:
        progress(g, best_res["util"], best_res["ulo"], best_res["cg"])

    out = dict(best_res)
    out["all_results"] = []
    out["generations_run"] = g
    return out
