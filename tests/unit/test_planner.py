import pytest

from aws_doc_sync.domain.models import BundleDocument, StoredDocument, SyncAction
from aws_doc_sync.sync.planner import decide_action

DOC = BundleDocument(
    bundle_id="b", collection_id="c", name="AWS_Example",
    markdown="# x\n", composition_hash="sha256:current",
)
EXISTING = StoredDocument(id="doc-1", name="AWS_Example")


def action(**kwargs) -> SyncAction:
    kwargs.setdefault("existing", None)
    kwargs.setdefault("previous_hash", None)
    kwargs.setdefault("bundle_complete", True)
    return decide_action(DOC, **kwargs)[0]


def test_create_when_nothing_exists():
    assert action() is SyncAction.CREATE


def test_no_change_when_the_hash_matches():
    assert action(existing=EXISTING, previous_hash="sha256:current") is SyncAction.NO_CHANGE


def test_update_when_the_hash_moved():
    assert action(existing=EXISTING, previous_hash="sha256:old") is SyncAction.UPDATE


def test_existing_document_with_no_recorded_hash_is_adopted_not_duplicated():
    """Manifest loss must not spawn a second copy of a knowledge source.

    The document is rewritten once so that stored state and Drive agree again.
    """
    resolved, reason = decide_action(
        DOC, existing=EXISTING, previous_hash=None, bundle_complete=True
    )
    assert resolved is SyncAction.UPDATE
    assert "adopting" in reason


def test_recorded_hash_without_a_document_still_creates():
    # The doc was deleted in Drive; the hash alone must not suppress recreation.
    assert action(existing=None, previous_hash="sha256:current") is SyncAction.CREATE


def test_incomplete_bundle_never_overwrites_a_good_document():
    resolved, reason = decide_action(
        DOC, existing=EXISTING, previous_hash="sha256:old", bundle_complete=False
    )
    assert resolved is SyncAction.SKIPPED_INCOMPLETE
    assert "left untouched" in reason


def test_incomplete_bundle_does_not_create_a_partial_document():
    resolved, reason = decide_action(
        DOC, existing=None, previous_hash=None, bundle_complete=False
    )
    assert resolved is SyncAction.SKIPPED_INCOMPLETE
    assert "partial" in reason


@pytest.mark.parametrize(
    ("existing", "previous", "expected"),
    [(None, None, SyncAction.CREATE), (EXISTING, "sha256:old", SyncAction.UPDATE)],
)
def test_allow_partial_overrides_the_incompleteness_guard(existing, previous, expected):
    assert action(
        existing=existing, previous_hash=previous, bundle_complete=False, allow_partial=True
    ) is expected


def test_no_change_still_applies_when_incomplete_but_forced():
    # Even forced, matching content must not trigger a pointless write.
    assert action(
        existing=EXISTING, previous_hash="sha256:current",
        bundle_complete=False, allow_partial=True,
    ) is SyncAction.NO_CHANGE


def test_a_run_where_every_document_failed_is_not_a_partial_failure():
    """Exit 1 tells a scheduler some sources synced. A total outage did not."""
    from aws_doc_sync.sync.results import BundleResult, DocumentPlan, SyncReport

    report = SyncReport()
    for name in ("AWS_A", "AWS_B"):
        result = BundleResult(bundle_id=name, collection_id="c", output=name)
        result.documents.append(
            DocumentPlan(name=name, bundle_id=name, action=SyncAction.ERROR, error="down")
        )
        report.bundles.append(result)

    assert report.exit_code() == 2


def test_a_run_where_one_document_failed_is_a_partial_failure():
    from aws_doc_sync.sync.results import BundleResult, DocumentPlan, SyncReport

    report = SyncReport()
    result = BundleResult(bundle_id="b", collection_id="c", output="AWS_A")
    result.documents.append(
        DocumentPlan(name="AWS_A", bundle_id="b", action=SyncAction.ERROR, error="down")
    )
    result.documents.append(
        DocumentPlan(name="AWS_B", bundle_id="b", action=SyncAction.NO_CHANGE)
    )
    report.bundles.append(result)

    assert report.exit_code() == 1
