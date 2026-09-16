"""Two Hypothesis budgets: the default one the whole suite pays, and a deeper one for `make
test-props`, so properties get a hard run without slowing every `make test`."""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile("default", max_examples=50)
settings.register_profile(
    "thorough", max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))
