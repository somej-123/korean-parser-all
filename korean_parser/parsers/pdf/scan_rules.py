"""스캔 쪽 그림에서 표의 괘선(가로·세로 선)을 찾는다.

텍스트 레이어 PDF는 선이 그리기 개체로 남아 있어 `rules.read_rules`로 읽는다. 스캔본은 쪽 그림뿐이므로, 잉크 화소가
가로(세로)로 길게 이어진 줄을 선으로 본다. 찾은 선은 `rules.Rule`과 같은 모양이라 `build_grid`(행·열·병합 칸 찾기)를
그대로 쓴다. 표 구조 인식 모델(TableFormer)은 스캔에서 병합 칸을 자주 놓쳤다 (실제 스캔 '구분' 칸, 합성 스캔 표 TEDS 0.766).

- 가로선은 20pt 이상 이어져야 한다 (글자 획은 한 글자 폭 정도). 세로선은 8pt 이상이고 양 끝이 가로선에 닿아야 한다
- 두께가 THIN(2.5pt)보다 두꺼운 잉크 띠(칸 배경, 굵은 글씨 덩어리)는 선이 아니다
- 스캔하면서 1~2px 끊기거나 흔들린 선을 잇기 위해 이웃한 화소 줄의 조각을 묶는다
"""

from __future__ import annotations

import numpy as np

from .rules import THIN, Rule

MIN_H = 20.0  # 가로선 최소 길이 (pt)
MIN_V = 8.0  # 세로선 최소 길이 (pt)


def _runs(line: np.ndarray, min_len: int, bridge: int = 2) -> list[tuple[int, int]]:
    """한 줄(1차원 잉크 배열)에서 min_len 이상 이어진 잉크 구간 [시작, 끝). bridge 이하로 끊긴 곳은 잇는다."""
    padded = np.concatenate(([False], line, [False]))
    d = np.diff(padded.astype(np.int8))
    starts, ends = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
    out: list[list[int]] = []
    for s, e in zip(starts, ends):
        if out and s - out[-1][1] <= bridge:
            out[-1][1] = e
        else:
            out.append([s, e])
    return [(s, e) for s, e in out if e - s >= min_len]


def _axis_rules(ink: np.ndarray, axis: str, scale: float) -> list[Rule]:
    """axis='h'면 행마다, 'v'면 열마다 긴 잉크 구간을 찾아 이웃한 줄끼리 묶어 선으로."""
    grid = ink if axis == "h" else ink.T
    min_len = int((MIN_H if axis == "h" else MIN_V) * scale)
    # 긴 구간이 있을 수 없는 줄은 건너뛴다 (잉크 화소 수가 min_len보다 적은 줄)
    rows = np.nonzero(grid.sum(axis=1) >= min_len)[0]
    open_: list[dict] = []  # 지금 이어지고 있는 선: {pos0, pos1, a, b}
    done: list[dict] = []
    for r in rows:
        segs = _runs(grid[r], min_len)
        still = []
        for seg in segs:
            hit = next((o for o in open_ if o["pos1"] >= r - 2 and seg[0] < o["b"] and seg[1] > o["a"]), None)
            if hit is not None:
                hit["pos1"], hit["a"], hit["b"] = r, min(hit["a"], seg[0]), max(hit["b"], seg[1])
                if hit not in still:
                    still.append(hit)
            else:
                still.append({"pos0": r, "pos1": r, "a": seg[0], "b": seg[1]})
        done += [o for o in open_ if o not in still and o["pos1"] < r - 2]
        open_ = still + [o for o in open_ if o not in still and o["pos1"] >= r - 2]
    done += open_
    out = []
    for o in done:
        width = (o["pos1"] - o["pos0"] + 1) / scale
        if width > THIN:
            continue  # 칸 배경·굵은 글씨 덩어리
        pos = (o["pos0"] + o["pos1"] + 1) / 2 / scale
        out.append(Rule(axis, round(pos, 2), round(o["a"] / scale, 2), round(o["b"] / scale, 2), round(max(width, 0.25), 2)))
    return out


def image_rules(ink: np.ndarray, scale: float, tol: float = 1.2) -> list[Rule]:
    """쪽 그림(잉크 여부 배열, scale 배율)의 가로·세로 선. 좌표는 왼쪽 위 원점, pt.

    세로선은 위아래 끝이 모두 가로선에 닿는 것만 남긴다. 글자의 세로 획(8~10pt)도 길이로는 선과 구별되지 않았다
    (실제 스캔 2쪽: 세로선 후보 97개 중 진짜 칸 경계는 6개). 칸 여백이 좁으면 글자 획 끝도 가로선에서 2pt까지 다가가므로,
    닿았다고 보는 거리는 1.2pt (진짜 선의 끝은 0.5pt 안).
    """
    hs = _axis_rules(ink, "h", scale)

    def touches(y: float, x: float) -> bool:
        return any(abs(h.pos - y) <= tol and h.a - tol <= x <= h.b + tol for h in hs)

    vs = [v for v in _axis_rules(ink, "v", scale) if touches(v.a, v.pos) and touches(v.b, v.pos)]
    return hs + vs
