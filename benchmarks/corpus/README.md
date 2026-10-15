# 문서 코퍼스 일괄 실행

웹에서 받은 공개 라이선스 문서(DOCX, HWPX)를 한꺼번에 파싱·렌더링해서 실패, 경고, 처리 시간, 추출 요소 수를 집계한다.

```bash
./fetch_samples.sh samples_web        # Apache POI DOCX + OpenOffice 템플릿 + hwpxlib HWPX
python run.py samples_web --out corpus_report.md
```

| 출처 | 형식 | 개수 | 라이선스 |
|---|---|---:|---|
| [Apache POI](https://github.com/apache/poi) `test-data/document` | DOCX | 130 | Apache-2.0 |
| [OpenOffice 개조식 보고서 템플릿](https://templates.openoffice.org/) | DOCX (변환) | 1 | BSD |
| [hwpxlib](https://github.com/neolord0/hwpxlib) `testFile` | HWPX | 49 | Apache-2.0 |

## 결과 (2026-10-15)

| 항목 | 값 |
|---|---|
| 문서 | 180개 (DOCX 131, HWPX 49) |
| 성공 | 164개: DOCX 정상 문서 115개, HWPX 49개 전부 |
| 실패 | 16개, 모두 의도적으로 손상·암호화된 DOCX 테스트 파일 (퍼저 생성 파일, 잘린 파일, 암호 걸린 문서) → `DocumentError`로 이유 안내 |
| 처리 시간 (파싱+HTML+LLM) | 중앙값 4ms, 최대 약 5초 (표 셀 11,505개짜리 DOCX) |

HWPX에는 실제 공공기관 문서(2015년 12월 재난안전종합상황 분석 및 전망: 문단 3,758 · 표 110 · 도식 408,
구청 행사 용역 제안요청서 등)가 포함되어 있다.

추출한 요소 (전체): 문단 27,013 · 제목 163 · 표 386 (병합 셀 있는 표 126) · 번호 항목 350 · 이미지 93 ·
도식 452 (도형 598) · 차트 7 · 각주 12

남은 경고:

- 브라우저가 표시할 수 없는 이미지 형식(EMF/WMF) 11건: 회색 상자로 대체. PNG 변환 예정
- 지원하지 않는 그래픽 요소 1건: Office 2016 신형 차트(`chartex`)
- 표시하지 않는 개체: HWPX OLE 1건, 동영상 1건
- 표 중첩 32단계 초과 1건: 5,000단계로 중첩된 시험용 DOCX. 안쪽 표는 글자만 추출
