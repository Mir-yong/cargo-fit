"""
목적함수 평가 (Z1, Z2, Z3) 와 박스 랭킹.

Z1: 용적률 (최대화) → 내부적으로는 -Z1 최소화
Z2: ULO 개수 (최소화)
Z3: CG 편차 평균 (최소화)
"""
from __future__ import annotations
from typing import List, Tuple
from .model import Box, Container, Placement


def utilization(placed: List[Placement], cont: Container) -> float:
    """Z1: 용적률 = 적재 부피 합 / 컨테이너 부피. (첫 컨테이너 기준)"""
    vol = sum(p.dl * p.dw * p.dh for p in placed)
    return vol / cont.volume


def ulo_scan(placed: List[Placement]) -> Tuple[int, set]:
    """
    [추가됨] ULO 개수와 리핸들링 대상 박스 id 집합을 한 번의 순회로 동시에 계산.

    원래 count_ulo()와 blocking_box_ids()가 완전히 똑같은 이중 루프(같은 조건,
    같은 쌍)를 각각 따로 돌리고 있었다. 배치 하나를 평가할 때마다(현장 대응은
    수백~수천 번, NSGA-II도 세대*개체군만큼) 이 O(n²) 순회를 두 번씩 하는 건
    낭비라서, 한 번 순회하면서 둘 다 채우도록 합쳤다.

    좌표계: x=0이 문(door) 쪽. DBLF는 priority가 빠른(먼저 내릴) 박스를
    먼저 배치하며 항상 가장 작은 x부터 채우므로, 정상적으로는
    priority가 빠른 박스일수록 문 쪽(작은 x)에 자리잡는다.

    선행쌍 (i,k) with priority(i) < priority(k) (i가 먼저 내릴 화물)에 대해,
    k(나중 화물)가 i(먼저 화물)보다 "문쪽(작은 x)" 또는 "위(+z)"에 있으면
    물리적으로 i를 꺼내는 길을 막으므로 장애물 1.
    같은 priority는 쌍 생성 안 함 → ULO 0.

    반환: (ulo 개수, 리핸들링 대상 box_id 집합)
    """
    ulo = 0
    blockers = set()
    n = len(placed)
    for i in range(n):
        pi = placed[i]
        for k in range(n):
            if i == k:
                continue
            pk = placed[k]
            if pi.box.priority < pk.box.priority:
                # k(나중 화물)가 i(먼저 화물)보다 문쪽(작은 x)에 있으면
                # 문에서 i를 꺼내는 경로를 k가 막고 있다 → 장애물
                blocks_path = pk.x < pi.x - 1e-6
                # k가 i보다 위(above): z 시작이 더 큼 → 위에서 눌러 못 뺌
                above = pk.z > pi.z + 1e-6
                if blocks_path or above:
                    ulo += 1
                    blockers.add(pk.box.box_id)
    return ulo, blockers


def count_ulo(placed: List[Placement]) -> int:
    """Z2: ULO 개수. (다른 지표도 같이 필요하면 ulo_scan()을 직접 써서 이중 계산을 피할 것)"""
    ulo, _ = ulo_scan(placed)
    return ulo


def blocking_box_ids(placed: List[Placement]) -> set:
    """
    '실제로 옮겨야 할 박스' 집합.

    ULO는 (i,k) 쌍의 개수를 세므로, 방해 박스 하나가 여러 화물을
    동시에 막으면 그만큼 여러 번 중복 카운트된다(예: 문 앞을 막는
    박스 하나가 뒤쪽 20개 화물을 다 막으면 ULO 20).

    실무에서 중요한 건 "몇 쌍이 막혔나"가 아니라 "몇 개의 박스를
    실제로 옮겨야 접근할 수 있나"이므로, 방해 역할을 한 번이라도
    한 박스의 box_id만 집합으로 모아 반환한다. 이 집합의 크기가
    바로 현장에서 리핸들링해야 할 실제 박스 개수다.
    """
    _, blockers = ulo_scan(placed)
    return blockers


def count_blocking_boxes(placed: List[Placement]) -> int:
    """실제 리핸들링이 필요한 박스 개수 (blocking_box_ids의 크기)."""
    return len(blocking_box_ids(placed))


