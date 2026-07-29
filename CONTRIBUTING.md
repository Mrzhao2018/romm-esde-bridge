# Contributing

Issues and pull requests are welcome. Please keep changes focused and explain
the RomM version, client operating system and emulator involved.

Before submitting a pull request:

```bash
python3 -m py_compile *.py bootstrap/*.py
python3 -m unittest discover -p 'test_*.py' -v
```

Never commit ROMs, BIOS, artwork collections, save data, generated catalogues,
Client API Tokens, `.env` files or machine-specific configuration. Add tests for
catalogue normalization, archive validation, synchronization merges and path
handling when changing those areas.

The current built-in ES-DE system targets PC-98/NP2Kai. New platforms should
add an explicit launch profile instead of weakening validation in the existing
profile.
