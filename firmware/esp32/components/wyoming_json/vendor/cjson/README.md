# cJSON dependency

Unmodified upstream cJSON v1.7.19, MIT licensed. Source:
https://github.com/DaveGamble/cJSON/tree/v1.7.19

Files and SHA-256:

| File | SHA-256 |
| --- | --- |
| cJSON.c | `298581a04a36c0165da4b0aade235c23088cb2faa58651d720ea2f3706ed0b0d` |
| cJSON.h | `25b0145150d500498e4d209cec69c18c42cf818bffcc54690be3b895a2a16dee` |
| LICENSE | `a36dda207c36db5818729c54e7ad4e8b0c6fba847491ba64f372c1a2037b6d5c` |

Host tests compile this exact source with `CJSON_NESTING_LIMIT=16`.
Firmware build integration must use the same source and depth bound, not silently
substitute a different SDK JSON library. Actual ESP-IDF integration remains pending.
