from korean_parser.ir import Chart, ChartSeries
from korean_parser.render.chart_svg import render_chart_svg


def chart(kind, **kw):
    return Chart(chart_type=kind, categories=["1분기", "2분기"],
                 series=[ChartSeries(name="매출", values=[1.0, 2.0]), ChartSeries(name="비용", values=[0.5, None])], **kw)


def test_supported_charts_render_svg():
    for kind, extra in (("bar", {"bar_dir": "col"}), ("bar", {"bar_dir": "bar"}), ("line", {}), ("pie", {}),
                        ("bar", {"bar_dir": "col", "grouping": "stacked"})):
        svg = render_chart_svg(chart(kind, **extra))
        assert svg and svg.startswith("<svg") and "1분기" in svg


def test_unsupported_chart_returns_none():
    assert render_chart_svg(chart("radar")) is None


def test_chart_text_is_escaped():
    c = chart("bar", bar_dir="col", title="<b>제목</b>")
    assert "&lt;b&gt;" in render_chart_svg(c)
