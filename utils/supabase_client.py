"""Supabase client singleton for the Store Anomaly Monitor API."""

import os

from supabase import Client, create_client

_client: Client | None = None


def get_supabase() -> Client:
    """Return the shared Supabase client singleton.

    Reads SUPABASE_URL and SUPABASE_KEY from environment variables.
    Creates the client on the first call and reuses it on subsequent calls.
    """
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client
