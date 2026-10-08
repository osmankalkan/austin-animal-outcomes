"""
Socrata Open Data API (SODA) Client for Austin Animal Center datasets.
Provides authenticated/anonymous HTTP access with environment configuration.
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()


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
