"""Swing-only research workflow; production promotion always requires a human."""

from .fitness import FitnessMetrics, balanced_fitness
from .research import SwingResearchEngine

__all__ = ["FitnessMetrics", "SwingResearchEngine", "balanced_fitness"]
