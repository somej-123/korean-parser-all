# Docling CPU 벤치마크

[Docling](https://github.com/docling-project/docling)으로 문서를 파싱할 때, **CPU 환경에서** 설정(프리셋)별 처리 속도와 결과를 비교하는 벤치마크 스크립트입니다.

## 왜 만들었나

- DOCX/HWPX/PPTX/XLSX는 XML에 구조가 들어 있어 **AI 모델 없이** 규칙 기반으로 파싱됩니다.
- AI가 필요한 곳은 **PDF의 레이아웃 인식, 표 구조 인식, (스캔 문서의) OCR** 뿐입니다.
- 이 스크립트는 그 구간들이 CPU에서 얼마나 걸리는지, 어떤 설정이 적절한지 실제 문서로 확인하기 위한 것입니다.

## 설치

```bash
python -m venv .venv && source .venv/bin/activate
# CPU 전용 torch를 먼저 설치하면 GPU용 대용량 패키지를 받지 않습니다.
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

첫 실행 시 레이아웃/표/OCR 모델을 자동으로 내려받습니다.

## 사용법

```bash
# 기본: fast, accurate, auto 프리셋 비교
python benchmark.py ./samples

# 운영용 설정만 3회 반복 측정
python benchmark.py ./my_docs --presets auto --repeat 3 --threads 8

# 변환 결과(Markdown/JSON)와 추출 이미지까지 저장해서 품질 비교
python benchmark.py ./my_docs --extract-images --save-outputs
```

### 프리셋

| 프리셋 | 내용 |
|---|---|
| `fast` | OCR 끔, TableFormer FAST |
| `accurate` | OCR 끔, TableFormer ACCURATE (표 품질 비교용) |
| `ocr` | 전체 페이지 OCR 강제 (RapidOCR, 기본 한국어 모델) |
| `auto` | PDF마다 텍스트 레이어를 검사해 있으면 `fast`, 없으면 `ocr` (운영 추천) |

비 PDF 문서(DOCX 등)는 프리셋과 무관하게 AI 없이 처리되므로 한 번만 측정합니다.

### 주요 옵션

| 옵션 | 설명 |
|---|---|
| `--threads N` | CPU 스레드 수 (기본: 코어 수) |
| `--repeat N` | 문서당 반복 횟수 |
| `--no-warmup` | 문서별 워밍업 실행 생략 |
| `--ocr-lang` | RapidOCR 인식 언어 (`korean`, `en`, `ch` 등) |
| `--extract-images` | 그림 영역을 PNG로 잘라 저장 (`--save-outputs`와 함께) |
| `--images-scale` | 이미지 추출 해상도 배율 (기본 2.0) |
| `--out DIR` | 결과 폴더 (기본 `bench_results`) |

## 결과물

```
bench_results/
├── results.csv        # 문서·프리셋·반복별 시간, 페이지, 표/그림 수, 단계별 시간
├── summary.md         # 페이지당 평균/중앙값/p90, 모델 로딩 시간, 단계별 병목, 최대 메모리
└── outputs/<preset>/<문서>_<확장자>/
    ├── *.md, *.json   # 변환 결과 (JSON에는 셀 병합 rowspan/colspan, 좌표가 보존됨)
    ├── picture_001.png
    └── pictures.json  # 그림별 페이지, bbox, 캡션
```

## 참고 측정치

`samples/`의 3페이지 한국어 문서, 4 vCPU 클라우드 컨테이너 기준입니다. 절대값보다 경로 간 비율을 참고하세요.

| 경로 | 처리 시간 |
|---|---:|
| DOCX (AI 없음) | 문서당 약 0.16초 |
| 디지털 PDF (`fast`) | 페이지당 약 1.7초 |
| 디지털 PDF (`accurate`) | 페이지당 약 2.3초 |
| 스캔 PDF (`ocr`) | 페이지당 약 6.1초 |

관찰된 점:

- 디지털 PDF는 레이아웃 모델이, 스캔 PDF는 OCR이 병목입니다.
- Markdown 출력에서는 병합 셀이 평평해지지만, JSON에는 `row_span`/`col_span`이 보존됩니다. 화면 표시용으로는 JSON을 기준으로 삼는 것이 좋습니다.
- 스캔 문서의 한국어 OCR은 표 안 단어 오인식 등 품질 편차가 있어, 실제 문서로 확인이 필요합니다.
- PDF를 만든 도구에 따라 한국어 띄어쓰기가 사라지는 경우가 있습니다.

## 라이선스

이 스크립트는 저장소의 MIT 라이선스를 따릅니다. Docling 코드는 MIT, Docling 모델 가중치와 OCR 엔진 등 의존성은 각자의 라이선스를 따릅니다. `pip-licenses`로 설치된 패키지의 라이선스를 확인하세요.
