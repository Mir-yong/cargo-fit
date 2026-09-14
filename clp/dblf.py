"""
DBLF (Deepest-Bottom-Left Fill) 디코더.

genotype (박스 순서 + 회전) → 3D 배치(Placement 리스트)로 디코딩.

핵심 규칙:
- 극점을 (x, z, y) 사전식 정렬 → 가장 깊고 낮고 왼쪽 자리 우선
- feasibility: 경계 / 비겹침 / 안정성(지지면적 θ=0.70) / stackable 해석A
- 중량 한도(Qmax): 누적 중량 + 새 박스 무게가 컨테이너 max_weight를 넘으면
  자리가 있어도 unplaced (수리모델 정의서 제약식(32) Σ mᵢuᵢ ≤ Qmax 대응)
- B6: (x,z,y) 완전 동률일 때만 moment difference로 tie-break
- B4: 못 놓는 박스는 unplaced 리스트로 (전량 적재 원칙은 상위에서 처리)
- 옆면 극점의 z는 _resting_z로 실제 지지 높이를 계산 (Algorithm 1 대응)
"""
from __future__ import annotations
from typing import List, Tuple, Optional
from .model import Box, Container, Placement

EPS = 1e-6


def _overlap_1d(a1: float, a2: float, b1: float, b2: float) -> float:
    """1D 구간 [a1,a2], [b1,b2]의 겹치는 길이."""
    return max(0.0, min(a2, b2) - max(a1, b1))


def _overlaps_3d(x, dl, y, dw, z, dh, p: Placement) -> bool:
    """
    새 박스(좌표+치수)와 기존 배치 p가 3D로 겹치는가.
    한 축이라도 분리되면 즉시 False (조기 종료) — 이 함수가 디코딩 시간의
    90%를 차지하므로 불필요한 계산을 최대한 피한다.
    """
    if x + dl <= p.x + EPS or p.x2 <= x + EPS:
        return False
    if y + dw <= p.y + EPS or p.y2 <= y + EPS:
        return False
    if z + dh <= p.z + EPS or p.z2 <= z + EPS:
        return False
    return True


def _support_ratio(x, dl, y, dw, z, placed: List[Placement],
                   tol: float = 0.5) -> float:
    """
    z 높이에 놓일 박스의 밑면(dl×dw) 중, 바로 아래 박스들 윗면에
    지지되는 면적 비율. B5: 사각형 겹침 면적 합산.

    tol: 지지로 인정할 높이 허용오차(cm).
    밑면이 여러 박스에 걸칠 때 높이가 미세하게 다르면(부동소수점 오차나
    수 mm 차이) 실제로는 닿아 있는데도 지지에서 누락되어, 물리적으로
    안정적인 자리가 계속 거부되었다. 이 누락이 용적률의 최대 병목이었다
    (제약 해제 실험에서 49.8% -> 62.9%). 현실적인 허용오차를 준다.
    """
    base_area = dl * dw
    if base_area < EPS:
        return 0.0
    supported = 0.0
    for p in placed:
        if abs(p.z2 - z) > tol:      # 윗면이 이 박스 밑면 높이에 (오차 내) 닿아야 지지
            continue
        ox = _overlap_1d(x, x + dl, p.x, p.x2)
        oy = _overlap_1d(y, y + dw, p.y, p.y2)
        supported += ox * oy
    return min(supported / base_area, 1.0)


def _can_stack_on(x, dl, y, dw, z, placed: List[Placement],
                  tol: float = 0.5) -> bool:
    """
    stackable 해석 A: 이 박스가 다른 박스 '위'에 놓일 때,
    바로 아래 지지 박스 중 stackable=0 인 게 있으면 금지.
    tol은 _support_ratio와 동일 기준을 써야 판정이 일관된다.

    [수정됨] fragile=1인 박스도 같은 이유(위에 아무것도 못 올림)로 금지
    조건에 추가했다 — stackable과 fragile은 의미는 다르지만("적재 방식상"
    vs "파손 위험") 배치 제약으로서의 효과는 동일하다.
    """
    for p in placed:
        if abs(p.z2 - z) > tol:
            continue
        ox = _overlap_1d(x, x + dl, p.x, p.x2)
        oy = _overlap_1d(y, y + dw, p.y, p.y2)
        if ox > EPS and oy > EPS and (p.box.stackable == 0 or p.box.fragile):
            return False
    return True


