"""Ticker symbol validation, shared by the API and the seed universe."""

from __future__ import annotations

import re

# Covers ordinary symbols plus class shares and foreign listings (BRK.B, RY.TO).
SYMBOL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,11}$")


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def is_valid_symbol(symbol: str) -> bool:
    return bool(SYMBOL_PATTERN.match(symbol))
