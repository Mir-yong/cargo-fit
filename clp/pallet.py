"""
팔레타이징 — 화물을 표준 팔레트에 묶는다.

실무 배경:
  LCL 화물은 규격이 제각각이라 그대로 실으면(바닥적재) 빈틈이 많고
  하역 시 앞 화물을 빼내는 재작업이 잦다. 표준 팔레트에 묶으면
  규격이 통일되고 지게차로 통째로 내릴 수 있어 재작업이 급감한다.
  대신 팔레트 내부 자투리 + 팔레트 자체 부피 때문에 한 컨테이너에
  실을 수 있는 화물 총량은 줄어든다.

규칙:
  - 하역지가 같은 화물끼리만 묶는다 (다르면 통째로 내릴 수 없다)
  - 팔레트 적재는 DBLF 디코더를 재사용한다 (팔레트 = 작은 컨테이너)
  - 컨테이너에 들어가는 개수만큼만 만든다
"""
from __future__ import annotations
from typing import List, Dict, Any, Tuple
from collections import defaultdict

from .model import Box, Container
from .dblf import decode

PALLET_TARE = 25.0        # 목재 T11 팔레트 자체 중량(kg)

# 실측 기준(BR6 inst1, 587×233×220 컨테이너):
#   T11-100 → 20개 적재, 화물 67% 수용   ← 가장 효율적
#   T12-100 → 16개 적재, 화물 43% 수용
#   T11-150 → 10개 적재, 화물 38% 수용
PALLET_SPECS = {
    "T11-100": {"label": "T11 표준 (110×110×100)", "L": 110.0, "W": 110.0, "H": 100.0,
                "note": "국내 표준 · 2단 적재"},
    "T12-100": {"label": "T12 국제 (120×100×100)", "L": 120.0, "W": 100.0, "H": 100.0,
                "note": "국제 표준 · 2단 적재"},
    "T11-150": {"label": "T11 고단적 (110×110×150)", "L": 110.0, "W": 110.0, "H": 150.0,
                "note": "높이 활용 · 1단 적재"},
}


def pallets_per_container(spec: dict, cont: Container) -> int:
    """컨테이너에 들어가는 팔레트 개수 (회전 2가지 중 유리한 쪽)."""
    best = 0
    for a, b in ((spec["L"], spec["W"]), (spec["W"], spec["L"])):
        n = int(cont.L // a) * int(cont.W // b) * int(cont.H // spec["H"])
        best = max(best, n)
    return best


def build_pallets(boxes: List[Box], spec_key: str,
                  cont: Container) -> Tuple[List[Box], List[Box], List[Dict]]:
    """
    화물을 팔레트로 묶는다.
    반환: (팔레트를 Box로 표현한 리스트, 이월 화물, 팔레트 상세정보)
    """
    spec = PALLET_SPECS[spec_key]
    cap = pallets_per_container(spec, cont)
    pbox = Container(L=spec["L"], W=spec["W"], H=spec["H"],
                     max_weight=1500.0, code="PALLET")

    by_dest: Dict[str, List[Box]] = defaultdict(list)
    for b in boxes:
        by_dest[b.destination].append(b)

    # 하역지별 부피 비율로 팔레트 수 배분
    vols = {d: sum(b.volume for b in g) for d, g in by_dest.items()}
    total = sum(vols.values()) or 1.0
    quota = {d: max(1, round(cap * vols[d] / total)) for d in by_dest}
    while sum(quota.values()) > cap and len(quota) > 1:
        quota[max(quota, key=quota.get)] -= 1
    while sum(quota.values()) < cap:
        quota[min(quota, key=quota.get)] += 1

    pallets: List[Box] = []
    details: List[Dict] = []
    leftover: List[Box] = []
    pid = 1

    for dest in sorted(by_dest):
        remain = sorted(by_dest[dest], key=lambda b: -b.volume)
        made = 0
        while remain and made < quota[dest]:
            placed, _ = decode(remain, [0] * len(remain), pbox, use_zones=False)
            if not placed:
                break
            ids = {p.box.box_id for p in placed}
            content = [p.box for p in placed]
            stack_h = max(p.z + p.dh for p in placed)
            h = max(60.0, round(stack_h))
            wt = round(sum(b.weight for b in content) + PALLET_TARE, 2)
            fill = sum(b.volume for b in content) / (spec["L"] * spec["W"] * h)

            pallets.append(Box(
                box_id=pid, type_id=1,
                l=spec["L"], w=spec["W"], h=h,
                weight=wt,
                destination=dest,
                priority=content[0].priority,
                shipper=content[0].shipper,
                stackable=1,
            ))
            details.append({
                "pallet_id": pid, "destination": dest,
                "content_boxes": len(content),
                "fill_pct": round(fill * 100, 1),
                "weight": wt, "height": h,
                "shippers": sorted({b.shipper for b in content}),
            })
            remain = [b for b in remain if b.box_id not in ids]
            made += 1
            pid += 1
        leftover.extend(remain)

    return pallets, leftover, details


def preview_all(boxes: List[Box], cont: Container) -> Dict[str, Any]:
    """
    팔레트 규격 3종을 모두 계산해 비교 정보를 준다.
    화물 수용량이 가장 많은 것을 추천한다.
    """
    out = []
    for key, spec in PALLET_SPECS.items():
        pallets, leftover, details = build_pallets(boxes, key, cont)
        packed = sum(d["content_boxes"] for d in details)
        avg_fill = (sum(d["fill_pct"] for d in details) / len(details)
                    if details else 0.0)
        out.append({
            "key": key,
            "label": spec["label"],
            "note": spec["note"],
            "pallet_count": len(pallets),
            "packed_boxes": packed,
            "leftover_boxes": len(leftover),
            "packed_pct": round(packed / len(boxes) * 100, 1) if boxes else 0,
            "avg_fill_pct": round(avg_fill, 1),
        })

    best = max(out, key=lambda o: o["packed_boxes"])
    return {"recommended": best["key"], "options": out}