def _stack_level(x, dl, y, dw, z, placed: List[Placement],
                 tol: float = 0.5) -> int:
    """
    [추가됨] 최대 적재 단수 제약용 — 이 위치에 놓일 박스의 적재 단수.
    바닥(z<=EPS)이면 0(1단째), 아니면 바로 아래서 지지하는 박스들 중
    가장 높은 level + 1. _support_ratio()/_can_stack_on()과 동일한 방식
    (z 근접 + xy 겹침)으로 지지 박스를 찾는다.
    """
    if z <= EPS:
        return 0
    lvl = -1
    for p in placed:
        if abs(p.z2 - z) > tol:
            continue
        ox = _overlap_1d(x, x + dl, p.x, p.x2)
        oy = _overlap_1d(y, y + dw, p.y, p.y2)
        if ox > EPS and oy > EPS:
            lvl = max(lvl, p.level)
    return lvl + 1


def _resting_z(x: float, y: float, placed: List[Placement]) -> float:
    """
    (x, y) 위치에 박스를 놓으면 자연스럽게 도달하는 높이.
    = 그 지점을 밑에서 덮고 있는 박스들 중 가장 높은 윗면. 없으면 0(바닥).
    극점의 z를 이걸로 계산해야, 옆 칸에 더 높거나 낮은 박스가 있어도
    올바른 착지 높이를 후보점으로 만들 수 있다. (Algorithm 1의 Hmax_x/Hmax_y 대응)

    주의: 먼 쪽 경계(x2, y2)는 엄격히 제외해야 함. 그렇지 않으면 박스
    바로 옆(같은 바닥)에 놓일 점이 "그 박스 위"로 잘못 판정되어
    대부분의 배치가 실패하게 된다.
    """
    z = 0.0
    for p in placed:
        if p.x - EPS <= x < p.x2 - EPS and p.y - EPS <= y < p.y2 - EPS:
            if p.z2 > z:
                z = p.z2
    return z


def _feasible(x, dl, y, dw, z, dh,
              placed: List[Placement], cont: Container) -> bool:
    """한 위치에 박스를 놓을 수 있는지 전체 feasibility 체크."""
    # 1) 경계
    if x + dl > cont.L + EPS or y + dw > cont.W + EPS or z + dh > cont.H + EPS:
        return False
    if x < -EPS or y < -EPS or z < -EPS:
        return False
    # 2) 비겹침
    for p in placed:
        if _overlaps_3d(x, dl, y, dw, z, dh, p):
            return False
    # 3) 안정성: 바닥이 아니면 지지면적 cont.min_support_ratio 이상
    # [수정됨] 하드코딩 0.70 → cont.min_support_ratio (기본 0.80으로 상향,
    # 요청별로 설정 가능하게). 논문 기본값 0.70을 그대로 쓰고 싶으면
    # Container(min_support_ratio=0.70)로 명시하면 된다.
    if z > EPS:
        if _support_ratio(x, dl, y, dw, z, placed) < cont.min_support_ratio - EPS:
            return False
        # 4) stackable 해석 A(+fragile): 아래 박스가 못 올리게 돼 있으면 금지
        if not _can_stack_on(x, dl, y, dw, z, placed):
            return False
        # 5) [추가됨] 최대 적재 단수 제약. cont.max_stack_levels가 None이면
        # (기존 동작대로) 제한 없음.
        # level은 0-based(바닥=0단째)라 "최대 단수 N"은 level 0..N-1까지만
        # 허용 → level이 N 이상이면(=N+1단째) 거부. (>= 이지 > 가 아님:
        # max_stack_levels=2일 때 level=2(3단째)까지 허용되는 버그가 있었음.)
        if cont.max_stack_levels is not None:
            if _stack_level(x, dl, y, dw, z, placed) >= cont.max_stack_levels:
                return False
    return True


