"""
NSGA-II 엔진 (DBLF 디코더와 결합).

이배체 인코딩:
  chromosome1 = 박스 순서 (priority 블록 구조 유지)
  chromosome2 = 각 박스 회전 (0..5)

확정 설계:
  B1: crossover/mutation은 priority 블록 내부에서만
  B2: 회전 염색체는 uniform crossover
  B3: 회전 리셋은 박스 단위
  B4: 미적재는 unplaced로, 상위에서 리포트
"""
from __future__ import annotations
import random
from dataclasses import dataclass, field
from typing import List, Tuple, Dict
from collections import defaultdict

from .model import Box, Container, Placement
from .dblf import decode
from .objectives import evaluate, apply_ranking, utilization


# ---- 개체 ----
@dataclass
class Individual:
    order: List[Box]          # chromosome1
    rot: List[int]            # chromosome2 (order와 같은 순서)
    obj: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # (-Z1, Z2, Z3)
    placed: List[Placement] = field(default_factory=list)
    unplaced: List[Box] = field(default_factory=list)
    rank: int = 0
    crowd: float = 0.0


def _priority_blocks(boxes: List[Box]) -> Dict[int, List[Box]]:
    """priority별로 박스 그룹핑."""
    blocks: Dict[int, List[Box]] = defaultdict(list)
    for b in boxes:
        blocks[b.priority].append(b)
    return dict(blocks)


