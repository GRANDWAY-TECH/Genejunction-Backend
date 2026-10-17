"""AI lab report summaries (a paid extra, 10 credits per report).

The model call is behind this interface so the provider can be chosen per deployment.
The default implementation produces a structured placeholder and never invents clinical findings.
"""

from typing import Protocol


class ReportSummarizer(Protocol):
    async def summarize(self, *, tests: list[dict], report_url: str) -> str: ...


class PlaceholderSummarizer:
    async def summarize(self, *, tests: list[dict], report_url: str) -> str:
        names = ", ".join(t["name"] for t in tests)
        return (f"Your report for {names} is ready. A plain-language summary will appear here once the "
                "AI summary provider is configured. Always discuss results with your doctor.")


summarizer: ReportSummarizer = PlaceholderSummarizer()