def _moment_diff(x, dl, y, dw, weight: float,
                 placed: List[Placement], cont: Container) -> float:
    """
    B6 tie-break용. 이 박스를 놓았을 때 좌우/전후 모멘트 불균형 크기.
    무게(없으면 부피)를 가중치로. 값이 작을수록 균형.
    """
    cx = x + dl / 2
    cy = y + dw / 2
    # 기존 배치들의 모멘트 누적
    mx = weight * (cx - cont.center[0])
    my = weight * (cy - cont.center[1])
    for p in placed:
        pcx, pcy, _ = p.centroid
        mx += p.box.weight * (pcx - cont.center[0])
        my += p.box.weight * (pcy - cont.center[1])
    return abs(mx) + abs(my)


def compute_zones(order: List[Box], cont: Container) -> dict:
    """
    하역지(priority)별로 x축(깊이) 구역을 할당한다.

    문제: 랭킹이 priority를 최우선으로 두므로 priority 1 화물이 먼저
    전부 배치되는데, DBLF는 '가장 깊고 낮은 곳'부터 채우므로 앞 순위가
    컨테이너 전체에 퍼져버린다. 그 결과 뒤 순위(2,3)는 파편화된 틈만
    남아 절반 이상이 미적재된다. (실측: p1 미적재 2%, p2 53%, p3 78%)

    해결: 각 priority에 부피 비율만큼 x구간을 예약한다.
    나중에 내릴 화물(priority 큰 값)일수록 안쪽(x가 큰 쪽)에 둔다.
    → 문 쪽부터 p1, p2, p3 순으로 세로 구획이 생겨
      용적률·미적재·리핸들링이 동시에 개선된다.

    반환: {priority: (x_start, x_end)}
    """
    vol_by_pr = {}
    for b in order:
        vol_by_pr[b.priority] = vol_by_pr.get(b.priority, 0.0) + b.volume
    total = sum(vol_by_pr.values())
    if total <= 0:
        return {}

    # 화물이 컨테이너보다 많으면 컨테이너 전체를 비율대로 나눔
    prs = sorted(vol_by_pr.keys())          # 1, 2, 3 ...
    zones = {}
    cursor = 0.0
    for pr in prs:                          # priority 1이 문쪽(x=0)부터
        share = vol_by_pr[pr] / total
        width = cont.L * share
        zones[pr] = (cursor, min(cursor + width, cont.L))
        cursor += width
    return zones


def _find_position(box: Box, o: int, zx0: float, zx1: float,
                   eps: List[Tuple[float, float, float]],
                   placed: List[Placement], cont: Container):
    """
    한 박스를 놓을 자리를 eps(극점 후보, 이미 (x,z,y) 정렬된 상태)에서 찾는다.
    o: 우선 시도할 회전. 그 회전으로 못 찾으면 나머지 5개를 시도.
    반환: (best_xyz 또는 None, best_o, best_dims)
    decode()의 메인 루프와 2차 삽입 재시도 루프가 이 로직을 공유한다.
    """
    rot_candidates = [o] + [r for r in range(6) if r != o]

    best: Optional[Tuple[float, float, float]] = None
    best_o = o
    best_dims = box.dims_for_orientation(o)

    for cand_o in rot_candidates:
        dl, dw, dh = box.dims_for_orientation(cand_o)

        # 1단계: 자기 구역 안에서만 / 2단계: 전체 컨테이너로 확장
        found: Optional[Tuple[float, float, float]] = None
        for stage in (0, 1):
            best_key = None
            for (px, py, pz) in eps:
                if stage == 0:
                    if px < zx0 - EPS or px + dl > zx1 + EPS:
                        continue
                if _feasible(px, dl, py, dw, pz, dh, placed, cont):
                    key = (px, pz, py)
                    if found is None:
                        found, best_key = (px, py, pz), key
                    elif key == best_key:
                        # B6: (x,z,y) 완전 동률일 때만 moment로 tie-break
                        md_new = _moment_diff(px, dl, py, dw, box.weight, placed, cont)
                        md_old = _moment_diff(found[0], dl, found[1], dw, box.weight, placed, cont)
                        if md_new < md_old:
                            found, best_key = (px, py, pz), key
                    if key != best_key:
                        break
            if found is not None:
                break

        if found is not None:
            best = found
            best_o = cand_o
            best_dims = (dl, dw, dh)
            break

    return best, best_o, best_dims


