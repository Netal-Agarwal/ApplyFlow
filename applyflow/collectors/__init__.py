"""Collectors turn a source (an ATS job board, an HN thread) into normalized Jobs."""

from applyflow.collectors.ashby import AshbyCollector
from applyflow.collectors.greenhouse import GreenhouseCollector
from applyflow.collectors.hn import HNWhoIsHiringCollector
from applyflow.collectors.lever import LeverCollector

ATS_COLLECTORS = {
    "greenhouse": GreenhouseCollector,
    "lever": LeverCollector,
    "ashby": AshbyCollector,
}

__all__ = [
    "ATS_COLLECTORS",
    "AshbyCollector",
    "GreenhouseCollector",
    "HNWhoIsHiringCollector",
    "LeverCollector",
]
