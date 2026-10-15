"""띄어쓰기 복원과 줄 합치기.

PDF에서 한국어를 꺼내면 두 가지 문제가 자주 생긴다.

1. 띄어쓰기 소실: "본문서는2026년도사업추진방향과..."
2. 줄바꿈 자리의 잘못된 공백: 줄이 어절 중간("진행 상|황을")에서 바뀌었는데
   줄을 합칠 때 공백을 넣어 "진행 상 황을"이 된다.

형태소 분석기 Kiwi의 띄어쓰기 교정을 쓰되, Kiwi가 과하게 띄우는 경우를
형태소 정보로 걸러낸다.

- 한 형태소 안은 띄우지 않는다 ("문서파싱" → "문서 파 싱" 방지)
- 조사·어미·접미사 앞은 띄우지 않는다 ("보고합니다" → "보고 합니다" 방지)
- 숫자 바로 뒤는 띄우지 않는다 ("1단계" → "1 단계" 방지)
- 새로 띄운 결과 한 글자짜리 명사 조각이 생기면 되돌린다 ("파싱" → "파 싱" 방지)
- 한글 뒤 문장부호 다음에 빠진 공백을 채운다 ("이며,각" → "이며, 각")
- reset=False일 때는 비정상적으로 긴 어절만 고친다 ("요구사항" 같은 정상 복합어 보존)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from .normalize import is_hangul_syllable

# 이 품사로 시작하는 자리 앞에는 공백을 새로 넣지 않는다.
# J*: 조사, E*: 어미, XS*: 접미사, VCP: 긍정 지정사(이다), VCN: 부정 지정사(아니다)
_NO_SPACE_BEFORE_TAGS = ("J", "E", "XS", "VCP", "VCN")

# 한 글자로 혼자 띄어 써도 자연스러운 품사: 관형사(본, 각), 부사(더, 잘), 접속부사(및),
# 의존명사(수, 것, 등), 대명사, 수사, 감탄사
_STANDALONE_SINGLE_TAGS = ("MM", "MAG", "MAJ", "NNB", "NP", "NR", "IC")

# 정상적인 한국어 문서의 어절은 대부분 6음절 이하다.
_LONG_EOJEOL = 7
_HANGUL_RUN = re.compile(r"[가-힣]+")

# OCR 결과에서 흔한 "이며,각" "합니다.본" 처럼 문장부호 뒤 공백이 빠진 경우.
# 왼쪽이 한글일 때만 적용하므로 "1.5", "3:1" 같은 숫자 표기는 건드리지 않는다.
_PUNCT_NO_SPACE = re.compile(r"(?<=[가-힣])([.,!?;:])(?=[가-힣\d])")


@dataclass
class SpacingStats:
    eojeols: int
    long_eojeols: int

    @property
    def long_ratio(self) -> float:
        return self.long_eojeols / self.eojeols if self.eojeols else 0.0


def spacing_stats(text: str) -> SpacingStats:
    """한글이 포함된 어절 중 비정상적으로 긴 어절의 비율을 센다."""
    eojeols = [w for w in text.split() if _HANGUL_RUN.search(w)]
    long_ = [w for w in eojeols if max(map(len, _HANGUL_RUN.findall(w))) >= _LONG_EOJEOL]
    return SpacingStats(len(eojeols), len(long_))


def spacing_lost(text: str, threshold: float = 0.15) -> bool:
    """띄어쓰기가 대량으로 사라진 텍스트인지 판단한다.

    문서를 만든 도구에 따라 문서 전체에서 일관되게 생기는 문제라서,
    문단 하나보다 문서 전체 텍스트로 판단하는 것이 정확하다.
    """
    stats = spacing_stats(text)
    return stats.eojeols >= 5 and stats.long_ratio >= threshold


class SpacingFixer:
    """Kiwi 기반 띄어쓰기 교정기. 모델 로딩에 1~2초 걸리므로 한 번 만들어 재사용한다."""

    def __init__(self, kiwi=None):
        self._kiwi = kiwi

    @cached_property
    def kiwi(self):
        if self._kiwi is None:
            from kiwipiepy import Kiwi

            self._kiwi = Kiwi()
        return self._kiwi

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def respace(self, text: str, reset: bool = False) -> str:
        """띄어쓰기를 교정한다.

        reset=False: 빠진 공백만 채운다. 기존 공백은 그대로 둔다. (안전한 기본값)
        reset=True:  기존 공백을 무시하고 다시 띄운다. 잘못 들어간 공백까지 고치지만,
                     띄어쓰기가 대량으로 사라진 텍스트(spacing_lost)에만 쓰는 것이 좋다.
        """
        if not text or not _HANGUL_RUN.search(text):
            return text
        text = _PUNCT_NO_SPACE.sub(r"\1 ", text)
        return "\n".join(self._respace_line(line, reset) for line in text.split("\n"))

    def join_lines(self, lines: list[str]) -> str:
        """한 문단에 속한 줄들을 합친다.

        한글과 한글 사이에서 줄이 바뀐 경우, 그 자리가 어절 경계인지는 알 수 없다.
        일단 붙여서 합친 뒤 형태소 분석으로 필요한 곳에만 공백을 넣는다.
        """
        text = ""
        joins: set[int] = set()  # 한글-한글로 붙인 자리 (공백을 뺀 글자 기준 위치)
        for line in (ln.strip() for ln in lines):
            if not line:
                continue
            if not text:
                text = line
            elif is_hangul_syllable(text[-1]) and is_hangul_syllable(line[0]):
                joins.add(len(text.replace(" ", "")))
                text += line
            elif text.endswith("-") and text[-2:-1].isascii() and text[-2:-1].isalpha() and line[0].isascii():
                text = text[:-1] + line  # 영어 하이픈 줄바꿈: "infor-\nmation"
            else:
                text += " " + line
        if not text or not _HANGUL_RUN.search(text):
            return text
        return self._respace_line(text, reset=False, extra_editable=joins)

    # ------------------------------------------------------------------
    # 내부 구현
    # ------------------------------------------------------------------

    def _respace_line(self, line: str, reset: bool, extra_editable: set[int] = frozenset()) -> str:
        if not _HANGUL_RUN.search(line):
            return line

        # 공백을 뺀 글자열과, 원래 공백이 있던 위치(그 글자 앞)를 기록한다.
        chars: list[str] = []
        orig_spaces: set[int] = set()
        for ch in line:
            if ch == " ":
                orig_spaces.add(len(chars))
            else:
                chars.append(ch)
        stripped = "".join(chars)

        candidate = self.kiwi.space(line, reset_whitespace=reset)
        cand_spaces = _space_positions(candidate)

        tokens = self.kiwi.tokenize(stripped)
        token_starts = {t.start: t.tag for t in tokens}
        tag_at = {i: t.tag for t in tokens for i in range(t.start, t.start + t.len)}
        inside_token = {i for t in tokens for i in range(t.start + 1, t.start + t.len)}
        editable = None if reset else _long_eojeol_positions(stripped, orig_spaces) | extra_editable

        spaces = set(orig_spaces) if not reset else set()
        new_spaces: set[int] = set()
        for pos in cand_spaces:
            if pos in orig_spaces:
                spaces.add(pos)
                continue
            # 새로 넣으려는 공백: 보호 규칙에 걸리면 넣지 않는다.
            if editable is not None and pos not in editable:
                continue
            if pos in inside_token:
                continue
            if token_starts.get(pos, "").startswith(_NO_SPACE_BEFORE_TAGS):
                continue
            if 0 < pos < len(stripped) and stripped[pos - 1].isdigit() and is_hangul_syllable(stripped[pos]):
                continue
            spaces.add(pos)
            new_spaces.add(pos)

        if reset:
            # 원래 있던 공백 중 Kiwi가 지운 것은, 한글-한글 또는 숫자-한글 사이일 때만 지운다.
            # (영어, 문장부호 주변의 공백은 원문을 따른다.)
            for pos in orig_spaces - cand_spaces:
                left = stripped[pos - 1] if pos > 0 else ""
                right = stripped[pos] if pos < len(stripped) else ""
                if not ((is_hangul_syllable(left) or left.isdigit()) and is_hangul_syllable(right)):
                    spaces.add(pos)

        _merge_single_fragments(stripped, spaces, new_spaces, tag_at)

        out = []
        for i, ch in enumerate(stripped):
            if i in spaces and i > 0:
                out.append(" ")
            out.append(ch)
        return "".join(out)


def _space_positions(text: str) -> set[int]:
    """공백을 뺀 글자열 기준으로, 공백이 앞에 붙은 글자의 위치를 돌려준다."""
    positions: set[int] = set()
    n = 0
    for ch in text:
        if ch == " ":
            positions.add(n)
        else:
            n += 1
    return positions


def _long_eojeol_positions(stripped: str, orig_spaces: set[int]) -> set[int]:
    """원래 공백 기준으로 나눈 어절 중, 한글이 비정상적으로 길게 이어진 어절의 내부 위치."""
    positions: set[int] = set()
    bounds = sorted({0, len(stripped), *orig_spaces})
    for start, end in zip(bounds, bounds[1:]):
        word = stripped[start:end]
        if any(len(run) >= _LONG_EOJEOL for run in _HANGUL_RUN.findall(word)):
            positions.update(range(start + 1, end))
    return positions


def _merge_single_fragments(
    stripped: str, spaces: set[int], new_spaces: set[int], tag_at: dict[int, str]
) -> None:
    """새로 넣은 공백 때문에 한 글자짜리 명사 조각이 생기면 그 공백을 되돌린다.

    Kiwi가 모르는 외래어("파싱")를 한 글자씩 쪼개는 경우를 막는다.
    """
    changed = True
    while changed:
        changed = False
        bounds = sorted({0, len(stripped), *spaces})
        for start, end in zip(bounds, bounds[1:]):
            if end - start != 1 or not is_hangul_syllable(stripped[start]):
                continue
            if tag_at.get(start, "").startswith(_STANDALONE_SINGLE_TAGS):
                continue
            # 뒤쪽 공백이 새로 넣은 것이면 뒤와 붙이고, 아니면 앞쪽을 확인한다.
            for pos in (end, start):
                if pos in new_spaces and pos in spaces:
                    spaces.discard(pos)
                    changed = True
                    break
            if changed:
                break
