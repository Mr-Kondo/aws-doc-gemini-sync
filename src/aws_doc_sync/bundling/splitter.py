"""Document splitting.

A bundle that grows past the configured size is split into numbered parts. The
requirement that matters is not the size limit itself but *stability*: if the
split boundary moved every run, Drive file ids would churn and every Gemini
Notebook referencing them would have to be rewired by hand.

Stability comes from three rules:

1. Sections are packed greedily in **registry order**, never sorted by size.
2. A section larger than the hard limit gets its own part rather than being
   truncated. Losing documentation to fit a quota is the worse failure.
3. Part names use a fixed-width suffix, so ``_01`` sorts before ``_10``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..logging_setup import get_logger
from .builder import SourceSection

log = get_logger("bundle.split")


@dataclass(frozen=True)
class PartPlan:
    """One output document: its 1-based index and the sections it carries."""

    part: int
    sections: tuple[SourceSection, ...]

    @property
    def char_count(self) -> int:
        return sum(s.char_count for s in self.sections)


def plan_parts(
    sections: list[SourceSection],
    *,
    target_max_chars: int,
    hard_max_chars: int,
    header_allowance: int = 2_000,
) -> list[PartPlan]:
    """Pack ``sections`` into parts without reordering them.

    ``header_allowance`` reserves room for the provenance header that will be
    prepended to each part, so a part does not overshoot the target once rendered.
    """
    if not sections:
        return []

    budget = max(target_max_chars - header_allowance, 1)
    parts: list[list[SourceSection]] = []
    current: list[SourceSection] = []
    current_size = 0

    for section in sections:
        size = section.char_count

        if size > hard_max_chars:
            # Cannot be made to fit with any packing; isolate it and say so.
            if current:
                parts.append(current)
                current, current_size = [], 0
            parts.append([section])
            log.warning(
                "bundle_section_exceeds_hard_limit",
                extra={
                    "source_url": section.source_url,
                    "chars": size,
                    "hard_max_chars": hard_max_chars,
                },
            )
            continue

        if current and current_size + size > budget:
            parts.append(current)
            current, current_size = [], 0

        current.append(section)
        current_size += size

    if current:
        parts.append(current)

    return [
        PartPlan(part=index + 1, sections=tuple(items))
        for index, items in enumerate(parts)
    ]


def document_name(output: str, *, part: int, part_count: int, suffix_format: str) -> str:
    """Name for one part.

    A single-part bundle keeps the bare ``output`` name, so the common case never
    carries a meaningless ``_01``.
    """
    if part_count <= 1:
        return output
    return output + suffix_format.format(part=part)
