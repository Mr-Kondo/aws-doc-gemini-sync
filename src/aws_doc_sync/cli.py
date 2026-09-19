"""Command line interface.

Exit codes are part of the contract, because this is meant to run unattended:

``0``
    Everything succeeded.
``1``
    Partial failure -- some sources or documents failed while others succeeded.
    A scheduler should notice, but the run did produce usable output.
``2``
    Nothing usable was produced (bad config, no credentials, total failure).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config.registry import resolve_selection
from .domain.errors import AwsDocSyncError, ConfigError
from .domain.models import SyncAction
from .domain.urls import canonical_page_url
from .logging_setup import configure_logging, get_logger
from .runtime import DEFAULT_SETTINGS, DEFAULT_SOURCES, build_runtime
from .sync.results import SyncReport

log = get_logger("cli")

app = typer.Typer(
    name="aws-doc-sync",
    help="Synchronize AWS official documentation into Google Docs for Gemini Notebook.",
    add_completion=False,
)

SourcesOpt = Annotated[
    Path, typer.Option("--sources", "-s", help="Path to the source registry YAML.")
]
SettingsOpt = Annotated[
    Path, typer.Option("--settings", help="Path to the settings YAML.")
]
CollectionOpt = Annotated[
    list[str] | None, typer.Option("--collection", "-c", help="Collection id (repeatable).")
]
BundleOpt = Annotated[
    list[str] | None, typer.Option("--bundle", "-b", help="Bundle id (repeatable).")
]
AllOpt = Annotated[bool, typer.Option("--all", help="Operate on every bundle.")]
LogLevelOpt = Annotated[str | None, typer.Option("--log-level", help="DEBUG|INFO|WARNING|ERROR.")]
LogFormatOpt = Annotated[str | None, typer.Option("--log-format", help="json|text.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit the report as JSON.")]


def _log_stream(as_json: bool):
    """Where log records go for this command.

    Reports that are meant to be parsed take stdout to themselves; logs move to
    stderr so a caller can pipe one without the other corrupting it.
    """
    return sys.stderr if as_json else sys.stdout


def _fail(message: str, code: int = 2) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


# --------------------------------------------------------------------------------------
# validate-config
# --------------------------------------------------------------------------------------


@app.command("validate-config")
def validate_config(
    sources: SourcesOpt = DEFAULT_SOURCES,
    settings: SettingsOpt = DEFAULT_SETTINGS,
    log_level: LogLevelOpt = None,
    log_format: LogFormatOpt = "text",
) -> None:
    """Check configuration without touching the network or Google."""
    try:
        runtime = build_runtime(
            sources_path=sources,
            settings_path=settings,
            log_level=log_level,
            log_format=log_format,
        )
    except ConfigError as exc:
        _fail(str(exc))
        return

    with runtime:
        registry = runtime.registry
        collections = list(registry.collections)
        bundles = list(registry.iter_bundles())
        all_sources = list(registry.iter_sources())

        typer.echo(f"aws-doc-sync {__version__}")
        typer.echo(f"settings:   {runtime.settings.settings_path or '(defaults)'}")
        typer.echo(f"sources:    {sources}")
        typer.echo(f"manifest:   {runtime.settings.manifest_path}")
        typer.echo(f"fetchers:   {' -> '.join(runtime.fetcher.backends)}")
        typer.echo("")
        typer.echo(
            f"collections: {len(collections)}   bundles: {len(bundles)}   "
            f"sources: {len(all_sources)}"
        )

        for collection in collections:
            typer.echo(f"\n  {collection.id}")
            for bundle in collection.bundles:
                feeds = f", rss: {len(bundle.rss_feeds)}" if bundle.rss_feeds else ""
                typer.echo(
                    f"    - {bundle.id} -> {bundle.output} "
                    f"({len(bundle.sources)} sources{feeds})"
                )

        typer.echo("\nGoogle configuration:")
        for key, value in runtime.settings.google.describe().items():
            typer.echo(f"  {key}: {value}")

        notes = runtime.settings.google.warnings()
        if notes:
            typer.echo("")
            for note in notes:
                typer.secho(f"warning: {note}", fg=typer.colors.YELLOW)

        problems = runtime.settings.google.validate_for_sync()
        if problems:
            typer.echo("")
            typer.secho(
                "config is valid; Google is not ready for a real sync:",
                fg=typer.colors.YELLOW,
            )
            for problem in problems:
                typer.secho(f"  - {problem}", fg=typer.colors.YELLOW)
            typer.echo("\n'dry-run' works without Google credentials.")
        else:
            typer.echo("")
            typer.secho("configuration OK", fg=typer.colors.GREEN)


# --------------------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------------------


@app.command("fetch")
def fetch(
    collection: CollectionOpt = None,
    bundle: BundleOpt = None,
    all_: AllOpt = False,
    sources: SourcesOpt = DEFAULT_SOURCES,
    settings: SettingsOpt = DEFAULT_SETTINGS,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write normalized Markdown into this directory."),
    ] = None,
    log_level: LogLevelOpt = None,
    log_format: LogFormatOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Fetch, normalize, and render bundles locally. Never touches Google Drive.

    Use ``--out`` to inspect exactly what would be uploaded before any credential
    is involved.
    """
    report = _run(
        collection,
        bundle,
        all_,
        sources,
        settings,
        log_level,
        log_format,
        dry_run=True,
        full_scan=True,
        write=True,
        use_store=False,
        out_dir=out,
        as_json=as_json,
    )
    _emit(report, as_json=as_json, title="fetch")
    raise typer.Exit(report.exit_code())


