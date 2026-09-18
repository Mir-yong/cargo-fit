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
import sys
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
    if sys.platform == "darwin":
        raise RuntimeError(
            "wkhtmltopdf를 찾을 수 없습니다. macOS에서는 원 프로젝트가 아카이브되어 "
            "Homebrew 설치(brew install wkhtmltopdf)가 막혀 있습니다 — "
            "브라우저 인쇄(PDF로 저장)로 대신 받으세요. 직접 설치한 경우에는 "
            "환경변수 WKHTMLTOPDF_PATH에 실행 파일 전체 경로를 지정하면 됩니다."
        )
    raise RuntimeError(
        "wkhtmltopdf가 설치되어 있지 않거나 PATH에 없습니다. "
        "https://wkhtmltopdf.org/downloads.html 에서 설치 프로그램을 "
        "받아 설치한 뒤(설치 경로 기본값 그대로 두면 자동으로 찾습니다), "
        "server.py를 다시 실행해 주세요. 다른 경로에 설치했다면 환경변수 "
        "WKHTMLTOPDF_PATH에 실행 파일의 전체 경로를 지정해도 됩니다."
    )


def wkhtmltopdf_available() -> bool:
    """[추가됨] 예외를 던지지 않고 존재 여부만 본다 — 서버가 PDF 변환과
    브라우저 인쇄 폴백 중 무엇을 줄지 고르는 데 쓴다."""
    try:
        _find_wkhtmltopdf()
        return True
    except RuntimeError:
        return False


# [추가됨] wkhtmltopdf가 없을 때의 폴백 — 같은 명세서 HTML을 "인쇄용"으로
# 손봐서 브라우저에 띄우고, 사용자가 인쇄 창에서 'PDF로 저장'을 고르게 한다.
# 설치가 필요 없고 Mac/Windows/리눅스 어디서나 같은 결과가 나온다.
# 원본 템플릿은 body{margin:0} + wkhtmltopdf가 붙여주던 여백을 전제로 하므로,
# @page로 그 여백(15mm/12mm)을 그대로 재현하고 화면에서만 패딩을 준다.
_PRINT_HEAD = """<style>
  @page { size: A4; margin: 15mm 12mm; }
  @media screen {
    html { background:#EEF1F6; }
    body { max-width:190mm; margin:0 auto; padding:86px 18px 48px; background:#fff; }
  }
  @media print { .pf-bar { display:none !important; } }
  .pf-bar {
    position:fixed; top:0; left:0; right:0; z-index:9999;
    background:#4B5BD6; color:#fff; padding:12px 18px;
    font:600 13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    display:flex; align-items:center; gap:14px;
  }
  .pf-bar button {
    border:0; border-radius:7px; padding:8px 15px; cursor:pointer;
    font:600 13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:#fff; color:#4B5BD6;
  }
  .pf-bar span { font-weight:500; opacity:.95; }
</style>"""

_PRINT_BODY = """<div class="pf-bar">
  <button onclick="window.print()">PDF로 저장</button>
  <span>인쇄 창에서 대상(또는 좌측 하단 PDF ▾)을 &quot;PDF로 저장&quot;으로 선택하세요.</span>
</div>
<script>
  // 자동으로 인쇄 창을 띄우되, 실패해도 위 버튼으로 다시 열 수 있다.
  window.addEventListener('load', function(){
    setTimeout(function(){ try { window.print(); } catch (e) {} }, 400);
  });
</script>"""


def make_printable_html(html_content: str) -> str:
    """명세서 HTML에 인쇄용 스타일과 안내 바를 끼워 넣는다."""
    out = html_content
    if "</head>" in out:
        out = out.replace("</head>", _PRINT_HEAD + "\n</head>", 1)
    else:
        out = _PRINT_HEAD + out
    if "<body>" in out:
        out = out.replace("<body>", "<body>\n" + _PRINT_BODY, 1)
    else:
        out = out + _PRINT_BODY
    return out


