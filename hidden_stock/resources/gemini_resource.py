import json
import re
import time

import dagster as dg
from google import genai
from google.genai import types
from google.genai.errors import ServerError

from ..quirks.holdings.extract import EXTRACT_HOLDINGS_PROMPT_TEMPLATE
from .claude_cli_resource import (
    BUSINESS_DESCRIPTION_PROMPT_TEMPLATE,
    CLASSIFICATION_PROMPT_TEMPLATE,
)


def _parse_json_object(text: str) -> dict:
    """Gemini sometimes wraps JSON in markdown fences; strip and parse."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


class GeminiResource(dg.ConfigurableResource):
    """Gemini Flash for filing extract jobs (LIFO/FIFO + business blurb).
    Uses GEMINI_API_KEY — does not touch Claude Code session limits."""

    api_key: str = dg.EnvVar("GEMINI_API_KEY")
    model: str = "gemini-3.6-flash"

    def _client(self) -> genai.Client:
        # Cache on the instance — creating a Client per call can race with
        # SDK cleanup and raise "client has been closed".
        if not hasattr(self, "_cached_client") or self._cached_client is None:
            self._cached_client = genai.Client(api_key=self.api_key)
        return self._cached_client

    def _generate_json(self, prompt: str) -> dict:
        last_err: Exception | None = None
        for attempt in range(5):
            try:
                response = self._client().models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0,
                        response_mime_type="application/json",
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                    ),
                )
                text = response.text
                if not text:
                    raise RuntimeError(f"Gemini {self.model} returned empty text")
                return _parse_json_object(text)
            except ServerError as e:
                last_err = e
                time.sleep(2**attempt)
        raise RuntimeError(f"Gemini {self.model} failed after retries: {last_err}")

    def classify_footnote(self, ticker: str, filing_text: str) -> dict:
        prompt = CLASSIFICATION_PROMPT_TEMPLATE.format(ticker=ticker, filing_text=filing_text)
        return self._generate_json(prompt)

    def describe_business(self, ticker: str, filing_text: str) -> dict:
        prompt = BUSINESS_DESCRIPTION_PROMPT_TEMPLATE.format(ticker=ticker, filing_text=filing_text)
        return self._generate_json(prompt)

    def extract_equity_holdings(self, ticker: str, filing_text: str) -> dict:
        prompt = EXTRACT_HOLDINGS_PROMPT_TEMPLATE.format(ticker=ticker, filing_text=filing_text)
        return self._generate_json(prompt)
