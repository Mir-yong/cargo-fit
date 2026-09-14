"""
적재 순서(작업 순서) 계산 — DBLF가 정한 최종 좌표는 그대로 두고,
"실제 사람이 문(x=0)으로 들어가 하나씩 쌓을 수 있는" 순서로 재정렬한다.

문제: DBLF decode()가 만드는 placed 리스트의 순서는 지오노타입 처리
순서(랭킹: priority 오름차순 → 문쪽 구역부터)일 뿐, 배치 알고리즘이
그 순서로 "쌓아야 한다"고 정한 게 아니다. 그런데 이 순서가 그대로
3D 애니메이션의 재생 순서(그리고 명세서의 적재 순번)로 쓰이고 있었다.
구역(zones)이 priority 1(문쪽, x 작음) → 2 → 3(안쪽, x 큼) 순으로
배정되므로, 이 순서 그대로 애니메이션을 재생하면 문쪽부터 먼저 채워
버려서, 뒤에(나중 순번으로) 나오는 안쪽 화물을 놓을 때는 이미 문쪽이
막혀 있어 실제로는 그 자리에 도달할 수 없는 장면이 나온다 — 화면
캡처로 확인됨(문쪽을 먼저 채운 뒤, 이미 둘러싸인 안쪽 빈틈에 화물이
"순간이동"하듯 나타남).

실제로 가능한 순서가 되려면 두 가지 의존관계를 지켜야 한다:
  1) 지지 의존성 — 박스는 자신을 떠받치는 아래 박스가 먼저 놓여야 한다.
  2) 접근 의존성 — 문에서 봤을 때 어떤 박스보다 앞(x가 작음)에 있고
     같은 y·z 범위를 차지하는 박스는, 그 박스를 놓기 전에 뒤(x가 큰)
     박스부터 먼저 놓아야 한다(안 그러면 나중에 그 앞자리를 채운
     뒤로는 뒤쪽 자리에 물리적으로 접근할 수 없다).

두 의존관계를 위상정렬(topological sort)로 동시에 만족하는 순서를
찾는다 — 깊은 곳(안쪽)부터, 같은 깊이면 아래부터 놓는 순서가 된다.
이렇게 정렬한 뒤에도 "만약 사람이 이 순서대로 놓았다면 각 단계에서
막힘이 없었는가"를 재생하며 검증하는 함수도 함께 둔다(테스트용).
"""
from __future__ import annotations
from typing import List
from .model import Placement

EPS = 1e-6
SUPPORT_TOL = 0.5  # dblf._support_ratio와 동일 기준 (부동소수점/치수 오차 허용)


def _overlap_1d(a1: float, a2: float, b1: float, b2: float) -> float:
    return max(0.0, min(a2, b2) - max(a1, b1))


def _supports(p_below: Placement, p_above: Placement) -> bool:
    """p_below의 윗면이 p_above의 밑면을 (일부라도) 떠받치는가."""
    if abs(p_below.z2 - p_above.z) > SUPPORT_TOL:
        return False
    ox = _overlap_1d(p_above.x, p_above.x2, p_below.x, p_below.x2)
    oy = _overlap_1d(p_above.y, p_above.y2, p_below.y, p_below.y2)
    return ox > EPS and oy > EPS


def _blocks_access(p_shallow: Placement, p_deep: Placement) -> bool:
    """
    p_shallow(문에 더 가까움)가 p_deep으로 가는 길목을 막는가.
    x가 더 작고(문쪽), y·z 범위가 겹치면 그 통로를 차지하는 것이다.
    """
    if not (p_shallow.x < p_deep.x - EPS):
        return False
    oy = _overlap_1d(p_shallow.y, p_shallow.y2, p_deep.y, p_deep.y2)
    if oy <= EPS:
        return False
    oz = _overlap_1d(p_shallow.z, p_shallow.z2, p_deep.z, p_deep.z2)
    return oz > EPS


def compute_load_sequence(placed: List[Placement]) -> List[Placement]:
    """
    최종 배치(placed)를 실제로 적재 가능한 순서로 재정렬해 반환한다.
    (같은 Placement 객체들의 순서만 바뀐다 — 좌표는 그대로.)
    """
    n = len(placed)
    if n <= 1:
        return list(placed)

    # must_before[k] = k를 놓기 전에 반드시 먼저 놓여야 하는 인덱스 집합
    must_before: List[set] = [set() for _ in range(n)]

    for i in range(n):
        pi = placed[i]
        for j in range(n):
            if i == j:
                continue
            pj = placed[j]
            # 1) 지지 의존성: i가 j를 떠받치면, j는 i보다 뒤에.
            if _supports(pi, pj):
                must_before[j].add(i)
            # 2) 접근 의존성: i가 문쪽에서 j로 가는 길을 막으면, i는 j보다 뒤에.
            if _blocks_access(pi, pj):
                must_before[i].add(j)

    # Kahn 위상정렬: succ(순방향 간선) + indegree로 O(n^2) 안에 처리.
    succ: List[set] = [set() for _ in range(n)]
    indegree = [0] * n
    for k in range(n):
        for i in must_before[k]:
            if k not in succ[i]:
                succ[i].add(k)
                indegree[k] += 1

    # 매 단계: 선행 조건이 모두 끝난 후보 중 "더 깊고(x 큼) 더 낮은(z 작음)
    # 것"을 우선한다 — 실무적으로 자연스러운 순서(안쪽부터, 아래부터)이자
    # 안정적인 tie-break 기준이다.
    def sort_key(k: int):
        p = placed[k]
        return (-p.x, p.z, k)

    ready = [k for k in range(n) if indegree[k] == 0]
    order: List[int] = []
    while ready:
        ready.sort(key=sort_key)
        pick = ready.pop(0)
        order.append(pick)
        for k in succ[pick]:
            indegree[k] -= 1
            if indegree[k] == 0:
                ready.append(k)

    if len(order) < n:
        # 이론상 유효한 배치라면 순환이 생기지 않아야 하지만(사이클은
        # "서로가 서로를 막는" 모순 배치를 뜻함), 방어적으로 남은 것을
        # x desc, z asc 기준으로 이어 붙여 전체 계산이 멈추지 않게 한다.
        done = set(order)
        rest = [k for k in range(n) if k not in done]
        rest.sort(key=sort_key)
        order.extend(rest)

    return [placed[i] for i in order]


def find_sequence_violations(ordered: List[Placement]) -> List[str]:
    """
    (테스트/검증용) ordered 순서대로 실제로 하나씩 놓았다고 재생하면서,
    각 단계에서 지지·접근 의존성이 깨지는 곳이 있는지 찾는다.
    빈 리스트를 반환하면 그 순서가 물리적으로 유효하다는 뜻이다.
    """
    problems: List[str] = []
    done: List[Placement] = []
    for idx, p in enumerate(ordered):
        # 지지: 바닥이 아니면 done 중에 이 박스를 떠받치는 게 있어야 함
        if p.z > EPS:
            if not any(_supports(q, p) for q in done):
                problems.append(f"step {idx}: box {p.box.box_id} 는 아래 지지대 없이 공중에 놓임")
        # 접근: done 중에 이 박스로 가는 길을 막는 게 있으면 안 됨
        for q in done:
            if _blocks_access(q, p):
                problems.append(
                    f"step {idx}: box {p.box.box_id} 는 이미 놓인 box {q.box.box_id} 에 막혀 접근 불가")
        done.append(p)
    return problems
