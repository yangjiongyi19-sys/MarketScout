from __future__ import annotations

import pytest

from scripts.sync_product_catalog import _single_instance_lock


def test_product_sync_lock_prevents_overlapping_instances_and_releases(tmp_path):
    with _single_instance_lock(tmp_path):
        with pytest.raises(RuntimeError, match="已在运行"):
            with _single_instance_lock(tmp_path):
                pass

    with _single_instance_lock(tmp_path):
        pass
