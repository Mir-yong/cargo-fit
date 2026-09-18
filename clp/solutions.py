"""
파레토 프론트에서 사용자에게 보여줄 대표 해 3개를 고른다.

네이버 지도가 "최단시간 / 최소환승 / 추천"을 제시하듯,
용적률과 리핸들링의 트레이드오프 양 끝과 중간을 보여준다.

  max_fill    : 공간을 최대한 활용 (운임 절감 우선)
  balanced    : 두 지표를 함께 만족하는 중간 (기본 추천)
  min_reloc   : 하역 재작업 최소 (인건비·시간 절감 우선)
"""
from __future__ import annotations
from typing import List, Dict, Any

from .model import Container
from .objectives import utilization, ulo_scan, cg_deviation, avg_support_ratio, blocking_by_destination
from .load_sequence import compute_load_sequence
from .dblf import retry_unplaced


def _metrics(ind, cont: Container) -> Dict[str, Any]:
    """개체 하나의 전체 지표를 계산."""
    placed = ind.placed
    total = len(placed) + len(ind.unplaced)
    # [수정됨] 4번 항목: count_ulo() + count_blocking_boxes()를 따로 부르면
    # 같은 순회를 두 번 하게 되어 ulo_scan() 한 번으로 합쳤다.
    ulo, blockers = ulo_scan(placed)
    reloc = len(blockers)
    return {
        "utilization": round(utilization(placed, cont) * 100, 2),
        "ulo": ulo,
        "relocation": reloc,
        "relocation_pct": round(reloc / total * 100, 1) if total else 0.0,
        "cg_deviation": round(cg_deviation(placed, cont) * 100, 2),
        "support_avg": round(avg_support_ratio(placed) * 100, 1),
        "placed": len(placed),
        "unplaced": len(ind.unplaced),
        "total": total,
        "weight": round(sum(p.box.weight for p in placed), 1),
        # [추가됨] 목적지별 LIFO 검증 — parse_csv()가 업로드 시점에 이미
        # "같은 destination은 같은 priority"를 강제하므로, relocation==0이면
        # 진짜로 어느 목적지도 재배치 없이 LIFO 하역이 가능하다는 뜻이다.
        "lifo_compliant": reloc == 0,
        "relocation_by_destination": blocking_by_destination(placed),
    }


