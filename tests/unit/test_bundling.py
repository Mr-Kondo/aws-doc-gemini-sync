from datetime import UTC, datetime

import pytest

from aws_doc_sync.bundling.builder import BundleBuilder, composition_hash
from aws_doc_sync.bundling.splitter import document_name, plan_parts
from aws_doc_sync.domain.models import Bundle, DocumentSource, NormalizedDocument
from aws_doc_sync.normalize.common import compute_content_hash
from aws_doc_sync.sync.planner import plan_bundle_documents

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
BASE = "https://docs.aws.amazon.com/example/latest/dg"


def doc(slug: str, body: str = "Body text.", title: str | None = None) -> NormalizedDocument:
    content = f"# {title or slug.title()}\n\n{body}\n"
    return NormalizedDocument(
        source_url=f"{BASE}/{slug}.html",
        title=title or slug.title(),
        retrieved_at=T0,
        content_hash=compute_content_hash(content),
        content=content,
        fetcher="markdown",
    )


def bundle(slugs, **kwargs) -> Bundle:
    return Bundle(
        id=kwargs.pop("id", "b"),
        collection_id="c",
        output=kwargs.pop("output", "AWS_Example"),
        title=kwargs.pop("title", "AWS Example"),
        description=kwargs.pop("description", "Example bundle."),
        sources=tuple(DocumentSource(url=f"{BASE}/{s}.html") for s in slugs),
        **kwargs,
    )


def render(b: Bundle, docs, **kwargs):
    return plan_bundle_documents(
        b,
        docs,
        {d.source_url: T0 for d in docs},
        builder=BundleBuilder(),
        target_max_chars=kwargs.pop("target_max_chars", 250_000),
        hard_max_chars=kwargs.pop("hard_max_chars", 500_000),
        **kwargs,
    )


def test_bundle_renders_header_provenance_and_body():
    docs = [doc("alpha"), doc("beta")]
    [document] = render(bundle(["alpha", "beta"]), docs)

    md = document.markdown
    assert md.startswith("# AWS Example")
    assert "Source Type: AWS Official Documentation" in md
    assert "Example bundle." in md
    # Every source carries its own traceable provenance block.
    for d in docs:
        assert f"Source URL:\n{d.source_url}" in md
        assert f"Content Hash:\n{d.content_hash}" in md
    assert "Retrieved:\n2026-03-01T12:00:00Z" in md


def test_section_order_follows_the_registry_not_the_fetch_order():
    """Fetch order must not leak into the document.

    Two pages returning in a different order would otherwise change the rendered
    bytes and the composition hash, forcing a rewrite for no reason.
    """
    b = bundle(["alpha", "beta", "gamma"])
    forward = render(b, [doc("alpha"), doc("beta"), doc("gamma")])[0]
    shuffled = render(b, [doc("gamma"), doc("alpha"), doc("beta")])[0]

    assert forward.markdown == shuffled.markdown
    assert forward.composition_hash == shuffled.composition_hash


def test_body_headings_are_demoted_below_the_section_heading():
    document = render(
        bundle(["alpha"]), [doc("alpha", body="## Sub\n\nText.\n\n### Deeper\n\nMore.")]
    )[0]
    assert "## Alpha" in document.markdown       # the section itself
    assert "#### Sub" in document.markdown       # was h2 -> h4
    assert "##### Deeper" in document.markdown   # was h3 -> h5


def test_duplicate_title_heading_is_not_rendered_twice():
    document = render(bundle(["alpha"]), [doc("alpha")])[0]
    assert document.markdown.count("Alpha") >= 1
    assert "\n### Alpha\n" not in document.markdown


def test_headings_inside_code_blocks_are_not_demoted():
    body = "```\n# not a heading\n```\n"
    document = render(bundle(["alpha"]), [doc("alpha", body=body)])[0]
    assert "\n# not a heading\n" in document.markdown


def test_composition_hash_ignores_rendering_time_and_depends_only_on_inputs():
    b = bundle(["alpha"])
    docs = [doc("alpha")]
    later = {docs[0].source_url: datetime(2026, 12, 25, tzinfo=UTC)}

    first = render(b, docs)[0]
    second = plan_bundle_documents(
        b, docs, later, builder=BundleBuilder(),
        target_max_chars=250_000, hard_max_chars=500_000,
    )[0]
    assert first.composition_hash == second.composition_hash


