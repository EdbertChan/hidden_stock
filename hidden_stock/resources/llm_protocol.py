from typing import Protocol, runtime_checkable


@runtime_checkable
class FilingLLM(Protocol):
    """Provider-agnostic interface for SEC filing extract jobs.
    Implementations: GeminiResource today; Claude/Haiku/etc. can swap in."""

    def classify_footnote(self, ticker: str, filing_text: str) -> dict: ...

    def describe_business(self, ticker: str, filing_text: str) -> dict: ...

    def extract_equity_holdings(self, ticker: str, filing_text: str) -> dict: ...
