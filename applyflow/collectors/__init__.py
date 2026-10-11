"""Collectors turn a source (an ATS job board, an HN thread) into normalized Jobs."""

from applyflow.collectors.ashby import AshbyCollector
from applyflow.collectors.greenhouse import GreenhouseCollector
from applyflow.collectors.hn import HNWhoIsHiringCollector
from applyflow.collectors.lever import LeverCollector
from applyflow.collectors.workable import WorkableCollector

ATS_COLLECTORS = {
    "greenhouse": GreenhouseCollector,
    "lever": LeverCollector,
    "ashby": AshbyCollector,
    "workable": WorkableCollector,
}

__all__ = [
    "ATS_COLLECTORS",
    "AshbyCollector",
    "GreenhouseCollector",
    "HNWhoIsHiringCollector",
    "LeverCollector",
    "WorkableCollector",
]
