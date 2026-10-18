"""스캔(OCR) 글의 띄어쓰기와 개조식 기호를 쪽 그림에서 되살린다.

OCR(RapidOCR 한국어 모델)은 줄 안의 공백을 대부분 빠뜨린다 ('계약방법:협상에의한계약'). 그런데 쪽 그림에는 공백이
글자 사이의 넓은 빈칸으로 그대로 남아 있다. 텍스트 레이어가 있는 PDF에서 글자 간격으로 공백을 정하는 것과 같은
방식으로, 줄 그림에서 잉크가 없는 세로 띠(빈칸)의 폭을 재서 공백 자리를 찾는다.

1. 줄 그림을 흑백으로 바꾸고 표 괘선(줄 전체를 가로지르는 잉크)을 지운다.
2. 빈칸 폭이 글자 높이(em)의 일정 배수 이상이면 공백이다. 한 낱말 안의 빈칸(받침과 모음 사이 등)은 이보다 좁다.
   이 기준은 글꼴마다 다르므로(바탕 0.4 안팎, 공공기관 문서의 고딕 0.2~0.3) 문서 전체 빈칸 폭의 분포에서 두 무리
   (낱말 안 / 낱말 사이)를 가르는 값으로 정한다 (`space_threshold`).
3. 공백으로 나뉜 조각들의 폭과 OCR 글자의 예상 폭(한글 1em, 숫자 0.5em …)을 맞춰, 각 공백이 들어갈 글자 위치를
   정한다 (동적 계획법). 목차 점선(····)은 그림과 OCR 글 양쪽에서 떼어 놓고 맞춘다.
4. 줄 맨 앞의 1em 크기 조각이 개조식 기호 모양(□ ○ ■ ● -)이면 그 기호를 되살린다. OCR은 이런 기호를 빠뜨리거나
   'O'·'0'·'ㅇ'·'ㅁ'으로 읽는다.

그림 조각과 글자 수가 크게 어긋나면(OCR이 글자를 빠뜨리거나 더 읽음) None을 돌려준다. 부르는 쪽이 형태소 분석으로 띄운다.
좌표는 '왼쪽 위 원점, pt'.
"""

from __future__ import annotations

import re
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


_WIDE_PUNCT = "□■◇◆▣○●◦◎❍※【】「」『』〈〉《》〔〕"
_LOOSE = set("()[]{}<>【】「」『』〈〉《》〔〕'\"‘’“”·ㆍ・‧∙•")  # 글꼴마다 폭이 크게 다르거나 OCR이 전각·반각을 바꿔 읽는 글자
_DOTS = set("·ㆍ・‧∙")  # 가운뎃점: 앞뒤 낱말에 붙여 쓴다
_OPEN = set("([{<【「『〈《〔")


def char_width(ch: str) -> float:
    """글자의 대략적인 폭 (em). 한글·한자·기호는 정사각형, 숫자·영문은 절반 정도."""
    if "가" <= ch <= "힣" or "㄰" <= ch <= "㆏" or "一" <= ch <= "鿿":
        return 1.0
    if ch in _WIDE_PUNCT or "　" <= ch <= "〿" or "！" <= ch <= "｠":
        return 1.0
    if ch in ".,:;'`|!":
        return 0.28
    if ch in "()[]{}-–·":
        return 0.36
    if ch in "%&@WMmw":
        return 0.85
    if ch in "iIjlft":  # 좁은 영문 ('PIC'의 I를 0.66으로 보면 'OP IChardwa re'처럼 밀렸다)
        return 0.3
    if ch == "r":
        return 0.38
    if ch.isupper():
        return 0.66
    if ch.isdigit():
        return 0.52
    if "Ⅰ" <= ch <= "ⅿ":  # 로마 숫자 Ⅰ Ⅱ …
        return 0.7
    return 0.5