# ---- 초기화 ----
def init_population(boxes: List[Box], n: int, rng: random.Random) -> List[Individual]:
    """
    구조화된 다양성 초기화.
    - priority 블록 유지
    - 블록 내부: 타입 그룹 셔플 / 부피·치수 정렬 변형
    - 회전: 랜덤 (타입 동일 회전은 초기화에만 적용)
    """
    blocks = _priority_blocks(boxes)
    priorities = sorted(blocks.keys())
    pop: List[Individual] = []

    variants = ["type_shuffle", "vol_desc", "len_desc", "wid_desc", "hgt_desc"]

    for idx in range(n):
        order: List[Box] = []
        for pr in priorities:
            grp = list(blocks[pr])
            v = variants[idx % len(variants)]
            if v == "type_shuffle":
                by_type: Dict[int, List[Box]] = defaultdict(list)
                for b in grp:
                    by_type[b.type_id].append(b)
                tkeys = list(by_type.keys())
                rng.shuffle(tkeys)
                grp = [b for t in tkeys for b in by_type[t]]
            elif v == "vol_desc":
                grp.sort(key=lambda b: -b.volume)
            elif v == "len_desc":
                grp.sort(key=lambda b: -b.l)
            elif v == "wid_desc":
                grp.sort(key=lambda b: -b.w)
            elif v == "hgt_desc":
                grp.sort(key=lambda b: -b.h)
            order.extend(grp)

        # 같은 변형끼리는 결정론적이라 개체가 중복된다.
        # 랭킹을 priority만으로 줄인 뒤로는 순서 탐색이 실제로 작동하므로,
        # 초기 개체군의 다양성이 중요해졌다. 첫 개체(idx=0)는 순수 정렬을
        # 유지하고(좋은 시작점 보존), 나머지는 일부 구간을 섞어 다양화한다.
        if idx > 0:
            n_swap = max(1, len(order) // 10)
            for _ in range(n_swap):
                i, j = rng.randrange(len(order)), rng.randrange(len(order))
                if order[i].priority == order[j].priority:   # 블록 구조 보존
                    order[i], order[j] = order[j], order[i]

        # 회전: 타입별 동일 회전 (초기화 규칙)
        type_rot: Dict[int, int] = {}
        rot: List[int] = []
        for b in order:
            if b.type_id not in type_rot:
                type_rot[b.type_id] = rng.randint(0, 5)
            rot.append(type_rot[b.type_id])

        pop.append(Individual(order=order, rot=rot))
    return pop


# ---- 디코드 & 평가 ----
def decode_eval(ind: Individual, cont: Container) -> None:
    """
    랭킹 재정렬 → DBLF 디코드 → 목적 평가. ind를 in-place 갱신.

    [수정됨] 탐색(GA 루프) 내부에서는 항상 구역 기반 디코드(use_zones=True)만
    쓴다. use_zones 토글은 더 이상 이 함수에 전달되지 않는다 — 이유는
    run_nsga2()의 마지막 후처리 설명 참고. 구역 기반 디코드가 프론트를
    훨씬 크게 유지해 탐색 다양성이 좋다는 게 이미 실측으로 확인됐으므로,
    "정밀 탐색"이라는 검색 과정 자체는 두 옵션에서 항상 동일하게 동작한다.
    """
    # 랭킹은 priority 블록을 보존하며 정렬 (rank_key 첫 키가 priority)
    ranked = apply_ranking(ind.order)
    # rot을 ranked 순서에 맞춰 재정렬 (box_id로 매핑)
    rot_by_id = {b.box_id: r for b, r in zip(ind.order, ind.rot)}
    ranked_rot = [rot_by_id[b.box_id] for b in ranked]

    placed, unplaced = decode(ranked, ranked_rot, cont, use_zones=True)
    ind.placed = placed
    ind.unplaced = unplaced
    ind.obj = evaluate(placed, cont, relocation_weight=1.0)


def _maybe_use_unzoned(ind: Individual, cont: Container) -> None:
    """
    [추가됨] "하역지 구역 제약 해제(use_zones=False)"의 최종 반영 지점.

    compute_zones()의 구역 분할은 리핸들링 축소뿐 아니라 "앞 순위 화물이
    공간을 독점해 뒷 순위가 파편화된 틈만 남는 문제"도 막는 장치라서
    (dblf.py의 compute_zones() 주석 참고), 구역을 없앤다고 늘 용적률이
    오르는 게 아니다. DBLF는 백트래킹 없는 탐욕(greedy) 배치라 같은
    유전자를 구역 있음/없음으로 각각 디코드하면 어느 쪽이 더 높은
    용적률을 내는지 배치마다 다르다 (실측: BR7 90개 유전자 샘플에서
    무구역이 이기는 비율 76%, 나머지 24%는 오히려 구역 쪽이 더 높음,
    최대 -3.4%p 역전).

    탐색 자체(decode_eval)는 항상 구역 기반으로만 진행하고, 결과를
    보여주기 직전(run_nsga2()가 프론트를 반환하기 전) 이 함수로 딱 한 번
    "같은 유전자를 무구역으로도 디코드해서 실제로 더 높으면 그걸로
    교체"한다. 탐색 도중 두 디코드를 매번 비교하면(과거 시도) 그 비교
    결과가 세대마다 부모 선택에 영향을 줘 탐색 경로 자체가 갈라지고,
    그러면 "이번엔 하필 이 시드가 운이 나빠서" 역전되는 일이 남아있었다.
    반면 여기서는 zones=True 모드와 완전히 동일한 탐색을 거쳐 나온
    '같은 프론트'의 개체들에 대해서만 사후적으로 무구역 대안을 추가로
    시도하므로, max(구역, 무구역) >= 구역 단독이라는 부등식이 개체 단위로
    항상 성립하고, 따라서 "리핸들링을 고려 안 하면 용적률은 항상 같거나
    오른다"는 사용자 기대가 어떤 데이터셋·시드에서도 예외 없이 보장된다.
    """
    ranked = apply_ranking(ind.order)
    rot_by_id = {b.box_id: r for b, r in zip(ind.order, ind.rot)}
    ranked_rot = [rot_by_id[b.box_id] for b in ranked]
    p_free, u_free = decode(ranked, ranked_rot, cont, use_zones=False)
    if utilization(p_free, cont) > utilization(ind.placed, cont):
        ind.placed = p_free
        ind.unplaced = u_free
        ind.obj = evaluate(p_free, cont, relocation_weight=1.0)


# ---- non-dominated sorting ----
def dominates(a: Tuple, b: Tuple) -> bool:
    """a가 b를 지배하는가 (모든 목적 ≤, 하나 이상 <). 전부 최소화."""
    le = all(x <= y for x, y in zip(a, b))
    lt = any(x < y for x, y in zip(a, b))
    return le and lt


def fast_nondominated_sort(pop: List[Individual]) -> List[List[int]]:
    """개체 인덱스를 프론트별로 반환. rank도 세팅."""
    S = [[] for _ in pop]
    ncount = [0] * len(pop)
    fronts: List[List[int]] = [[]]

    for p in range(len(pop)):
        for q in range(len(pop)):
            if p == q:
                continue
            if dominates(pop[p].obj, pop[q].obj):
                S[p].append(q)
            elif dominates(pop[q].obj, pop[p].obj):
                ncount[p] += 1
        if ncount[p] == 0:
            pop[p].rank = 0
            fronts[0].append(p)

    i = 0
    while fronts[i]:
        nxt: List[int] = []
        for p in fronts[i]:
            for q in S[p]:
                ncount[q] -= 1
                if ncount[q] == 0:
                    pop[q].rank = i + 1
                    nxt.append(q)
        i += 1
        fronts.append(nxt)
    fronts.pop()
    return fronts


def crowding_distance(pop: List[Individual], front: List[int]) -> None:
    """한 프론트 내 crowding distance 계산."""
    if not front:
        return
    for i in front:
        pop[i].crowd = 0.0
    m = len(pop[front[0]].obj)
    for obj_i in range(m):
        front.sort(key=lambda idx: pop[idx].obj[obj_i])
        pop[front[0]].crowd = float("inf")
        pop[front[-1]].crowd = float("inf")
        omin = pop[front[0]].obj[obj_i]
        omax = pop[front[-1]].obj[obj_i]
        span = omax - omin
        if span < 1e-12:
            continue
        for k in range(1, len(front) - 1):
            prev_o = pop[front[k - 1]].obj[obj_i]
            next_o = pop[front[k + 1]].obj[obj_i]
            pop[front[k]].crowd += (next_o - prev_o) / span


# ---- 선택 ----
def binary_tournament(pop: List[Individual], rng: random.Random) -> Individual:
    a, b = rng.sample(pop, 2)
    if a.rank < b.rank:
        return a
    if b.rank < a.rank:
        return b
    return a if a.crowd > b.crowd else b


# ---- crossover (B1: priority 블록 내부에서만) ----
def _order_crossover_block(p1: List[Box], p2: List[Box],
                           rng: random.Random) -> List[Box]:
    """order-based crossover, 한 블록(같은 priority) 내부용."""
    n = len(p1)
    if n <= 1:
        return list(p1)
    i, j = sorted(rng.sample(range(n), 2))
    child: List[Box] = [None] * n
    # p1의 [i,j] 구간 복사
    segment_ids = set()
    for k in range(i, j + 1):
        child[k] = p1[k]
        segment_ids.add(p1[k].box_id)
    # 나머지를 p2 순서로 채움
    fill = [b for b in p2 if b.box_id not in segment_ids]
    fi = 0
    for k in range(n):
        if child[k] is None:
            child[k] = fill[fi]
            fi += 1
    return child


def crossover(parent1: Individual, parent2: Individual,
              cp: float, rng: random.Random) -> Tuple[Individual, Individual]:
    """
    두 부모 → 두 자식.
    순서: priority 블록별 order-based crossover
    회전: uniform crossover (B2)
    """
    # box_id → rot 매핑
    r1 = {b.box_id: r for b, r in zip(parent1.order, parent1.rot)}
    r2 = {b.box_id: r for b, r in zip(parent2.order, parent2.rot)}

    blocks1 = _priority_blocks(parent1.order)
    blocks2 = _priority_blocks(parent2.order)
    priorities = sorted(blocks1.keys())

    c1_order: List[Box] = []
    c2_order: List[Box] = []
    for pr in priorities:
        b1 = blocks1[pr]
        # p2의 같은 priority 블록을 b1의 원소만으로 정렬 (교차 대상 일치)
        ids1 = {b.box_id for b in b1}
        b2 = [b for b in blocks2[pr] if b.box_id in ids1]
        if rng.random() < cp and len(b1) > 1:
            c1_order.extend(_order_crossover_block(b1, b2, rng))
            c2_order.extend(_order_crossover_block(b2, b1, rng))
        else:
            c1_order.extend(list(b1))
            c2_order.extend(list(b2))

    # 회전: uniform crossover
    def uniform_rot(order, ra, rb):
        return [ra[b.box_id] if rng.random() < 0.5 else rb[b.box_id] for b in order]

    c1 = Individual(order=c1_order, rot=uniform_rot(c1_order, r1, r2))
    c2 = Individual(order=c2_order, rot=uniform_rot(c2_order, r2, r1))
    return c1, c2


# ---- mutation ----
def mutate(ind: Individual, pm1: float, pm2: float, rng: random.Random) -> None:
    """
    2-OPT (순서, priority 블록 내부) + 회전 리셋 (박스 단위).
    """
    # 2-OPT: 블록 내부에서 구간 반전
    if rng.random() < pm1:
        blocks = _priority_blocks(ind.order)
        # 블록 경계 인덱스 재구성
        new_order: List[Box] = []
        for pr in sorted(blocks.keys()):
            grp = list(blocks[pr])
            if len(grp) > 1:
                i, j = sorted(rng.sample(range(len(grp)), 2))
                grp[i:j + 1] = reversed(grp[i:j + 1])
            new_order.extend(grp)
        # rot 재매핑
        rot_by_id = {b.box_id: r for b, r in zip(ind.order, ind.rot)}
        ind.order = new_order
        ind.rot = [rot_by_id[b.box_id] for b in new_order]

    # 회전 리셋: 박스 단위 (B3)
    for k in range(len(ind.rot)):
        if rng.random() < pm2:
            cur = ind.rot[k]
            choices = [o for o in range(6) if o != cur]
            ind.rot[k] = rng.choice(choices)


# ---- 메인 루프 ----
def run_nsga2(boxes: List[Box], cont: Container,
              pop_size: int = 50, generations: int = 200,
              cp: float = 0.8, pm1: float = 0.6, pm2: float = 0.3,
              seed: int = 42, progress=None,
              use_zones: bool = True,
              stagnation_limit: int = 8,
              inject_fraction: float = 0.4) -> List[Individual]:
    """
    NSGA-II 실행. Pareto front(rank 0) 반환.
    progress: 콜백 func(gen, best_util, best_ulo, best_cg) — 진행률 표시용.
    use_zones: 탐색 자체는 항상 구역 기반 디코드로 진행된다(아래 참고).
        False("하역지 구역 제약 해제")면, 탐색이 끝난 뒤 최종 프론트에
        한해 무구역 디코드도 시도해 실제 용적률이 더 높은 쪽을 채택한다
        — 리핸들링은 신경 쓰지 않고 순수 최대 용적률만 우선하는 옵션.

    [추가됨] 다양성 주입 (random immigrants).
    실측 결과, 최고 용적률(best obj[0])이 1세대 만에 나온 값에서 80세대까지
    전혀 개선되지 않는 조기 수렴(premature convergence)이 관찰됐다. 우선순위
    블록 내부로만 제한된 교차/변이(설계 결정 B1) 때문에 한 번 강한 개체가
    나오면 이후 세대들이 그 근방을 벗어나지 못하고 맴도는 것이 원인이다.

    해결책: 최고 용적률이 stagnation_limit 세대 연속 개선되지 않으면, 현재
    개체군에서 rank가 가장 나쁘고(비지배 전선에서 밀려나고) 그 중에서도
    crowding이 가장 좁은(주변에 비슷한 개체가 몰려 있어 다양성 기여가 적은)
    개체들부터 inject_fraction 비율만큼 새로 무작위 생성한 개체로 직접
    교체한다. merge+truncate(적합도 경쟁)를 거치지 않고 pop에 바로 넣기
    때문에, 순수 적합도 비교였다면 곧장 도태됐을 새 유전자도 최소 한 세대는
    교배 후보(binary_tournament의 대상)로 살아남아 기존 엘리트와 재조합될
    기회를 얻는다. rank 0(현재 파레토 전선)의 상위 개체는 교체 대상에서
    자연히 제외되므로 이미 찾은 최선은 잃지 않는다.
    """
    rng = random.Random(seed)

    pop = init_population(boxes, pop_size, rng)
    for ind in pop:
        decode_eval(ind, cont)
    fronts = fast_nondominated_sort(pop)
    for fr in fronts:
        crowding_distance(pop, fr)

    best_z1_so_far = min(ind.obj[0] for ind in pop)
    stall_gens = 0

    for gen in range(generations):
        # 자손 생성
        offspring: List[Individual] = []
        while len(offspring) < pop_size:
            p1 = binary_tournament(pop, rng)
            p2 = binary_tournament(pop, rng)
            c1, c2 = crossover(p1, p2, cp, rng)
            mutate(c1, pm1, pm2, rng)
            mutate(c2, pm1, pm2, rng)
            decode_eval(c1, cont)
            decode_eval(c2, cont)
            offspring.append(c1)
            if len(offspring) < pop_size:
                offspring.append(c2)

        # 부모 + 자손 병합
        merged = pop + offspring
        fronts = fast_nondominated_sort(merged)
        new_pop: List[Individual] = []
        for fr in fronts:
            crowding_distance(merged, fr)
            if len(new_pop) + len(fr) <= pop_size:
                new_pop.extend(merged[i] for i in fr)
            else:
                # crowding 큰 순으로 채움
                fr_sorted = sorted(fr, key=lambda i: -merged[i].crowd)
                need = pop_size - len(new_pop)
                new_pop.extend(merged[i] for i in fr_sorted[:need])
                break
        pop = new_pop
        fronts = fast_nondominated_sort(pop)
        for fr in fronts:
            crowding_distance(pop, fr)

        # [추가됨] 정체 판정 (최고 용적률 = min(obj[0]) 기준) 및 다양성 주입
        cur_best_z1 = min(ind.obj[0] for ind in pop)
        if cur_best_z1 < best_z1_so_far - 1e-9:
            best_z1_so_far = cur_best_z1
            stall_gens = 0
        else:
            stall_gens += 1

        if stall_gens >= stagnation_limit:
            n_replace = max(1, int(pop_size * inject_fraction))
            # rank 나쁜(큰) 순 → 동률이면 crowding 좁은(작은) 순으로 교체 대상 선정
            order = sorted(range(len(pop)), key=lambda i: (-pop[i].rank, pop[i].crowd))
            replace_idx = order[:n_replace]
            fresh = init_population(boxes, n_replace, rng)
            for ind in fresh:
                decode_eval(ind, cont)
            for slot, new_ind in zip(replace_idx, fresh):
                pop[slot] = new_ind
            stall_gens = 0  # 다음 정체 판정까지 다시 stagnation_limit세대의 여유를 준다

        if progress is not None:
            best = min(pop, key=lambda x: x.obj[0])  # 최대 용적률
            progress(gen + 1, -best.obj[0], best.obj[1], best.obj[2])

    fronts = fast_nondominated_sort(pop)
    front0 = [pop[i] for i in fronts[0]]

    # [수정됨] use_zones=False("하역지 구역 제약 해제")는 탐색 전체가 아니라
    # 여기, 최종 프론트에만 적용한다. _maybe_use_unzoned() 문서 참고 —
    # 탐색 도중 두 디코드를 매번 비교하면 그 결과가 부모 선택에 영향을 줘
    # 탐색 경로 자체가 zones=True 모드와 갈라지고, 그러면 시드에 따라
    # zones=False가 zones=True보다 낮은 용적률로 끝나는 경우가 실제로
    # 남아있었다(BR7 instance 1 실측: 두 모드 다 73.66%/73.84%에서
    # 요지부동). 반면 여기서는 zones=True 모드와 완전히 동일한 탐색을
    # 거쳐 나온 '같은 프론트'에 대해서만 사후적으로 무구역 대안을
    # 추가 시도하므로, max(구역, 무구역) >= 구역 단독이 개체 단위로 항상
    # 성립해 "리핸들링 고려 안 하면 용적률은 항상 같거나 오른다"가
    # 어떤 시드·데이터셋에서도 예외 없이 보장된다.
    # [추가됨] 2차 삽입 재시도(retry_unplaced, dblf.py)는 여기서 프론트
    # 전체(pop_size개, 보통 20~30개)에 매번 적용하지 않는다 — 실측해보니
    # 후보 자리를 "박스 하나당 극점 3개"보다 훨씬 넓게(모든 박스의 x·y
    # 경계 전 조합) 잡아야 실제로 빈틈을 다 찾아내는데(아래 solutions.py
    # 참고), 그 넓은 탐색을 프론트 전체에 매번 돌리면 계산 시간이 눈에
    # 띄게 늘어난다(실측: BR6, pop=20·gen=15 기준 수십 초 추가). 그래서
    # 이 재시도는 사용자에게 실제로 보여줄 3개 대표 해가 정해진 뒤
    # (solutions.pick_three) 그 3개에 대해서만 적용한다 — 여기 프론트
    # 자체에는 적용하지 않는다.
    if not use_zones:
        for ind in front0:
            _maybe_use_unzoned(ind, cont)
        if progress is not None:
            best = min(front0, key=lambda x: x.obj[0])
            progress(generations, -best.obj[0], best.obj[1], best.obj[2])

    return front0