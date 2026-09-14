"""
컨테이너 적재 최적화 API 서버.

실행:
  python server.py
  → http://localhost:8000 접속

흐름:
  1. POST /api/upload    CSV 업로드 → 화물 요약 + 컨테이너 추천
  2. POST /api/optimize  계산 시작 → job_id 즉시 반환 (백그라운드 실행)
  3. GET  /api/status    진행률 폴링 (세대별 지표 실시간)
  4. GET  /api/result    완료 시 3개 해 + 좌표 반환
  5. GET  /api/manifest  완료된 작업의 선택한 해를 작업명세서 PDF로 반환

NSGA-II는 1~2분이 걸리므로 요청을 붙잡지 않고 백그라운드로 돌린다.
"""
from __future__ import annotations

import io
import os
import csv
import time
import uuid
import threading
import dataclasses
from typing import Dict, Any, List
from urllib.parse import quote

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from clp.model import Box, Container, CONTAINER_SPECS, recommend_container
from clp.nsga2 import run_nsga2
from clp.heuristic import solve as heuristic_solve
from clp.solutions import pick_three, single_solution, pick_by_weight, pick_by_point
from clp.pallet import preview_all, build_pallets, PALLET_SPECS
from clp.manifest import render_manifest_html, build_manifest_pdf

app = FastAPI(title="LCL 컨테이너 적재 최적화")

# 업로드된 화물과 진행 중인 작업을 메모리에 보관 (로컬 데모용)
CARGO: Dict[str, List[Box]] = {}
JOBS: Dict[str, Dict[str, Any]] = {}


# ---------- CSV 파싱 ----------
def parse_csv(content: bytes) -> List[Box]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    boxes: List[Box] = []

    first_instance = None
    for row in reader:
        # instance_id가 있으면 첫 번째 것만 사용
        inst = row.get("instance_id")
        if inst is not None:
            if first_instance is None:
                first_instance = inst
            elif inst != first_instance:
                continue
        try:
            boxes.append(Box(
                box_id=int(row["box_id"]),
                type_id=int(row.get("type_id", 0)),
                l=float(row["length_cm"]),
                w=float(row["width_cm"]),
                h=float(row["height_cm"]),
                weight=float(row["weight_kg"]),
                destination=row["destination"].strip(),
                priority=int(row["priority"]),
                shipper=row["shipper"].strip(),
                stackable=int(row.get("stackable", 1)),
                fragile=int(row.get("fragile", 0)),  # [추가됨] 없으면 기본 0
            ))
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"CSV 형식 오류: {e}. "
                                     "필요한 컬럼: box_id, length_cm, width_cm, "
                                     "height_cm, weight_kg, destination, priority, shipper")
    if not boxes:
        raise HTTPException(400, "화물 데이터가 비어 있습니다.")

    # [추가됨] 목적지별 LIFO 검증 (1/2 — 업로드 시점 데이터 정합성).
    # "같은 목적지는 항상 같은 priority(하역 순서)를 가져야" 진짜 LIFO가
    # 성립한다 — 목적지가 섞인 priority로 들어오면 ulo_scan()/
    # blocking_by_destination()이 계산하는 "목적지별 재배치 수"가 그
    # 목적지의 실제 LIFO 위반을 의미하지 않게 된다. 그래서 계산을
    # 시작하기 전, CSV 단계에서 미리 걸러 명확한 오류로 안내한다.
    dest_priority: Dict[str, int] = {}
    conflicts = []
    for b in boxes:
        prev = dest_priority.get(b.destination)
        if prev is None:
            dest_priority[b.destination] = b.priority
        elif prev != b.priority:
            conflicts.append(f"'{b.destination}'(priority {prev} vs {b.priority})")
    if conflicts:
        raise HTTPException(
            400,
            "목적지별 LIFO 검증 실패: 같은 목적지에 서로 다른 priority가 "
            "섞여 있습니다 — " + ", ".join(sorted(set(conflicts))) +
            ". 같은 목적지의 화물은 모두 같은 priority(하역 순서)여야 합니다."
        )
    return boxes


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """CSV 업로드 → 화물 요약 + 컨테이너 추천."""
    content = await file.read()
    boxes = parse_csv(content)

    cargo_id = uuid.uuid4().hex[:8]
    CARGO[cargo_id] = boxes

    # ... (생략) ...
    rec = recommend_container(boxes)
    shippers: Dict[str, int] = {}
    dests: Dict[str, int] = {}
    types_detail: Dict[int, dict] = {}  # [수정됨] 규격과 수량을 함께 담기 위해 dict 타입으로 변경
    
    for b in boxes:
        shippers[b.shipper] = shippers.get(b.shipper, 0) + 1
        dests[b.destination] = dests.get(b.destination, 0) + 1
        
        # [수정됨] 처음 나오는 타입이면 가로, 세로, 높이 정보를 함께 기록
        if b.type_id not in types_detail:
            types_detail[b.type_id] = {
                "count": 0, 
                "l": int(b.l), 
                "w": int(b.w), 
                "h": int(b.h)
            }
        types_detail[b.type_id]["count"] += 1

    return {
        "cargo_id": cargo_id,
        "filename": file.filename,
        "summary": {
            "box_count": len(boxes),
            "type_count": len(types_detail),
            "types_detail": dict(sorted(types_detail.items())), # 추가: 프론트로 전달
            "shippers": dict(sorted(shippers.items())),
            "destinations": dict(sorted(dests.items())),
        },
        "container": rec,
    }