def blocking_by_destination(placed: List[Placement]) -> dict:
    """
    [추가됨] 목적지별 LIFO 위반 리포트 — "몇 개의 각 목적지 소속 박스가
    나중에 재배치돼야 하는가"를 destination별로 집계한다.

    ulo_scan()이 이미 계산해 둔 blockers(재배치 대상 box_id 집합)를
    재사용할 뿐, 새로운 O(n²) 순회는 만들지 않는다. server.py의 parse_csv()
    가 업로드 시점에 "같은 destination은 같은 priority"를 강제해 두므로,
    여기서 destination별로 집계한 값은 곧 "그 목적지의 LIFO 하역이 실제로
    막힘없이 되는가"를 의미하게 된다 — 값이 0이면 그 목적지는 재배치 없이
    바로 하역 가능.
    """
    _, blockers = ulo_scan(placed)
    id_to_dest = {p.box.box_id: p.box.destination for p in placed}
    by_dest: dict = {}
    for bid in blockers:
        d = id_to_dest.get(bid, "?")
        by_dest[d] = by_dest.get(d, 0) + 1
    return by_dest


def avg_support_ratio(placed: List[Placement]) -> float:
    """
    바닥에 놓이지 않은 박스들의 평균 지지면적 비율.

    지지면적 70%는 하드 제약이라 모든 배치가 항상 만족한다. 따라서
    '통과 여부'는 정보량이 없고, 실제로 의미 있는 값은 '얼마나 여유 있게
    안정적인가'이다. 70%에 겨우 걸친 배치와 95%로 든든히 받쳐진 배치는
    현장에서 체감이 다르다.

    바닥 박스(z=0)는 지면이 100% 받치므로 평균에서 제외한다.
    상단 박스가 없으면 100%를 반환.
    """
    ratios = []
    for p in placed:
        if p.z < 1e-6:
            continue                      # 바닥 박스는 제외
        base = p.dl * p.dw
        if base < 1e-6:
            continue
        supported = 0.0
        for q in placed:
            if q is p:
                continue
            if abs(q.z2 - p.z) > 0.5:     # 윗면이 이 박스 밑면에 닿는 것만
                continue
            ox = max(0.0, min(p.x2, q.x2) - max(p.x, q.x))
            oy = max(0.0, min(p.y2, q.y2) - max(p.y, q.y))
            supported += ox * oy
        ratios.append(min(supported / base, 1.0))

    if not ratios:
        return 1.0
    return sum(ratios) / len(ratios)


def cg_deviation(placed: List[Placement], cont: Container) -> float:
    """
    Z3: CG 편차 평균.
    각 축 편차 = |적재 CG - 컨테이너 중점| / (축 길이/2), 즉 half-length 대비 비율.
    3축 평균 반환 (0~1, 작을수록 균형).
    """
    if not placed:
        return 1.0
    total_m = sum(p.box.weight for p in placed)
    if total_m < 1e-9:
        return 1.0

    cx = sum(p.box.weight * p.centroid[0] for p in placed) / total_m
    cy = sum(p.box.weight * p.centroid[1] for p in placed) / total_m
    cz = sum(p.box.weight * p.centroid[2] for p in placed) / total_m

    gx, gy, gz = cont.center
    dev_x = abs(cx - gx) / (cont.L / 2)
    dev_y = abs(cy - gy) / (cont.W / 2)
    dev_z = abs(cz - gz) / (cont.H / 2)
    return (dev_x + dev_y + dev_z) / 3.0


