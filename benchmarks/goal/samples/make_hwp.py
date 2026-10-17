"""v1.0 기준 3 확인용 HWPX·HWP 표본을 한글(한컴오피스)로 만들고 '들어 있어야 할 요소 목록'(manifest)을 쓴다.

한글을 자동 조작(COM, HWPFrame.HwpObject)해서 문서를 작성한다. 우리 파서와 무관한 한글이 만든 진짜 파일이어야 시험이
의미가 있기 때문이다 (XML을 손으로 쓰면 파서가 기대하는 모양만 만들게 된다). 단계마다 한글 명령의 성공 여부를 기록하고,
성공한 요소만 목록에 넣는다.

저장 (2026-10-17, 한컴오피스 2022에서 확인한 것):
- 자동 조작의 저장(SaveAs)은 보안 승인 모듈이 없으면 '파일 접근 승인'을 묻는데, 자동 조작 한글은 창이 화면에 나오지
  않아 승인 창도 보이지 않고 멈췄다. 보안 승인 모듈 등록은 보안 설정이라 이 스크립트는 하지 않는다.
- 그래서 기본은 작성한 문서를 한글 XML(HML) 글로 꺼내 둔다 (GetTextFile, 파일 접근이 없어 묻지 않는다). 화면에 띄운
  한글에서 그 .hml을 열어 '다른 이름으로 저장'으로 name_hwpx.hwpx(한글 표준 문서)와 name_hwp.hwp(한글 문서)를 만든다.
  저장은 한글이 하므로 진짜 한글 파일이다.

    python benchmarks/goal/samples/make_hwp.py          # 작성 → .hml과 요소 목록. 저장은 화면의 한글에서
    python benchmarks/goal/samples/make_hwp.py --save   # 보안 승인 모듈이 등록된 환경: 스크립트가 바로 저장
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))


class Hwp:
    def __init__(self):
        import win32com.client as w

        self.h = w.gencache.EnsureDispatch("HWPFrame.HwpObject")
        self.h.XHwpWindows.Item(0).Visible = True  # 승인 창을 사람이 볼 수 있게
        self.log: list[tuple[str, bool]] = []

    def run(self, name: str) -> bool:
        return bool(self.h.HAction.Run(name))

    def act(self, name: str, set_name: str, **items) -> bool:
        pset = getattr(self.h.HParameterSet, set_name)
        self.h.HAction.GetDefault(name, pset.HSet)
        for k, v in items.items():
            setattr(pset, k, v)
        return bool(self.h.HAction.Execute(name, pset.HSet))

    def text(self, s: str) -> bool:
        return self.act("InsertText", "HInsertText", Text=s)

    def para(self, s: str = "", style: int | None = None) -> bool:
        ok = True
        if style is not None:
            ok = self.act("Style", "HStyle", Apply=style)
        if s:
            ok = self.text(s) and ok
        return self.run("BreakPara") and ok

    def note(self, what: str, ok: bool) -> bool:
        self.log.append((what, ok))
        print(("  ok  " if ok else "  FAIL") + " " + what, flush=True)
        return ok

    def footnote(self, s: str) -> bool:
        ok = self.run("InsertFootnote") and self.text(s)
        self.run("CloseEx")
        return ok

    def header(self, page_type: int, s: str) -> bool:
        # HeaderFooter의 type은 쪽 종류다: 0 양쪽, 1 짝수 쪽 (저장된 HWPX의 applyPageType으로 확인). 머리말이 만들어진다.
        # 꼬리말을 고르는 값은 형식 라이브러리에 없어 아직 만들지 못한다 (꼬리말은 DOCX 표본과 HWPX 시험 파일이 다룬다)
        ok = self.act("HeaderFooter", "HHeaderFooter", type=page_type) and self.text(s)
        self.run("CloseEx")
        return ok

    def link(self, text: str, url: str) -> bool:
        return self.act("InsertHyperlink", "HHyperLink", Text=text, Command=f"{url};1;0;0;")

    def equation(self, script: str) -> bool:
        ok = self.act("EquationCreate", "HEqEdit", string=script, TreatAsChar=1)
        self.run("Cancel")
        self.run("MoveRight")
        return ok

    def picture(self, path: str) -> bool:
        ok = self.h.InsertPicture(path, True, 0) is not None
        self.run("Cancel")
        self.run("MoveRight")
        return ok

    def table(self, rows: int, cols: int, merges: list[tuple[str, int]], cells: list[str]) -> bool:
        """표를 만들고 병합한 뒤 칸을 읽는 순서대로 채운다. merges: (방향 'down'·'right', 늘릴 칸 수) — 처음 칸부터 차례로."""
        ok = self.act("TableCreate", "HTableCreation", Rows=rows, Cols=cols, WidthType=2, HeightType=0)
        for i, (direction, n) in enumerate(merges):
            if i:
                ok = self.run("TableRightCell") and ok
            self.run("TableCellBlock")
            self.run("TableCellBlockExtend")
            for _ in range(n):
                self.run("TableLowerCell" if direction == "down" else "TableRightCell")
            ok = self.run("TableMergeCell") and ok
            self.run("Cancel")
        for _ in range(len(merges) - 1):
            self.run("TableLeftCell")
        for i, c in enumerate(cells):
            if i:
                self.run("TableRightCell")
            if c:
                ok = self.text(c) and ok
        self.run("Cancel")
        self.run("MoveDocEnd")
        return self.run("BreakPara") and ok

    def save(self, base: Path) -> bool:
        ok = bool(self.h.SaveAs(str(base.with_suffix(".hwpx")), "HWPX", ""))
        ok = bool(self.h.SaveAs(str(base.with_suffix(".hwp")), "HWP", "")) and ok
        return ok

    def new(self) -> None:
        self.h.HAction.Run("FileNew")

    def close(self) -> None:
        try:
            self.h.Clear(1)
            self.h.Quit()
        except Exception:  # noqa: BLE001 - 이미 닫힌 창 정리 중의 COM 오류
            pass


def _png(folder: str) -> str:
    from make_docx import _png as png_b64

    path = Path(folder) / "diagram.png"
    path.write_bytes(base64.b64decode(png_b64()))
    return str(path)


def make_rfp(hwp: Hwp, png: str) -> dict:
    """제안요청서: 개요 문단 제목 3단계, 개조식 줄, 병합 표, 각주, 머리말(양쪽·짝수 쪽), 링크, 그림, 수식."""
    m: dict = {"headings": [], "list_items": [], "texts": []}
    m["header"] = []
    if hwp.note("머리말 (양쪽)", hwp.header(0, "공공 사업 제안요청서")):
        m["header"].append("공공 사업 제안요청서")
    if hwp.note("머리말 (짝수 쪽)", hwp.header(1, "- 대외비 -")):
        m["header"].append("- 대외비 -")
    hwp.para("2026년 문서 관리 시스템 구축 제안요청서", style=0)
    m["texts"].append("2026년 문서 관리 시스템 구축 제안요청서")
    # 한글 기본 스타일 번호: 0 바탕글, 1 본문, 2 개요 1, 3 개요 2, 4 개요 3
    for text, level in (("사업 개요", 1), ("추진 배경", 2)):
        if hwp.note(f"개요 {level}: {text}", hwp.para(text, style=1 + level)):
            m["headings"].append([text, level])
    hwp.act("Style", "HStyle", Apply=0)
    hwp.text("기관 내부 문서가 여러 시스템에 흩어져 있어 검색과 재사용이 어렵다")
    if hwp.note("각주 1", hwp.footnote("2025년 정보화 실태 조사 결과를 따른다.")):
        m.setdefault("footnotes", []).append("2025년 정보화 실태 조사 결과를 따른다.")
    hwp.para(".")
    for marker, text in (("□", "현행 시스템 노후화"), ("○", "서버 도입 후 9년 경과"), ("-", "운영체제 지원 종료"),
                         ("□", "문서 형식 다양화")):
        if hwp.note(f"개조식 {marker}", hwp.para(f"{marker} {text}")):
            m["list_items"].append([marker, text])
    for text, level in (("사업 범위", 2), ("예산", 3)):
        if hwp.note(f"개요 {level}: {text}", hwp.para(text, style=1 + level)):
            m["headings"].append([text, level])
    hwp.act("Style", "HStyle", Apply=0)
    hwp.para("(단위: 백만원)")
    m["texts"].append("(단위: 백만원)")
    # 칸 이동(TableRightCell)은 둘째 줄 첫 칸에서 위의 병합 칸('구분')으로 다시 들어간다: 그 자리는 빈 글
    cells = ["구분", "2026년", "", "상반기", "하반기", "합계", "인건비", "120", "180", "300", "장비비", "80", "40", "120"]
    if hwp.note("병합 표", hwp.table(4, 4, [("down", 1), ("right", 2)], cells)):
        m["tables"] = [{"rows": 4, "cols": 4, "spans": [[0, 0, 2, 1], [0, 1, 1, 3]]}]
        m["texts"] += ["인건비", "장비비"]
    hwp.text("총 사업비는 4억 2천만원이다")
    if hwp.note("각주 2", hwp.footnote("부가가치세를 포함한 금액이다.")):
        m.setdefault("footnotes", []).append("부가가치세를 포함한 금액이다.")
    hwp.para(".")
    if hwp.note("개요 1: 제안 안내", hwp.para("제안 안내", style=2)):
        m["headings"].append(["제안 안내", 1])
    hwp.act("Style", "HStyle", Apply=0)
    hwp.text("입찰 공고는 ")
    if hwp.note("링크", hwp.link("국가종합전자조달시스템", "https://www.g2b.go.kr")):
        m["links"] = [["국가종합전자조달시스템", "https://www.g2b.go.kr"]]
    hwp.para("에서 확인한다.")
    hwp.text("시스템 구성도: ")
    if hwp.note("그림", hwp.picture(png)):
        m["images"] = 1
    hwp.run("BreakPara")
    hwp.text("성능 지표: ")
    if hwp.note("수식", hwp.equation("E = mc ^{2}")):
        m["equations"] = ["mc^{2}"]
    hwp.run("BreakPara")
    return m


def make_letter(hwp: Hwp) -> dict:
    """공문: 번호 줄(1. 가.), 표 양식, 짝수 쪽 머리말."""
    m: dict = {"list_items": [], "texts": []}
    if hwp.note("머리말 (짝수 쪽)", hwp.header(1, "정보화담당관실")):
        m["header"] = ["정보화담당관실"]
    hwp.para("수신  각 부서장", style=0)
    hwp.para("제목  문서 관리 시스템 사용자 교육 안내")
    m["texts"] += ["수신  각 부서장", "문서 관리 시스템 사용자 교육 안내"]
    for marker, text in (("1.", "관련: 정보화담당관-1234(2026. 3. 2.)"), ("2.", "교육 일정을 다음과 같이 알립니다."),
                         ("가.", "일시: 2026. 4. 1.(수) 14:00"), ("나.", "장소: 본관 대강당")):
        if hwp.note(f"번호 {marker}", hwp.para(f"{marker} {text}")):
            m["list_items"].append([marker, text])
    cells = ["교육 대상", "부서별 문서 담당자", "인원", "부서당 2명", "준비물", "개인 노트북"]
    if hwp.note("양식 표", hwp.table(3, 2, [], cells)):
        m["tables"] = [{"rows": 3, "cols": 2, "spans": []}]
        m["texts"] += ["부서별 문서 담당자", "개인 노트북"]
    hwp.para("끝.")
    m["texts"].append("끝.")
    return m


def main() -> None:
    """기본: 작성한 문서를 .hml로 꺼낸다. --save: 스크립트가 바로 저장한다 (모듈 docstring의 '저장' 참고)."""
    save = "--save" in sys.argv
    hml = not save
    hwp = Hwp()
    names = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            png = _png(tmp)
            for name, make in (("hwp_rfp", lambda: make_rfp(hwp, png)), ("hwp_letter", lambda: make_letter(hwp))):
                print(f"== {name}", flush=True)
                hwp.new()
                manifest = {k: v for k, v in make().items() if v}
                for ext in ("hwpx", "hwp"):
                    (HERE / f"{name}_{ext}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                                            encoding="utf-8")
                names.append(name)
                if hml:
                    # 문서 내용을 한글 XML(HML) 글로 받는다. 파일 접근이 없어 승인 창이 뜨지 않는다.
                    # 화면에 보이는 한글에서 이 파일을 열어 HWPX·HWP로 저장한다 (저장은 한글이 한다)
                    text = hwp.h.GetTextFile("HWPML2X", "")
                    ok = bool(text) and "<HWPML" in text
                    if hwp.note("HML로 꺼내기", ok):
                        (HERE / f"{name}.hml").write_text(text, encoding="utf-16")  # 머리에 UTF-16이라고 적혀 있다
                if save and hwp.note("저장 (HWPX, HWP)", hwp.save(HERE / name)):
                    for ext in ("hwpx", "hwp"):
                        src = HERE / f"{name}.{ext}"
                        if src.exists():
                            src.replace(HERE / f"{name}_{ext}.{ext}")
    finally:
        hwp.close()
    if hml:
        print("\n화면에 띄운 한글에서 각 .hml을 열어 'name_hwpx.hwpx'(한글 표준 문서)와 'name_hwp.hwp'(한글 문서)로 저장한다:")
        for name in names:
            print(f"  {HERE / (name + '.hml')}")


if __name__ == "__main__":
    main()
