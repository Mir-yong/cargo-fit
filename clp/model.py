"""
데이터 모델: Box, Container, CSV 로더.

좌표계: 원점 = front-bottom-left (0,0,0)
  +x = 안쪽(문 반대, deeper)
  +y = 오른쪽
  +z = 위(above)
"""
from __future__ import annotations
from dataclasses import dataclass, field
import csv
from typing import List, Dict, Tuple


# ---- 컨테이너 (40ft GP, 논문 치수) ----
@dataclass(frozen=True)
class Container:
    L: float = 587.0   # x 축 (길이, 안쪽 방향)
    W: float = 233.0   # y 축 (폭, 오른쪽)
    H: float = 220.0   # z 축 (높이)
    max_weight: float = 26500.0  # kg, 40ft GP 페이로드 근사
    code: str = "40ft"
    # [추가됨] 물리 제약 3종 — dblf.py/objectives.py가 하드코딩하던 상수를
    # 여기(Container)로 옮겨, 요청마다 다른 값을 쓰고 싶을 때 dblf.py/
    # objectives.py/nsga2.py/heuristic.py의 함수 시그니처를 하나도 안 건드리고
    # cont(모든 배치·평가 함수에 이미 전달되는 객체) 하나만 바꿔서 반영한다.
    min_support_ratio: float = 0.80   # 지지면적 하한 (기존 하드코딩 0.70 → 기본 80%)
    max_stack_levels: int | None = None  # 최대 적재 단수. None = 제한 없음(기존 동작)
    cg_tolerance: float = 0.05        # CoG 편차 허용오차 (objectives.evaluate 기본값 이전)

    @property
    def volume(self) -> float:
        return self.L * self.W * self.H

    @property
    def center(self) -> Tuple[float, float, float]:
        return (self.L / 2, self.W / 2, self.H / 2)


# 실무에서 쓰이는 규격 3종 (ISO 668 내부치수에서 적재 여유를 뺀 값)
# 20ft는 CLP 연구에서 표준으로 쓰이는 587×233×220을 그대로 사용한다.
# (BR 벤치마크와 논문이 채택한 치수이며, 실제 20ft 내부치수와 사실상 동일)
CONTAINER_SPECS = {
    "20ft":   Container(L=587.0, W=233.0, H=220.0, max_weight=28200.0, code="20ft"),
    "40ft":   Container(L=1203.0, W=233.0, H=220.0, max_weight=26500.0, code="40ft"),
    "40ftHC": Container(L=1203.0, W=233.0, H=250.0, max_weight=26200.0, code="40ftHC"),
}
CONTAINER_SPECS["BR"] = CONTAINER_SPECS["20ft"]   # 하위 호환


def recommend_container(boxes: List["Box"], target_fill: float = 0.85) -> dict:
    """
    화물 총부피·총중량으로 적합한 컨테이너를 추천한다.

    실무의 포워더가 하는 판단(이 화물에 어떤 규격이 맞는가)을 자동화한 것.
    target_fill: 이 정도로 차면 '적합'으로 본다. 컨테이너는 100% 채울 수
    없으므로(고정·환기·형상 손실) 85%를 기준으로 잡는다.
    """
    total_vol = sum(b.volume for b in boxes)
    total_wt = sum(b.weight for b in boxes)

    options = []
    for code in ("20ft", "40ft", "40ftHC"):
        c = CONTAINER_SPECS[code]
        fill = total_vol / c.volume
        options.append({
            "code": code,
            "label": {"20ft": "20ft 드라이",
                      "40ft": "40ft 드라이",
                      "40ftHC": "40ft 하이큐브"}[code],
            "dims": f"{int(c.L)}×{int(c.W)}×{int(c.H)}",
            "L": c.L, "W": c.W, "H": c.H,
            "volume_cbm": round(c.volume / 1e6, 1),
            "max_weight": c.max_weight,
            "fill_ratio": round(fill * 100, 1),
            "weight_ok": total_wt <= c.max_weight,
            "fits": fill <= target_fill and total_wt <= c.max_weight,
        })

    # 가장 작은 규격부터 검토해 화물이 담기는 첫 번째를 추천한다.
    # 컨테이너 운임은 크기에 비례하므로 불필요하게 큰 규격을 쓸 이유가 없다.
    # 판정 기준은 '부피가 넘치지 않는가'(100%)로 둔다. 실제로는 형상 손실
    # 때문에 100%를 다 채울 수 없지만, 못 실은 화물은 이월 리포트로
    # 안내하므로 여기서 미리 큰 규격으로 올릴 필요는 없다.
    def _ok(o):
        return o["fill_ratio"] <= 100.0 and o["weight_ok"]
    best = next((o for o in options if _ok(o)), options[-1])

    return {
        "recommended": best["code"],
        "cargo_cbm": round(total_vol / 1e6, 2),
        "cargo_weight_kg": round(total_wt, 1),
        "box_count": len(boxes),
        "needs_split": not any(o["fits"] for o in options),
        "options": options,
    }


