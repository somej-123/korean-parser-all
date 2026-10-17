"""Word 수식(OMML, m:oMath) → LaTeX 문자열.

Word·LibreOffice가 DOCX에 쓰는 수식은 글자(m:r/m:t)와 구조 요소(위·아래 첨자, 분수, 근호, 큰 연산자, 괄호, 행렬 …)의
나무다. 화면에 그리지는 않고(수식 렌더링은 v1.0 목표 밖), 원문 수식을 잃지 않도록 LaTeX 문자열로 옮겨 Equation에 둔다.
모르는 요소는 안의 글을 그대로 잇는다.
"""

from __future__ import annotations

M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"

_NARY = {"∑": r"\sum", "∏": r"\prod", "∐": r"\coprod", "∫": r"\int", "∬": r"\iint", "∭": r"\iiint", "∮": r"\oint",
         "⋃": r"\bigcup", "⋂": r"\bigcap", "⋁": r"\bigvee", "⋀": r"\bigwedge"}
_ACC = {"̂": r"\hat", "̃": r"\tilde", "̇": r"\dot", "̈": r"\ddot", "̄": r"\bar",
        "̅": r"\bar", "⃗": r"\vec", "̆": r"\breve", "̌": r"\check"}


def _ln(el) -> str:
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else ""


def _val(el, path: str, default: str | None = None) -> str | None:
    """요소 아래 path(예: 'naryPr/chr')의 m:val 값."""
    node = el
    for part in path.split("/"):
        node = node.find(M + part) if node is not None else None
    return node.get(M + "val", default) if node is not None else default


def _kid(el, name: str):
    return el.find(M + name)


def _arg(el, name: str) -> str:
    k = _kid(el, name)
    return omml_to_latex(k) if k is not None else ""


def omml_to_latex(el) -> str:
    name = _ln(el)
    if name == "r":
        return "".join(t.text or "" for t in el.iter(M + "t"))
    if name == "sSup":
        return f"{{{_arg(el, 'e')}}}^{{{_arg(el, 'sup')}}}"
    if name == "sSub":
        return f"{{{_arg(el, 'e')}}}_{{{_arg(el, 'sub')}}}"
    if name == "sSubSup":
        return f"{{{_arg(el, 'e')}}}_{{{_arg(el, 'sub')}}}^{{{_arg(el, 'sup')}}}"
    if name == "sPre":
        return f"{{}}_{{{_arg(el, 'sub')}}}^{{{_arg(el, 'sup')}}}{{{_arg(el, 'e')}}}"
    if name == "f":
        if _val(el, "fPr/type") == "lin":
            return f"{_arg(el, 'num')}/{_arg(el, 'den')}"
        return rf"\frac{{{_arg(el, 'num')}}}{{{_arg(el, 'den')}}}"
    if name == "rad":
        deg = _arg(el, "deg")
        hide = _val(el, "radPr/degHide") in ("1", "on", "true")
        return rf"\sqrt[{deg}]{{{_arg(el, 'e')}}}" if deg and not hide else rf"\sqrt{{{_arg(el, 'e')}}}"
    if name == "nary":
        op = _NARY.get(_val(el, "naryPr/chr", "∫") or "∫", _val(el, "naryPr/chr", "∫"))
        sub, sup = _arg(el, "sub"), _arg(el, "sup")
        return op + (f"_{{{sub}}}" if sub else "") + (f"^{{{sup}}}" if sup else "") + f"{{{_arg(el, 'e')}}}"
    if name == "d":
        beg = _val(el, "dPr/begChr", "(")
        end = _val(el, "dPr/endChr", ")")
        sep = _val(el, "dPr/sepChr", "|")
        inner = f" {sep} ".join(omml_to_latex(e) for e in el.findall(M + "e"))
        return f"\\left{beg or '.'}{inner}\\right{end or '.'}"
    if name == "func":
        return f"{_arg(el, 'fName')} {_arg(el, 'e')}".strip()
    if name == "acc":
        return f"{_ACC.get(_val(el, 'accPr/chr', chr(0x302)) or '', r'\hat')}{{{_arg(el, 'e')}}}"
    if name == "bar":
        cmd = r"\underline" if _val(el, "barPr/pos") == "bot" else r"\overline"
        return f"{cmd}{{{_arg(el, 'e')}}}"
    if name == "groupChr":
        cmd = r"\overbrace" if _val(el, "groupChrPr/pos") == "top" else r"\underbrace"
        return f"{cmd}{{{_arg(el, 'e')}}}"
    if name == "limLow":
        return f"{{{_arg(el, 'e')}}}_{{{_arg(el, 'lim')}}}"
    if name == "limUpp":
        return f"{{{_arg(el, 'e')}}}^{{{_arg(el, 'lim')}}}"
    if name == "eqArr":
        return r" \\ ".join(omml_to_latex(e) for e in el.findall(M + "e"))
    if name == "m":
        rows = [" & ".join(omml_to_latex(e) for e in mr.findall(M + "e")) for mr in el.findall(M + "mr")]
        return r"\begin{matrix}" + r" \\ ".join(rows) + r"\end{matrix}"
    if name.endswith("Pr") or name in ("ctrlPr", "rPr"):
        return ""
    # m:oMath, m:e, m:num, m:den, m:sub, m:sup, m:deg, m:fName, m:lim, m:box, m:borderBox … : 안의 것을 잇는다
    return "".join(omml_to_latex(k) for k in el if isinstance(k.tag, str))
