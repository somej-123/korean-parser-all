"""스캔(OCR) 글의 띄어쓰기와 개조식 기호를 쪽 그림에서 되살린다.

OCR(RapidOCR 한국어 모델)은 줄 안의 공백을 대부분 빠뜨린다 ('계약방법:협상에의한계약'). 그런데 쪽 그림에는 공백이
글자 사이의 넓은 빈칸으로 그대로 남아 있다. 텍스트 레이어가 있는 PDF에서 글자 간격으로 공백을 정하는 것과 같은
방식으로, 줄 그림에서 잉크가 없는 세로 띠(빈칸)의 폭을 재서 공백 자리를 찾는다.

1. 줄 그림을 흑백으로 바꾸고 표 괘선(줄 전체를 가로지르는 잉크)을 지운다.
2. 빈칸 폭이 글자 높이(em)의 일정 배수 이상이면 공백이다. 한 낱말 안의 빈칸(받침과 모음 사이 등)은 이보다 좁다.
   이 기준은 글꼴마다 다르므로(바탕 0.42, 고딕 0.33 안팎) 문서 전체 빈칸 폭의 분포에서 두 무리(낱말 안 / 낱말 사이)를
   가르는 값으로 정한다 (`space_threshold`).
3. 공백으로 나뉜 조각들의 폭과 OCR 글자의 예상 폭(한글 1em, 숫자 0.5em …)을 맞춰, 각 공백이 들어갈 글자 위치를
   정한다 (동적 계획법).
4. 줄 맨 앞의 1em 크기 조각이 개조식 기호 모양(□ ○ ■ ● -)이면 그 기호를 되살린다. OCR은 이런 기호를 빠뜨리거나
   'O'·'0'·'ㅇ'·'ㅁ'으로 읽는다.

그림 조각과 글자 수가 크게 어긋나면(OCR이 글자를 빠뜨리거나 더 읽음) None을 돌려준다. 부르는 쪽이 형태소 분석으로 띄운다.
좌표는 '왼쪽 위 원점, pt'.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SCALE = 3.0  # 쪽 그림 배율 (216dpi). 빈칸 폭을 1px(약 0.05em) 단위로 잴 수 있다
INK = 160  # 이보다 어두운 화소를 잉크로 본다 (0 검정 ~ 255 흰색)
SPACE_GAP = 0.42  # 빈칸 폭 분포로 정하지 못할 때 쓰는 공백 기준 (em 배수). 바탕체 정답 세트에서 공백 0.45~0.65, 낱말 안 0.37 이하
MIXED_RATIO = 0.6  # 한글과 숫자·영문·문장부호가 맞닿은 자리는 공백 기준의 이 배수로 본다 (영문 글꼴의 공백이 더 좁다)

# OCR이 개조식 기호를 잘못 읽은 글자. 그림 모양이 기호이면 이 글자를 기호로 바꾼다
_BULLET_LOOKALIKE = set("Oo0ㅇㅁ口ロ○◦●□■◇◆▣-–·•")
_MARKER_CHARS = set("□■◇◆▣○●◦◎❍※·•-–")


@dataclass
class OcrCell:
    text: str
    box: tuple[float, float, float, float]


def char_width(ch: str) -> float:
    """글자의 대략적인 폭 (em). 한글·한자·기호는 정사각형, 숫자·영문은 절반 정도."""
    if "가" <= ch <= "힣" or "㄰" <= ch <= "㆏" or "一" <= ch <= "鿿":
        return 1.0
    if ch in "□■◇◆▣○●◦◎❍※":
        return 1.0
    if ch in ".,:;'`|!":
        return 0.28
    if ch in "()[]{}-–·":
        return 0.36
    if ch in "%&@WMmw":
        return 0.85
    if ch.isupper():
        return 0.66
    if ch.isdigit():
        return 0.52
    if "Ⅰ" <= ch <= "ⅿ":  # 로마 숫자 Ⅰ Ⅱ …
        return 0.7
    return 0.5


class PageImage:
    """스캔 쪽 한 장의 흑백 그림 (잉크 여부)."""

    def __init__(self, pdf_page):
        gray = pdf_page.render(scale=SCALE, grayscale=True).to_pil().convert("L")
        self.ink = np.asarray(gray) < INK

    def crop(self, box) -> np.ndarray:
        l, t, r, b = (int(round(v * SCALE)) for v in box)
        h, w = self.ink.shape
        return self.ink[max(t, 0):min(b, h), max(l, 0):min(r, w)]


def _strip_rules(mask: np.ndarray) -> np.ndarray:
    """줄 그림에서 표 괘선을 지운다: 가로·세로로 거의 전부(90%·95% 넘게) 칠해진 행과 열."""
    if mask.size == 0:
        return mask
    mask = mask[mask.mean(axis=1) < 0.9]
    if mask.size == 0:
        return mask
    cols = mask.mean(axis=0) > 0.95
    if cols.any():
        mask = mask.copy()
        mask[:, cols] = False
    return mask


def _em(mask: np.ndarray, box_h_px: float) -> float:
    """글자 높이(px). 잉크가 어느 정도 있는 행의 범위로 잰다 (쉼표 꼬리 같은 작은 돌출은 뺀다)."""
    rows = mask.sum(axis=1)
    if not rows.any():
        return box_h_px / 1.4
    core = np.nonzero(rows >= rows.max() * 0.2)[0]
    em = float(core[-1] - core[0] + 1)
    # 숫자·기호만 있는 줄은 높이가 낮게 나온다. OCR 상자 높이(대개 글자 높이의 1.3~1.5배)로 하한을 둔다
    return max(em, box_h_px / 1.6)


def line_gaps(mask: np.ndarray, box_h_px: float) -> list[float]:
    """줄 그림의 빈칸 폭들 (em 배수). 문서의 공백 기준을 정하는 데 쓴다."""
    mask = _strip_rules(mask)
    if mask.size == 0:
        return []
    em = _em(mask, box_h_px)
    xs = np.nonzero(mask.any(axis=0))[0]
    return [(b - a - 1) / em for a, b in zip(xs, xs[1:]) if (b - a - 1) / em >= 0.1]


def space_threshold(gaps: list[float]) -> float:
    """빈칸 폭 분포를 두 무리(낱말 안 / 낱말 사이)로 가르는 값 (오츠 방법). 표본이 적으면 SPACE_GAP.

    0.1~1.2em 사이의 빈칸만 본다 (더 넓은 것은 칸·단 사이). 결과는 0.3~0.45em으로 묶는다.
    """
    vals = np.sort(np.array([g for g in gaps if 0.1 <= g <= 1.2]))
    if len(vals) < 30:
        return SPACE_GAP
    best, best_t = -1.0, SPACE_GAP
    for t in np.arange(0.25, 0.55, 0.01):
        lo, hi = vals[vals < t], vals[vals >= t]
        if len(lo) == 0 or len(hi) == 0:
            continue
        w0, w1 = len(lo) / len(vals), len(hi) / len(vals)
        between = w0 * w1 * (lo.mean() - hi.mean()) ** 2
        if between > best:
            best, best_t = between, float(t)
    return min(max(best_t, 0.3), 0.45)


def _segments(mask: np.ndarray, em: float, space_gap: float = SPACE_GAP) -> list[tuple[int, int, float]]:
    """공백 빈칸으로 나눈 잉크 조각 (시작 x, 끝 x, 앞 빈칸 폭 em)."""
    cols = mask.any(axis=0)
    xs = np.nonzero(cols)[0]
    if len(xs) == 0:
        return []
    segs: list[list] = [[int(xs[0]), int(xs[0]), 0.0]]
    for a, b in zip(xs, xs[1:]):
        gap = (b - a - 1) / em
        if gap >= space_gap:
            segs.append([int(b), int(b), gap])
        else:
            segs[-1][1] = int(b)
    return [tuple(s) for s in segs]


def bullet_shape(blob: np.ndarray, em: float) -> str | None:
    """잉크 조각 하나가 개조식 기호 모양인가. □ ○ ■ ● - 중 하나, 아니면 None."""
    ys, xs = np.nonzero(blob)
    if len(xs) == 0:
        return None
    blob = blob[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    bh, bw = blob.shape
    if bh <= 0.2 * em and 0.25 * em <= bw <= 1.0 * em:
        return "-"
    if not (0.4 * em <= bh <= 1.1 * em and 0.75 <= bw / bh <= 1.33):
        return None
    fill = blob.mean()
    cy0, cy1, cx0, cx1 = int(bh * 0.3), int(bh * 0.7) + 1, int(bw * 0.3), int(bw * 0.7) + 1
    center = blob[cy0:cy1, cx0:cx1].mean()
    k = max(1, int(min(bh, bw) * 0.15))
    corners = np.mean([blob[:k, :k].mean(), blob[:k, -k:].mean(), blob[-k:, :k].mean(), blob[-k:, -k:].mean()])
    # 테두리만 있는 기호는 안쪽이 비어 있다. □는 네 변이 거의 다 칠해져 있고 모서리가 차 있다
    edges = min(blob[:, 0].mean(), blob[:, -1].mean(), blob[0].mean(), blob[-1].mean())
    if center < 0.03 and edges > 0.6 and corners > 0.5:
        return "□"
    if center < 0.03 and corners < 0.15 and blob[bh // 2].sum() >= 2 and blob[:, bw // 2].sum() >= 2:
        # 원: 가로·세로 가운데 줄에 테두리 잉크가 양쪽에 있다
        mid_row, mid_col = blob[bh // 2], blob[:, bw // 2]
        if mid_row[: bw // 3].any() and mid_row[-(bw // 3):].any() and mid_col[: bh // 3].any() and mid_col[-(bh // 3):].any():
            return "○"
    if fill > 0.85:
        return "■"
    if 0.6 < fill <= 0.85 and corners < 0.35 and center > 0.9:
        return "●"
    return None


def respace_line(mask: np.ndarray, text: str, box_h_px: float, space_gap: float = SPACE_GAP) -> str | None:
    """줄 그림(잉크 여부)과 OCR 글로 공백과 맨 앞 개조식 기호를 되살린 글. 맞출 수 없으면 None.

    space_gap: 공백으로 보는 빈칸 폭 (em 배수, `space_threshold`로 문서마다 정한다).
    """
    chars = [c for c in text if not c.isspace()]
    mask = _strip_rules(mask)
    if not chars or mask.size == 0:
        return None
    em = _em(mask, box_h_px)
    segs = _segments(mask, em, space_gap)
    if not segs:
        return None

    prefix = ""
    if len(segs) >= 2:
        x0, x1, _ = segs[0]
        shape = bullet_shape(mask[:, x0:x1 + 1], em) if (x1 - x0 + 1) <= 1.3 * em else None
        if shape is not None and chars[0] not in _MARKER_CHARS - _BULLET_LOOKALIKE:
            if chars[0] in _BULLET_LOOKALIKE:
                chars = chars[1:]
            prefix = shape + " "
            segs = segs[1:]
            if not chars:
                return None

    widths = [(b - a + 1) / em for a, b, _ in segs]
    expected = [char_width(c) for c in chars]
    total_w, total_e = sum(widths), sum(expected)
    ratio = total_w / total_e if total_e else 0
    # 글자 수가 그림과 맞지 않으면 (OCR이 글자를 빠뜨리거나 덧붙임) 위치를 믿을 수 없다
    if not 0.75 <= ratio <= 1.3 or len(segs) > len(chars):
        return None
    # 동적 계획법: 글자 0..i를 조각 0..j에 나눠 담을 때 폭 차이의 제곱합이 가장 작은 경계
    expected = [e * ratio for e in expected]
    prefix_sum = [0.0]
    for e in expected:
        prefix_sum.append(prefix_sum[-1] + e)
    n, k = len(chars), len(segs)
    inf = float("inf")
    cost = [[inf] * (n + 1) for _ in range(k + 1)]
    back = [[0] * (n + 1) for _ in range(k + 1)]
    cost[0][0] = 0.0
    for j in range(1, k + 1):
        for i in range(j, n - (k - j) + 1):
            best, arg = inf, 0
            for p in range(j - 1, i):
                if cost[j - 1][p] == inf:
                    continue
                d = (prefix_sum[i] - prefix_sum[p]) - widths[j - 1]
                c = cost[j - 1][p] + d * d
                if c < best:
                    best, arg = c, p
            cost[j][i], back[j][i] = best, arg
    cuts, i = [], n
    for j in range(k, 0, -1):
        p = back[j][i]
        cuts.append(p)
        i = p
    cuts = sorted(set(cuts) - {0})
    spaces = set(cuts)
    # 낱말 안의 '섞인 자리'(한글-숫자·영문, 문장부호 뒤): 영문 글꼴의 공백은 한글 글꼴보다 좁아 더 낮은 기준으로 본다
    bounds = [0] + cuts + [n]
    for (a, b), (x0, x1, _) in zip(zip(bounds, bounds[1:]), segs):
        if not any(_mixed(chars[i - 1], chars[i]) for i in range(a + 1, b)):
            continue
        spans = _char_spans(mask[:, x0:x1 + 1], expected[a:b], em)
        if spans is None:
            continue
        for i in range(a + 1, b):
            gap = (spans[i - a][0] - spans[i - a - 1][1] - 1) / em
            if _mixed(chars[i - 1], chars[i]) and gap >= space_gap * MIXED_RATIO:
                spaces.add(i)
    spaces = {i for i in spaces if _space_allowed(chars[i - 1], chars[i])}
    out = []
    for i, ch in enumerate(chars):
        if i in spaces:
            out.append(" ")
        out.append(ch)
    return prefix + "".join(out)


def _char_spans(mask: np.ndarray, widths: list[float], em: float) -> list[tuple[int, int]] | None:
    """낱말 조각 안에서 글자마다 차지한 가로 범위(px). 맞출 수 없으면 None.

    빈 열로 나뉜 잉크 덩어리를 글자에 차례로 나눠 준다 (동적 계획법). 한 글자가 덩어리 1~3개를 차지하거나
    ('이' = 'ㅇ' + 'ㅣ'), 붙어 그려진 두 글자가 덩어리 하나를 나눠 가질 수 있다. 덩어리 폭과 예상 글자 폭의 차이가
    가장 작은 나눔을 고른다. 숫자가 길게 이어진 낱말('1,250,000,000원(VAT')에서도 글자 사이 빈칸을 정확히 잴 수 있다.
    """
    xs = np.nonzero(mask.any(axis=0))[0]
    if len(xs) == 0:
        return None
    blobs: list[list[int]] = [[int(xs[0]), int(xs[0])]]
    for a, b in zip(xs, xs[1:]):
        if b - a > 1:
            blobs.append([int(b), int(b)])
        else:
            blobs[-1][1] = int(b)
    n, m = len(widths), len(blobs)
    scale = (blobs[-1][1] - blobs[0][0] + 1) / em / max(sum(widths), 1e-6)
    w = [x * scale * em for x in widths]
    inf = float("inf")
    # cost[i][j]: 글자 i개에 덩어리 j개를 나눠 준 최소 비용
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            c0 = cost[i][j]
            if c0 == inf:
                continue
            for di, dj, extra in ((1, 1, 0.0), (1, 2, 0.0), (1, 3, 0.0), (2, 1, 0.3)):
                ni, nj = i + di, j + dj
                if ni > n or nj > m:
                    continue
                span = blobs[nj - 1][1] - blobs[j][0] + 1
                d = (span - sum(w[i:ni])) / em
                c = c0 + d * d + extra
                if c < cost[ni][nj]:
                    cost[ni][nj], back[ni][nj] = c, (i, j)
    if cost[n][m] == inf:
        return None
    spans: list[tuple[int, int]] = [(0, 0)] * n
    i, j = n, m
    while i > 0:
        pi, pj = back[i][j]
        x0, x1 = blobs[pj][0], blobs[j - 1][1]
        if i - pi == 2:  # 두 글자가 한 덩어리: 폭 비율로 나눈다
            mid = x0 + int((x1 - x0) * w[pi] / (w[pi] + w[pi + 1]))
            spans[pi], spans[pi + 1] = (x0, mid), (mid + 1, x1)
        else:
            spans[pi] = (x0, x1)
        i, j = pi, pj
    return spans


def _kind(ch: str) -> str:
    if "가" <= ch <= "힣":
        return "H"
    if ch.isdigit():
        return "D"
    if ch.isascii() and ch.isalpha():
        return "L"
    return "P"


def _mixed(a: str, b: str) -> bool:
    """글자 종류가 바뀌는 자리 (한글-숫자, 영문-한글, 문장부호-한글 …). 숫자 안의 쉼표·마침표('1,250', '2.5')는 뺀다."""
    ka, kb = _kind(a), _kind(b)
    if ka == kb or (ka, kb) == ("D", "H"):
        return False
    return not ({ka, kb} <= {"D", "P"} and (a in ",." or b in ",."))


def _space_allowed(a: str, b: str) -> bool:
    """닫는 문장부호 앞, 여는 괄호 뒤에는 공백이 오지 않는다. 숫자 바로 뒤의 한글('2026년', '8개월')도 붙인다.

    숫자 글꼴은 옆 여백이 넓어 '2026년' 사이도 공백만큼 벌어지는 경우가 있다. 형태소 분석 띄어쓰기의 '숫자 뒤' 규칙과 같다.
    """
    if b in ".,:;)]}%" or a in "([{":
        return False
    if a.isdigit() and "가" <= b <= "힣":
        return False
    return True


def group_lines(cells: list[OcrCell]) -> list[list[OcrCell]]:
    """OCR 상자를 줄로 묶는다 (세로로 절반 넘게 겹치면 같은 줄). 줄은 위에서 아래로, 줄 안은 왼쪽부터."""
    lines: list[list[OcrCell]] = []
    for c in sorted(cells, key=lambda c: (c.box[1] + c.box[3]) / 2):
        cy, ch = (c.box[1] + c.box[3]) / 2, c.box[3] - c.box[1]
        for line in lines:
            ly0, ly1 = min(x.box[1] for x in line), max(x.box[3] for x in line)
            overlap = min(ly1, c.box[3]) - max(ly0, c.box[1])
            if overlap > 0.5 * min(ch, ly1 - ly0) and ly0 - ch < cy < ly1 + ch:
                line.append(c)
                break
        else:
            lines.append([c])
    for line in lines:
        line.sort(key=lambda c: c.box[0])
    lines.sort(key=lambda ln: min(c.box[1] for c in ln))
    return lines


def _line_box(line: list[OcrCell]):
    return (min(c.box[0] for c in line), min(c.box[1] for c in line),
            max(c.box[2] for c in line), max(c.box[3] for c in line))


def cells_gaps(page: PageImage, cells: list[OcrCell]) -> list[float]:
    """OCR 상자들(줄로 묶음)의 빈칸 폭들. 문서 전체의 공백 기준(`space_threshold`)을 정할 때 모은다."""
    out: list[float] = []
    for line in group_lines(cells):
        box = _line_box(line)
        out += line_gaps(page.crop(box), (box[3] - box[1]) * SCALE)
    return out


def respace_cells(page: PageImage, cells: list[OcrCell],
                  space_gap: float = SPACE_GAP) -> list[tuple[str | None, str]]:
    """OCR 상자들을 줄로 묶어 줄마다 (공백을 되살린 글 또는 None, OCR 글 그대로)."""
    out = []
    for line in group_lines(cells):
        box = _line_box(line)
        text = "".join(c.text for c in line)
        mask = page.crop(box)
        h_px = (box[3] - box[1]) * SCALE
        out.append((respace_line(mask, text, h_px, space_gap), " ".join(c.text for c in line)))
    return out