# --------------------------------------------------------------------------------------
# sync / dry-run / scan
# --------------------------------------------------------------------------------------


@app.command("sync")
def sync(
    collection: CollectionOpt = None,
    bundle: BundleOpt = None,
    all_: AllOpt = False,
    sources: SourcesOpt = DEFAULT_SOURCES,
    settings: SettingsOpt = DEFAULT_SETTINGS,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan only; never write to Drive.")
    ] = False,
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="Ignore RSS hints and cached ETags; re-fetch and re-hash every source.",
        ),
    ] = False,
    allow_partial: Annotated[
        bool,
        typer.Option(
            "--allow-partial",
            help="Write a document even when some of its sources failed to fetch.",
        ),
    ] = False,
    log_level: LogLevelOpt = None,
    log_format: LogFormatOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Synchronize bundles into Google Docs."""
    report = _run(
        collection,
        bundle,
        all_,
        sources,
        settings,
        log_level,
        log_format,
        dry_run=dry_run,
        full_scan=full,
        write=True,
        allow_partial=allow_partial,
        as_json=as_json,
    )
    _emit(report, as_json=as_json, title="dry-run" if dry_run else "sync")
    raise typer.Exit(report.exit_code())


@app.command("dry-run")
def dry_run_command(
    collection: CollectionOpt = None,
    bundle: BundleOpt = None,
    all_: AllOpt = False,
    sources: SourcesOpt = DEFAULT_SOURCES,
    settings: SettingsOpt = DEFAULT_SETTINGS,
    full: Annotated[
        bool,
        typer.Option(
            "--full/--fast",
            help="Ignore RSS hints and cached ETags (default), or use them as 'sync' would.",
        ),
    ] = True,
    log_level: LogLevelOpt = None,
    log_format: LogFormatOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Show what a sync would do. Google Drive is never modified."""
    report = _run(
        collection,
        bundle,
        all_,
        sources,
        settings,
        log_level,
        log_format,
        dry_run=True,
        full_scan=full,
        write=True,
        as_json=as_json,
    )
    _emit(report, as_json=as_json, title="dry-run")
    raise typer.Exit(report.exit_code())


