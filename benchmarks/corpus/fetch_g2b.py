#!/usr/bin/env python3
"""나라장터 입찰공고 첨부파일(제안요청서·과업지시서 등)을 내려받는다.

공공데이터포털의 '조달청_나라장터 입찰공고정보서비스' API를 쓴다.
서비스 키는 https://www.data.go.kr 에서 해당 API를 '활용신청'하면 발급된다.

    export G2B_SERVICE_KEY='발급받은 일반 인증키(Decoding)'
    python fetch_g2b.py --days 7 --limit 30 --out samples_g2b

- 용역(서비스) 공고에서 HWP·HWPX·PDF·DOCX 첨부만 받는다.
- 받은 파일은 저장소에 넣지 않는다. 공고 문서의 저작권과 재배포 조건은 공고마다 다를 수 있다.
- 공고 정보(번호, 제목, 기관, 파일명, 주소)는 manifest.jsonl에 함께 남긴다.

주의: API 응답 필드 이름은 조달청 API 명세(ntceSpecDocUrl1~10, ntceSpecFileNm1~10)를 따랐다.
명세가 바뀌면 FIELDS_URL/FIELDS_NAME을 맞춰야 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

API = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServc"
FIELDS_URL = [f"ntceSpecDocUrl{i}" for i in range(1, 11)]
FIELDS_NAME = [f"ntceSpecFileNm{i}" for i in range(1, 11)]
WANTED = (".hwp", ".hwpx", ".pdf", ".docx")


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 - 공공데이터포털 고정 주소
        body = r.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"JSON이 아닌 응답: {body[:300]}") from None


def list_notices(key: str, days: int, rows: int, page: int) -> list[dict]:
    end = datetime.now()
    start = end - timedelta(days=days)
    params = {
        "serviceKey": key, "numOfRows": rows, "pageNo": page, "type": "json", "inqryDiv": 1,
        "inqryBgnDt": start.strftime("%Y%m%d0000"), "inqryEndDt": end.strftime("%Y%m%d2359"),
    }
    data = fetch_json(f"{API}?{urllib.parse.urlencode(params)}")
    header = data.get("response", {}).get("header", {})
    if header.get("resultCode") not in (None, "00"):
        raise RuntimeError(f"API 오류: {header}")
    if "OpenAPI_ServiceResponse" in data:
        raise RuntimeError(f"API 오류: {data['OpenAPI_ServiceResponse'].get('cmmMsgHeader')}")
    items = data.get("response", {}).get("body", {}).get("items", [])
    if isinstance(items, dict):  # 결과가 하나면 dict로 오는 경우
        items = items.get("item", [])
    return items if isinstance(items, list) else [items]


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:120]


def main() -> int:
    p = argparse.ArgumentParser(description="나라장터 입찰공고 첨부파일 수집")
    p.add_argument("--days", type=int, default=7, help="최근 며칠 공고")
    p.add_argument("--limit", type=int, default=30, help="받을 파일 수 상한")
    p.add_argument("--out", default="samples_g2b")
    args = p.parse_args()

    key = os.environ.get("G2B_SERVICE_KEY")
    if not key:
        print("G2B_SERVICE_KEY 환경 변수에 공공데이터포털 서비스 키를 넣어 주세요.", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = (out / "manifest.jsonl").open("a", encoding="utf-8")
    saved, page = 0, 1
    while saved < args.limit:
        notices = list_notices(key, args.days, rows=100, page=page)
        if not notices:
            break
        for n in notices:
            for uf, nf in zip(FIELDS_URL, FIELDS_NAME):
                url, name = n.get(uf), n.get(nf)
                if not url or not name or not name.lower().endswith(WANTED):
                    continue
                target = out / safe_name(f"{n.get('bidNtceNo', 'x')}_{name}")
                if target.exists():
                    continue
                try:
                    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310
                        target.write_bytes(r.read())
                except Exception as exc:  # noqa: BLE001
                    print(f"실패  {name}: {exc}", file=sys.stderr)
                    continue
                manifest.write(json.dumps({
                    "file": target.name, "bidNtceNo": n.get("bidNtceNo"), "title": n.get("bidNtceNm"),
                    "org": n.get("ntceInsttNm"), "url": url,
                }, ensure_ascii=False) + "\n")
                saved += 1
                print(f"받음  {target.name}")
                time.sleep(0.3)
                if saved >= args.limit:
                    break
            if saved >= args.limit:
                break
        page += 1
    print(f"\n{saved}개 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
