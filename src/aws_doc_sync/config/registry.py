"""Source registry loading.

The set of documents to synchronize is data, not code. This module is the only
place that knows the YAML shape; everything downstream sees domain objects.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from ..domain.errors import ConfigError
from ..domain.models import (
    Bundle,
    Collection,
    DocumentSource,
    FetchStrategy,
    SourceRegistry,
)
from ..domain.urls import canonical_page_url

_OUTPUT_ALLOWED = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-. "
)


def load_registry(path: Path | str) -> SourceRegistry:
    """Parse and validate a sources YAML file.

    Raises:
        ConfigError: the file is missing, malformed, or violates an invariant
            that would make sync results ambiguous (duplicate ids, duplicate
            output names, a source listed in two bundles).
    """
    resolved = Path(path)
    if not resolved.exists():
        raise ConfigError(f"sources file not found: {resolved}")
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"sources file is not valid YAML: {resolved}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"sources file must contain a mapping: {resolved}")

    raw_collections = data.get("collections")
    if not isinstance(raw_collections, dict) or not raw_collections:
        raise ConfigError(f"{resolved}: 'collections' must be a non-empty mapping")

    collections: list[Collection] = []
    for collection_id, raw_collection in raw_collections.items():
        collections.append(_parse_collection(str(collection_id), raw_collection, resolved))

    registry = SourceRegistry(collections=tuple(collections))
    _check_global_invariants(registry, resolved)
    return registry


def _parse_collection(collection_id: str, raw: Any, path: Path) -> Collection:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: collection '{collection_id}' must be a mapping")
    raw_bundles = raw.get("bundles")
    if not isinstance(raw_bundles, dict) or not raw_bundles:
        raise ConfigError(
            f"{path}: collection '{collection_id}' must define a non-empty 'bundles' mapping"
        )

    bundles: list[Bundle] = []
    for bundle_id, raw_bundle in raw_bundles.items():
        bundles.append(_parse_bundle(collection_id, str(bundle_id), raw_bundle, path))

    return Collection(
        id=collection_id,
        description=str(raw.get("description", "") or ""),
        bundles=tuple(bundles),
    )


def _parse_bundle(collection_id: str, bundle_id: str, raw: Any, path: Path) -> Bundle:
    where = f"{path}: bundle '{collection_id}/{bundle_id}'"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")

    output = raw.get("output")
    if not isinstance(output, str) or not output.strip():
        raise ConfigError(f"{where} must define a non-empty 'output' name")
    output = output.strip()
    bad = sorted(set(output) - _OUTPUT_ALLOWED)
    if bad:
        raise ConfigError(f"{where}: 'output' contains unsupported characters: {bad}")

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError(f"{where} must define a non-empty 'sources' list")

    sources: list[DocumentSource] = []
    seen_in_bundle: set[str] = set()
    for index, raw_source in enumerate(raw_sources):
        source = _parse_source(collection_id, bundle_id, index, raw_source, path)
        canonical = canonical_page_url(source.url)
        if canonical in seen_in_bundle:
            raise ConfigError(f"{where}: duplicate source url within bundle: {source.url}")
        seen_in_bundle.add(canonical)
        sources.append(source)

    rss = raw.get("rss_feeds") or raw.get("rss") or []
    if isinstance(rss, str):
        rss = [rss]
    if not isinstance(rss, list) or not all(isinstance(f, str) for f in rss):
        raise ConfigError(f"{where}: 'rss_feeds' must be a string or list of strings")

    return Bundle(
        id=bundle_id,
        collection_id=collection_id,
        output=output,
        title=str(raw.get("title", "") or ""),
        description=str(raw.get("description", "") or ""),
        sources=tuple(sources),
        rss_feeds=tuple(rss),
    )


def _parse_source(
    collection_id: str, bundle_id: str, index: int, raw: Any, path: Path
) -> DocumentSource:
    where = f"{path}: {collection_id}/{bundle_id} sources[{index}]"
    if isinstance(raw, str):
        raw = {"url": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a string URL or a mapping")

    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"{where} must define 'url'")

    strategy_raw = str(raw.get("strategy", "auto")).lower()
    try:
        strategy = FetchStrategy(strategy_raw)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in FetchStrategy)
        raise ConfigError(
            f"{where}: unknown strategy {strategy_raw!r} (allowed: {allowed})"
        ) from exc

    try:
        return DocumentSource(
            url=url.strip(),
            collection_id=collection_id,
            bundle_id=bundle_id,
            title_override=(str(raw["title"]).strip() if raw.get("title") else None),
            strategy=strategy,
            markdown_url=(str(raw["markdown_url"]).strip() if raw.get("markdown_url") else None),
        )
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"{where}: {exc}") from exc


def _check_global_invariants(registry: SourceRegistry, path: Path) -> None:
    """Reject registries whose results would be ambiguous.

    Duplicate outputs would make two bundles fight over one Drive file; the same
    source in two bundles would silently duplicate content across knowledge
    sources and inflate the notebook's token budget.
    """
    seen_bundle_ids: dict[str, str] = {}
    seen_outputs: dict[str, str] = {}
    seen_sources: dict[str, str] = {}

    for collection in registry.collections:
        for bundle in collection.bundles:
            key = bundle.id
            if key in seen_bundle_ids:
                raise ConfigError(
                    f"{path}: bundle id '{key}' is used in both "
                    f"'{seen_bundle_ids[key]}' and '{collection.id}'; ids must be globally unique"
                )
            seen_bundle_ids[key] = collection.id

            out = bundle.output.lower()
            if out in seen_outputs:
                raise ConfigError(
                    f"{path}: output name '{bundle.output}' is used by both "
                    f"'{seen_outputs[out]}' and '{collection.id}/{bundle.id}'"
                )
            seen_outputs[out] = f"{collection.id}/{bundle.id}"

            for source in bundle.sources:
                canonical = canonical_page_url(source.url)
                if canonical in seen_sources:
                    raise ConfigError(
                        f"{path}: source {source.url} appears in both "
                        f"'{seen_sources[canonical]}' and '{collection.id}/{bundle.id}'"
                    )
                seen_sources[canonical] = f"{collection.id}/{bundle.id}"


def resolve_selection(
    registry: SourceRegistry,
    *,
    collections: Iterable[str] = (),
    bundles: Iterable[str] = (),
    all_: bool = False,
) -> tuple[Bundle, ...]:
    """Turn CLI selectors into the bundles to operate on.

    Raises:
        ConfigError: nothing was selected, or a named selector does not exist.
            Silently doing nothing would look like a successful no-op sync.
    """
    collection_ids = [c for c in collections if c]
    bundle_ids = [b for b in bundles if b]

    if all_:
        selected = tuple(registry.iter_bundles())
        if not selected:
            raise ConfigError("registry contains no bundles")
        return selected

    if not collection_ids and not bundle_ids:
        raise ConfigError("select at least one of --collection, --bundle, or --all")

    chosen: list[Bundle] = []
    seen: set[str] = set()

    for collection_id in collection_ids:
        collection = registry.collection(collection_id)
        if collection is None:
            known = ", ".join(c.id for c in registry.collections)
            raise ConfigError(f"unknown collection '{collection_id}' (known: {known})")
        for bundle in collection.bundles:
            if bundle.id not in seen:
                seen.add(bundle.id)
                chosen.append(bundle)

    for bundle_id in bundle_ids:
        found = registry.bundle(bundle_id)
        if found is None:
            known = ", ".join(b.id for b in registry.iter_bundles())
            raise ConfigError(f"unknown bundle '{bundle_id}' (known: {known})")
        if found.id not in seen:
            seen.add(found.id)
            chosen.append(found)

    return tuple(chosen)
