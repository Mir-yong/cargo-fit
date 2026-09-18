"""
정적 데모용 결과 파일 생성 — web/demo-result.json

GitHub Pages에는 파이썬 서버가 없어서 최적화를 돌릴 수 없다. 그래서 미리
한 번 계산한 결과를 번들해 두는데, 기존에는 대표 해 3개만 담아서 가중치
슬라이더와 파레토 점 클릭이 정적 모드에서 비활성화돼 있었다.

이 스크립트는 슬라이더로 도달 가능한 해를 전부(보통 5~15개) 좌표까지
함께 내보내, 정적 배포본에서도 슬라이더와 그래프 점 클릭이 동작하게 한다.
(선택 로직 자체는 프론트가 서버와 동일하게 계산하므로 서버가 필요 없다)

사용법:
    python3 tools/export_demo.py
    python3 tools/export_demo.py --csv modified_BR6_full.csv --pop 30 --gen 30

끝나면 tools/build_static.py 를 돌려 docs/ 를 갱신한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from clp.model import load_instance, recommend_container, CONTAINER_SPECS  # noqa: E402
from clp.nsga2 import run_nsga2  # noqa: E402
from clp.solutions import pick_three, sweep_reachable  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="modified_BR6_full.csv")
    ap.add_argument("--instance", type=int, default=1)
    ap.add_argument("--pop", type=int, default=30)
    ap.add_argument("--gen", type=int, default=30)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--container", default=None, help="20ft / 40ft / 40ftHC (기본: 자동 추천)")
    ap.add_argument("--out", default=os.path.join(ROOT, "web", "demo-result.json"))
    a = ap.parse_args()

    csv_path = a.csv if os.path.isabs(a.csv) else os.path.join(ROOT, a.csv)
    boxes = load_instance(csv_path, a.instance)
    if a.container:
        cont = CONTAINER_SPECS[a.container]
    else:
        rec = recommend_container(boxes)
        cont = CONTAINER_SPECS[rec["recommended"] if isinstance(rec, dict) else rec]

    print(f"화물 {len(boxes)}개 · 컨테이너 {cont.code} · pop={a.pop} gen={a.gen}")
    t0 = time.time()

    def progress(g, util, ulo, cg):
        print(f"\r  {g}/{a.gen}세대  용적률 {util*100:.1f}%", end="", flush=True)

    front = run_nsga2(boxes, cont, pop_size=a.pop, generations=a.gen,
                      seed=a.seed, progress=progress, use_zones=True)
    print()

    result = pick_three(front, cont)
    result["container"] = {"code": cont.code, "L": cont.L, "W": cont.W, "H": cont.H,
                           "max_weight": cont.max_weight}
    result["mode"] = "precise"
    result["use_zones"] = True
    result["elapsed"] = round(time.time() - t0, 1)

    # [핵심] 슬라이더로 도달 가능한 해 전체를 좌표까지 번들
    result["slider_solutions"] = sweep_reachable(front, cont)

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    kb = os.path.getsize(a.out) / 1024
    print(f"✅ {os.path.relpath(a.out, ROOT)} 생성 — {kb:.0f} KB")
    print(f"   대표 해 {len(result['solutions'])}개 · "
          f"슬라이더 도달 가능 해 {len(result['slider_solutions'])}개 · "
          f"파레토 프론트 {result['front_size']}개 · {result['elapsed']}초")
    print("   다음: python3 tools/build_static.py")


if __name__ == "__main__":
    main()