def evaluate(placed: List[Placement], cont: Container,
             cg_tolerance: float | None = None,
             relocation_weight: float = 1.0) -> Tuple[float, float, float]:
    """
    NSGA-II용 목적 벡터 반환 (모두 최소화 방향).
    (-용적률, ULO * relocation_weight, CG편차)

    [수정 철회 — 2025 재검토] 한때 "하역지 구역 제약 해제(use_zones=False)"를
    relocation_weight=0.0에 직접 연결했었다("리핸들링을 아예 고려하지 않는다"는
    요구를 문자 그대로 구현). 하지만 실측(BR7, pop=30, gen=30)에서 zones=False가
    zones=True보다 오히려 낮은 용적률을 내는 역효과가 나타나 원인을 추적했다.

    dominates()는 순서 비교만 하고 crowding_distance()도 축별로 정규화(span
    나누기)하기 때문에, relocation_weight는 "0이 아닌 어떤 양수든" 결과가
    완전히 동일하다 — 0.001이든 1.0이든 파레토 지배·혼잡도 계산에 차이가
    없다. 오직 정확히 0.0일 때만 그 축이 모든 개체에서 상수가 되어 파레토
    비교에서 완전히 사라진다. 즉 "가중치를 0으로" = "목적을 3개에서 2개로
    줄인다"와 동일한 효과였다.

    목적이 2개로 줄면 비지배 조건이 더 엄격해져(두 축 다 이겨야 비지배)
    파레토 프론트 크기가 크게 줄어든다(실측: BR7에서 프론트가 20~27개에서
    3~6개로 축소). 프론트가 작을수록 다음 세대 교배 후보(부모)의 다양성이
    줄어 GA 탐색력 자체가 떨어진다 — 특히 박스 종류가 많아 어려운 BR7 같은
    데이터셋에서, 정체 시 다양성 주입을 넣어도 30세대 안에 극복하지 못할
    정도로 탐색이 위축됐다.

    반면 최종 결과 선택(solutions.py의 pick_three() → max_fill)은 이 목적
    벡터와 무관하게 _metrics()로 다시 계산한 "실제 용적률"을 기준으로
    고르므로(동률일 때만 리핸들링 적은 쪽), relocation_weight가 1이든
    0이든 "화면에 표시되는 최대 용적률 해"는 원래도 리핸들링에 좌우되지
    않았다. 따라서 relocation_weight를 다시 1.0 고정으로 되돌려도
    "리핸들링을 신경 안 쓰고 최대 용적률을 고른다"는 목표는 그대로
    달성되면서, 탐색 과정의 프론트 다양성만 회복된다.
    """
    z1 = utilization(placed, cont)
    z2 = count_ulo(placed) * relocation_weight
    z3_raw = cg_deviation(placed, cont)

    # [수정됨] cg_tolerance를 명시적으로 안 넘기면(기존 모든 호출부가 그렇다)
    # cont.cg_tolerance를 쓴다 — 요청별 CoG 허용오차 설정(server.py의
    # OptimizeReq.cg_tolerance)이 nsga2.py/heuristic.py의 호출부를 하나도
    # 안 고치고도 그대로 반영되게 하기 위함. 명시적으로 넘기면 그 값이
    # 우선(테스트/디버깅용 오버라이드).
    tol = cont.cg_tolerance if cg_tolerance is None else cg_tolerance
    # 허용치를 10% -> 5%로 줄이고, 초과 시 페널티 가중치(* 2.0) 부여
    z3 = 0.0 if z3_raw <= tol else (z3_raw - tol) * 2.0
    return (-z1, float(z2), z3)

# ---- 박스 랭킹 (디코딩 전 재정렬) ----
def rank_key(box: Box) -> tuple:
    """
    priority(하역 순서)만으로 정렬한다.

    논문은 5단계 랭킹(priority → stackable → 부피 → 밑면적 → 최대치수)을
    쓰지만, 그렇게 하면 정렬이 거의 완전해져서 동점이 사라진다.
    그 결과 GA가 순서 염색체를 아무리 섞어도 디코딩 직전에 항상 같은
    순서로 되돌려져 순서 탐색이 무력화되고, 1세대 결과가 100세대까지
    그대로 유지되는 현상이 나타났다(실측: gen 1에서 이미 75.6%).

    priority만 남기면 블록 구조(하역 순서)는 보장하면서
    블록 내부 순서는 GA가 자유롭게 탐색할 수 있다.
    """
    return (box.priority,)


def apply_ranking(order: List[Box]) -> List[Box]:
    """
    genotype 순서를 priority 블록으로만 재정렬.
    Python의 sorted는 stable하므로, 같은 priority 안에서는
    GA가 정한 순서가 그대로 유지된다. (순서 탐색이 실제로 작동)
    """
    return sorted(order, key=rank_key)
