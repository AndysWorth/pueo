Add a new configuration key to Pueo. The key to add: $ARGUMENTS

Follow this exact sequence — all three locations must be updated together:

1. **`config.py`** — add a typed constant in the appropriate section (`_ha`, `_ollama`, or `_agent`) with a sensible default matching `config.yaml.default`.

2. **`config.yaml.default`** — add the key under the correct section with an inline comment explaining what it controls and any valid value range.

3. **`setup.sh`** — add an `ask` call in the "Configuration" section (section 4) with a matching default value. Place it near related keys.

After making the changes, verify by grepping for the new key name across all three files to confirm consistency. Then update CLAUDE.md's Key Patterns section if this key affects a documented invariant.
