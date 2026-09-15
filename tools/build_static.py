"""
정적 데모 빌드 — web/ → docs/ (GitHub Pages 배포용)

Cargo Fit의 최적화 엔진은 파이썬 서버(FastAPI + NSGA-II)에서 돈다. GitHub
Pages 같은 정적 호스팅에는 서버가 없으므로, 미리 계산해 둔 결과
(web/demo-result.json)를 그대로 읽어 3D 뷰어·적재안 비교·적재 순서 재생을
보여주는 "데모 모드"로 빌드한다.

핵심은 window.CARGOFIT_STATIC 플래그 주입 하나다. index.html은 이 플래그를
보고 서버가 필요한 기능(새 CSV 계산 · 가중치 재계산 · 명세서 PDF)을 숨긴다.
없는 API를 fetch해서 감지하면 브라우저 콘솔에 404가 남기 때문에 플래그로 한다.

사용법:
    python3 tools/build_static.py
    (web/ 을 고친 뒤 다시 실행하면 docs/ 가 갱신된다 — 두 벌을 손으로
     맞추지 않아도 되도록 항상 web/ 이 원본이다)
"""
from __future__ import annotations

import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "web")
OUT = os.path.join(ROOT, "docs")

FLAG = "<script>window.CARGOFIT_STATIC=true;</script>\n"


def main() -> None:
    if not os.path.isfile(os.path.join(SRC, "demo-result.json")):
        raise SystemExit(
            "web/demo-result.json 이 없습니다.\n"
            "서버를 띄운 뒤 최적화를 한 번 돌려 결과를 저장해야 합니다."
        )

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    shutil.copytree(SRC, OUT)

    idx = os.path.join(OUT, "index.html")
    with open(idx, encoding="utf-8") as f:
        html = f.read()

    # 1) 정적 모드 플래그 주입
    if "CARGOFIT_STATIC" not in html.split("<script type=\"module\">")[0]:
        html = html.replace("</head>", FLAG + "</head>", 1)

    # 2) 정적 파일 경로를 상대경로로. 로컬 서버는 web/ 을 /static 으로
    #    마운트하지만, GitHub Pages는 하위 경로(/<repo>/)로 서빙하기 때문에
    #    "/static/..." 는 도메인 루트로 잘못 해석돼 404가 난다.
    n = html.count("/static/")
    html = html.replace('"/static/', '"').replace("'/static/", "'")
    print(f"   정적 경로 {n}곳을 상대경로로 변환")

    with open(idx, "w", encoding="utf-8") as f:
        f.write(html)

    # Pages가 _ 로 시작하는 경로를 Jekyll 처리하지 않도록
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    total = sum(
        os.path.getsize(os.path.join(dp, fn))
        for dp, _, fns in os.walk(OUT)
        for fn in fns
    )
    print(f"✅ docs/ 생성 완료 — {total/1024:.0f} KB")
    for dp, _, fns in os.walk(OUT):
        for fn in sorted(fns):
            rel = os.path.relpath(os.path.join(dp, fn), OUT)
            print(f"   {rel}")


if __name__ == "__main__":
    main()