class PalletReq(BaseModel):
    cargo_id: str
    container: str = "20ft"


@app.post("/api/pallet-preview")
async def pallet_preview(req: PalletReq):
    """
    선택한 컨테이너 기준으로 팔레트 규격 3종을 계산해 비교 정보를 준다.
    사용자가 '팔레타이징'을 고르면 즉시 호출된다(1초 미만).
    """
    if req.cargo_id not in CARGO:
        raise HTTPException(404, "화물을 찾을 수 없습니다.")
    boxes = CARGO[req.cargo_id]
    cont = _get_container(req.container)
    return preview_all(boxes, cont)


# ---------- 최적화 ----------
class OptimizeReq(BaseModel):
    cargo_id: str
    container: str = "20ft"
    mode: str = "precise"        # precise(NSGA-II) | fast(휴리스틱)
    stow: str = "floor"          # floor(바닥적재) | pallet(팔레타이징)
    pallet_spec: str = "T11-100"
    pop: int = 30
    gen: int = 30
    # [추가됨] True(기본): 하역지별 구역을 우선 시도해 리핸들링을 줄인다 (실무 기본값).
    # False: 구역 제약 없이 순수 공간만 채워 용적률을 최대화한다 (리핸들링 증가 가능).
    use_zones: bool = True
    # [추가됨] 신규 물리 제약 3종 — Container(clp/model.py)에 새로 추가한
    # 필드와 1:1 대응. 기본값은 Container 쪽 기본값과 동일하게 맞췄다.
    min_support_ratio: float = 0.80
    max_stack_levels: int | None = None
    cg_tolerance: float = 0.05


def _get_container(code: str, **overrides) -> Container:
    """
    [수정됨] 신규 물리 제약(min_support_ratio 등)을 요청별로 다르게 쓸 수
    있도록 **overrides를 받는다. CONTAINER_SPECS[code]는 여러 요청이
    공유하는 싱글턴이라 직접 mutate하면 안 되므로, dataclasses.replace()로
    요청 전용 사본을 만든다(Container는 frozen dataclass).
    """
    if code not in CONTAINER_SPECS:
        raise HTTPException(400, f"알 수 없는 컨테이너 규격: {code}")
    base = CONTAINER_SPECS[code]
    return dataclasses.replace(base, **overrides) if overrides else base