def test_composition_hash_moves_when_a_source_changes():
    b = bundle(["alpha"])
    before = render(b, [doc("alpha", body="Original.")])[0]
    after = render(b, [doc("alpha", body="Rewritten.")])[0]
    assert before.composition_hash != after.composition_hash


def test_composition_hash_moves_when_the_template_version_changes():
    from aws_doc_sync.bundling import builder as builder_module

    b = bundle(["alpha"])
    sections = [BundleBuilder().section(doc("alpha"), retrieved_at=T0)]
    first = composition_hash(b, sections, part=1, part_count=1)

    original = builder_module.RENDER_VERSION
    builder_module.RENDER_VERSION = original + 1
    try:
        second = composition_hash(b, sections, part=1, part_count=1)
    finally:
        builder_module.RENDER_VERSION = original
    assert first != second


# -- splitting --------------------------------------------------------------------


def section(size: int, index: int = 0):
    return BundleBuilder().section(
        doc(f"page{index}", body="x" * size), retrieved_at=T0
    )


def test_small_bundle_is_a_single_unsuffixed_document():
    documents = render(bundle(["alpha", "beta"]), [doc("alpha"), doc("beta")])
    assert len(documents) == 1
    assert documents[0].name == "AWS_Example"
    assert documents[0].part_count == 1


def test_oversized_bundle_is_split_with_stable_zero_padded_names():
    slugs = [f"page{i}" for i in range(6)]
    docs = [doc(s, body="x" * 4_000) for s in slugs]
    documents = render(bundle(slugs), docs, target_max_chars=12_000, hard_max_chars=50_000)

    assert len(documents) > 1
    expected = [f"AWS_Example_{i:02d}" for i in range(1, len(documents) + 1)]
    assert [d.name for d in documents] == expected
    assert all(d.part_count == len(documents) for d in documents)


def test_splitting_is_deterministic_for_identical_input():
    slugs = [f"page{i}" for i in range(6)]
    docs = [doc(s, body="x" * 4_000) for s in slugs]
    b = bundle(slugs)
    first = render(b, docs, target_max_chars=12_000, hard_max_chars=50_000)
    second = render(b, docs, target_max_chars=12_000, hard_max_chars=50_000)
    assert [d.name for d in first] == [d.name for d in second]
    assert [d.composition_hash for d in first] == [d.composition_hash for d in second]


def test_packing_preserves_order_and_never_reorders_to_fit():
    parts = plan_parts(
        [section(6_000, 0), section(1_000, 1), section(6_000, 2)],
        target_max_chars=9_000, hard_max_chars=50_000, header_allowance=0,
    )
    flattened = [s.source_url for p in parts for s in p.sections]
    assert flattened == [f"{BASE}/page{i}.html" for i in range(3)]


def test_a_section_over_the_hard_limit_is_isolated_never_truncated():
    """Losing documentation to satisfy a size limit is the worse failure."""
    huge = section(60_000, 0)
    parts = plan_parts(
        [section(100, 1), huge], target_max_chars=10_000, hard_max_chars=50_000,
        header_allowance=0,
    )
    oversized = [p for p in parts if p.oversized]
    assert len(oversized) == 1
    assert oversized[0].sections[0].source_url == huge.source_url
    assert oversized[0].sections[0].char_count > 50_000  # kept in full


def test_empty_input_produces_no_documents():
    assert plan_parts([], target_max_chars=1_000, hard_max_chars=2_000) == []
    assert render(bundle(["alpha"]), []) == []


@pytest.mark.parametrize(
    ("part", "count", "expected"),
    [(1, 1, "OUT"), (1, 2, "OUT_01"), (2, 2, "OUT_02"), (10, 12, "OUT_10")],
)
def test_document_names_sort_correctly(part, count, expected):
    name = document_name("OUT", part=part, part_count=count, suffix_format="_{part:02d}")
    assert name == expected
