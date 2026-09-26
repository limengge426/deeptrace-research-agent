# Retrieval evaluation

2868 chunks from 40 pinned Wikipedia articles. Embeddings: `sentence-transformers/all-MiniLM-L6-v2` in a Faiss inner-product index. Hybrid = Reciprocal Rank Fusion. Gold articles are hand-labelled ([labels](../data/retrieval_labels.json)).

## Questions (48 queries: the 48 research questions)

| Method | Recall@5 | Recall@10 | Hit@5 | MRR@10 |
|---|--:|--:|--:|--:|
| bm25 | 0.920 | 0.972 | 1.000 | 0.969 |
| vector | 0.920 | 0.938 | 1.000 | 1.000 |
| hybrid (bm25+vector) | 0.917 | 0.944 | 1.000 | 1.000 |
| graph | 0.906 | 0.955 | 1.000 | 0.908 |
| hybrid (bm25+vector+graph) | 0.941 | 0.979 | 1.000 | 1.000 |

## Paraphrases (40 queries: one query per article, written without the article title's words)

| Method | Recall@5 | Recall@10 | Hit@5 | MRR@10 |
|---|--:|--:|--:|--:|
| bm25 | 1.000 | 1.000 | 1.000 | 0.927 |
| vector | 1.000 | 1.000 | 1.000 | 0.983 |
| hybrid (bm25+vector) | 1.000 | 1.000 | 1.000 | 0.963 |
| graph | 0.600 | 0.750 | 0.600 | 0.523 |
| hybrid (bm25+vector+graph) | 1.000 | 1.000 | 1.000 | 0.942 |