def pick_three(front: List, cont: Container) -> Dict[str, Any]:
    """
    파레토 프론트에서 3개 대표 해를 선택.
    프론트가 3개 미만이면 있는 만큼만 반환(중복 없이).
    """
    if not front:
        return {}

    scored = [(ind, _metrics(ind, cont)) for ind in front]

    # [추가됨] 2차 삽입 재시도(retry_unplaced) 적용 "전" 지표를 각 개체에
    # 붙여 둔다 — pick_by_weight()가 나중에(사용자가 슬라이더를 움직일
    # 때마다) front를 다시 스캔할 때 이 스냅샷을 비교 기준으로 쓰기
    # 위해서다. front의 Individual 객체는 이 함수와 pick_by_weight()가
    # 같은 job에서 계속 공유·재사용하는데, retry_unplaced는 ind.placed를
    # 제자리에서(in-place) 바꾼다. 만약 비교를 매번 "지금 상태" 기준으로
    # 다시 하면, 이전에 슬라이더로 이미 재시도를 거친 개체(예: 용적률은
    # 올랐지만 그 부작용으로 리핸들링도 같이 오른 개체)와 아직 손대지
    # 않은 개체를 공정하게 비교할 수 없어 — 같은 가중치로 다시 슬라이더를
    # 움직여도 그 사이 다른 가중치를 탐색했는지에 따라 결과가 달라지는
    # 비결정적 동작이 생긴다(실측으로 확인함). 이 스냅샷을 고정 기준으로
    # 쓰면 어떤 순서로 슬라이더를 움직이든 같은 가중치는 항상 같은 개체를
    # 가리킨다.
    for ind, m in scored:
        if not hasattr(ind, "_orig_metrics"):
            ind._orig_metrics = m

    # [추가됨] 파레토 최적해 집합 전체 곡선(용적률-리핸들링 산점도) 시각화용.
    # 화면에 실제로 보여줄 3개 대표 해뿐 아니라, 탐색이 찾아낸 프론트 전체
    # (보통 20~30개)의 용적률/리핸들링 좌표를 함께 내려보낸다 — 2차 삽입
    # 재시도 "전" 값이라 아래 3개 대표 해의 좌표와는 살짝 다를 수 있는데,
    # 이 곡선은 "탐색이 실제로 찾아낸 트레이드오프 분포"를 보여주는 배경
    # 용도라 괜찮다(강조 표시되는 3개 점은 재시도 반영된 최종 metrics를 쓴다).
    front_curve = sorted(
        [{"utilization": m["utilization"], "relocation": m["relocation"],
          "cg_deviation": m["cg_deviation"]} for _, m in scored],
        key=lambda p: p["utilization"],
    )

    # 1) 최대 용적률 — 동률이면 리핸들링 적은 쪽
    max_fill = max(scored, key=lambda s: (s[1]["utilization"], -s[1]["relocation"]))

    # 2) 최소 리핸들링 — 동률이면 용적률 높은 쪽
    min_reloc = min(scored, key=lambda s: (s[1]["relocation"], -s[1]["utilization"]))

    # 3) 균형 — 두 지표를 0~1로 정규화해 합이 최대인 해
    utils = [m["utilization"] for _, m in scored]
    relocs = [m["relocation"] for _, m in scored]
    u_lo, u_hi = min(utils), max(utils)
    r_lo, r_hi = min(relocs), max(relocs)

    def balance_score(m):
        un = (m["utilization"] - u_lo) / (u_hi - u_lo) if u_hi > u_lo else 1.0
        rn = (r_hi - m["relocation"]) / (r_hi - r_lo) if r_hi > r_lo else 1.0
        return un + rn

    balanced = max(scored, key=lambda s: balance_score(s[1]))

    picks = [
        ("max_fill", "최대 용적률", "공간을 최대한 활용합니다", max_fill),
        ("balanced", "균형", "용적률과 하역 편의를 함께 고려합니다", balanced),
        ("min_reloc", "최소 리핸들링", "하역 시 재작업을 최소화합니다", min_reloc),
    ]

    # 같은 개체가 여러 슬롯에 뽑히면 중복 표시하지 않음
    out = []
    seen = set()
    for key, label, desc, (ind, m) in picks:
        # [추가됨] 2차 삽입 재시도(retry_unplaced, dblf.py) — 화면에 실제로
        # 보여줄 대표 해 3개(중복 포함 최대 3번)에 대해서만 적용한다.
        # 프론트 전체(pop_size개)에 매번 적용하면 계산 시간이 눈에 띄게
        # 늘어나는데(run_nsga2.py 참고), 사용자가 실제로 보는 건 이 3개
        # 뿐이므로 여기서만 하면 충분하다. 이미 놓인 박스는 그대로 두고
        # "더 채우기"만 하므로 결과가 나빠질 일은 없다.
        if ind.unplaced:
            p2, u2 = retry_unplaced(ind.placed, ind.unplaced, cont, use_zones=True)
            if len(p2) > len(ind.placed):
                ind.placed = p2
                ind.unplaced = u2
        # 재시도 여부와 무관하게 최신 상태 기준으로 다시 계산한다 — 같은
        # 개체가 다른 슬롯에서 이미 재시도됐을 수도 있어서(중복 픽) 위에서
        # 캡처해둔 m이 이미 낡았을 수 있다.
        m = _metrics(ind, cont)
        sig = (m["utilization"], m["relocation"], m["cg_deviation"])
        out.append({
            "key": key,
            "label": label,
            "description": desc,
            "duplicate": sig in seen,
            "metrics": m,
            "placements": _serialize(ind, cont),
            "unplaced_by_shipper": _unplaced_by_shipper(ind),
        })
        seen.add(sig)

    return {"solutions": out, "front_size": len(front), "front_curve": front_curve}