def _run_precise(job_id: str, boxes: List[Box], cont: Container,
                 pop: int, gen: int, use_zones: bool = True):
    """NSGA-II 백그라운드 실행."""
    job = JOBS[job_id]
    t0 = time.time()

    def progress(g, util, ulo, cg):
        job["progress"] = {
            "generation": g,
            "total": gen,
            "utilization": round(util * 100, 1),
            "ulo": int(ulo),
            "cg": round(cg * 100, 1),
            "elapsed": round(time.time() - t0, 1),
        }

    try:
        front = run_nsga2(boxes, cont, pop_size=pop, generations=gen,
                          seed=1, progress=progress, use_zones=use_zones)
        job["result"] = pick_three(front, cont)
        job["result"]["container"] = {
            "code": cont.code, "L": cont.L, "W": cont.W, "H": cont.H,
            "max_weight": cont.max_weight,
            # [추가됨] 화면에 실제 적용된 제약값을 표시할 수 있도록 노출.
            "min_support_ratio": cont.min_support_ratio,
            "max_stack_levels": cont.max_stack_levels,
            "cg_tolerance": cont.cg_tolerance,
        }
        job["result"]["mode"] = "precise"
        job["result"]["stow"] = job.get("stow")
        job["result"]["use_zones"] = use_zones
        job["result"]["elapsed"] = round(time.time() - t0, 1)
        # [추가됨] 용적률/리핸들링 가중치 슬라이더(/api/reweight)용 — 이미
        # 계산해 둔 파레토 프론트를 잡(job)에 보관해 둔다. j["result"]는
        # /api/result에서 그대로 JSON으로 반환되는 값이라 여기에 프론트
        # 객체(직렬화 불가능한 Individual 리스트)를 넣으면 안 되므로,
        # job 딕셔너리에 별도 키(front/_cont)로 저장한다.
        job["front"] = front
        job["_cont"] = cont
        # [추가됨] "계산 중 화면엔 81%인데 실제 결과는 83.02%" 불일치 수정.
        # progress()는 run_nsga2() 탐색 루프 안에서만 불려서, 세대마다의
        # "탐색 중" 최고 용적률(2차 삽입 재시도 전) 값을 보여준다. 그런데
        # pick_three()가 화면에 보여줄 3개 해에 2차 삽입 재시도
        # (retry_unplaced, dblf.py)를 적용하면서 용적률이 한 번 더
        # 오르는데(예: 81.05% → 83.02%), 이 개선은 run_nsga2()가 끝난
        # "뒤"에 일어나는 별도 단계라 진행률(job["progress"])에는 반영이
        # 안 됐었다. pick_three() 계산에 걸리는 수 초 동안 클라이언트가
        # 상태를 계속 폴링하면 마지막 세대의 낡은 숫자가 그대로 떠 있다가
        # 완료되는 순간 결과 화면에서만 새 숫자로 바뀌어 보여서 "계산
        # 화면과 실제 결과가 다르다"는 인상을 줬다. 최종 결과(3개 해 중
        # 최고 용적률 = max_fill) 기준으로 진행률을 한 번 더 갱신해 이
        # 간극을 없앤다.
        best_sol = max(job["result"]["solutions"], key=lambda s: s["metrics"]["utilization"])
        job["progress"] = {
            "generation": gen,
            "total": gen,
            "utilization": best_sol["metrics"]["utilization"],
            "ulo": best_sol["metrics"]["ulo"],
            "cg": best_sol["metrics"]["cg_deviation"],
            "elapsed": round(time.time() - t0, 1),
        }
        job["status"] = "done"
    except Exception as e:                       # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)


FAST_TIME_BUDGET_SEC = 60.0  # "현장 대응" 계산 시간 예산 (약 1분)


def _run_fast(job_id: str, boxes: List[Box], cont: Container, gen: int, use_zones: bool = True):
    """휴리스틱 백그라운드 실행 (약 1분의 시간 예산 동안 최대한 탐색)."""
    job = JOBS[job_id]
    t0 = time.time()

    def progress_callback(g, util, ulo, cg):
        job["progress"] = {
            "generation": g,
            "total": gen,
            "utilization": round(util * 100, 1),
            "ulo": int(ulo),
            "cg": 0.0,
            "elapsed": round(time.time() - t0, 1),
            "time_budget": FAST_TIME_BUDGET_SEC,
        }

    try:
        job["progress"] = {"generation": 0, "total": gen, "utilization": 0, "ulo": 0, "cg": 0,
                           "elapsed": 0, "time_budget": FAST_TIME_BUDGET_SEC}

        # [수정됨] 세대(gen) 수 대신 실제 시간 예산(약 1분)만큼 계속 탐색한다.
        # gen은 안전판(최대 세대 수)으로만 넘긴다 — 데이터가 아주 작아 1세대가
        # 순식간에 끝나도 무한 루프가 되지 않도록.
        r = heuristic_solve(boxes, cont, objective="balanced",
                            iterations=max(gen, 200),
                            time_budget=FAST_TIME_BUDGET_SEC,
                            use_zones=use_zones,
                            progress=progress_callback)

        sol = single_solution(r["placed"], r["unplaced"], cont)
        sol["strategy"] = r["strategy"]
        job["result"] = {
            "solutions": [sol],
            "front_size": 1,
            "mode": "fast",
            "strategies": [],
            "container": {"code": cont.code, "L": cont.L, "W": cont.W,
                          "H": cont.H, "max_weight": cont.max_weight,
                          "min_support_ratio": cont.min_support_ratio,
                          "max_stack_levels": cont.max_stack_levels,
                          "cg_tolerance": cont.cg_tolerance},
            "stow": job.get("stow"),
            "use_zones": use_zones,
            "elapsed": round(time.time() - t0, 1),
        }
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)

