from uuid import uuid4

import pytest

from app.services.deployment_queue import DISPATCH_PREFIX, DispatchMarker, epoch_milliseconds


def test_dispatch_keys_sort_by_due_time_and_preserve_job_revision():
    now = epoch_milliseconds()
    job_id = uuid4()
    first = DispatchMarker(now, "transfer", job_id, uuid4())
    next_run = DispatchMarker(now + 60_000, "transfer", job_id, uuid4())
    assert first.key < next_run.key
    assert DispatchMarker.from_key(first.key) == first
    assert DispatchMarker.from_key(next_run.key) == next_run
    assert first.revision != next_run.revision


def test_dispatch_keys_reject_other_object_paths_and_unrecognized_job_types():
    marker = DispatchMarker(epoch_milliseconds(), "video", uuid4(), uuid4())
    with pytest.raises(ValueError):
        DispatchMarker.from_key(marker.key.removeprefix(DISPATCH_PREFIX))
    with pytest.raises(ValueError):
        DispatchMarker.from_key(marker.key.replace("/video/", "/unknown/"))
    with pytest.raises(ValueError):
        DispatchMarker.from_key(marker.key + ".json")