def width_spread(ch: str) -> float:
    """char_width가 실제 폭과 얼마나 어긋날 수 있는가 (em). 한글은 거의 정확하고, 괄호·따옴표·가운뎃점은 글꼴과
    OCR(전각 '【'을 반각 '['로 읽는 등)에 따라 0.5em 넘게 틀린다. 조각과 글자를 맞출 때 어긋남을 이런 글자 쪽에 둔다."""
    if "가" <= ch <= "힣":
        return 0.1
    if ch in _LOOSE:
        return 0.45
    if ch.isdigit():
        return 0.12
    if ch.isascii() and ch.isalpha():
        return 0.15
    return 0.3


class PageImage:
    """스캔 쪽 한 장의 흑백 그림 (잉크 여부)."""

    def __init__(self, pdf_page):
        gray = pdf_page.render(scale=SCALE, grayscale=True).to_pil().convert("L")
        self.gray = np.asarray(gray)
        self.ink = self.gray < INK

    def crop(self, box) -> np.ndarray:
        """상자 안의 잉크 (글자 화소).

        - 어두운 띠 위의 흰 글자('이용 프로세스' 제목 띠): 상자의 절반 넘게 잉크다. 밝은 화소를 글자로 본다.
          보통 글 줄은 굵은 제목도 잉크가 35%를 넘지 않는다.
        - 연한 색 글자(밝기 160 넘음)만 있어 잉크가 거의 없으면, 상자 안의 가장 어두운 쪽과 바탕 밝기의 가운데를
          기준으로 다시 정한다 (회색 제목 '2026년도 교육훈련계획': 글자 186~193, 바탕 250).
        """
        l, t, r, b = (int(round(v * SCALE)) for v in box)
        h, w = self.ink.shape
        ink = self.ink[max(t, 0):min(b, h), max(l, 0):min(r, w)]
        if ink.size and ink.mean() > 0.55:
            return ~ink
        if ink.size and ink.mean() < 0.005:
            gray = self.gray[max(t, 0):min(b, h), max(l, 0):min(r, w)]
            dark, paper = np.percentile(gray, 2), np.percentile(gray, 90)
            if paper - dark >= 40:
                return gray < (dark + paper) / 2
        return ink


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

    0.1~0.8em 사이의 빈칸만 본다. 더 넓은 빈칸(칸·단 사이, 문장 끝)이 섞이면 가르는 값이 위로 끌려 올라간다.
    결과는 0.2~0.45em으로 묶는다. 공공기관 문서의 고딕은 낱말 안 빈칸이 0.22em 이하, 공백이 0.25~0.45em이라
    예전 하한(0.3)으로는 공백을 대부분 놓쳤다 (실제 PDF로 만든 스캔본, `benchmarks/eval/real_scan.py`).
    """
    vals = np.sort(np.array([g for g in gaps if 0.1 <= g <= 0.8]))
    if len(vals) < 30:
        return SPACE_GAP
    best, best_t = -1.0, SPACE_GAP
    for t in np.arange(0.18, 0.55, 0.01):
        lo, hi = vals[vals < t], vals[vals >= t]
        if len(lo) == 0 or len(hi) == 0:
            continue
        w0, w1 = len(lo) / len(vals), len(hi) / len(vals)
        between = w0 * w1 * (lo.mean() - hi.mean()) ** 2
        if between > best:
            best, best_t = between, float(t)
    return min(max(best_t, 0.2), 0.45)


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
    # 목차 점선: 그림에는 점이 수십 개 있는데 OCR은 빠뜨리거나 '…' 몇 개로 읽는다. 양쪽에서 떼어 놓고 맞춘 뒤
    # OCR이 읽은 점은 제자리에 앞뒤를 띄워 되돌린다 (점선 때문에 줄 전체를 형태소 분석에 넘기면 '사망 재해 분석 결과'처럼 쪼갰다)
    mask, has_leader = _drop_leader(mask, em)
    leader_at, leader = -1, ""
    if has_leader:
        m = _LEADER_TEXT.search("".join(chars))
        if m:
            leader_at, leader = m.start(), m.group()
            chars = chars[:m.start()] + chars[m.end():]
            if not chars:
                return None
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
                leader_at -= 1
            prefix = shape + " "
            segs = segs[1:]
            if not chars:
                return None

    widths = [(b - a + 1) / em for a, b, _ in segs]
    # 점·티끌만 한 조각 (줄 끝에 남은 점선 한 점, 거친 스캔의 얼룩): OCR 글에 해당 글자가 없으면 비워 둘 수 있다
    tiny = [_is_dot(mask[:, a:b + 1], em) for a, b, _ in segs]
    # 줄 맨 앞의 1.5em 이하 조각은 OCR이 읽지 않은 그림 기호(체크 표시, 기차 그림)일 수 있다: 벌점을 주고 비울 수 있다.
    # 예전에는 첫 글자를 그 기호에 짝지어 '오 시는 길', '지 하철이용'처럼 띄웠다. 첫 글자가 기호를 닮은 글자('O' '0' 'ㅇ')이면
    # OCR이 그 기호를 읽은 것이므로 비우지 않는다
    lead = len(segs) >= 2 and widths[0] <= 1.5 and chars[0] not in _BULLET_LOOKALIKE
    expected = [char_width(c) for c in chars]
    spread = [width_spread(c) for c in chars]
    if chars[0] in _BULLET_LOOKALIKE and len(chars) > 1:
        # 줄 맨 앞의 '0'·'O'·'ㅇ'은 기호(ㅇ ○, 1em 안팎)를 잘못 읽은 것일 수 있다. 숫자 폭으로 보면 기호 조각을 비우고
        # '0'을 다음 낱말에 붙였다 ('0액화석유가스의')
        if chars[0] in "Oo0ㅇㅁ口ロ":
            expected[0] = 0.9
        spread[0] = 0.45
    total_w = sum(w for w, t in zip(widths, tiny) if not t)
    total_e = sum(expected)
    ratio = total_w / total_e if total_e else 0
    # 글자 수가 그림과 맞지 않으면 (OCR이 글자를 빠뜨리거나 덧붙임) 위치를 믿을 수 없다
    if not 0.75 <= ratio <= 1.3 or len(segs) - sum(tiny) - lead > len(chars):
        return None
    # 비율을 조금씩 바꿔 가며 가장 잘 맞는 나눔을 고른다. 전체 폭으로 잰 비율은 폭이 틀리기 쉬운 글자('【'을 '['로
    # 읽음)에 끌려가서, 장평을 줄인 글꼴에서는 한글 폭을 크게 잡아 글자를 앞 조각으로 밀었다 ('[별첨1]해 외범…')
    # 그림에서 쉼표·마침표·닫는 괄호는 거의 늘 앞 낱말에 붙고, 여는 괄호는 뒤 낱말에 붙는다: 이런 자리에서 조각이
    # 시작하면 벌점을 준다 (OCR이 앞의 쉼표를 빠뜨리면 '…(0.32%) ,위치추적기'처럼 쉼표가 다음 조각으로 넘어갔다)
    starts = [True] + [chars[i] not in ".,:;)]}" and chars[i - 1] not in "([{" for i in range(1, len(chars))]
    best = None
    for f in (0.9, 0.95, 1.0, 1.05, 1.1):
        r = ratio * f
        got = _assign(widths, tiny, [e * r for e in expected], [v * r for v in spread], starts, lead)
        if got is not None and (best is None or got[0] < best[0]):
            best = (got[0], got[1], r)
    if best is None:
        return None
    _, ranges, ratio = best
    expected = [e * ratio for e in expected]
    # 숫자 옆의 공백은 더 넓어야 한다 (`_need_gap`)
    spaces = {a for (a, b), seg in zip(ranges, segs)
              if 0 < a < b and seg[2] >= _need_gap(chars[a - 1], chars[a], space_gap, space_gap)}
    # 낱말 안의 '섞인 자리'(한글-숫자·영문, 문장부호 뒤): 영문 글꼴의 공백은 한글 글꼴보다 좁아 더 낮은 기준으로 본다.
    for (a, b), (x0, x1, _) in zip(ranges, segs):
        if b - a < 2 or not any(_mixed(chars[i - 1], chars[i]) for i in range(a + 1, b)):
            continue
        spans = _char_spans(mask[:, x0:x1 + 1], expected[a:b], em)
        if spans is None:
            continue
        for i in range(a + 1, b):
            gap = (spans[i - a][0] - spans[i - a - 1][1] - 1) / em
            if _mixed(chars[i - 1], chars[i]) and gap >= _need_gap(chars[i - 1], chars[i], space_gap,
                                                                    space_gap * MIXED_RATIO):
                spaces.add(i)
    spaces = {i for i in spaces if _space_allowed(chars, i)}
    out = []
    for i, ch in enumerate(chars):
        if i in spaces:
            out.append(" ")
        out.append(ch)
    body = "".join(out)
    if leader:
        k = _pos_with_spaces(chars, spaces, leader_at)
        body = (body[:k].rstrip() + " " + leader + " " + body[k:].lstrip()).strip()
    return prefix + body


DIGIT_GAP = 0.5  # 숫자 옆(숫자·하이픈 사이)을 띄우는 최소 빈칸 (em)
_NARROW = set("-–~/")


def _need_gap(a: str, b: str, space_gap: float, base: float) -> float:
    """a와 b 사이를 띄우려면 필요한 빈칸 폭 (em). base는 보통 기준(조각 사이는 space_gap, 섞인 자리는 그 0.6배).

    - 여는 괄호 앞: 괄호 글자의 왼쪽 여백이 넓어 낮은 기준으로 보면 '방문취업 (H-2)'처럼 띄운다 → space_gap
    - 숫자-숫자, 숫자-하이픈·물결표: 숫자 글꼴은 폭이 같아서 '1'처럼 좁은 숫자 옆이 0.38em까지 빈다. 실제 PDF로 만든
      스캔본에서 이런 자리의 정답 공백은 0.57em 이상이었다. 고딕 문서는 공백 기준이 0.25em 안팎이라 '1 2', '전-예- 10'처럼
      띄웠다 → DIGIT_GAP
    """
    if (a.isdigit() and (b.isdigit() or b in _NARROW)) or (b.isdigit() and a in _NARROW):
        return max(base, DIGIT_GAP)
    if b in _OPEN:
        return max(base, space_gap)
    return base


_LEADER_TEXT = re.compile(r"[.·ㆍ∙•…‥]*[…‥][.·ㆍ∙•…‥*]*|[.·ㆍ∙•]{2,}\*?")


def _pos_with_spaces(chars: list[str], spaces: set[int], i: int) -> int:
    """공백을 넣은 글에서 i번째 글자(공백 빼고 센 위치)가 놓이는 자리."""
    return max(i, 0) + sum(1 for s in spaces if s < i)


def _is_dot(blob: np.ndarray, em: float) -> bool:
    """잉크가 점 하나만 한가 (가로·세로 모두 0.35em 이하)."""
    ys = np.nonzero(blob.any(axis=1))[0]
    xs = np.nonzero(blob.any(axis=0))[0]
    if len(xs) == 0:
        return True
    return xs[-1] - xs[0] + 1 <= 0.35 * em and ys[-1] - ys[0] + 1 <= 0.35 * em


def _drop_leader(mask: np.ndarray, em: float) -> tuple[np.ndarray, bool]:
    """목차 점선(점 3개 이상이 0.8em 이하 간격으로 1em 넘게 이어진 것)을 줄 그림에서 지운다. (그림, 지웠는가)."""
    xs = np.nonzero(mask.any(axis=0))[0]
    if len(xs) == 0:
        return mask, False
    blobs: list[list[int]] = [[int(xs[0]), int(xs[0])]]
    for a, b in zip(xs, xs[1:]):
        if b - a > 1:
            blobs.append([int(b), int(b)])
        else:
            blobs[-1][1] = int(b)
    dots = [_is_dot(mask[:, a:b + 1], em) for a, b in blobs]
    runs, cur = [], []
    for (a, b), d in zip(blobs, dots):
        if d and cur and a - cur[-1][1] - 1 <= 0.8 * em:
            cur.append((a, b))
            continue
        if len(cur) >= 3 and cur[-1][1] - cur[0][0] + 1 >= em:
            runs.append((cur[0][0], cur[-1][1]))
        cur = [(a, b)] if d else []
    if len(cur) >= 3 and cur[-1][1] - cur[0][0] + 1 >= em:
        runs.append((cur[0][0], cur[-1][1]))
    if not runs:
        return mask, False
    mask = mask.copy()
    for a, b in runs:
        mask[:, a:b + 1] = False
    return mask, True


LEAD_PENALTY = 3.0  # 줄 맨 앞 조각을 OCR이 읽지 않은 그림 기호로 볼 때의 벌점
START_PENALTY = 2.0  # 조각이 쉼표·닫는 괄호 같은 글자로 시작할 때의 벌점 (폭 어긋남 약 1.4배 불확실성만큼)


def _assign(widths: list[float], tiny: list[bool], expected: list[float], spread: list[float],
            starts: list[bool], lead: bool = False) -> tuple[float, list[tuple[int, int]]] | None:
    """글자들을 조각에 차례로 나눠 담는다 (동적 계획법). (비용, 조각마다 담은 글자 범위 (시작, 끝)) — 빈 조각은 시작 = 끝.

    조각 폭과 담은 글자 예상 폭의 차이를, 그 글자들의 폭 불확실성(`width_spread`)으로 나눈 제곱의 합이 가장 작은
    나눔을 고른다. 폭이 불확실한 괄호·따옴표가 든 조각 쪽에 어긋남을 둔다. 점만 한 조각(tiny)은 비울 수 있다.
    조각 폭의 절반~두 배(±1.5em)를 넘는 글자 묶음은 보지 않는다 (긴 줄에서도 빠르게). starts[i]가 거짓인 글자에서
    조각이 시작하면 벌점(START_PENALTY)을 더한다. lead이면 첫 조각을 벌점(LEAD_PENALTY)을 내고 비울 수 있다.
    """
    import bisect

    n, k = len(expected), len(widths)
    ps, pv = [0.0], [0.0]
    for e, s in zip(expected, spread):
        ps.append(ps[-1] + e)
        pv.append(pv[-1] + s * s)
    inf = float("inf")
    cost = [[inf] * (n + 1) for _ in range(k + 1)]
    back = [[0] * (n + 1) for _ in range(k + 1)]
    cost[0][0] = 0.0
    for j in range(1, k + 1):
        w = widths[j - 1]
        for i in range(n + 1):
            best, arg = inf, 0
            if tiny[j - 1] and cost[j - 1][i] < inf:
                best, arg = cost[j - 1][i] + (w / 0.1) ** 2, i
            elif j == 1 and lead and i == 0:
                best, arg = LEAD_PENALTY, 0
            # 담을 글자 폭 (ps[i] - ps[p])이 [w/2 - 1.5, 2w + 1.5] 안에 드는 p만 본다
            lo = bisect.bisect_left(ps, ps[i] - 2 * w - 1.5, 0, i)
            hi = bisect.bisect_right(ps, ps[i] - w / 2 + 1.5, 0, i)
            for p in range(lo, min(hi, i)):
                if cost[j - 1][p] == inf:
                    continue
                d = (ps[i] - ps[p]) - w
                c = cost[j - 1][p] + d * d / (pv[i] - pv[p] + 0.0025) + (0.0 if starts[p] else START_PENALTY)
                if c < best:
                    best, arg = c, p
            cost[j][i], back[j][i] = best, arg
    if cost[k][n] == inf:
        return None
    ranges: list[tuple[int, int]] = [(0, 0)] * k
    i = n
    for j in range(k, 0, -1):
        p = back[j][i]
        ranges[j - 1] = (p, i)
        i = p
    return cost[k][n], ranges


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


# 숫자 뒤에 붙여 쓰는 단위·세는 말. 숫자 뒤 한글이 이것으로 시작하면 빈칸이 넓어도 붙인다 ('2026년도', '2단계')
_UNITS = tuple(sorted("""
년 년도 년간 년차 월 일 시 분 초 주 주년 주차 개 개월 개소 개사 개국 명 인 원 억 만 천 백 조 경 차 차년도 단계 분기 반기
회 회차 호 번 번째 층 대 세 세기 건 권 장 쪽 면 종 곳 박 학년 학기 기 위 등 점 배 퍼센트 평 톤 개년 가지 부 항 절 편
과 과목 시간 시경 동 리 가 로 길 석 판 쇄 군 급 명분 선 칸 줄 쌍 벌 켤레 마리 그루 대째 째 품목 식 본 매 통 병 잔 그램 킬로
""".split(), key=len, reverse=True))


def _space_allowed(chars: list[str], i: int) -> bool:
    """chars[i] 앞에 공백을 둘 수 있는가. 닫는 문장부호 앞, 여는 괄호 뒤에는 공백이 오지 않는다.

    숫자 바로 뒤의 단위('2026년', '8개월', '2단계')도 붙인다: 숫자 글꼴은 옆 여백이 넓어 '2026년' 사이도 공백만큼
    벌어진다 (실제 스캔에서 '2026 년도'의 빈칸 0.38em, 진짜 공백인 제목 '2026 사업계획서'는 0.37em으로 폭으로는 가를 수
    없다). 숫자 뒤가 단위가 아닌 낱말이면 빈칸대로 띄운다.
    """
    a, b = chars[i - 1], chars[i]
    if b in ".,:;)]}%" or a in "([{":
        return False
    # 가운뎃점은 앞뒤 낱말에 붙여 쓴다 ('대내·외', '사업·시설'). 점 둘레의 여백이 넓어 빈칸으로는 가를 수 없다.
    # 줄 맨 앞의 점은 개조식 기호라 뒤를 띄운다
    if b in _DOTS or (a in _DOTS and i > 1):
        return False
    if a.isdigit() and "가" <= b <= "힣":
        rest = "".join(chars[i:i + 4])
        return not any(rest.startswith(u) for u in _UNITS)
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
    """OCR 상자마다 그 안의 빈칸 폭들. 문서 전체의 공백 기준(`space_threshold`)을 정할 때 모은다.

    상자 사이의 빈칸은 넣지 않는다. 표 영역은 여러 칸의 상자가 한 줄로 묶여 칸 사이 빈칸(0.4~1em)이 섞이고,
    그러면 기준이 올라가 공백을 놓쳤다 (교육 과정 표: 0.42em, 맞는 값 0.27em 안팎).
    """
    out: list[float] = []
    for c in cells:
        out += line_gaps(page.crop(c.box), (c.box[3] - c.box[1]) * SCALE)
    return out


def _between_boxes_cleared(mask: np.ndarray, line: list[OcrCell], box) -> np.ndarray:
    """한 줄의 OCR 상자와 상자 사이(어느 상자에도 들지 않는 곳)의 잉크를 지운다.

    줄이 표의 두 칸에 걸치면 그 사이의 세로 괘선 조각(위아래가 끊겨 괘선으로 지워지지 않은 것)이 글자 조각으로 잡혀
    글자와 조각의 짝이 밀렸다 ('구분 제 품(인체보호기 준대 비%)'). 상자 사이에는 OCR이 읽은 글자가 없으므로 지워도 된다.
    """
    if len(line) < 2:
        return mask
    mask = mask.copy()
    for a, b in zip(line, line[1:]):
        x0, x1 = int(round((a.box[2] - box[0]) * SCALE)), int(round((b.box[0] - box[0]) * SCALE))
        if x1 > x0:
            mask[:, max(x0, 0):max(x1, 0)] = False
    return mask


def respace_cells(page: PageImage, cells: list[OcrCell],
                  space_gap: float = SPACE_GAP) -> list[tuple[str | None, str]]:
    """OCR 상자들을 줄로 묶어 줄마다 (공백을 되살린 글 또는 None, OCR 글 그대로)."""
    out = []
    for line in group_lines(cells):
        box = _line_box(line)
        text = "".join(c.text for c in line)
        mask = _between_boxes_cleared(page.crop(box), line, box)
        h_px = (box[3] - box[1]) * SCALE
        out.append((respace_line(mask, text, h_px, space_gap), " ".join(c.text for c in line)))
    return out
