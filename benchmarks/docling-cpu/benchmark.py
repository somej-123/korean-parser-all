#!/usr/bin/env python3
"""Docling CPU 벤치마크.

여러 설정(프리셋)으로 문서를 변환하고 페이지당 처리 시간, 단계별 시간,
추출된 표/이미지 수를 비교한다. 결과는 CSV와 Markdown 요약으로 저장한다.

사용 예:
    python benchmark.py ./samples
    python benchmark.py ./samples --presets fast,accurate,auto --threads 8 --repeat 3
    python benchmark.py ./samples --extract-images --save-outputs

프리셋:
    fast      OCR 끔, TableFormer FAST       (디지털 PDF 기본 추천)
    accurate  OCR 끔, TableFormer ACCURATE   (표 품질 비교용)
    ocr       전체 페이지 OCR 강제, FAST      (스캔 문서 상황)
    auto      PDF마다 텍스트 레이어를 검사해서 있으면 fast, 없으면 ocr (운영 추천)

DOCX/PPTX/XLSX/HTML/MD는 AI 모델 없이 XML/텍스트 규칙 기반으로 변환되므로
프리셋과 관계없이 같은 경로로 처리된다.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import platform
import resource
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md"}
PRESETS = ("fast", "accurate", "ocr", "auto")

# 페이지당 이 글자 수보다 적으면 텍스트 레이어가 없는 스캔 PDF로 본다.
TEXT_LAYER_MIN_CHARS_PER_PAGE = 30


@dataclass
class RunResult:
    file: str
    preset: str
    route: str  # 실제로 탄 경로: fast / accurate / ocr / no-ai
    run: int
    pages: int
    seconds: float
    sec_per_page: float
    tables: int
    pictures: int
    text_chars: int
    status: str
    stage_seconds: dict[str, float] = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------


def build_pdf_options(route: str, args: argparse.Namespace):
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.pipeline_options import (
        OcrMode,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableFormerMode,
    )

    opts = PdfPipelineOptions()
    opts.accelerator_options = AcceleratorOptions(
        num_threads=args.threads, device=AcceleratorDevice.CPU
    )
    opts.do_table_structure = True
    opts.table_structure_options.mode = (
        TableFormerMode.ACCURATE if route == "accurate" else TableFormerMode.FAST
    )
    opts.table_structure_options.do_cell_matching = True

    if route == "ocr":
        opts.do_ocr = True
        opts.ocr_options = RapidOcrOptions(lang=[args.ocr_lang], mode=OcrMode.FULL_PAGE)
    else:
        # 디지털 PDF는 텍스트 레이어에서 글자를 바로 읽으므로 OCR이 필요 없다.
        opts.do_ocr = False

    # 이미지 추출: 그림 영역을 잘라서 PNG로 저장할 수 있게 한다 (비용이 조금 든다).
    opts.generate_picture_images = args.extract_images
    opts.images_scale = args.images_scale
    return opts


def build_converter(route: str, args: argparse.Namespace):
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=build_pdf_options(route, args))
        }
    )


def has_text_layer(path: Path) -> bool:
    """PDF에 추출 가능한 텍스트가 충분히 있는지 확인한다 (모델 없이, 수 ms)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        n = len(pdf)
        if n == 0:
            return False
        sample = range(min(n, 5))  # 앞쪽 몇 페이지만 보면 충분하다
        chars = 0
        for i in sample:
            page = pdf[i]
            textpage = page.get_textpage()
            chars += len(textpage.get_text_range().strip())
            textpage.close()
            page.close()
        return chars / len(sample) >= TEXT_LAYER_MIN_CHARS_PER_PAGE
    finally:
        pdf.close()


def pick_route(path: Path, preset: str) -> str:
    if path.suffix.lower() != ".pdf":
        return "no-ai"
    if preset == "auto":
        return "fast" if has_text_layer(path) else "ocr"
    return preset


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


def collect_files(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED_SUFFIXES)
        elif p.suffix.lower() in SUPPORTED_SUFFIXES:
            files.append(p)
    return files


def stage_seconds(conv_result) -> dict[str, float]:
    timings = getattr(conv_result, "timings", None) or {}
    out = {}
    for key, item in timings.items():
        times = getattr(item, "times", None)
        if times:
            out[key] = round(sum(times), 4)
    return out


