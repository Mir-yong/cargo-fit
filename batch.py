"""
배치 실험 스크립트.

여러 CSV(BR1/2/6/7)와 여러 instance를 한 번에 돌려 평균 성능을 낸다.
단일 실행은 운(seed)에 좌우되므로, 여러 인스턴스 평균이 진짜 성능이다.

사용법:
  # BR6의 instance 1~5를 각각 돌려 평균
  python batch.py --csv modified_BR6_full.csv --instances 1 2 3 4 5 --pop 20 --gen 30

  # 여러 데이터셋 비교
  python batch.py --csv modified_BR1_full.csv modified_BR6_full.csv --instances 1 2 3

결과: 데이터셋별 평균 용적률/ULO/리핸들링/미적재 표
"""
import argparse
import statistics
import time

from clp.model import Container, load_instance
from clp.nsga2 import run_nsga2
from clp.objectives import utilization, ulo_scan, cg_deviation


def run_one(csv_path, instance, cont, args):
    """한 인스턴스를 돌려 최상위 용적률 해의 지표를 반환."""
    boxes = load_instance(csv_path, instance)
    front = run_nsga2(boxes, cont,
                      pop_size=args.pop, generations=args.gen,
                      cp=args.cp, pm1=args.pm1, pm2=args.pm2,
                      seed=args.seed, progress=None,
                      use_zones=not args.no_zones)
    # 용적률 최대 해 기준으로 평가
    best = min(front, key=lambda i: i.obj[0])
    # [수정됨] 4번 항목: count_ulo() + count_blocking_boxes()를 따로 부르면
    # 같은 순회를 두 번 하게 되어 ulo_scan() 한 번으로 합쳤다.
    ulo, blockers = ulo_scan(best.placed)
    return {
        "util": utilization(best.placed, cont) * 100,
        "ulo": ulo,
        "reloc": len(blockers),
        "cg": cg_deviation(best.placed, cont) * 100,
        "unplaced": len(best.unplaced),
        "total": len(boxes),
        "front": len(front),
    }


def main():
    ap = argparse.ArgumentParser(description="CLP 배치 실험")
    ap.add_argument("--csv", nargs="+", required=True, help="CSV 경로들")
    ap.add_argument("--instances", nargs="+", type=int, default=[1, 2, 3],
                    help="돌릴 instance_id 목록")
    ap.add_argument("--pop", type=int, default=20)
    ap.add_argument("--gen", type=int, default=30)
    ap.add_argument("--cp", type=float, default=0.8)
    ap.add_argument("--pm1", type=float, default=0.6)
    ap.add_argument("--pm2", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-zones", action="store_true")
    args = ap.parse_args()

    cont = Container()
    print(f"[설정] pop={args.pop} gen={args.gen} seed={args.seed} "
          f"zones={'OFF' if args.no_zones else 'ON'}")
    print(f"[대상] {len(args.csv)}개 CSV x {len(args.instances)}개 instance\n")

    summary = {}
    for csv_path in args.csv:
        results = []
        for inst in args.instances:
            t0 = time.time()
            try:
                r = run_one(csv_path, inst, cont, args)
            except Exception as e:
                print(f"  {csv_path} inst {inst}: 실패 ({e})")
                continue
            dt = time.time() - t0
            results.append(r)
            print(f"  {csv_path} inst {inst:2d}: "
                  f"용적률 {r['util']:5.1f}%  ULO {r['ulo']:4d}  "
                  f"리핸들링 {r['reloc']:3d}  CG {r['cg']:4.1f}%  "
                  f"미적재 {r['unplaced']:3d}/{r['total']:3d}  ({dt:.0f}초)")
        if results:
            summary[csv_path] = results
        print()

    # 요약표
    print("=" * 72)
    print(f"{'데이터셋':<26}{'용적률':>9}{'ULO':>8}{'리핸들링':>10}{'미적재':>9}")
    print("=" * 72)
    for csv_path, rs in summary.items():
        name = csv_path.split("/")[-1].split("\\")[-1]
        u = statistics.mean(r["util"] for r in rs)
        ul = statistics.mean(r["ulo"] for r in rs)
        rl = statistics.mean(r["reloc"] for r in rs)
        up = statistics.mean(r["unplaced"] for r in rs)
        sd = statistics.stdev([r["util"] for r in rs]) if len(rs) > 1 else 0.0
        print(f"{name:<26}{u:>7.1f}%{ul:>8.0f}{rl:>10.1f}{up:>9.1f}"
              f"   (±{sd:.1f})")
    print("=" * 72)
    print("* 용적률 최대 해 기준. ±는 인스턴스 간 표준편차.")


if __name__ == "__main__":
    main()
