"""요소별 평가 지표.

모든 지표는 '정답 블록 목록'과 '예측 블록 목록'을 비교한다. 블록은 다음 형식의 dict다.

    {"type": "heading", "level": 1, "text": "Ⅰ. 사업 개요"}
    {"type": "paragraph", "text": "..."}
    {"type": "table", "rows": [[{"text": "구분", "rowspan": 2, "colspan": 1}, ...], ...]}

지표
- 글자 오류율(CER): 공백을 뺀 전체 글의 편집 거리 / 정답 글자 수. 글자를 빠뜨리거나 잘못 읽은 정도
- 어절 F1: 띄어쓰기 단위(어절)가 정답과 같은 비율. 띄어쓰기 복원 품질
- 읽기 순서: 정답 블록과 예측 블록을 글 내용으로 짝지은 뒤, 순서가 맞는 쌍의 비율 (켄달 타우를 0~1로)
- 블록 재현율: 정답 블록 중 예측에서 찾은 비율
- 제목: 정답 제목을 예측 제목으로 찾은 비율(재현율)과 예측 제목 중 정답 제목인 비율(정밀도), 단계 일치율
- 표 TEDS: 표를 HTML 트리(table → tr → td)로 보고 트리 편집 거리로 잰 유사도 (PubTabNet 지표).
  TEDS-S는 칸 글을 무시한 구조만의 유사도
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein


def norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace(" ", " ").replace("　", " ")
    text = re.sub(r"[○◦❍◯]", "○", text)  # ○ ◦ ❍ ◯ 은 같은 기호로 본다
    return " ".join(text.split())


def block_text(b: dict) -> str:
    if b["type"] == "table":
        return " ".join(c["text"] for row in b["rows"] for c in row)
    return b.get("text", "")


def full_text(blocks: list[dict]) -> str:
    return norm(" ".join(block_text(b) for b in blocks))


# ---------------------------------------------------------------------------
# 글
# ---------------------------------------------------------------------------


def cer(truth: list[dict], pred: list[dict]) -> float:
    t = full_text(truth).replace(" ", "")
    p = full_text(pred).replace(" ", "")
    return Levenshtein.distance(t, p) / max(len(t), 1)


def eojeol_f1(truth: list[dict], pred: list[dict]) -> float:
    t, p = Counter(full_text(truth).split()), Counter(full_text(pred).split())
    common = sum((t & p).values())
    if not common:
        return 0.0
    precision, recall = common / sum(p.values()), common / sum(t.values())
    return 2 * precision * recall / (precision + recall)


# ---------------------------------------------------------------------------
# 블록 짝짓기, 읽기 순서, 제목
# ---------------------------------------------------------------------------


def match_blocks(truth: list[dict], pred: list[dict], threshold: float = 80) -> list[tuple[int, int]]:
    """글 내용이 가장 비슷한 블록끼리 일대일로 짝짓는다 (표 제외). (정답 번호, 예측 번호) 목록."""
    tb = [(i, norm(b["text"]).replace(" ", "")) for i, b in enumerate(truth) if b["type"] != "table"]
    pb = [(j, norm(b["text"]).replace(" ", "")) for j, b in enumerate(pred) if b["type"] != "table"]
    scores = []
    for i, t in tb:
        if not t:
            continue
        for j, p in pb:
            if p and abs(len(p) - len(t)) <= max(len(t), len(p)) * 0.5:
                s = fuzz.ratio(t, p)
                if s >= threshold:
                    scores.append((s, -abs(i - j), i, j))
    scores.sort(reverse=True)
    used_t, used_p, pairs = set(), set(), []
    for _, _, i, j in scores:
        if i not in used_t and j not in used_p:
            used_t.add(i)
            used_p.add(j)
            pairs.append((i, j))
    return sorted(pairs)


def reading_order(pairs: list[tuple[int, int]]) -> float:
    """짝지은 블록 중 순서가 맞는 쌍의 비율."""
    order = [j for _, j in sorted(pairs)]
    n = len(order)
    if n < 2:
        return 1.0
    concordant = sum(1 for a in range(n) for b in range(a + 1, n) if order[a] < order[b])
    return concordant / (n * (n - 1) / 2)


def block_recall(truth: list[dict], pairs) -> float:
    n = sum(1 for b in truth if b["type"] != "table" and norm(b["text"]))
    return len(pairs) / max(n, 1)


def heading_scores(truth: list[dict], pred: list[dict], pairs) -> dict:
    t_head = {i for i, b in enumerate(truth) if b["type"] == "heading"}
    p_head = {j for j, b in enumerate(pred) if b["type"] == "heading"}
    paired = dict(pairs)
    hit = [i for i in t_head if paired.get(i) in p_head]
    recall = len(hit) / max(len(t_head), 1)
    rev = {j: i for i, j in pairs}
    precision = sum(1 for j in p_head if rev.get(j) in t_head) / max(len(p_head), 1)
    # 단계: 정답과 예측 모두 제목인 것들끼리, 정답 단계가 같은 제목은 예측 단계도 같고 다른 것은 다르게 나뉘었는가
    lv = [(truth[i]["level"], pred[paired[i]].get("level", 0)) for i in hit]
    agree = total = 0
    for a in range(len(lv)):
        for b in range(a + 1, len(lv)):
            total += 1
            agree += (lv[a][0] == lv[b][0]) == (lv[a][1] == lv[b][1])
    return {"recall": recall, "precision": precision, "level": agree / total if total else 1.0}


# ---------------------------------------------------------------------------
# 표 (TEDS)
# ---------------------------------------------------------------------------


class _Node:
    def __init__(self, tag: str, text: str = "", colspan: int = 1, rowspan: int = 1, children=None):
        self.tag, self.text, self.colspan, self.rowspan = tag, text, colspan, rowspan
        self.children = children or []


def _tree(table: dict, structure_only: bool) -> _Node:
    rows = []
    for row in table["rows"]:
        cells = [_Node("td", "" if structure_only else norm(c["text"]).replace(" ", ""),
                       c.get("colspan", 1), c.get("rowspan", 1)) for c in row]
        rows.append(_Node("tr", children=cells))
    return _Node("table", children=rows)


def _count(n: _Node) -> int:
    return 1 + sum(_count(c) for c in n.children)


def teds(truth_table: dict, pred_table: dict, structure_only: bool = False) -> float:
    from apted import APTED, Config

    class TedsConfig(Config):
        def rename(self, a, b):
            if a.tag != b.tag or a.colspan != b.colspan or a.rowspan != b.rowspan:
                return 1.0
            if a.tag == "td" and (a.text or b.text):
                return Levenshtein.normalized_distance(a.text, b.text)
            return 0.0

        def children(self, node):
            return node.children

    t, p = _tree(truth_table, structure_only), _tree(pred_table, structure_only)
    dist = APTED(t, p, TedsConfig()).compute_edit_distance()
    return 1.0 - dist / max(_count(t), _count(p))


def table_scores(truth: list[dict], pred: list[dict]) -> dict:
    """정답 표마다 가장 비슷한 예측 표와 짝지어 TEDS를 잰다. 못 찾은 표는 0점."""
    t_tables = [b for b in truth if b["type"] == "table"]
    p_tables = [b for b in pred if b["type"] == "table"]
    if not t_tables:
        return {}
    used, teds_all, teds_s = set(), [], []
    for tt in t_tables:
        best, best_j = 0.0, None
        tt_text = norm(block_text(tt))
        for j, pt in enumerate(p_tables):
            if j in used:
                continue
            s = fuzz.token_set_ratio(tt_text, norm(block_text(pt)))
            if s > best:
                best, best_j = s, j
        if best_j is None or best < 50:
            teds_all.append(0.0)
            teds_s.append(0.0)
            continue
        used.add(best_j)
        teds_all.append(teds(tt, p_tables[best_j]))
        teds_s.append(teds(tt, p_tables[best_j], structure_only=True))
    return {"teds": sum(teds_all) / len(teds_all), "teds_s": sum(teds_s) / len(teds_s),
            "found": sum(1 for x in teds_s if x > 0) / len(teds_s)}


def evaluate(truth: list[dict], pred: list[dict]) -> dict:
    pairs = match_blocks(truth, pred)
    out = {
        "cer": cer(truth, pred),
        "eojeol_f1": eojeol_f1(truth, pred),
        "block_recall": block_recall(truth, pairs),
        "reading_order": reading_order(pairs),
        "heading": heading_scores(truth, pred, pairs),
    }
    out.update({f"table_{k}": v for k, v in table_scores(truth, pred).items()})
    return out