# ---- 박스 ----
@dataclass
class Box:
    box_id: int
    type_id: int
    l: float          # 원 치수 (length, x)
    w: float          # 원 치수 (width, y)
    h: float          # 원 치수 (height, z)
    weight: float     # kg
    destination: str  # 'A'/'B'/'C'
    priority: int     # 하역 순서 (작을수록 먼저 내림) = π
    shipper: str      # 화주 (목적함수 밖, 비용 배분용)
    stackable: int    # 1=위에 적재 가능, 0=이 박스 위에 아무것도 못 올림 (해석 A)
    # [추가됨] fragile: 1=파손주의 화물, 이 박스 위에는 아무것도 못 올림.
    # stackable=0과 최종 효과(위에 아무것도 못 올림)는 같지만 의미가 다르다 —
    # stackable은 "적재 방식상" 위에 뭘 올릴 수 있는지(예: 뚜껑 없는 팔레트),
    # fragile은 "화물 자체가 파손 위험이 있어서" 못 올리는 것. 두 필드를
    # 분리해 두면 향후 리포트에서 "왜 못 쌓았는지" 원인을 구분할 수 있고,
    # CSV에 fragile 컬럼이 없는 기존 데이터도 기본값 0으로 안전하게 동작한다.
    fragile: int = 0

    @property
    def volume(self) -> float:
        return self.l * self.w * self.h

    def dims_for_orientation(self, o: int) -> Tuple[float, float, float]:
        """
        6방향 회전. 원 치수 (l,w,h)를 (x,y,z) 축에 매핑한 유효치수 반환.
        o = 0..5, 6개 축 순열.
        """
        l, w, h = self.l, self.w, self.h
        return {
            0: (l, w, h),
            1: (l, h, w),
            2: (w, l, h),
            3: (w, h, l),
            4: (h, l, w),
            5: (h, w, l),
        }[o]


# ---- 배치 결과 (한 박스의 최종 위치) ----
@dataclass
class Placement:
    box: Box
    x: float
    y: float
    z: float
    orientation: int
    dl: float  # 유효 치수 (x)
    dw: float  # 유효 치수 (y)
    dh: float  # 유효 치수 (z)
    # [추가됨] 최대 적재 단수 제약용 — 이 박스가 몇 단째에 쌓였는지(바닥=0).
    # dblf.py가 배치 시점에 계산해 채운다.
    level: int = 0

    def __post_init__(self):
        # 끝 좌표는 디코딩 중 수십만 번 조회되므로 미리 계산해 저장한다.
        # (property로 매번 더하면 그 자체가 병목이 된다)
        self.x2 = self.x + self.dl
        self.y2 = self.y + self.dw
        self.z2 = self.z + self.dh

    @property
    def centroid(self) -> Tuple[float, float, float]:
        return (self.x + self.dl / 2, self.y + self.dw / 2, self.z + self.dh / 2)


# ---- CSV 로더 ----
def load_instance(csv_path: str, instance_id: int = 1) -> List[Box]:
    """지정 instance_id의 박스들을 로드."""
    boxes: List[Box] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row["instance_id"]) != instance_id:
                continue
            boxes.append(Box(
                box_id=int(row["box_id"]),
                type_id=int(row["type_id"]),
                l=float(row["length_cm"]),
                w=float(row["width_cm"]),
                h=float(row["height_cm"]),
                weight=float(row["weight_kg"]),
                destination=row["destination"].strip(),
                priority=int(row["priority"]),
                shipper=row["shipper"].strip(),
                stackable=int(row["stackable"]),
                fragile=int(row.get("fragile", 0)),  # [추가됨] 없는 CSV는 기본 0
            ))
    if not boxes:
        raise ValueError(f"instance_id={instance_id} 에 해당하는 박스가 없습니다.")
    return boxes


def list_instances(csv_path: str) -> List[int]:
    """CSV에 존재하는 instance_id 목록."""
    ids = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ids.add(int(row["instance_id"]))
    return sorted(ids)