@app.post("/api/optimize")
async def optimize(req: OptimizeReq):
    """계산 시작. job_id를 즉시 반환하고 백그라운드에서 실행."""
    if req.cargo_id not in CARGO:
        raise HTTPException(404, "화물을 찾을 수 없습니다. 파일을 다시 올려주세요.")
    boxes = CARGO[req.cargo_id]
    # [수정됨] 신규 물리 제약 3종을 요청 바디에서 받아 이 job 전용 Container
    # 사본에 실어 보낸다 — 이후 decode()/evaluate() 등은 전부 이 cont
    # 객체를 통해서만 값을 읽으므로 별도 배선이 필요 없다.
    cont = _get_container(
        req.container,
        min_support_ratio=req.min_support_ratio,
        max_stack_levels=req.max_stack_levels,
        cg_tolerance=req.cg_tolerance,
    )

    # 팔레타이징이면 화물을 팔레트로 묶어서 그 팔레트를 적재한다
    stow_info = None
    if req.stow == "pallet":
        if req.pallet_spec not in PALLET_SPECS:
            raise HTTPException(400, f"알 수 없는 팔레트 규격: {req.pallet_spec}")
        pallets, leftover, details = build_pallets(boxes, req.pallet_spec, cont)
        if not pallets:
            raise HTTPException(400, "팔레트를 구성할 수 없습니다.")
        stow_info = {
            "mode": "pallet",
            "spec": req.pallet_spec,
            "spec_label": PALLET_SPECS[req.pallet_spec]["label"],
            "pallet_count": len(pallets),
            "packed_boxes": sum(d["content_boxes"] for d in details),
            "leftover_boxes": len(leftover),
            "original_boxes": len(boxes),
            "details": details,
        }
        boxes = pallets
    else:
        stow_info = {"mode": "floor", "original_boxes": len(boxes)}

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "running", "progress": None,
                    "result": None, "error": None, "mode": req.mode,
                    "stow": stow_info}

    if req.mode == "fast":
        # 👇 [수정됨] req.gen을 인자로 추가 전달
        t = threading.Thread(target=_run_fast,
                             args=(job_id, boxes, cont, req.gen, req.use_zones))
    else:
        t = threading.Thread(target=_run_precise,
                             args=(job_id, boxes, cont, req.pop, req.gen, req.use_zones))
    t.daemon = True
    t.start()

    return {"job_id": job_id, "mode": req.mode, "stow": req.stow}

@app.get("/api/status/{job_id}")
async def status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    j = JOBS[job_id]
    return {"status": j["status"], "progress": j["progress"],
            "error": j["error"], "mode": j["mode"]}


