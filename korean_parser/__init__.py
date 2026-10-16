"""한국어 문서 파서: 통합 문서 모델, 후처리 없는 HTML, LLM용 텍스트, 한국어 처리."""

__version__ = "0.13.4"

from .normalize import TextHealth, inspect_text, normalize_text
from .outline import OutlineItem, detect_outline, outline_to_markdown
from .spacing import SpacingFixer, spacing_lost, spacing_stats

__all__ = [
    "__version__",
    "OutlineItem",
    "SpacingFixer",
    "TextHealth",
    "detect_outline",
    "inspect_text",
    "normalize_text",
    "outline_to_markdown",
    "spacing_lost",
    "spacing_stats",
]