def render_manifest_html(result: Dict[str, Any], sol_index: int, job_id: str) -> str:
    """job의 result(dict)와 해 인덱스로부터 작업명세서 HTML을 만든다.

    [전면 재설계] 화면 UI와 같은 원칙으로 정리했다 — 카드 남발 대신
    큰 숫자 하나 + 옅은 회색 그룹박스 + 헤어라인 리스트.

    인쇄물이라 화면과 다르게 잡은 것 두 가지:
      · 흑백 출력 대비 — 창고 프린터는 대부분 흑백이다. 색은 보조일 뿐
        정보를 혼자 나르지 않게 했다(하역지는 색 + 글자를 함께 쓴다).
      · "배경 그래픽" 옵션을 꺼도 구조가 남아야 한다 — 회색 면에만
        기대지 않고 테두리·헤어라인을 함께 둬서, 배경이 빠져도 표와
        그룹박스의 경계가 그대로 보인다.
      · 기존의 검정 꽉 찬 표 헤더는 토너를 많이 먹고 무거워서, 옅은 면 +
        아래 굵은 선으로 바꿨다.
    """
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
    # 서버가 내려주는 label("최대 용적률" 등)은 화면에서 ALT_STYLE로 새 용어로
    # 덮어쓰고 있다. 명세서에도 같은 용어가 나와야 화면과 문서가 어긋나지 않는다.
    SOL_LABEL = {"max_fill": "최대 적재", "balanced": "균형형",
                 "min_reloc": "하역 최적화", "custom": "직접 조정", "fast": "빠른 계획"}
    sol_label = SOL_LABEL.get(sol.get("key"), sol.get("label", ""))
    use_zones = result.get("use_zones", True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    placements = sorted(sol["placements"], key=lambda p: p["seq"])
    unplaced = sol.get("unplaced_by_shipper") or {}

    # 3D 화면의 하역지 색과 같은 값. 흑백으로 뽑아도 글자가 남으므로
    # 색이 없어도 정보는 그대로다.
    DEST_COLOR = {"A": "#E8512F", "B": "#1F6FEB", "C": "#F2A81D",
                  "D": "#16A34A", "E": "#8B5CF6", "F": "#0E9488", "G": "#DB2777"}

    def _dest(d) -> str:
        c = DEST_COLOR.get(str(d), "#4B5563")
        return f'<span class="dchip" style="color:{c};border-color:{c}">{_esc(d)}</span>'

    def _row(p) -> str:
        return (
            "<tr>"
            f"<td class='n dim'>{p['seq'] + 1}</td>"
            f"<td class='mono'>{_esc(p['box_id'])}</td>"
            f"<td class='n dim'>{_esc(p['type_id'])}</td>"
            f"<td>{_esc(p['shipper'])}</td>"
            f"<td>{_dest(p['destination'])}</td>"
            f"<td class='n dim'>{p['priority']}</td>"
            f"<td class='mono dim'>{p['x']:.0f}, {p['y']:.0f}, {p['z']:.0f}</td>"
            f"<td class='mono'>{p['dx']:.0f} × {p['dy']:.0f} × {p['dz']:.0f}</td>"
            f"<td class='n mono'>{p['weight']:.0f}</td>"
            f"<td class='c'>{'가능' if p['stackable'] else '<b>불가</b>'}</td>"
            f"<td class='c'>{'<b>주의</b>' if p.get('fragile') else '<span class=dim>-</span>'}</td>"
            f"<td class='n dim'>{p.get('stack_level', 0) + 1}</td>"
            "</tr>"
        )

    # [유지] wkhtmltopdf(WebKit 구버전)는 <thead>를 페이지마다 반복하지 않아서
    # 표를 덩어리로 쪼개 각자 헤더를 갖게 한다. 1페이지는 위에 요약이 있어 더 작게.
    FIRST_CHUNK, OTHER_CHUNK = 20, 44
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
        continued = ' <span class="sec-sub">(계속)</span>' if ci > 0 else ""
        box_table_blocks.append(f"""
        <div{page_break}>
          <div class="sec">박스별 적재 명세{continued}
            <span class="sec-sub">적재 순서 · 총 {len(placements)}개</span></div>
          <table class="bt">
            <thead>
              <tr>
                <th class="n">순번</th><th>Box ID</th><th class="n">종류</th><th>화주</th>
                <th>하역지</th><th class="n">우선</th><th>위치 x, y, z</th>
                <th>크기 L × W × H</th><th class="n">무게</th>
                <th class="c">적재</th><th class="c">파손</th><th class="n">단</th>
              </tr>
            </thead>
            <tbody>
              {''.join(_row(p) for p in chunk)}
            </tbody>
          </table>
        </div>""")
    box_table_html = "".join(box_table_blocks)

    # 확인 필요 — 채워진 경고 박스 대신 조용한 헤어라인 리스트
    unplaced_html = ""
    if unplaced:
        detail = " · ".join(f"{_esc(s)} {n}개" for s, n in unplaced.items())
        unplaced_html = f"""
        <div class="sec">확인 필요</div>
        <div class="chk">
          <div class="chk-row">
            <div><div class="chk-t">미적재 화물</div>
                 <div class="chk-d">{detail} — 다음 선적으로 이월이 필요합니다</div></div>
            <div class="chk-v">{m['unplaced']}건</div>
          </div>
          <div class="chk-row">
            <div><div class="chk-t">재배치 필요</div>
                 <div class="chk-d">하역 시 앞 화물을 옮겨야 하는 건수 · 하역 순서 확인</div></div>
            <div class="chk-v">{m['relocation']}건</div>
          </div>
        </div>"""

    pallet_html = ""
    if stow.get("mode") == "pallet":
        pallet_html = f"""
        <div class="sec">팔레타이징</div>
        <div class="chk">
          <div class="chk-row"><div class="chk-t">규격</div>
            <div class="chk-v">{_esc(stow.get('spec_label', ''))}</div></div>
          <div class="chk-row"><div class="chk-t">팔레트 수</div>
            <div class="chk-v">{stow.get('pallet_count', '')}개</div></div>
          <div class="chk-row"><div class="chk-t">수용 화물</div>
            <div class="chk-v">{stow.get('packed_boxes', '')} / {stow.get('original_boxes', '')}</div></div>
          <div class="chk-row"><div class="chk-t">이월 화물</div>
            <div class="chk-v">{stow.get('leftover_boxes', '')}개</div></div>
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  * {{ box-sizing:border-box; }}
  html {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
  body {{ font-family:"Noto Sans CJK KR","Noto Sans KR","Apple SD Gothic Neo",sans-serif;
          color:#17191F; font-size:10.5px; margin:0; line-height:1.45; }}
  .mono {{ font-family:"IBM Plex Mono","SFMono-Regular",Menlo,monospace; }}
  .dim {{ color:#6B7280; }}

  /* ── 문서 머리 ── */
  .top {{ display:flex; justify-content:space-between; align-items:flex-end;
          border-bottom:1.5px solid #17191F; padding-bottom:9px; }}
  h1 {{ font-size:19px; margin:0; letter-spacing:-.01em; }}
  .top-sub {{ color:#6B7280; font-size:10.5px; margin-top:3px; }}
  .top-right {{ text-align:right; color:#6B7280; font-size:10px; line-height:1.6; }}
  .top-right b {{ color:#17191F; }}

  /* ── 히어로: 가장 중요한 숫자 하나만 크게 ── */
  .hero {{ margin:18px 0 14px; }}
  .hero-l {{ font-size:10.5px; color:#6B7280; }}
  .hero-v {{ font-size:38px; font-weight:700; letter-spacing:-.03em; line-height:1.05;
             margin:1px 0 3px; }}
  .hero-v small {{ font-size:19px; font-weight:700; margin-left:1px; }}
  .hero-s {{ font-size:11px; color:#6B7280; }}
  .hero-s b {{ color:#17191F; }}

  /* ── 보조 지표: 카드 8장 대신 옅은 회색 그룹박스 하나 ──
     배경 그래픽을 꺼도 경계가 남도록 테두리를 함께 둔다. */
  .grp {{ border:1px solid #E5E7EB; background:#F7F8FA; border-radius:7px;
          width:100%; border-collapse:collapse; margin-bottom:6px; }}
  .grp td {{ padding:9px 12px; border:0; border-left:1px solid #E5E7EB;
             vertical-align:top; width:25%; }}
  .grp tr + tr td {{ border-top:1px solid #E5E7EB; }}
  .grp td:first-child {{ border-left:0; }}
  .grp .l {{ font-size:9.5px; color:#6B7280; }}
  .grp .v {{ font-size:14px; font-weight:700; margin-top:1px; letter-spacing:-.01em; }}
  .grp .v small {{ font-size:10px; font-weight:600; color:#6B7280; margin-left:1px; }}

  /* ── 섹션 제목: 왼쪽 막대 없이 굵은 글자만 ── */
  .sec {{ font-size:13px; font-weight:700; margin:18px 0 7px; letter-spacing:-.01em; }}
  .sec-sub {{ font-size:10px; font-weight:500; color:#6B7280; margin-left:7px; }}

  /* ── 확인 필요: 채운 경고 박스 대신 헤어라인 행 ── */
  .chk {{ border-top:1px solid #E5E7EB; }}
  .chk-row {{ display:flex; justify-content:space-between; align-items:baseline;
              gap:16px; padding:8px 2px; border-bottom:1px solid #E5E7EB; }}
  .chk-t {{ font-weight:600; font-size:11px; }}
  .chk-d {{ font-size:9.5px; color:#6B7280; margin-top:1px; }}
  .chk-v {{ font-weight:700; font-size:13px; white-space:nowrap; }}

  /* ── 명세 표 ── */
  table {{ width:100%; border-collapse:collapse; }}
  .bt {{ font-size:9px; }}
  .bt thead {{ display:table-header-group; }}
  .bt th {{ background:#F0F1F4; color:#374151; padding:6px 7px; text-align:left;
            font-weight:700; font-size:8.5px; letter-spacing:.02em; white-space:nowrap;
            border-bottom:1.2px solid #9CA3AF; }}
  .bt td {{ padding:5px 7px; border-bottom:1px solid #EDEFF3; }}
  .bt tr {{ page-break-inside:avoid; }}
  .bt .n {{ text-align:right; }}
  .bt .c {{ text-align:center; }}
  .bt b {{ font-weight:700; }}
  /* 하역지 — 색 + 글자. 흑백으로 뽑아도 글자와 테두리가 남는다. */
  .dchip {{ display:inline-block; min-width:15px; padding:0 4px; border:1.2px solid;
            border-radius:3px; font-weight:700; font-size:9px; text-align:center; }}

  .footer {{ margin-top:16px; padding-top:7px; border-top:1px solid #E5E7EB;
             font-size:9px; color:#9CA3AF; display:flex; justify-content:space-between; }}
</style>
</head>
<body>
  <div class="top">
    <div>
      <h1>작업명세서</h1>
      <div class="top-sub">{_esc(cont.get('code',''))} 컨테이너 · {_esc(sol_label)} · {mode_label}</div>
    </div>
    <div class="top-right">
      작업 ID <b>{_esc(job_id)}</b><br>생성 <b>{now}</b>
    </div>
  </div>

  <div class="hero">
    <div class="hero-l">공간 활용률</div>
    <div class="hero-v">{m['utilization']}<small>%</small></div>
    <div class="hero-s"><b>{m['placed']} / {m['total']}개</b> 적재 · 재배치 <b>{m['relocation']}건</b> ({m['relocation_pct']}%)</div>
  </div>

  <table class="grp">
    <tr>
      <td><div class="l">무게중심 편차</div><div class="v">{m['cg_deviation']}<small>%</small></div></td>
      <td><div class="l">평균 지지율</div><div class="v">{m['support_avg']}<small>%</small></div></td>
      <td><div class="l">적재중량</div><div class="v">{m['weight'] / 1000:.1f}<small>t</small></div></td>
      <td><div class="l">LIFO 선행쌍 (ULO)</div><div class="v">{m['ulo']}</div></td>
    </tr>
    <tr>
      <td><div class="l">컨테이너 규격</div><div class="v">{cont['L']:.0f} × {cont['W']:.0f} × {cont['H']:.0f}<small>cm</small></div></td>
      <td><div class="l">최대 적재중량</div><div class="v">{cont['max_weight'] / 1000:.1f}<small>t</small></div></td>
      <td><div class="l">하역지 구역 제약</div><div class="v">{'적용' if use_zones else '해제'}</div></td>
      <td><div class="l">계산 시간</div><div class="v">{result.get('elapsed','')}<small>초</small></div></td>
    </tr>
  </table>
  {pallet_html}
  {unplaced_html}
  {box_table_html}

  <div class="footer">
    <span>Cargo Fit · NSGA-II 컨테이너 적재 최적화</span>
    <span>작업 ID {_esc(job_id)}</span>
  </div>
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