def _update_eps(eps: List[Tuple[float, float, float]], bx: float, by: float, bz: float,
                dl: float, dw: float, dh: float,
                placed: List[Placement], cont: Container) -> List[Tuple[float, float, float]]:
    """박스 하나를 놓은 뒤 극점 리스트 갱신: 사용한 점 제거 + 새 점 3개."""
    if (bx, by, bz) in eps:
        eps.remove((bx, by, bz))

    # 옆면 두 점은 그 (x,y) 위치의 실제 지지 높이로 계산 (Algorithm 1)
    side_x_z = _resting_z(bx + dl, by, placed)
    side_y_z = _resting_z(bx, by + dw, placed)

    eps.append((bx + dl, by, side_x_z))   # +x 면 (실제 지지 높이)
    eps.append((bx, by + dw, side_y_z))   # +y 면 (실제 지지 높이)
    eps.append((bx, by, bz + dh))         # +z 면 (위)

    # 경계를 벗어난 극점 pruning
    eps = [(ex, ey, ez) for (ex, ey, ez) in eps
           if ex < cont.L - EPS and ey < cont.W - EPS and ez < cont.H - EPS]
    # 중복 제거
    return list(set(eps))


def decode(order: List[Box], rotations: List[int],
           cont: Container, use_zones: bool = True) -> Tuple[List[Placement], List[Box]]:
    """
    genotype 디코딩.
    order: 배치 순서대로 정렬된 박스 리스트
    rotations: 각 박스의 회전 모드 (order와 같은 순서, box_id 매칭 아님)
    use_zones: 하역지별 x구역 할당을 쓸지 여부.
        True(기본) : 리핸들링을 줄이기 위해 구역을 우선 시도 (실무 기본값)
        False      : 구역 제약 없이 순수하게 공간만 채움
                     (리핸들링을 신경 안 쓰는 "최대 용적률" 옵션용.
                      네이버지도의 "환승 제약 없는 최단시간 경로"와 같은 개념)

    반환: (placed, unplaced)
    """
    placed: List[Placement] = []
    unplaced: List[Box] = []

    # 극점 후보 리스트. 시작은 원점 하나.
    eps: List[Tuple[float, float, float]] = [(0.0, 0.0, 0.0)]

    # 하역지별 x구역 할당 (앞 순위가 공간을 독점하는 문제 해결)
    # use_zones=False면 구역 없이 전체 컨테이너를 바로 씀 (최대 용적률용)
    zones = compute_zones(order, cont) if use_zones else {}

    # [추가됨] 중량 한도(Qmax) 추적. 수리모델 정의서 제약식(32) Σ mᵢuᵢ ≤ Qmax 대응.
    # 지금까지 실은 박스들의 총중량 누적치. 이 값 + 새 박스 무게가 컨테이너
    # max_weight를 넘으면 그 박스는 (자리가 있어도) 못 싣는다.
    total_weight = 0.0

    for box, o in zip(order, rotations):
        # [추가됨] 중량 체크는 자리(위치)와 무관한 전역 제약이라, 자리를 찾기 전에
        # 딱 한 번만 확인한다 — eps 후보마다 반복 체크하는 것보다 정확하고 빠르다.
        # 넘으면 이 박스는 자리를 찾아볼 것도 없이 바로 unplaced로 보낸다(B4 원칙과 동일).
        if total_weight + box.weight > cont.max_weight + EPS:
            unplaced.append(box)
            continue

        # DBLF: 극점을 (x, z, y) 사전식 정렬
        # 가장 깊고(deeper) 낮고(bottom) 왼쪽(left) 자리 우선.
        eps.sort(key=lambda p: (p[0], p[2], p[1]))

        zx0, zx1 = zones.get(box.priority, (0.0, cont.L))

        # 회전 보정: GA가 지정한 회전 o를 1순위로 시도하고,
        # 그 회전으로는 놓을 자리가 없을 때만 나머지 회전을 시도한다.
        # (미적재의 상당수가 "그 회전으로는 안 들어가는" 큰 박스였다.
        #  GA의 탐색 의도는 존중하면서 미적재만 줄이는 절충.)
        # 주: '여러 회전을 평가해 최선을 고르는' 방식도 시험했으나
        #     용적률이 오히려 떨어져 원복함.
        best, best_o, best_dims = _find_position(box, o, zx0, zx1, eps, placed, cont)

        if best is None:
            unplaced.append(box)
            continue

        bx, by, bz = best
        dl, dw, dh = best_dims          # 회전 보정 결과 반영
        # [추가됨] 최대 적재 단수 리포팅/제약용 — 이 자리의 실제 적재 단수 기록.
        lvl = _stack_level(bx, dl, by, dw, bz, placed)
        pl = Placement(box=box, x=bx, y=by, z=bz,
                       orientation=best_o, dl=dl, dw=dw, dh=dh, level=lvl)
        placed.append(pl)
        total_weight += box.weight  # [추가됨] 중량 누적
        eps = _update_eps(eps, bx, by, bz, dl, dw, dh, placed, cont)

    return placed, unplaced