@app.get("/api/result/{job_id}")
async def result(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    j = JOBS[job_id]
    if j["status"] == "running":
        raise HTTPException(409, "아직 계산 중입니다.")
    if j["status"] == "error":
        raise HTTPException(500, j["error"] or "계산에 실패했습니다.")
    return JSONResponse(j["result"])


@app.get("/api/reweight/{job_id}")
async def reweight(job_id: str, util_weight: float = 0.5):
    """
    [추가됨] 용적률/리핸들링 가중치 슬라이더.

    /api/optimize가 이미 계산해 둔 파레토 프론트(job["front"]) 중에서
    util_weight(0=리핸들링 최소화만 고려 ~ 1=용적률 최대화만 고려)에 가장
    잘 맞는 해 하나를 즉시 골라 반환한다 — NSGA-II를 다시 돌리지 않으므로
    슬라이더를 움직일 때마다 호출해도 사실상 즉시 응답한다.
    "현장 대응"(휴리스틱) 잡이나 아직 계산 중인 잡은 프론트 자체가 없거나
    준비돼 있지 않으므로 지원하지 않는다.
    """
    if job_id not in JOBS:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    j = JOBS[job_id]
    if j["status"] == "running":
        raise HTTPException(409, "아직 계산 중입니다.")
    if j["status"] == "error":
        raise HTTPException(500, j["error"] or "계산에 실패했습니다.")
    front = j.get("front")
    cont = j.get("_cont")
    if not front or cont is None:
        raise HTTPException(400, "이 계산 방식(현장 대응)은 직접 조정을 지원하지 않습니다.")
    sol = pick_by_weight(front, cont, util_weight)
    return JSONResponse(sol)


@app.get("/api/pick_point/{job_id}")
async def pick_point(job_id: str, u: float, r: int):
    """
    [추가됨] 팀 피드백: 파레토 그래프의 점을 직접 클릭하면 그 해로 화면이
    바로 바뀌게 해달라는 요청. u(용적률 %)·r(재작업 개수)는 그래프에
    그려진 회색 점 좌표(result.front_curve, 2차 삽입 재시도 전 원본) 그대로
    넘어온다 — pick_by_weight()처럼 가중치로 "가장 가까운" 해를 고르는 게
    아니라, 그 좌표와 정확히 일치하는 개체를 파레토 프론트에서 찾아낸다.
    """
    if job_id not in JOBS:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    j = JOBS[job_id]
    if j["status"] == "running":
        raise HTTPException(409, "아직 계산 중입니다.")
    if j["status"] == "error":
        raise HTTPException(500, j["error"] or "계산에 실패했습니다.")
    front = j.get("front")
    cont = j.get("_cont")
    if not front or cont is None:
        raise HTTPException(400, "이 계산 방식(현장 대응)은 직접 조정을 지원하지 않습니다.")
    sol = pick_by_point(front, cont, u, r)
    if not sol:
        raise HTTPException(404, "해당 지점을 찾을 수 없습니다.")
    return JSONResponse(sol)


@app.get("/api/manifest/{job_id}")
async def manifest(job_id: str, sol: int = 0, util_weight: float | None = None,
                    u: float | None = None, r: int | None = None):
    """
    완료된 작업의 sol번째 해를 작업명세서 PDF로 만들어 다운로드로 반환.

    [추가됨] util_weight가 주어지면(직접 조정 슬라이더로 고른 해를 PDF로
    받고 싶을 때) sol 인덱스 대신 그 가중치로 pick_by_weight()가 고른
    해를 명세서로 만든다.
    [추가됨] 팀 피드백: 파레토 그래프 점 클릭(u/r)으로 고른 해는
    util_weight가 없으므로, u·r이 함께 주어지면 pick_by_point()로 같은
    해를 다시 찾아 명세서를 만든다.
    """
    if job_id not in JOBS:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    j = JOBS[job_id]
    if j["status"] == "running":
        raise HTTPException(409, "아직 계산 중입니다.")
    if j["status"] == "error" or not j["result"]:
        raise HTTPException(500, j["error"] or "계산에 실패했습니다.")
    try:
        if util_weight is not None or (u is not None and r is not None):
            front = j.get("front")
            cont = j.get("_cont")
            if not front or cont is None:
                raise HTTPException(400, "이 계산 방식(현장 대응)은 직접 조정을 지원하지 않습니다.")
            if util_weight is not None:
                custom = pick_by_weight(front, cont, util_weight)
            else:
                custom = pick_by_point(front, cont, u, r)
                if not custom:
                    raise HTTPException(404, "해당 지점을 찾을 수 없습니다.")
            result_for_pdf = dict(j["result"])
            result_for_pdf["solutions"] = [custom]
            html_content = render_manifest_html(result_for_pdf, 0, job_id)
        else:
            html_content = render_manifest_html(j["result"], sol, job_id)
        pdf_bytes = build_manifest_pdf(html_content)
    except HTTPException:
        raise
    except Exception as e:                        # noqa: BLE001
        raise HTTPException(500, f"명세서 PDF 생성 실패: {e}")

    filename = f"작업명세서_{job_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"manifest_{job_id}.pdf\"; "
                f"filename*=UTF-8''{quote(filename)}"
        },
    )


# ---------- 정적 파일 ----------
# [수정됨] 배포 대응 — 정적 파일 경로를 CWD가 아니라 이 파일 기준으로 잡는다.
# 기존 상대경로("web")는 프로젝트 루트에서 실행할 때만 동작해서, 호스팅
# 환경이나 다른 디렉터리에서 띄우면 화면이 404가 났다.
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn
    # [수정됨] 배포 대응 — Render/Railway/HF Spaces 등은 PORT 환경변수로
    # 포트를 지정해 준다. 없으면 기존처럼 8000을 쓴다.
    port = int(os.environ.get("PORT", "8000"))
    print("\n  적재 최적화 서버 시작")
    print(f"  → http://localhost:{port} 을 브라우저에서 열어주세요\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")