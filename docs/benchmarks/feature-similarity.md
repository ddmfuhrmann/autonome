# Feature similarity

`store.resolve_feature` refuses a new feature whose description is too close to one already in the
project's catalog. The limit is `store.FEATURE_SIMILAR_AT`, a cosine similarity between the
embeddings of the two descriptions (`purpose="document"`).

## Method

Descriptions written the way a plan would write them, in pt-BR, for pairs of features the project
has or had (`test_feature_similarity_ollama` in `context/tests/test_feature_catalog.py` holds the
texts). Embedder: `nomic-embed-text` on Ollama. The test skips when Ollama does not answer.

## Measured (2026-11-01)

| Pair | Should be | Similarity |
| --- | --- | --- |
| review-loop × review-history | same feature | 0.873 |
| models × models-config | same feature | 0.822 |
| memory × maintenance | different | 0.748 |
| plans × retro | different | 0.718 |
| gates × ui | different | 0.705 |
| runner × triage | different | 0.694 |

The lowest pair that must be caught is 0.822; the highest that must pass is 0.748. The limit is the
middle of that gap: **`FEATURE_SIMILAR_AT = 0.785`**.

## Caveats

- The gap is 0.074 wide, from 12 descriptions: a hint, not a proof. Descriptions that are very short
  or share many words will score higher.
- Changing the embedding model means measuring again; only features embedded by the same model are
  compared.
- The limit only refuses; the caller decides what to do with `similar_to` (the handoff and decision
  paths file the work under the existing feature).
