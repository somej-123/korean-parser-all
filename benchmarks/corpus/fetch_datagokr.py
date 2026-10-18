#!/usr/bin/env python3
"""공공데이터포털(data.go.kr) '파일데이터'에서 문서 파일(HWP·HWPX·DOCX·PDF·PPTX·XLSX)을 형식마다 몇 개씩 받는다.

    python benchmarks/corpus/fetch_datagokr.py --out samples_rfp/datagokr
    python benchmarks/goal/check.py --folder samples_rfp/datagokr --skip-eval --out datagokr_report.md

목록 화면(selectDataSetList.do, 확장자 거르기)에서 '다운로드' 단추가 있는 데이터만 고른다. 한 기관 것만 몰리지 않게
기관마다 하나씩 먼저 고른다. zip으로 받은 것은 안의 문서 파일만 꺼낸다 (경로는 버리고 이름만 쓴다).

포털은 많이 받으면 보안 문자(CAPTCHA)를 묻는다. 그러면 바로 멈춘다 (풀지 않는다). 받는 사이에 3초씩 쉰다.
받은 파일은 저장소에 올리지 않는다 (samples_rfp/는 .gitignore). 출처는 받은 폴더의 sources.json에 남긴다.
2026-10-17에 이 스크립트로 147개를 받아 v1.0 기준 1을 쟀다 (docs/GOAL.md).
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import re
import sys
import time
import urllib.parse
import zipfile

import requests

BASE = "https://www.data.go.kr"
LIMIT = 40 * 1024 * 1024
WANT = {"HWPX": 12, "DOCX": 12, "PDF": 12, "PPTX": 8, "XLSX": 6, "HWP": 4}
DOC_EXT = {".hwp", ".hwpx", ".docx", ".pdf", ".pptx", ".xlsx"}


def listing(s: requests.Session, ext: str, pages: int = 2, first: int = 1) -> list[dict]:
    out = []
    for n in range(first, first + pages):
        r = s.get(BASE + "/tcs/dss/selectDataSetList.do",
                  params={"dType": "FILE", "extsn": ext, "perPage": 40, "currentPage": n}, timeout=60)
        r.raise_for_status()
        r.encoding = "utf-8"
        for block in r.text.split('<div class="apply-result-item">')[1:]:
            m = re.search(r"fn_fileDataDown\(\s*'(\d+)',\s*'([^']+)',\s*'([^']*)',\s*'(\d+)',\s*'(\d+)'", block)
            t = re.search(r'<a href="/data/(\d+)/fileData.do">(.*?)</a>', block, re.S)
            org = re.search(r"<strong>제공기관</strong>(.*?)</li>", block, re.S)
            if not (m and t):
                continue  # 바로가기(외부 누리집)만 있는 데이터
            clean = lambda x: html.unescape(re.sub(r"<[^>]+>|\s+", " ", x)).strip()  # noqa: E731
            out.append({"pk": t.group(1), "title": clean(t.group(2)), "org": clean(org.group(1)) if org else "",
                        "ext": ext, "down": list(m.groups())})
    return out


def download(s: requests.Session, row: dict, folder: pathlib.Path) -> pathlib.Path | None | bool:
    """받은 파일 경로. 건너뛰면 None, 보안 문자를 요구하면 False."""
    pk, detail, atch, sn, _ = row["down"]
    j = s.post(BASE + "/tcs/dss/selectFileDataDownload.do",
               data={"publicDataDetailPk": detail, "publicDataPk": pk, "atchFileId": atch,
                     "fileDetailSn": sn, "publicDataTyCode": "PR0051"}, timeout=60).json()
    if not j.get("status"):
        return None
    info = j["dataSetFileDetailInfo"]
    name = info["publicDataSj"] if info.get("publicDataTyDetailCode") == "DATY05" else info["dataNm"]
    lim = s.post(BASE + "/cmm/cmm/check-limit.json",
                 data={"atchFileId": j["atchFileId"], "fileDetailSn": j["fileDetailSn"]}, timeout=60)
    if lim.status_code == 403 or lim.json().get("needCaptcha"):
        return False
    url = (f"{BASE}/cmm/cmm/fileDownload.do?atchFileId={j['atchFileId']}&fileDetailSn={j['fileDetailSn']}"
           f"&dataNm={urllib.parse.quote(name)}")
    with s.get(url, stream=True, timeout=120) as resp:
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", resp.headers.get("content-disposition", ""))
        fn = urllib.parse.unquote(m.group(1)) if m else name
        try:
            fn = fn.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (pk + "_" + re.sub(r'[\\/:*?"<>|]', "_", fn))
        size = 0
        with open(path, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                size += len(chunk)
                if size > LIMIT:
                    break
                f.write(chunk)
    if size > LIMIT:
        path.unlink()
        return None
    return path


def unzip_docs(z: pathlib.Path) -> int:
    """zip 안의 문서 파일만 꺼낸다. 이름이 cp949인 zip(한국 윈도우)도 읽는다."""
    out, n = z.with_suffix(""), 0
    with zipfile.ZipFile(z) as zf:
        for info in zf.infolist():
            raw = info.filename
            if not info.flag_bits & 0x800:
                try:
                    raw = raw.encode("cp437").decode("cp949")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            name = pathlib.PurePosixPath(raw.replace("\\", "/")).name
            if info.is_dir() or pathlib.Path(name).suffix.lower() not in DOC_EXT or info.file_size > LIMIT:
                continue
            out.mkdir(exist_ok=True)
            (out / re.sub(r'[\\/:*?"<>|]', "_", name)).write_bytes(zf.read(info))
            n += 1
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=pathlib.Path, required=True)
    p.add_argument("--skip", type=pathlib.Path, action="append", default=[],
                   help="이 sources.json에 있는 데이터는 받지 않는다 (처음 보는 문서 묶음을 따로 만들 때)")
    p.add_argument("--first-page", type=int, default=1, help="목록의 몇 번째 쪽부터 고를지 (한 쪽 40개)")
    p.add_argument("--want", action="append", default=[], metavar="EXT=N",
                   help="이 형식만 이만큼 받는다 (예: --want DOCX=20). 없으면 기본 개수 (WANT)")
    args = p.parse_args()
    want_map = {k.upper(): int(v) for k, v in (w.split("=") for w in args.want)} or WANT
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (korean-parser-all test collection)"
    s.headers["Referer"] = BASE + "/tcs/dss/selectDataSetList.do"
    log_path = args.out / "sources.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    done = {x["pk"] for x in log}
    for other in args.skip:
        done |= {x["pk"] for x in json.loads(other.read_text(encoding="utf-8"))}
    stopped = False
    for ext, want in want_map.items():
        if stopped:
            break
        rows = listing(s, ext, first=args.first_page)
        first, rest, seen = [], [], set()
        for r in rows:
            (rest if r["org"] in seen else first).append(r)
            seen.add(r["org"])
        got = sum(1 for x in log if x["ext"] == ext)
        for r in first + rest:
            if got >= want:
                break
            if r["pk"] in done:
                continue
            path = download(s, r, args.out / ext)
            if path is False:
                print("포털이 보안 문자를 요구해서 멈춘다. 나중에 다시 실행하면 이어 받는다.")
                stopped = True
                break
            if path is None:
                continue
            extracted = unzip_docs(path) if path.suffix.lower() == ".zip" else 0
            log.append({"pk": r["pk"], "ext": ext, "org": r["org"], "title": r["title"],
                        "file": str(path.relative_to(args.out)), "url": f"{BASE}/data/{r['pk']}/fileData.do",
                        "extracted": extracted})
            log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
            got += 1
            print(ext, got, path.name)
            time.sleep(3)
    print("받은 데이터:", len(log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