def _finalize_pick(ind, cont: Container, key: str, label: str, description: str,
                    extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    [추가됨] pick_by_weight()와 pick_by_point()가 공통으로 쓰는 마무리 단계 —
    화면에 실제로 보여줄 해 하나에 대해 2차 삽입 재시도(retry_unplaced)를
    적용하고, 최종 지표를 다시 계산해 직렬화까지 마친 dict를 돌려준다.
    두 함수 모두 "어느 개체를 고를지"는 서로 다른 방식으로 정하지만,
    고른 뒤 마무리하는 절차는 완전히 같아 중복을 없앴다.
    """
    if ind.unplaced:
        p2, u2 = retry_unplaced(ind.placed, ind.unplaced, cont, use_zones=True)
        if len(p2) > len(ind.placed):
            ind.placed = p2
            ind.unplaced = u2
    m = _metrics(ind, cont)
    out = {
        "key": key,
        "label": label,
        "description": description,
        "duplicate": False,
        "metrics": m,
        "placements": _serialize(ind, cont),
        "unplaced_by_shipper": _unplaced_by_shipper(ind),
    }
    if extra:
        out.update(extra)
    return out


def pick_by_point(front: List, cont: Container, utilization_v: float, relocation_v: float) -> Dict[str, Any]:
    """
    [추가됨] 팀 피드백: 파레토 그래프의 회색 점을 직접 클릭했을 때 —
    그 점은 result.front_curve(재시도 "전" 원본 좌표)에서 그려진 것이므로,
    각 개체의 _orig_metrics(pick_three()가 남겨둔 재시도 전 스냅샷)와
    좌표가 정확히 일치하는 개체를 찾아 반환한다. relocation은 정수라
    정확히 비교하고, utilization은 서버·클라이언트 반올림 과정에서 생길
    수 있는 부동소수점 오차를 감안해 작은 허용오차(0.005%p)로 비교한다.
    """
    if not front:
        return {}
    for ind in front:
        m0 = getattr(ind, "_orig_metrics", None)
        if m0 and abs(m0["utilization"] - utilization_v) < 0.005 and m0["relocation"] == relocation_v:
            return _finalize_pick(
                ind, cont, "custom", "직접 조정",
                f"그래프에서 직접 고른 해입니다 (용적률 {utilization_v}% · 재작업 {int(relocation_v)}개)",
            )
    return {}


def pick_by_weight(front: List, cont: Container, util_weight: float) -> Dict[str, Any]:
    """
    [추가됨] 용적률/리핸들링 가중치 슬라이더용.

    pick_three()가 고정된 3개 지점(최대 용적률/균형/최소 리핸들링)만 보여주는
    것과 달리, 이 함수는 사용자가 슬라이더로 고른 임의의 가중치(util_weight,
    0.0=리핸들링만 고려 ~ 1.0=용적률만 고려)에 가장 잘 맞는 해 하나를
    파레토 프론트에서 골라준다.

    run_nsga2()가 이미 계산해 둔 프론트(front) 안에서만 고르고 새로
    탐색하지 않으므로, pick_three()와 마찬가지로 사실상 즉시(수 ms) 응답할
    수 있다 — 슬라이더를 움직일 때마다 서버가 NSGA-II를 다시 돌리는 게
    아니라, 이미 찾아둔 트레이드오프 곡선 위에서 사용자가 원하는 지점을
    짚어주는 것뿐이다.
    """
    if not front:
        return {}
    util_weight = max(0.0, min(1.0, util_weight))

    # [수정됨] "어느 개체를 고를지" 판단은 pick_three()가 남겨둔 재시도 전
    # 스냅샷(_orig_metrics)으로 한다 — 지금 이 순간의 ind.placed로 다시
    # 재는 게 아니다. 그렇지 않으면, 이전에 다른 가중치로 슬라이더를
    # 움직이면서 이미 재시도를 거친 개체(용적률은 올랐지만 그 부작용으로
    # 리핸들링도 같이 늘어난 경우가 있음)와 아직 손대지 않은 개체를
    # 비교할 때 기준이 뒤섞여, 같은 가중치를 나중에 다시 골라도 그 사이
    # 다른 가중치를 탐색했는지에 따라 결과가 달라지는 비결정적 동작이
    # 생긴다(실측으로 확인함). _orig_metrics가 없는(=pick_three()를 아직
    # 거치지 않은, 이론상 없어야 하는) 개체만 방어적으로 그 자리에서 계산.
    scored = [(ind, getattr(ind, "_orig_metrics", None) or _metrics(ind, cont)) for ind in front]
    utils = [m["utilization"] for _, m in scored]
    relocs = [m["relocation"] for _, m in scored]
    u_lo, u_hi = min(utils), max(utils)
    r_lo, r_hi = min(relocs), max(relocs)

    # [수정됨] "슬라이더를 끝에서 끝까지 움직여도 용적률이 3개 값에서만
    # 바뀐다"는 문제의 원인 수정.
    #
    #   원인: 기존 식은 선형 가중합(util_weight*un + (1-util_weight)*rn)의
    #   최대값을 골랐다. 유한한 점 집합 위에서 선형 함수를 최대화하면,
    #   가중치를 0~1 어떻게 바꾸더라도 **볼록 껍질(convex hull)의 꼭짓점**
    #   에 해당하는 점만 선택된다 — 이건 가중합 스칼라화의 잘 알려진
    #   한계다. 그런데 2목적(용적률↑ / 리핸들링↓) 파레토 프론트는 리핸들링이
    #   정수라 계단(staircase) 모양이 되고, 계단의 대부분 점은 껍질 "안쪽"
    #   으로 들어간다. 즉 프론트에 20~30개 해가 있어도 어떤 가중치로도
    #   도달할 수 없는 해가 대다수이고, 실제로 뽑히는 건 껍질 꼭짓점 몇
    #   개(보통 2~4개)뿐이었다. 슬라이더가 "선형으로 비례해서" 변하지
    #   않고 고정값 몇 개로만 점프한 이유가 이것이다.
    #
    #   해결: 증강 가중 체비셰프 스칼라화(augmented weighted Tchebycheff)로
    #   교체한다. 이상점(둘 다 최고인 가상의 점)까지의 "가중 최대 편차"를
    #   최소화하는 방식이라, 볼록하지 않은 구간에 있는 해도 어떤 가중치
    #   에서는 반드시 유일한 최소값이 된다 — 즉 프론트 위의 모든 파레토
    #   해가 슬라이더로 도달 가능해진다. 뒤의 RHO 항(아주 작은 값)은
    #   체비셰프 단독으로는 약하게 지배되는 해가 동점으로 뽑힐 수 있는
    #   문제를 막는 표준적인 보정항이다.
    #
    #   양 끝 동작은 기존과 동일하다: util_weight=1 → 용적률 최대,
    #   util_weight=0 → 리핸들링 최소.
    #
    #   실측(modified_BR6_full.csv, instance 1, 20ft): 프론트 12개 중
    #   비지배 해는 5개였는데, 선형 가중합은 슬라이더 0~100 전 구간에서
    #   3개((66.79,26) (71.4,36) (71.54,40))밖에 못 골랐다 — 보고된
    #   "고정값 3개" 증상과 정확히 일치한다. 체비셰프로 바꾸면 같은
    #   프론트에서 비지배 해 5개 전부가 선택 가능해진다. (나머지 7개는
    #   두 지표 모두 열세인 피지배 해라 선택되지 않는 게 맞다.)
    RHO = 1e-3

    def cheby_dist(m):
        un = (m["utilization"] - u_lo) / (u_hi - u_lo) if u_hi > u_lo else 1.0
        rn = (r_hi - m["relocation"]) / (r_hi - r_lo) if r_hi > r_lo else 1.0
        du, dr = 1.0 - un, 1.0 - rn          # 이상점 대비 편차(작을수록 좋음)
        return max(util_weight * du, (1.0 - util_weight) * dr) + RHO * (du + dr)

    # [수정됨] 스칼라화 점수만으로 고르면 동점(특히 슬라이더 양 끝 0/100%
    # 근처 — 용적률이 같은 프론트 개체가 여럿이면 un이 전부 1.0이 되어
    # 완전히 동점)일 때 front 순서상 우연히 먼저 오는 개체가 뽑혀
    # 리핸들링이 더 나쁜 해가 나올 수 있다(실측: util_weight=1.0인데
    # pick_three()의 max_fill(동점 시 리핸들링 적은 쪽)보다 리핸들링이
    # 더 큰 해가 선택됨). pick_three()의 max_fill/min_reloc과 같은 규칙
    # (동점이면 용적률 높은 쪽 → 그래도 동점이면 리핸들링 적은 쪽)을
    # 2·3순위 동점 처리 기준으로 추가해 일관되게 맞춘다.
    # 체비셰프는 "최소화"라 max()가 아니라 min()이다. 동점 처리 기준은
    # 기존과 동일 — 동점이면 용적률 높은 쪽 → 그래도 동점이면 리핸들링
    # 적은 쪽(pick_three()의 max_fill/min_reloc 규칙과 일치).
    ind, m = min(scored, key=lambda s: (cheby_dist(s[1]), -s[1]["utilization"], s[1]["relocation"]))

    # [수정됨] 마무리(2차 삽입 재시도 + 지표 재계산 + 직렬화)는 pick_by_point()와
    # 완전히 같은 절차라 _finalize_pick()으로 공통화했다. pick_three()와
    # 동일한 이유로 — 실제로 화면에 보여줄 해 하나(슬라이더가 지금
    # 가리키는 해)에 대해서만 2차 삽입 재시도를 적용한다. 프론트 전체에
    # 매번 적용하면 느려지지만, 슬라이더 반응 하나만 계산하는 건
    # pick_three()가 3번 하는 것보다도 저렴하다.
    pct = round(util_weight * 100)
    return _finalize_pick(
        ind, cont, "custom", "직접 조정",
        f"용적률 {pct}% · 리핸들링 {100 - pct}% 가중치로 직접 고른 해입니다",
        extra={"util_weight": util_weight},
    )


def sweep_reachable(front: List, cont: Container, steps: int = 1000) -> List[Dict[str, Any]]:
    """
    [추가됨] 정적 데모(GitHub Pages)용 — 가중치 슬라이더로 "도달 가능한" 해를
    전부 미리 뽑아 둔다.

    슬라이더 자체는 서버가 필요 없다. 어느 해를 고를지는 프론트엔드도
    똑같이 계산하고 있고(wbPickIndex, 눈금 표시용), 실제로 서버와 1,001개
    지점에서 완전히 일치하는 것을 확인했다. 정적 모드에서 못 쓰던 유일한
    이유는 "고른 해의 박스 좌표"가 demo-result.json에 없어서였다.

    그래서 여기서 가중치 0~1을 훑어 실제로 선택되는 해를 모두 모아,
    각각의 좌표까지 직렬화해 번들에 넣는다. 해 하나당 약 18KB이고 보통
    5~15개라 정적 호스팅에 올리기에 충분히 작다.

    각 항목의 w_lo/w_hi는 그 해가 선택되는 가중치 구간이라, 프론트는
    슬라이더 값이 어느 구간에 드는지만 보면 된다.
    """
    if not front:
        return []

    scored = [(ind, getattr(ind, "_orig_metrics", None) or _metrics(ind, cont)) for ind in front]
    utils = [m["utilization"] for _, m in scored]
    relocs = [m["relocation"] for _, m in scored]
    u_lo, u_hi = min(utils), max(utils)
    r_lo, r_hi = min(relocs), max(relocs)
    RHO = 1e-3

    def pick_index(w: float) -> int:
        best, bkey = -1, None
        for i, (_, m) in enumerate(scored):
            un = (m["utilization"] - u_lo) / (u_hi - u_lo) if u_hi > u_lo else 1.0
            rn = (r_hi - m["relocation"]) / (r_hi - r_lo) if r_hi > r_lo else 1.0
            du, dr = 1.0 - un, 1.0 - rn
            d = max(w * du, (1.0 - w) * dr) + RHO * (du + dr)
            key = (d, -m["utilization"], m["relocation"])
            if best < 0 or key < bkey:
                best, bkey = i, key
        return best

    # 가중치 구간별로 어떤 개체가 뽑히는지 스윕
    spans: List[Dict[str, Any]] = []
    prev_idx = None
    for k in range(steps + 1):
        w = k / steps
        idx = pick_index(w)
        if idx != prev_idx:
            spans.append({"idx": idx, "w_lo": w, "w_hi": w})
            prev_idx = idx
        else:
            spans[-1]["w_hi"] = w

    out: List[Dict[str, Any]] = []
    for sp in spans:
        ind, m0 = scored[sp["idx"]]
        sol = _finalize_pick(
            ind, cont, "custom", "직접 조정",
            "가중치 슬라이더로 고른 해입니다",
        )
        # w_lo/w_hi: 이 해가 선택되는 가중치 구간
        # orig: 파레토 그래프의 회색 점 좌표(재시도 전) — 점 클릭 매칭용
        sol["w_lo"] = round(sp["w_lo"], 4)
        sol["w_hi"] = round(sp["w_hi"], 4)
        sol["orig"] = {"utilization": m0["utilization"], "relocation": m0["relocation"]}
        out.append(sol)
    return out


def _serialize(ind, cont: Container) -> List[Dict[str, Any]]:
    """
    3D 렌더링용 좌표 직렬화. 적재 순서를 함께 담아 애니메이션(과 명세서
    순번)에 쓴다.

    [수정됨] ind.placed의 원래 순서는 DBLF가 지오노타입을 처리한
    순서(우선순위 랭킹 순)일 뿐, 실제로 그 순서대로 문(x=0)에서부터
    하나씩 놓을 수 있다는 보장이 없다 — 구역이 문쪽부터 우선순위
    순으로 배정되므로, 이 순서 그대로 재생하면 문쪽을 먼저 채운 뒤
    안쪽 화물을 나중에 "놓아야" 해서 실제로는 이미 막힌 자리에
    화물이 나타나는 장면이 된다(현장에서 재현 불가능한 순서).
    compute_load_sequence()가 지지·접근 의존성을 지켜 실제로 문에서
    부터 하나씩 놓을 수 있는 순서로 재배열해 준다 — 좌표는 그대로,
    순서만 바뀐다.
    """
    out = []
    for seq, p in enumerate(compute_load_sequence(ind.placed)):
        out.append({
            "seq": seq,
            "box_id": p.box.box_id,
            "type_id": p.box.type_id,
            "shipper": p.box.shipper,
            "destination": p.box.destination,
            "priority": p.box.priority,
            "x": round(p.x, 1), "y": round(p.y, 1), "z": round(p.z, 1),
            "dx": round(p.dl, 1), "dy": round(p.dw, 1), "dz": round(p.dh, 1),
            "weight": p.box.weight,
            "stackable": p.box.stackable,
            "fragile": p.box.fragile,     # [추가됨]
            "stack_level": p.level,       # [추가됨] 몇 단째(바닥=0)
        })
    return out


def _unplaced_by_shipper(ind) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for b in ind.unplaced:
        counts[b.shipper] = counts.get(b.shipper, 0) + 1
    return counts


def single_solution(placed, unplaced, cont: Container) -> Dict[str, Any]:
    """휴리스틱(빠른 대응)용 — 단일 해를 같은 형식으로 포장."""
    class _Tmp:
        pass
    t = _Tmp()
    t.placed = placed
    t.unplaced = unplaced
    return {
        "key": "fast",
        "label": "빠른 계획",
        "description": "현장 변동에 즉시 대응합니다",
        "duplicate": False,
        "metrics": _metrics(t, cont),
        "placements": _serialize(t, cont),
        "unplaced_by_shipper": _unplaced_by_shipper(t),
    }
