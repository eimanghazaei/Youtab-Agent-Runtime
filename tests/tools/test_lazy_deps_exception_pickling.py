"""``FeatureUnavailable`` must survive crossing a process boundary.

It is raised when an optional dependency is missing — which is precisely the
kind of failure that happens inside a worker process, where the message is the
only thing the operator will ever see.

``RuntimeError``'s default ``__reduce__`` replays ``cls(*self.args)``, and
``args`` here is the single formatted string passed to ``super().__init__``.
So an unmodified reconstruction calls ``FeatureUnavailable(<message>)`` and
raises ``TypeError: __init__() missing 2 required positional arguments``. The
parent process then sees that TypeError *instead of* the real diagnosis.

This is not hypothetical. Evaluating a wake-word candidate under a
``ProcessPoolExecutor``, a worker hit a missing dependency; the parent received
``BrokenProcessPool`` with the genuine cause destroyed at the boundary, and the
run looked like a memory problem rather than a missing file.
"""

from __future__ import annotations

import pickle
from concurrent.futures import ProcessPoolExecutor

import pytest

from tools.lazy_deps import FeatureUnavailable


def test_round_trips_through_pickle() -> None:
    original = FeatureUnavailable("wake-word", ("openwakeword==0.6.0",), "not installed")
    restored = pickle.loads(pickle.dumps(original))

    assert isinstance(restored, FeatureUnavailable)
    assert restored.feature == original.feature
    assert restored.missing == original.missing
    assert restored.reason == original.reason
    assert str(restored) == str(original), (
        "the reconstructed exception no longer formats the same message, so a "
        "worker-side failure would read differently from a local one"
    )


def _raise_it() -> None:
    raise FeatureUnavailable("wake-word", ("onnxruntime==1.27.0",), "import failed")


def test_the_real_exception_crosses_a_process_pool() -> None:
    """The end-to-end case: raised in a child, caught intact in the parent.

    A pickle round-trip in one process is necessary but not sufficient — the
    failure mode this guards appeared only when the exception travelled through
    a pool's result reader.
    """
    with ProcessPoolExecutor(max_workers=1) as pool:
        with pytest.raises(FeatureUnavailable) as caught:
            pool.submit(_raise_it).result()

    assert caught.value.feature == "wake-word"
    assert caught.value.missing == ("onnxruntime==1.27.0",)
    assert "import failed" in str(caught.value), (
        "the reason was lost crossing the process boundary"
    )
