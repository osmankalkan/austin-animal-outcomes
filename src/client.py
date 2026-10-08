"""
Socrata Open Data API (SODA) Client for Austin Animal Center datasets.
Provides authenticated/anonymous HTTP access with environment configuration.
"""

import os
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


class SodaAuthError(Exception):
    """Raised when authentication or authorization fails (401/403). Non-retryable."""
    pass


class SodaRequestError(Exception):
    """Raised when an unrecoverable request or network error occurs."""
    pass


class SodaClient:
    """Client for retrieving raw data from Socrata SODA API endpoints."""

    def __init__(self):
        self.app_token = os.getenv("AUSTIN_APP_TOKEN", "").strip()
        self.timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", 30))
        self.max_retries = int(os.getenv("MAX_RETRIES", 5))
        self.page_limit = int(os.getenv("PAGE_LIMIT", 1000))

    def _get_headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.app_token and self.app_token != "your_socrata_app_token_here":
            headers["X-App-Token"] = self.app_token
        return headers

    def _make_request(self, url: str, params: dict = None) -> list:
        """
        Executes an HTTP GET request with resilience per Section 3.2 rules:
        - 401/403: Abort immediately, raise SodaAuthError (do NOT retry).
        - 429: Rate limited; wait with exponential backoff (or Retry-After).
        - 5xx: Server errors; wait with exponential backoff and retry.
        - Timeout / ConnectionError: Retry with exponential backoff up to max_retries.
        """
        headers = self._get_headers()
        params = params or {}

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout
                )

                # 401 / 403: Security errors -> Abort immediately!
                if response.status_code in (401, 403):
                    error_msg = f"HTTP {response.status_code} Unauthorized/Forbidden: {response.text.strip()}"
                    logger.error(f"Permanent auth error: {error_msg}")
                    raise SodaAuthError(error_msg)

                # 429: Rate Limit Exceeded -> Exponential backoff
                elif response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait_time = float(retry_after) if retry_after and retry_after.isdigit() else (2 ** attempt)
                    logger.warning(
                        f"Rate limit 429 hit. Backing off for {wait_time}s (attempt {attempt}/{self.max_retries})..."
                    )
                    time.sleep(wait_time)
                    continue

                # 5xx: Transient Server Error -> Exponential backoff
                elif 500 <= response.status_code < 600:
                    wait_time = 2 ** attempt
                    logger.warning(
                        f"Server error {response.status_code}. Retrying in {wait_time}s (attempt {attempt}/{self.max_retries})..."
                    )
                    time.sleep(wait_time)
                    continue

                # 4xx: Other client errors -> Abort immediately
                elif 400 <= response.status_code < 500:
                    error_msg = f"HTTP {response.status_code} Client Error: {response.text.strip()}"
                    logger.error(error_msg)
                    raise SodaRequestError(error_msg)

                # Success
                response.raise_for_status()
                return response.json()

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as net_err:
                wait_time = 2 ** attempt
                logger.warning(
                    f"Network error ({net_err.__class__.__name__}). Retrying in {wait_time}s (attempt {attempt}/{self.max_retries})..."
                )
                if attempt == self.max_retries:
                    raise SodaRequestError(
                        f"Network failure after {self.max_retries} attempts: {net_err}"
                    ) from net_err
                time.sleep(wait_time)

        raise SodaRequestError(f"Failed to fetch data from {url} after {self.max_retries} attempts.")
