import dagster as dg
from anthropic import Anthropic

CLASSIFICATION_RUBRIC = """You are given the plain text of a company's SEC 10-K or 10-Q filing. Find the inventory accounting-policy footnote yourself — it's usually a numbered note in "Notes to Financial Statements" discussing how the company values/costs its inventory (LIFO, FIFO, weighted-average, etc). It is NOT a balance-sheet line item that just reads "Inventories  $X" — keep looking for the actual policy discussion.

Classify the inventory costing method and flag any notable quirks, such as:
- LIFO reserve disclosures
- A recent change in costing method (e.g. switching between FIFO and LIFO)
- Use of multiple methods across different inventory pools
- Vague or unusual language about the costing method

If you cannot find an actual inventory costing-method footnote anywhere in the text, classify method as "unclear" and say so in quirk_notes — do not classify based on a balance-sheet line item alone.

You must copy an exact, verbatim quote (not paraphrased) from the provided text into source_quote, supporting your classification. If method is "unclear" because no such footnote exists, quote the closest inventory-related text you found instead.

Also determine whether financials are under US GAAP or IFRS (look for "U.S. GAAP", "IFRS as issued by the IASB", etc.). Set accounting_basis accordingly. IFRS does not permit LIFO.

Also extract lifo_reserve_usd when a LIFO reserve dollar amount is disclosed: a raw number (e.g. 150000000 for $150 million), not millions. If no amount is given, or accounting_basis is IFRS, set lifo_reserve_usd to null.

Respond only by calling the record_classification tool."""

CLASSIFICATION_TOOL = {
    "name": "record_classification",
    "description": "Record the inventory accounting classification for a stock.",
    "input_schema": {
        "type": "object",
        "properties": {
            "accounting_basis": {
                "type": "string",
                "enum": ["US_GAAP", "IFRS", "other", "unclear"],
            },
            "method": {"type": "string", "enum": ["LIFO", "FIFO", "other", "unclear"]},
            "lifo_reserve_disclosed": {"type": "boolean"},
            "lifo_reserve_usd": {
                "type": ["number", "null"],
                "description": "Most recent LIFO reserve in raw USD, or null if not disclosed as a number.",
            },
            "method_change_disclosed": {"type": "boolean"},
            "quirk_notes": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "source_quote": {
                "type": "string",
                "description": "Exact verbatim quote from the provided filing text supporting this classification.",
            },
        },
        "required": [
            "accounting_basis",
            "method",
            "lifo_reserve_disclosed",
            "lifo_reserve_usd",
            "method_change_disclosed",
            "quirk_notes",
            "confidence",
            "source_quote",
        ],
    },
}


BUSINESS_DESCRIPTION_RUBRIC = """You are given the plain text of a company's SEC 10-K filing. Find the "Item 1. Business" section yourself — it's the section describing what the company actually does (products, services, business model), NOT the one-line table-of-contents entry for it, and NOT a stray cross-reference to it elsewhere in the document ("as described in Item 1. Business...").

Write ONE plain-English sentence describing what the company actually does. Avoid boilerplate legal language, ticker symbols, and stock exchange references.

You must copy an exact, verbatim quote (not paraphrased) from the Business section into source_quote, supporting your description.

Respond only by calling the record_description tool."""

BUSINESS_DESCRIPTION_TOOL = {
    "name": "record_description",
    "description": "Record a one-sentence plain-English description of what a company does.",
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "source_quote": {
                "type": "string",
                "description": "Exact verbatim quote from the filing's Business section supporting this description.",
            },
        },
        "required": ["description", "source_quote"],
    },
}


class AnthropicResource(dg.ConfigurableResource):
    api_key: str = dg.EnvVar("ANTHROPIC_API_KEY")
    model: str = "claude-sonnet-5"

    def describe_business(self, ticker: str, filing_text: str) -> dict:
        client = Anthropic(api_key=self.api_key)
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": BUSINESS_DESCRIPTION_RUBRIC,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {"role": "user", "content": f"Ticker: {ticker}\n\nFiling text:\n{filing_text}"}
            ],
            tools=[BUSINESS_DESCRIPTION_TOOL],
            tool_choice={"type": "tool", "name": "record_description"},
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        raise ValueError(f"No tool_use block in Claude response for {ticker}")

    def classify_footnote(self, ticker: str, filing_text: str) -> dict:
        client = Anthropic(api_key=self.api_key)
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": CLASSIFICATION_RUBRIC,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {"role": "user", "content": f"Ticker: {ticker}\n\nFiling text:\n{filing_text}"}
            ],
            tools=[CLASSIFICATION_TOOL],
            tool_choice={"type": "tool", "name": "record_classification"},
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        raise ValueError(f"No tool_use block in Claude response for {ticker}")

    def extract_equity_holdings(self, ticker: str, filing_text: str) -> dict:
        import json
        import re

        from ..quirks.holdings.extract import EXTRACT_HOLDINGS_PROMPT_TEMPLATE

        client = Anthropic(api_key=self.api_key)
        prompt = EXTRACT_HOLDINGS_PROMPT_TEMPLATE.format(ticker=ticker, filing_text=filing_text)
        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text"
        )
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return json.loads(cleaned)
