"""
Socrata Open Data API (SODA) Client for Austin Animal Center datasets.
Provides authenticated/anonymous HTTP access, error resilience (Section 3.2),
and pagination ($limit / $offset).
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

    def __init__(
        self,
        app_token: str = None,
        timeout: int = None,
        max_retries: int = None,
        page_limit: int = None
    ):
        self.app_token = (app_token or os.getenv("AUSTIN_APP_TOKEN", "")).strip()
        self.timeout = int(timeout or os.getenv("REQUEST_TIMEOUT_SECONDS", 30))
        self.max_retries = int(max_retries or os.getenv("MAX_RETRIES", 5))
        self.page_limit = int(page_limit or os.getenv("PAGE_LIMIT", 1000))

    def _get_headers(self) -> dict:
        """Constructs headers for SODA API requests."""
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

                # 200 OK: Success
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

    def get(self, endpoint_url: str, limit: int = None, offset: int = 0, params: dict = None) -> list:
        """Fetches a single page of records."""
        req_params = (params.copy() if params else {})
        req_params["$limit"] = limit or self.page_limit
        req_params["$offset"] = offset
        req_params.setdefault("$order", ":id")
        return self._make_request(endpoint_url, params=req_params)

    def fetch_all(
        self,
        endpoint_url: str,
        params: dict = None,
        max_records: int = None
    ) -> list:
        """
        Paginates through the entire SODA dataset using $limit and $offset.
        Stops when an empty list is returned or fewer records than limit are received.
        """
        offset = 0
        all_records = []
        base_params = params.copy() if params else {}

        logger.info(f"Starting pagination for endpoint: {endpoint_url} (page size: {self.page_limit})")

        while True:
            page_params = base_params.copy()
            fetch_count = self.page_limit

            if max_records and (len(all_records) + fetch_count > max_records):
                fetch_count = max_records - len(all_records)

            page_params["$limit"] = fetch_count
            page_params["$offset"] = offset
            page_params.setdefault("$order", ":id")

            records = self._make_request(endpoint_url, params=page_params)

            # Boş yanıt kontrolü (Empty response detection)
            if not records or len(records) == 0:
                logger.info("Empty response received. Pagination completed.")
                break

            all_records.extend(records)
            logger.info(f"Fetched {len(records)} records (running total: {len(all_records)})...")

            if len(records) < fetch_count:
                logger.info("Dataset fully consumed (fewer records returned than page limit).")
                break

            offset += len(records)

            if max_records and len(all_records) >= max_records:
                logger.info(f"Reached requested limit of {max_records} records.")
                break

        logger.info(f"Total retrieved records: {len(all_records)}")
        return all_records


if __name__ == "__main__":
    intakes_url = os.getenv(
        "INTAKES_API_URL",
        "https://data.austintexas.gov/resource/pyqf-r2dc.json"
    )
    test_client = SodaClient(page_limit=5)
    print("--- Test: Fetching 10 sample records from Austin Animal Center API ---")
    data = test_client.fetch_all(intakes_url, max_records=10)
    print(f"Success! Retrieved {len(data)} records.")
    if data:
        print("Sample Record ID:", data[0].get("animal_id"))
