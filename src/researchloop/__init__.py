"""researchloop: an evidence-first, resumable research agent."""

from .budget import Budget, BudgetExceeded
from .ledger import EvidenceLedger
from .llm import LLM, Completion, OpenAICompatLLM
from .runtime import ResearchRuntime, RunResult
from .search import LocalCorpusSearch, SearchHit, SearchProvider, TavilySearch
from .store import RunStore

__all__ = [
    "Budget",
    "BudgetExceeded",
    "Completion",
    "EvidenceLedger",
    "LLM",
    "LocalCorpusSearch",
    "OpenAICompatLLM",
    "ResearchRuntime",
    "RunResult",
    "RunStore",
    "SearchHit",
    "SearchProvider",
    "TavilySearch",
]
__version__ = "0.1.0"
