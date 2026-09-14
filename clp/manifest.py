"""
작업명세서(적재 명세서) PDF 생성.

현장에서 화물을 실을 때 참고하는 문서 — 상단에 요약 KPI, 아래에 적재
순서대로 정렬된 박스별 명세 표를 담는다.

HTML을 만들고 wkhtmltopdf로 PDF 변환한다. reportlab의 CID 폰트(예:
HYSMyeongJo-Medium)는 폰트 "이름"만 참조해서, 그 이름에 대응하는 실제
글꼴이 뷰어/변환기에 없으면 텍스트는 추출되지만 화면엔 안 보이는 빈
페이지가 나올 수 있다(직접 확인: pdf2image 렌더링 결과 완전 공백).
reportlab에 트루타입(glyf) 한글 폰트를 임베드하는 방법도 있지만, 시스템에
있는 한글 폰트(Noto Sans CJK 등)는 대부분 PostScript(CFF) 윤곽선이라
reportlab의 TTFont가 바로 못 읽는다. wkhtmltopdf(WebKit 기반)는 시스템에
설치된 폰트를 fontconfig로 그대로 찾아 쓰므로 이 문제를 피할 수 있다
(직접 렌더링해서 한글이 정상적으로 보이는 것까지 확인함).
"""
from __future__ import annotations
import html
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from typing import Any, Dict


def _esc(s: Any) -> str:
    return html.escape(str(s))


# [추가됨] Windows에서 "[WinError 2] 지정된 파일을 찾을 수 없습니다" 오류 —
# wkhtmltopdf가 PATH에 없어서 subprocess가 실행 파일 자체를 못 찾는 경우다.
# PATH만 보지 않고 Windows 기본 설치 경로도 함께 뒤져서 실제로 설치돼 있으면
# 자동으로 찾아 쓰고, 정말 설치가 안 돼 있을 때만 설치 방법을 알려준다.
_WKHTMLTOPDF_CANDIDATES = [
    r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe",
    r"C:\Program Files (x86)\wkhtmltopdf\bin\wkhtmltopdf.exe",
    "/usr/local/bin/wkhtmltopdf",
    "/usr/bin/wkhtmltopdf",
    "/opt/homebrew/bin/wkhtmltopdf",
]