@app.command("scan")
def scan(
    collection: CollectionOpt = None,
    bundle: BundleOpt = None,
    all_: AllOpt = False,
    sources: SourcesOpt = DEFAULT_SOURCES,
    settings: SettingsOpt = DEFAULT_SETTINGS,
    log_level: LogLevelOpt = None,
    log_format: LogFormatOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Re-read every source (the safety net behind both fast paths).

    Sends no RSS hints and no cached validators: every page is fetched in full
    and re-hashed, so neither a quiet feed nor a wrong ETag can hide a change
    indefinitely. Updates the manifest without writing to Drive, so a later sync
    knows exactly what moved. Run it on a slower cadence than the daily sync.
    """
    report = _run(
        collection,
        bundle,
        all_,
        sources,
        settings,
        log_level,
        log_format,
        dry_run=False,
        full_scan=True,
        write=False,
        as_json=as_json,
    )
    _emit(report, as_json=as_json, title="scan")
    raise typer.Exit(report.exit_code())


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------


@app.command("status")
def status(
    sources: SourcesOpt = DEFAULT_SOURCES,
    settings: SettingsOpt = DEFAULT_SETTINGS,
    log_level: LogLevelOpt = None,
    log_format: LogFormatOpt = "text",
    as_json: JsonOpt = False,
) -> None:
    """Report stored sync state: hashes, documents, orphans."""
    try:
        runtime = build_runtime(
            sources_path=sources,
            settings_path=settings,
            log_level=log_level,
            log_format=log_format,
            log_stream=_log_stream(as_json),
        )
    except ConfigError as exc:
        _fail(str(exc))
        return

    with runtime:
        manifest = runtime.manifests.load()
        registry = runtime.registry

        bundle_rows: list[dict[str, object]] = []
        payload: dict[str, object] = {
            "manifest_path": str(runtime.settings.manifest_path),
            **manifest.summary(),
            "registry": {
                "collections": len(registry.collections),
                "bundles": len(list(registry.iter_bundles())),
                "sources": len(list(registry.iter_sources())),
            },
            "bundles": bundle_rows,
        }

        for b in registry.iter_bundles():
            state = manifest.bundle(b.id)
            known = [
                state
                for state in (
                    manifest.source(canonical_page_url(s.url)) for s in b.sources
                )
                if state is not None
            ]
            bundle_rows.append(
                {
                    "bundle_id": b.id,
                    "collection_id": b.collection_id,
                    "output": b.output,
                    "sources_registered": len(b.sources),
                    "sources_tracked": len(known),
                    "orphaned": sum(1 for s in known if s.orphaned),
                    "documents": dict(state.document_ids) if state else {},
                    "retired_documents": dict(state.orphaned_documents) if state else {},
                    "last_synced": state.last_synced.isoformat()
                    if state and state.last_synced
                    else None,
                    "last_changed": state.last_changed.isoformat()
                    if state and state.last_changed
                    else None,
                }
            )

        if as_json:
            typer.echo(json.dumps(payload, indent=2, default=str))
            raise typer.Exit(0)

        typer.echo(f"manifest: {payload['manifest_path']}")
        typer.echo(
            f"tracked sources: {payload['sources']}   documents: {payload['documents']}   "
            f"orphaned: {payload['orphaned']}"
        )
        typer.echo("")
        for entry in bundle_rows:
            docs = entry["documents"]
            docs = docs if isinstance(docs, dict) else {}
            typer.echo(f"  {entry['bundle_id']}  ({entry['collection_id']})")
            typer.echo(
                f"    sources: {entry['sources_tracked']}/{entry['sources_registered']}"
                f"   orphaned: {entry['orphaned']}"
            )
            if docs:
                for name, doc_id in docs.items():
                    typer.echo(f"    doc: {name}  https://docs.google.com/document/d/{doc_id}")
            else:
                typer.echo("    doc: (not created yet)")

            retired = entry["retired_documents"]
            if isinstance(retired, dict) and retired:
                for name, doc_id in retired.items():
                    typer.secho(
                        f"    retired: {name}  "
                        f"https://docs.google.com/document/d/{doc_id}",
                        fg=typer.colors.YELLOW,
                    )
            typer.echo(f"    last synced: {entry['last_synced'] or 'never'}")

        orphans = manifest.orphans()
        if orphans:
            typer.echo("")
            typer.secho(f"orphaned sources ({len(orphans)}):", fg=typer.colors.YELLOW)
            for orphan in orphans:
                typer.echo(f"  - {orphan.source_url}  ({orphan.last_error or 'unknown reason'})")
            typer.echo("  These are recorded only. Nothing is deleted automatically.")


# --------------------------------------------------------------------------------------
# shared plumbing
# --------------------------------------------------------------------------------------


def _run(
    collection,
    bundle,
    all_,
    sources,
    settings,
    log_level,
    log_format,
    *,
    dry_run: bool,
    full_scan: bool,
    write: bool,
    allow_partial: bool = False,
    use_store: bool | None = None,
    out_dir: Path | None = None,
    as_json: bool = False,
) -> SyncReport:
    try:
        runtime = build_runtime(
            sources_path=sources,
            settings_path=settings,
            log_level=log_level,
            log_format=log_format,
            log_stream=_log_stream(as_json),
        )
    except ConfigError as exc:
        _fail(str(exc))
        raise AssertionError from exc  # pragma: no cover - _fail always exits

    with runtime:
        try:
            selected = resolve_selection(
                runtime.registry,
                collections=collection or [],
                bundles=bundle or [],
                all_=all_,
            )
        except ConfigError as exc:
            _fail(str(exc))

        store = None
        if use_store if use_store is not None else write:
            try:
                store = runtime.document_store(dry_run=dry_run)
            except (AwsDocSyncError, RuntimeError) as exc:
                _fail(str(exc))

        service = runtime.sync_service(store)
        report = service.run(
            list(selected),
            dry_run=dry_run,
            full_scan=full_scan,
            allow_partial=allow_partial,
            write=write,
        )

        if out_dir is not None:
            _write_fetched(report, out_dir)

        return report


def _write_fetched(report: SyncReport, out_dir: Path) -> None:
    """Dump rendered bundle Markdown for inspection.

    Useful for reviewing normalization before anything reaches Drive.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for plan in report.all_documents:
        if plan.document is None:
            continue
        (out_dir / f"{plan.name}.md").write_text(plan.document.markdown, encoding="utf-8")
    typer.echo(f"wrote rendered bundles to {out_dir}")


_ACTION_COLOR = {
    SyncAction.CREATE: typer.colors.GREEN,
    SyncAction.UPDATE: typer.colors.CYAN,
    SyncAction.NO_CHANGE: typer.colors.WHITE,
    SyncAction.SKIPPED_INCOMPLETE: typer.colors.YELLOW,
    SyncAction.ERROR: typer.colors.RED,
}


def _emit(report: SyncReport, *, as_json: bool, title: str) -> None:
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "command": title,
                    "summary": report.summary(),
                    "documents": [
                        {
                            "name": d.name,
                            "bundle_id": d.bundle_id,
                            "action": d.action.value,
                            "document_id": d.document_id,
                            "chars": d.chars,
                            "reason": d.reason,
                            "error": d.error,
                        }
                        for d in report.all_documents
                    ],
                    "sources": [
                        {
                            "source_url": s.source_url,
                            "bundle_id": s.bundle_id,
                            "status": s.status.value,
                            "fetcher": s.fetcher,
                            "chars": s.chars,
                            "not_modified": s.not_modified,
                            "error": s.error,
                        }
                        for s in report.all_sources
                    ],
                },
                indent=2,
                default=str,
            )
        )
        return

    typer.echo("")
    typer.secho(f"=== {title}{' (no writes)' if report.dry_run else ''} ===", bold=True)

    for bundle_result in report.bundles:
        typer.echo(f"\n{bundle_result.bundle_id}  ->  {bundle_result.output}")
        if bundle_result.skipped_reason:
            typer.secho(f"  skipped: {bundle_result.skipped_reason}", fg=typer.colors.BLUE)

        for plan in bundle_result.documents:
            color = _ACTION_COLOR.get(plan.action, typer.colors.WHITE)
            detail = f"{plan.chars:,} chars" if plan.chars else ""
            typer.secho(f"  {plan.action.value:<19} {plan.name}  {detail}", fg=color)
            if plan.reason and plan.action is not SyncAction.NO_CHANGE:
                typer.echo(f"      reason: {plan.reason}")
            if plan.error:
                typer.secho(f"      error: {plan.error}", fg=typer.colors.RED)
            if plan.document_id and not report.dry_run:
                typer.echo(f"      https://docs.google.com/document/d/{plan.document_id}")

        for source in bundle_result.failed_sources:
            typer.secho(f"  FAILED  {source.source_url}", fg=typer.colors.RED)
            typer.echo(f"      {source.error}")
        for source in bundle_result.orphaned_sources:
            typer.secho(f"  ORPHANED  {source.source_url}", fg=typer.colors.YELLOW)

    counts = report.source_counts()
    typer.echo("")
    source_line = "  ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    if report.not_modified_sources:
        # Worth calling out: these cost a request header and no body at all.
        source_line += f"   (revalidated via 304: {report.not_modified_sources})"
    typer.secho("sources:   " + source_line)
    typer.secho(
        "documents: "
        + ("  ".join(f"{k}={v}" for k, v in sorted(report.action_counts().items())) or "none")
    )
    if report.errors:
        typer.echo("")
        typer.secho(f"run-level errors ({len(report.errors)}):", fg=typer.colors.RED)
        for error in report.errors:
            typer.echo(f"  - {error}")


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", help="Show the version and exit.")
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


def main() -> None:
    configure_logging()
    try:
        app()
    except AwsDocSyncError as exc:  # pragma: no cover - top-level guard
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
