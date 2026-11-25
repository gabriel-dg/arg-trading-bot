# Contributing

Thanks for your interest. This is an educational project: clear, tested, small changes
are the most welcome.

## Before you start

- **Questions and ideas**: open a [Discussion](../../discussions). Strategy ideas go in
  the *Strategies* category; please include the hypothesis and how you would test it.
- **Bugs**: open an issue with the *Bug report* template.
- **Strategy proposals** that need code: open an issue with the *Strategy proposal*
  template first, so we can agree on scope before you write it.

## Development

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
ruff check .
```

All tests run offline. A change that needs the network or a real account in a test
will not be merged.

## Pull requests

1. One topic per pull request, with tests for new behavior.
2. `pytest -q` and `ruff check .` pass.
3. No real account data anywhere: no credentials, account numbers, balances,
   positions, orders or screenshots. Use synthetic fixtures with placeholder symbols.
4. New broker adapters implement `argbot.brokers.base.Broker` and `MarketData` and come
   with offline tests (see `tests/test_paper_broker.py` and the IOL adapter tests).
5. Explain the *why* in the description. Performance claims need the method and data.

By contributing you agree that your contribution is licensed under the MIT license.
Be kind and constructive; personal attacks or harassment are not tolerated.
