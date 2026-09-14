"""
실행 스크립트.

사용법:
  python run.py --csv modified_BR6_full.csv --instance 1
  python run.py --csv data.csv --instance 1 --gen 200 --pop 50 --out result.json

결과: 최적 적재 계획을 콘솔 요약 + JSON(3D 시뮬레이터용 좌표) 저장.
"""
import argparse
import json
import sys
import time
from collections import defaultdict

from clp.model import Container, load_instance, list_instances
from clp.nsga2 import run_nsga2
from clp.objectives import utilization, ulo_scan, cg_deviation, count_blocking_boxes


def pick_best(front, cont):
    """
    Pareto front에서 대표 해 1개 선택.
    전량 적재 우선 → 용적률 최대 순.
    (다목적이라 '유일한 최적'은 없지만 데모용 대표 선택)
    """
    # 미적재 적은 것 우선, 그 다음 용적률
    return min(front, key=lambda ind: (len(ind.unplaced), ind.obj[0]))


def to_json(ind, cont, boxes):
    """3D 시뮬레이터용 JSON 직렬화."""
    placements = []
    for p in ind.placed:
        placements.append({
            "box_id": p.box.box_id,
            "type_id": p.box.type_id,
            "destination": p.box.destination,
            "priority": p.box.priority,
            "shipper": p.box.shipper,
            "x": round(p.x, 2), "y": round(p.y, 2), "z": round(p.z, 2),
            "dx": round(p.dl, 2), "dy": round(p.dw, 2), "dz": round(p.dh, 2),
            "orientation": p.orientation,
            "weight": p.box.weight,
        })
    # 미적재 화물 (화주별 집계)
    unplaced_by_shipper = defaultdict(int)
    for b in ind.unplaced:
        unplaced_by_shipper[b.shipper] += 1

    # [수정됨] 4번 항목: count_ulo() + count_blocking_boxes()를 따로 부르면
    # 같은 순회를 두 번 하게 되어 ulo_scan() 한 번으로 합쳤다.
    ulo, blockers = ulo_scan(ind.placed)
    return {
        "container": {"L": cont.L, "W": cont.W, "H": cont.H},
        "kpi": {
            "utilization_pct": round(utilization(ind.placed, cont) * 100, 2),
            "ulo": ulo,
            "relocation_boxes": len(blockers),
            "cg_deviation_pct": round(cg_deviation(ind.placed, cont) * 100, 2),
            "placed": len(ind.placed),
            "unplaced": len(ind.unplaced),
            "total_boxes": len(boxes),
        },
        "unplaced_by_shipper": dict(unplaced_by_shipper),
        "placements": placements,
    }


def main():
    ap = argparse.ArgumentParser(description="CLP NSGA-II + DBLF 솔버")
    ap.add_argument("--csv", required=True, help="입력 CSV 경로")
    ap.add_argument("--instance", type=int, default=1, help="instance_id")
    ap.add_argument("--pop", type=int, default=50, help="개체군 크기")
    ap.add_argument("--gen", type=int, default=200, help="세대 수")
    ap.add_argument("--cp", type=float, default=0.8, help="crossover rate")
    ap.add_argument("--pm1", type=float, default=0.6, help="2-OPT mutation")
    ap.add_argument("--pm2", type=float, default=0.3, help="회전 리셋 mutation")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="result.json", help="출력 JSON 경로")
    ap.add_argument("--list", action="store_true", help="instance 목록만 출력")
    ap.add_argument("--no-zones", action="store_true",
                    help="하역지 구역 제약 없이 순수 용적률만 탐색 "
                         "(리핸들링 증가 가능, 최대 용적률 옵션 실험용)")
    args = ap.parse_args()

    if args.list:
        ids = list_instances(args.csv)
        print(f"사용 가능 instance_id: {ids[:20]}{' ...' if len(ids) > 20 else ''}")
        print(f"총 {len(ids)}개")
        return

    boxes = load_instance(args.csv, args.instance)
    cont = Container()
    use_zones = not args.no_zones
    print(f"[로드] instance {args.instance}: {len(boxes)}박스, "
          f"우선순위 {sorted(set(b.priority for b in boxes))}, "
          f"화주 {sorted(set(b.shipper for b in boxes))}")
    print(f"[설정] pop={args.pop} gen={args.gen} cp={args.cp} "
          f"pm1={args.pm1} pm2={args.pm2} zones={'ON' if use_zones else 'OFF'}")
    print("[실행] NSGA-II 시작...\n")

    t0 = time.time()

    def progress(gen, util, ulo, cg):
        if gen % 10 == 0 or gen == 1:
            bar = "#" * (gen * 30 // args.gen)
            print(f"\r  gen {gen:3d}/{args.gen} [{bar:<30}] "
                  f"용적률 {util*100:5.1f}%  ULO {int(ulo):3d}  "
                  f"CG {cg*100:4.1f}%", end="", flush=True)

    front = run_nsga2(boxes, cont,
                      pop_size=args.pop, generations=args.gen,
                      cp=args.cp, pm1=args.pm1, pm2=args.pm2,
                      seed=args.seed, progress=progress,
                      use_zones=use_zones)
    dt = time.time() - t0
    print(f"\n\n[완료] {dt:.1f}초, Pareto front {len(front)}개 해\n")

    best = pick_best(front, cont)
    result = to_json(best, cont, boxes)

    kpi = result["kpi"]
    print("=" * 52)
    print("  대표 해 (전량적재 우선 → 용적률 최대)")
    print("=" * 52)
    print(f"  용적률      : {kpi['utilization_pct']:.2f} %")
    print(f"  ULO(쌍)     : {kpi['ulo']} 개  (선행쌍 기준, 중복 카운트됨)")
    print(f"  실제 리핸들링: {kpi['relocation_boxes']} 개  (옮겨야 할 박스 수, 실무 지표)")
    print(f"  CG 편차     : {kpi['cg_deviation_pct']:.2f} %")
    print(f"  적재/전체   : {kpi['placed']} / {kpi['total_boxes']}")
    if kpi["unplaced"] > 0:
        print(f"  미적재      : {kpi['unplaced']} 개")
        print(f"    화주별    : {result['unplaced_by_shipper']}")
    print("=" * 52)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[저장] {args.out}  (3D 시뮬레이터용 좌표 JSON)")

    # Pareto front 전체 요약
    print(f"\n[Pareto front {len(front)}개]")
    seen = set()
    for ind in sorted(front, key=lambda x: x.obj[0])[:10]:
        key = (round(-ind.obj[0]*100, 1), int(ind.obj[1]), round(ind.obj[2]*100, 1))
        if key in seen:
            continue
        seen.add(key)
        rb = count_blocking_boxes(ind.placed)
        print(f"  용적률 {key[0]:5.1f}%  ULO {key[1]:4d}  리핸들링 {rb:3d}개  "
              f"CG {key[2]:4.1f}%  (미적재 {len(ind.unplaced)})")


if __name__ == "__main__":
    main()