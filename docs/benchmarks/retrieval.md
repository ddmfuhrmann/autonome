# Benchmark: does the vector index earn its keep?

Question: with a project memory this small, is pgvector + Ollama worth it over Postgres full-text
search alone?

## 2026-10-31, shortlink

- **Corpus**: shortlink after wave #18: 67 documents (11 handoffs, 32 decisions, plans, tasks, docs).
- **Questions**: 86 synthetic, two per handoff and decision (`autonome-context eval-generate`,
  Haiku, low effort, about US$ 0.40). The prompt asks for paraphrases, and one of the two in
  Portuguese, since the team asks in both languages and the memory is mostly English.
- **Embedding**: nomic-embed-text (768 dims) on Ollama.

`autonome-context eval-run --k 5` (recall counts every expected document; a handoff and the
decisions it records all count):

| Strategy | FTS recall | Vector recall | Hybrid recall | Vector MRR | Hybrid MRR |
|---|---|---|---|---|---|
| section-v1 | 0.21 | **0.46** | 0.39 | 0.42 | 0.40 |
| whole-v1 | 0.26 | **0.44** | 0.44 | 0.46 | 0.36 |
| section-bare-v1 | 0.20 | **0.44** | 0.42 | 0.40 | 0.42 |

Hit@5 (at least one expected document in the top 5), section-v1, by the language of the question:

| | FTS | Vector | Hybrid |
|---|---|---|---|
| English (43) | 0.67 | **0.84** | 0.84 |
| Portuguese (43) | 0.21 | **0.58** | 0.44 |

## Reading

- The vector index earns its keep: about twice the recall of full-text search. On paraphrased
  English it adds 17 points; on Portuguese questions over English text, full-text search barely
  works (0.21) and vectors carry the whole result.
- Hybrid (RRF over vector and full-text) is never better than vector alone here, and on
  Portuguese it is worse: the full-text list is mostly noise and the fusion lets it in.
- nomic-embed-text is English-first; 0.58 on Portuguese is the weak spot.

## Next

1. Make `vector` the default search mode, or down-weight full-text in the fusion when it finds
   little (measure both with the same 86 questions).
2. Try a multilingual embedding model (e.g. bge-m3 or multilingual-e5) as a new strategy in the
   lab, against the same questions.