def _rebuild_eps(placed: List[Placement], cont: Container) -> List[Tuple[float, float, float]]:
    """
    이미 완성된 배치(placed)로부터 극점 후보를 재구성한다.
    decode() 도중이 아니라, 다 끝난 배치에 대해 사후에 2차 삽입을
    시도할 때(retry_unplaced) 쓴다 — 그때는 decode() 내부의 eps
    상태를 더 이상 갖고 있지 않으므로 놓인 박스들로부터 다시 만든다.

    [주의] 이 방식(박스 하나당 극점 3개)은 decode()의 메인 루프와 같은
    표준 DBLF 방식이지만 완전하지 않다 — 서로 다른 두 박스의 경계를
    조합해야만 나오는 자리(예: 박스 A의 x2와 박스 B의 y 경계가 만나는
    지점)는 후보에 아예 안 잡힌다. retry_unplaced()는 이 함수 대신
    _rebuild_grid_points()(모든 박스 x·y 경계의 전 조합)를 쓴다 —
    실측으로 이 차이 때문에 빈틈을 놓치는 사례가 확인됐다
    (아래 retry_unplaced 문서 참고). 이 함수는 더 가벼운 후보 집합이
    필요할 수 있는 다른 용도를 위해 남겨 둔다.
    """
    pts = {(0.0, 0.0, 0.0)}
    for p in placed:
        pts.add((p.x2, p.y, _resting_z(p.x2, p.y, placed)))
        pts.add((p.x, p.y2, _resting_z(p.x, p.y2, placed)))
        pts.add((p.x, p.y, p.z2))
    return [(x, y, z) for x, y, z in pts
            if x < cont.L - EPS and y < cont.W - EPS and z < cont.H - EPS]


def _rebuild_grid_points(placed: List[Placement], cont: Container) -> List[Tuple[float, float, float]]:
    """
    이미 놓인 박스들의 x·y 경계를 모두 모아 "전 조합" 격자를 만든다.
    (x 후보: 0과 모든 박스의 x, x2 / y 후보: 0과 모든 박스의 y, y2)
    각 (x,y) 조합에서의 실제 높이는 _resting_z로 구한다.

    _rebuild_eps(박스 하나당 극점 3개)보다 비싸지만 — retry_unplaced는
    탐색 전체가 아니라 최종 결과에 대해 한 번만 돌기 때문에 감당할 만
    하다 — 서로 다른 박스의 x 경계와 y 경계를 조합한 자리까지 후보에
    잡는다. 실측(BR1): 극점 3개 방식으로는 놓쳤던 타입2 박스 3개가
    이 방식으로는 자리를 찾았다(용적률 81.84% -> 83.02%).
    """
    xs = sorted({0.0} | {p.x for p in placed} | {p.x2 for p in placed})
    ys = sorted({0.0} | {p.y for p in placed} | {p.y2 for p in placed})
    pts = []
    for x in xs:
        if x >= cont.L - EPS:
            continue
        for y in ys:
            if y >= cont.W - EPS:
                continue
            z = _resting_z(x, y, placed)
            if z < cont.H - EPS:
                pts.append((x, y, z))
    return pts