def _find_wkhtmltopdf() -> str:
    env_path = os.environ.get("WKHTMLTOPDF_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    found = shutil.which("wkhtmltopdf")
    if found:
        return found
    for cand in _WKHTMLTOPDF_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    raise RuntimeError(
        "wkhtmltopdf가 설치되어 있지 않거나 PATH에 없습니다. "
        "https://wkhtmltopdf.org/downloads.html 에서 Windows용 설치 프로그램을 "
        "받아 설치한 뒤(설치 경로 기본값 그대로 두면 자동으로 찾습니다), "
        "server.py를 다시 실행해 주세요. 다른 경로에 설치했다면 환경변수 "
        "WKHTMLTOPDF_PATH에 wkhtmltopdf.exe의 전체 경로를 지정해도 됩니다."
    )


def render_manifest_html(result: Dict[str, Any], sol_index: int, job_id: str) -> str:
    """job의 result(dict)와 해 인덱스로부터 작업명세서 HTML을 만든다."""
    solutions = result.get("solutions") or []
    if not solutions:
        raise ValueError("표시할 해가 없습니다.")
    if not (0 <= sol_index < len(solutions)):
        sol_index = 0
    sol = solutions[sol_index]
    m = sol["metrics"]
    cont = result["container"]
    stow = result.get("stow") or {}
    mode_label = "정밀 탐색 (NSGA-II)" if result.get("mode") == "precise" else "현장 대응 (휴리스틱)"
    use_zones = result.get("use_zones", True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    placements = sorted(sol["placements"], key=lambda p: p["seq"])
    unplaced = sol.get("unplaced_by_shipper") or {}

    def _row(p) -> str:
        return (
            "<tr>"
            f"<td>{p['seq'] + 1}</td>"
            f"<td>{_esc(p['box_id'])}</td>"
            f"<td>{_esc(p['type_id'])}</td>"
            f"<td>{_esc(p['shipper'])}</td>"
            f"<td>{_esc(p['destination'])}</td>"
            f"<td>{p['priority']}</td>"
            f"<td>{p['x']:.0f}, {p['y']:.0f}, {p['z']:.0f}</td>"
            f"<td>{p['dx']:.0f} × {p['dy']:.0f} × {p['dz']:.0f}</td>"
            f"<td>{p['weight']:.0f}</td>"
            f"<td>{'가능' if p['stackable'] else '불가'}</td>"
            # [추가됨] fragile/stack_level 컬럼 — 없는(구버전) 데이터도
            # .get()으로 안전하게 기본값(0/'-') 처리.
            f"<td>{'취급주의' if p.get('fragile') else '-'}</td>"
            f"<td>{p.get('stack_level', 0) + 1}단</td>"
            "</tr>"
        )

    # [추가됨] wkhtmltopdf(WebKit 구버전 기반)는 <thead>가 페이지마다 자동으로
    # 반복되지 않는다(직접 렌더링해서 확인: 2페이지부터 헤더 행 없이 바로
    # 데이터 행이 시작됨) — 표가 페이지 경계를 넘으면 어느 열이 뭔지 알 수
    # 없게 된다. 그래서 표 전체를 하나로 만들지 않고, 한 페이지에 안전하게
    # 들어가는 분량(rows-per-chunk)씩 별도의 <table>로 쪼개 각자 <thead>를
    # 갖게 하고, 사이사이 강제 페이지 나눔을 넣는다. 1페이지는 위에 요약
    # 지표·미적재 안내가 이미 자리를 차지하므로 첫 덩어리를 더 작게 잡는다.
    FIRST_CHUNK, OTHER_CHUNK = 18, 42
    chunks = []
    i = 0
    if placements:
        chunks.append(placements[i:i + FIRST_CHUNK])
        i += FIRST_CHUNK
        while i < len(placements):
            chunks.append(placements[i:i + OTHER_CHUNK])
            i += OTHER_CHUNK

    box_table_blocks = []
    for ci, chunk in enumerate(chunks):
        page_break = ' style="page-break-before:always"' if ci > 0 else ""
        continued = " (계속)" if ci > 0 else ""
        box_table_blocks.append(f"""
        <div{page_break}>
          <div class="section-title">박스별 적재 명세{continued} (적재 순서대로 정렬, 총 {len(placements)}개)</div>
          <table class="box-table">
            <thead>
              <tr>
                <th>순번</th><th>Box ID</th><th>종류</th><th>화주</th><th>하역지</th>
                <th>우선순위</th><th>위치 x,y,z (cm)</th><th>크기 L×W×H (cm)</th><th>무게(kg)</th><th>적재 가능</th>
                <th>파손주의</th><th>적재 단수</th>
              </tr>
            </thead>
            <tbody>
              {''.join(_row(p) for p in chunk)}
            </tbody>
          </table>
        </div>""")
    box_table_html = "".join(box_table_blocks)

    unplaced_html = ""
    if unplaced:
        items = "".join(f"<li>{_esc(s)}: {n}개</li>" for s, n in unplaced.items())
        unplaced_html = f"""
        <div class="warn">
          <div class="warn-title">⚠ 미적재 화물 {m['unplaced']}개</div>
          <ul>{items}</ul>
          <div class="warn-note">다음 선적으로 이월이 필요합니다.</div>
        </div>"""

    pallet_html = ""
    if stow.get("mode") == "pallet":
        pallet_html = f"""
        <div class="section-title">팔레타이징</div>
        <table class="kv-table">
          <tr><th>규격</th><td>{_esc(stow.get('spec_label', ''))}</td></tr>
          <tr><th>팔레트 수</th><td>{stow.get('pallet_count', '')}개</td></tr>
          <tr><th>수용 화물</th><td>{stow.get('packed_boxes', '')} / {stow.get('original_boxes', '')}</td></tr>
          <tr><th>이월 화물</th><td>{stow.get('leftover_boxes', '')}개</td></tr>
        </table>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: "Noto Sans CJK KR", "Noto Sans KR", sans-serif; color:#1a1a1a;
          font-size:11px; margin:0; }}
  h1 {{ font-size:20px; margin:0 0 2px; }}
  .subtitle {{ color:#555; font-size:11px; margin-bottom:14px; }}
  .meta {{ font-size:11px; color:#333; margin-bottom:16px;
           border-top:1px solid #ccc; border-bottom:1px solid #ccc; }}
  .meta td {{ padding:8px 20px 8px 0; vertical-align:top; }}
  .meta .k {{ display:block; color:#777; font-size:10px; }}
  .meta .v {{ display:block; font-weight:700; font-size:13px; margin-top:2px; }}
  .section-title {{ font-size:13px; font-weight:700; margin:16px 0 6px;
                     border-left:4px solid #333; padding-left:8px; }}
  table {{ width:100%; border-collapse:collapse; }}
  .kpi-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-bottom:6px; }}
  .kpi {{ border:1px solid #ddd; border-radius:4px; padding:8px 10px; }}
  .kpi .l {{ font-size:10px; color:#777; }}
  .kpi .v {{ font-size:16px; font-weight:700; }}
  .kv-table th {{ text-align:left; color:#777; font-weight:400; width:110px; padding:3px 8px 3px 0; }}
  .kv-table td {{ padding:3px 0; font-weight:600; }}
  .box-table {{ font-size:9.5px; margin-top:6px; }}
  .box-table thead {{ display: table-header-group; }}
  .box-table th {{ background:#2b2f36; color:#fff; padding:5px 6px; text-align:left; font-weight:600; }}
  .box-table td {{ padding:4px 6px; border-bottom:1px solid #e5e5e5; }}
  .box-table tr:nth-child(even) td {{ background:#f7f7f8; }}
  .warn {{ margin-top:14px; border:1px solid #e0a33d; background:#fff8ec; border-radius:4px;
           padding:10px 12px; font-size:11px; }}
  .warn-title {{ font-weight:700; color:#9a6a12; margin-bottom:4px; }}
  .warn ul {{ margin:4px 0; padding-left:18px; }}
  .warn-note {{ color:#8a6a2a; margin-top:4px; }}
  .footer {{ margin-top:18px; font-size:9.5px; color:#999; text-align:right; }}
</style>
</head>
<body>
  <h1>작업명세서</h1>
  <div class="subtitle">{_esc(cont.get('code', ''))} 컨테이너 · {mode_label} · {_esc(sol.get('label', ''))} 해 · 생성 {now}</div>

  <table class="meta"><tr>
    <td><span class="k">작업 ID</span><span class="v">{_esc(job_id)}</span></td>
    <td><span class="k">컨테이너 규격</span><span class="v">{cont['L']:.0f} × {cont['W']:.0f} × {cont['H']:.0f} cm</span></td>
    <td><span class="k">최대 적재중량</span><span class="v">{cont['max_weight'] / 1000:.1f} t</span></td>
    <td><span class="k">구역 제약</span><span class="v">{'적용' if use_zones else '해제(최대 용적률 우선)'}</span></td>
  </tr></table>

  <div class="section-title">요약 지표</div>
  <div class="kpi-grid">
    <div class="kpi"><div class="l">용적률</div><div class="v">{m['utilization']}%</div></div>
    <div class="kpi"><div class="l">적재 / 전체</div><div class="v">{m['placed']} / {m['total']}</div></div>
    <div class="kpi"><div class="l">재작업(리핸들링)</div><div class="v">{m['relocation']}개 ({m['relocation_pct']}%)</div></div>
    <div class="kpi"><div class="l">무게중심 편차</div><div class="v">{m['cg_deviation']}%</div></div>
    <div class="kpi"><div class="l">평균 지지율</div><div class="v">{m['support_avg']}%</div></div>
    <div class="kpi"><div class="l">적재 중량</div><div class="v">{m['weight'] / 1000:.1f} t</div></div>
    <div class="kpi"><div class="l">ULO</div><div class="v">{m['ulo']}</div></div>
    <div class="kpi"><div class="l">계산 시간</div><div class="v">{result.get('elapsed', '')}초</div></div>
  </div>
  {pallet_html}
  {unplaced_html}
  {box_table_html}

  <div class="footer">STOW·PLAN 컨테이너 적재 최적화 시뮬레이터 · 작업 ID {_esc(job_id)}</div>
</body>
</html>"""


def build_manifest_pdf(html_content: str) -> bytes:
    """wkhtmltopdf로 HTML -> PDF 변환."""
    exe = _find_wkhtmltopdf()
    fd_html, html_path = tempfile.mkstemp(suffix=".html")
    pdf_path = html_path.replace(".html", ".pdf")
    try:
        with os.fdopen(fd_html, "w", encoding="utf-8") as f:
            f.write(html_content)
        proc = subprocess.run(
            [exe, "--quiet", "--encoding", "utf-8",
             "--page-size", "A4",
             "--margin-top", "15mm", "--margin-bottom", "15mm",
             "--margin-left", "12mm", "--margin-right", "12mm",
             html_path, pdf_path],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0 or not os.path.exists(pdf_path):
            raise RuntimeError(f"PDF 변환 실패: {proc.stderr.decode('utf-8', 'ignore')}")
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        for p in (html_path, pdf_path):
            if os.path.exists(p):
                os.remove(p)
