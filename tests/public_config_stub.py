"""
tests/public_config_stub.py — the minimal configuration needed by the public lookahead test.

The live system's config.py is not published (it reads broker credentials from a private .env).
The lookahead test needs exactly one value from it: the number of bars in each strategy window.
That value is reproduced here with its default from config.py
(SymbolConfig.candles_lookback = CANDLES_LOOKBACK, default "200").
"""
from types import SimpleNamespace

CFG = SimpleNamespace(symbol=SimpleNamespace(candles_lookback=200))