def retry_unplaced(placed: List[Placement], unplaced: List[Box], cont: Container,
                   use_zones: bool = True, max_rounds: int = 50
                   ) -> Tuple[List[Placement], List[Box]]:
    """
    2차 삽입 재시도 (사후 처리 전용 — decode() 안에서는 부르지 않는다).

    DBLF decode()는 박스를 한 번 훑고 지나가면 되돌아가지 않는 구조라,
    어떤 박스를 검토하던 시점엔 자리가 없어서 unplaced로 넘어갔는데, 그
    뒤 다른 박스들이 자리를 잡으며 새로 생긴 빈틈은 다시 확인하지
    않는다. (실측: BR1에서 미적재 19개 중 2개가 이런 빈틈에 들어갈 수
    있는데도 놓치고 있었다 — 3D 애니메이션에서 눈에 보이는 빈 공간이 그
    흔적이었다.)

    이 재시도를 decode() 안에서 매번 돌리면 GA 진화 중 수천 번씩
    호출되는 decode()가 크게 느려진다 — 특히 초기 세대는 미적재가
    많은 개체가 흔해서, 시도해보니 BR1 한 건(pop=30·gen=30)이
    몇 초에서 80초 이상으로 늘어났다. 그래서 탐색이 다 끝난 뒤,
    최종적으로 사용자에게 보여줄 개체(파레토 프론트)에 대해서만 한
    번 호출한다. 이미 놓인 박스는 전혀 건드리지 않고 "더 채우기"만
    하므로 결과가 나빠질 일은 없다(용적률은 같거나 오른다).

    [추가됨] 후보 자리는 _rebuild_eps(박스 하나당 극점 3개, decode()와
    같은 표준 DBLF 방식)가 아니라 _rebuild_grid_points(모든 박스의
    x·y 경계 전 조합)를 쓴다. 처음엔 극점 방식을 썼는데, 그 방식으로
    "더 이상 넣을 게 없다"고 끝난 뒤에도 사용자가 화면에서 빈 공간을
    다시 지적해서 재확인해보니, 서로 다른 두 박스의 경계를 조합해야만
    나오는 자리(박스 A의 x2와 박스 B의 y 경계가 만나는 지점 같은)를
    극점 방식이 후보에 아예 못 올리고 있었다 — 실측: BR1에서 타입2
    박스 3개가 이 자리들에 더 들어가 81.84% -> 83.02%. 매 라운드마다
    격자를 다시 만드므로(아래 루프), 방금 놓은 박스의 경계도 다음
    라운드부터 바로 후보에 반영된다.
    """
    if not unplaced:
        return placed, unplaced

    placed = list(placed)
    unplaced = list(unplaced)
    total_weight = sum(p.box.weight for p in placed)

    all_boxes = [p.box for p in placed] + unplaced
    zones = compute_zones(all_boxes, cont) if use_zones else {}

    changed = True
    rounds = 0
    while changed and unplaced and rounds < max_rounds:
        changed = False
        rounds += 1
        pts = _rebuild_grid_points(placed, cont)
        pts.sort(key=lambda p: (p[0], p[2], p[1]))
        still_unplaced: List[Box] = []
        for box in unplaced:
            if total_weight + box.weight > cont.max_weight + EPS:
                still_unplaced.append(box)
                continue

            zx0, zx1 = zones.get(box.priority, (0.0, cont.L))
            # 사후 재시도라 GA가 지정했던 원래 회전 선호는 남아있지 않다 —
            # 0번부터 6방향을 모두 순서대로 시도한다(_find_position이 처리).
            best, best_o, best_dims = _find_position(box, 0, zx0, zx1, pts, placed, cont)

            if best is None:
                still_unplaced.append(box)
                continue

            bx, by, bz = best
            dl, dw, dh = best_dims
            lvl = _stack_level(bx, dl, by, dw, bz, placed)  # [추가됨]
            pl = Placement(box=box, x=bx, y=by, z=bz,
                           orientation=best_o, dl=dl, dw=dw, dh=dh, level=lvl)
            placed.append(pl)
            total_weight += box.weight
            changed = True

        unplaced = still_unplaced

    return placed, unplaced