def save_outputs(conv_result, path: Path, preset: str, out_dir: Path, args) -> None:
    from docling_core.types.doc import ImageRefMode

    # 같은 이름의 .docx/.pdf가 섞이지 않도록 확장자를 폴더명에 붙인다.
    target = out_dir / "outputs" / preset / f"{path.stem}_{path.suffix.lstrip('.')}"
    target.mkdir(parents=True, exist_ok=True)
    doc = conv_result.document
    image_mode = ImageRefMode.REFERENCED if args.extract_images else ImageRefMode.PLACEHOLDER
    doc.save_as_markdown(target / f"{path.stem}.md", image_mode=image_mode)
    doc.save_as_json(target / f"{path.stem}.json", image_mode=image_mode)

    if args.extract_images:
        # 그림마다 PNG + 메타데이터(페이지, 위치, 캡션)를 따로 저장한다.
        meta = []
        for i, pic in enumerate(doc.pictures, 1):
            img = pic.get_image(doc)
            if img is None:
                continue
            name = f"picture_{i:03d}.png"
            img.save(target / name)
            prov = pic.prov[0] if pic.prov else None
            meta.append(
                {
                    "file": name,
                    "ref": pic.self_ref,
                    "page": prov.page_no if prov else None,
                    "bbox": prov.bbox.as_tuple() if prov else None,
                    "size": list(img.size),
                    "caption": pic.caption_text(doc),
                }
            )
        (target / "pictures.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def convert_one(converter, path: Path, preset: str, route: str, run: int, args, out_dir: Path):
    from docling.datamodel.base_models import ConversionStatus

    start = time.perf_counter()
    try:
        res = converter.convert(path, raises_on_error=False)
    except Exception as exc:  # noqa: BLE001 - 벤치마크는 실패해도 계속 진행한다
        return RunResult(
            file=path.name, preset=preset, route=route, run=run, pages=0,
            seconds=round(time.perf_counter() - start, 3), sec_per_page=0.0,
            tables=0, pictures=0, text_chars=0, status="exception", error=repr(exc)[:300],
        )
    elapsed = time.perf_counter() - start

    doc = res.document
    pages = len(res.pages) or (doc.num_pages() if doc else 0) or 1
    if args.save_outputs and run == 1 and res.status in (
        ConversionStatus.SUCCESS,
        ConversionStatus.PARTIAL_SUCCESS,
    ):
        save_outputs(res, path, preset, out_dir, args)

    return RunResult(
        file=path.name,
        preset=preset,
        route=route,
        run=run,
        pages=pages,
        seconds=round(elapsed, 3),
        sec_per_page=round(elapsed / pages, 3),
        tables=len(doc.tables) if doc else 0,
        pictures=len(doc.pictures) if doc else 0,
        text_chars=sum(len(t.text) for t in doc.texts) if doc else 0,
        status=res.status.value,
        stage_seconds=stage_seconds(res),
        error="; ".join(e.error_message for e in res.errors)[:300] if res.errors else "",
    )


def run_benchmark(files: list[Path], args, out_dir: Path) -> tuple[list[RunResult], dict]:
    converters: dict[str, object] = {}
    init_seconds: dict[str, float] = {}

    def get_converter(route: str):
        key = "fast" if route == "no-ai" else route  # no-ai는 PDF 옵션과 무관하다
        if key not in converters:
            from docling.datamodel.base_models import InputFormat

            t0 = time.perf_counter()
            conv = build_converter(key, args)
            # 모델 로딩 시간은 문서 처리 시간과 분리해서 잰다 (서버에서는 한 번만 든다).
            conv.initialize_pipeline(InputFormat.PDF)
            init_seconds[key] = round(time.perf_counter() - t0, 2)
            print(f"  [init] {key}: 모델 로딩 {init_seconds[key]}s", flush=True)
            converters[key] = conv
        return converters[key]

    results: list[RunResult] = []
    for preset in args.presets:
        print(f"\n=== preset: {preset} ===", flush=True)
        for path in files:
            route = pick_route(path, preset)
            if route == "no-ai" and preset != args.presets[0]:
                continue  # 비 PDF는 프리셋과 무관하므로 한 번만 잰다
            conv = get_converter(route)
            if args.warmup:
                convert_one(conv, path, preset, route, 0, args, out_dir)
            for run in range(1, args.repeat + 1):
                r = convert_one(conv, path, preset, route, run, args, out_dir)
                results.append(r)
                print(
                    f"  {path.name:<40} route={route:<8} run={run} "
                    f"pages={r.pages:<3} {r.seconds:>7.2f}s  {r.sec_per_page:>6.2f}s/page "
                    f"tables={r.tables} pictures={r.pictures} [{r.status}]"
                    + (f" ERR: {r.error}" if r.error else ""),
                    flush=True,
                )
    return results, init_seconds


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[idx]


def write_reports(results: list[RunResult], init_seconds: dict, args, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        fields = [k for k in asdict(results[0]) if k != "stage_seconds"] + ["stage_seconds"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            row = asdict(r)
            row["stage_seconds"] = json.dumps(r.stage_seconds, ensure_ascii=False)
            w.writerow(row)

    lines = [
        "# Docling CPU 벤치마크 결과",
        "",
        f"- CPU: {platform.processor() or platform.machine()} / 논리 코어 {os.cpu_count()}",
        f"- 스레드 설정: {args.threads}, 반복: {args.repeat}, 워밍업: {args.warmup}",
        f"- 이미지 추출: {args.extract_images} (scale={args.images_scale}), OCR 언어: {args.ocr_lang}",
        f"- Python {platform.python_version()}, 최대 메모리(RSS): {peak_rss_mb():.0f} MB",
        "",
        "## 모델 로딩 시간 (서버 시작 시 1회)",
        "",
        "| 경로 | 초 |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in init_seconds.items()],
        "",
        "## 프리셋/경로별 요약",
        "",
        "| 프리셋 | 경로 | 문서 수 | 총 페이지 | 총 시간(s) | 페이지당 평균(s) | 페이지당 중앙값(s) | 페이지당 p90(s) | 표 | 그림 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    groups: dict[tuple[str, str], list[RunResult]] = {}
    for r in results:
        if r.status in ("success", "partial_success"):
            groups.setdefault((r.preset, r.route), []).append(r)

    for (preset, route), rs in groups.items():
        pages = sum(r.pages for r in rs)
        secs = sum(r.seconds for r in rs)
        spp = [r.sec_per_page for r in rs]
        lines.append(
            f"| {preset} | {route} | {len({r.file for r in rs})} | {pages // args.repeat} | "
            f"{secs / args.repeat:.2f} | {secs / pages:.3f} | {statistics.median(spp):.3f} | "
            f"{percentile(spp, 0.9):.3f} | {sum(r.tables for r in rs if r.run == 1)} | "
            f"{sum(r.pictures for r in rs if r.run == 1)} |"
        )

    stage_keys = sorted({k for r in results for k in r.stage_seconds})
    if stage_keys:
        lines += [
            "",
            "## 단계별 시간 (문서당 평균, 초)",
            "",
            "어느 단계가 병목인지 확인하는 용도. 키 이름은 Docling 버전에 따라 다를 수 있다.",
            "",
            "| 프리셋 | 경로 | " + " | ".join(stage_keys) + " |",
            "|---|---|" + "---:|" * len(stage_keys),
        ]
        for (preset, route), rs in groups.items():
            avg = [
                statistics.mean(r.stage_seconds.get(k, 0.0) for r in rs) for k in stage_keys
            ]
            lines.append(f"| {preset} | {route} | " + " | ".join(f"{v:.3f}" for v in avg) + " |")

    failed = [r for r in results if r.status not in ("success", "partial_success")]
    if failed:
        lines += ["", "## 실패", ""]
        lines += [f"- {r.file} ({r.preset}/{r.route}): {r.status} {r.error}" for r in failed]

    summary = out_dir / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1024 / 1024 if sys.platform == "darwin" else rss / 1024


# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Docling CPU 벤치마크")
    p.add_argument("inputs", nargs="+", help="문서 파일 또는 폴더")
    p.add_argument("--presets", default="fast,accurate,auto",
                   help=f"쉼표로 구분 ({', '.join(PRESETS)})")
    p.add_argument("--threads", type=int, default=os.cpu_count() or 4, help="CPU 스레드 수")
    p.add_argument("--repeat", type=int, default=1, help="문서당 반복 횟수")
    p.add_argument("--no-warmup", dest="warmup", action="store_false",
                   help="문서별 워밍업 실행을 건너뜀")
    p.add_argument("--ocr-lang", default="korean",
                   help="RapidOCR 인식 언어 (korean, en, ch 등)")
    p.add_argument("--extract-images", action="store_true",
                   help="그림 영역을 PNG로 잘라 저장 (--save-outputs와 함께 사용)")
    p.add_argument("--images-scale", type=float, default=2.0,
                   help="이미지 추출 해상도 배율 (1.0 = 72dpi)")
    p.add_argument("--save-outputs", action="store_true",
                   help="변환 결과(Markdown/JSON/이미지)를 저장해 품질 비교")
    p.add_argument("--out", default="bench_results", help="결과 폴더")
    args = p.parse_args()

    args.presets = [x.strip() for x in args.presets.split(",") if x.strip()]
    bad = [x for x in args.presets if x not in PRESETS]
    if bad:
        p.error(f"알 수 없는 프리셋: {bad}. 사용 가능: {PRESETS}")
    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.WARNING)

    # 스레드 수는 torch/onnxruntime이 import되기 전에 정해야 반영된다.
    os.environ.setdefault("OMP_NUM_THREADS", str(args.threads))

    from docling.datamodel.settings import settings

    settings.debug.profile_pipeline_timings = True  # 단계별 시간 수집

    files = collect_files(args.inputs)
    if not files:
        print("처리할 문서가 없습니다.", file=sys.stderr)
        return 1
    print(f"문서 {len(files)}개, 프리셋 {args.presets}, 스레드 {args.threads}")

    out_dir = Path(args.out)
    results, init_seconds = run_benchmark(files, args, out_dir)
    summary = write_reports(results, init_seconds, args, out_dir)
    print(f"\n결과: {out_dir / 'results.csv'}\n요약: {summary}\n")
    print(summary.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
