"""
recommendation/evaluation.py

Offline evaluation of a trained model against a held-out split.

This module exists to answer one question honestly: **is the model better than
simply showing everyone the best-sellers?** That is a real bar, and a
surprising number of recommenders in production do not clear it. Every metric
here is reported alongside the popularity baseline computed on the same split,
so the comparison cannot be quietly skipped.

Metrics, and what each one catches that the others miss:

  precision@10  How much of the rail was useful. Directly what a shopper feels.
  recall@10     How much of what they went on to want was surfaced. Catches a
                model that is precise about a narrow slice and blind elsewhere.
  MAP@10        Rewards putting the right items near the top. Precision alone is
                indifferent to whether the hit was slot 1 or slot 10; shoppers
                are not.
  NDCG@10       Same concern, with a smoother log discount.
  coverage      Share of the catalog that ever appears in anyone's top 10. A
                model can score well on all of the above by recommending the
                same fifty products to everyone — which is a popularity list
                wearing a costume, and terrible for a marketplace whose sellers
                need distribution.
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.sparse import csr_matrix

logger = logging.getLogger(__name__)

K = 10


def _top_k_for_row(scores: np.ndarray, exclude: np.ndarray, k: int) -> np.ndarray:
    """Top-k item indices for one shopper, with their training items masked out."""
    masked = scores.copy()
    if exclude.size:
        masked[exclude] = -np.inf
    k = min(k, masked.shape[0])
    if k <= 0:
        return np.empty(0, dtype=np.int64)
    top = np.argpartition(-masked, k - 1)[:k]
    return top[np.argsort(-masked[top])]


def _dcg(hits: np.ndarray) -> float:
    """Discounted cumulative gain for a binary relevance vector."""
    if hits.size == 0:
        return 0.0
    discounts = 1.0 / np.log2(np.arange(2, hits.size + 2))
    return float((hits * discounts).sum())


def _score_ranking(ranked: np.ndarray, relevant: set[int], k: int) -> tuple[float, float, float, float]:
    """(precision, recall, average precision, NDCG) for one shopper's ranking."""
    if ranked.size == 0 or not relevant:
        return 0.0, 0.0, 0.0, 0.0

    hits = np.array([1.0 if int(item) in relevant else 0.0 for item in ranked])
    n_hits = float(hits.sum())

    precision = n_hits / len(ranked)
    recall = n_hits / len(relevant)

    if n_hits > 0:
        cumulative = np.cumsum(hits)
        positions = np.arange(1, len(hits) + 1)
        average_precision = float(((cumulative / positions) * hits).sum() / min(len(relevant), k))
    else:
        average_precision = 0.0

    ideal = _dcg(np.ones(min(len(relevant), k)))
    ndcg = (_dcg(hits) / ideal) if ideal > 0 else 0.0

    return precision, recall, average_precision, ndcg


def evaluate_model(
    train: csr_matrix,
    holdout: dict[int, set[int]],
    user_factors: np.ndarray,
    item_factors: np.ndarray,
    k: int = K,
) -> dict:
    """
    Score the fitted model on held-out interactions.

    Only shoppers with held-out items are evaluated — everyone else contributes
    no information and would just dilute the averages toward zero.
    """
    if not holdout or user_factors.size == 0 or item_factors.size == 0:
        return {}

    precisions, recalls, average_precisions, ndcgs = [], [], [], []
    recommended_items: set[int] = set()

    for row, relevant in holdout.items():
        if row >= user_factors.shape[0]:
            continue
        scores = item_factors @ user_factors[row]
        start, end = train.indptr[row], train.indptr[row + 1]
        ranked = _top_k_for_row(scores, train.indices[start:end], k)
        recommended_items.update(int(item) for item in ranked)

        precision, recall, average_precision, ndcg = _score_ranking(ranked, relevant, k)
        precisions.append(precision)
        recalls.append(recall)
        average_precisions.append(average_precision)
        ndcgs.append(ndcg)

    if not precisions:
        return {}

    return {
        f'precision_at_{k}': float(np.mean(precisions)),
        f'recall_at_{k}': float(np.mean(recalls)),
        f'map_at_{k}': float(np.mean(average_precisions)),
        f'ndcg_at_{k}': float(np.mean(ndcgs)),
        'catalog_coverage': len(recommended_items) / item_factors.shape[0],
        'evaluated_users': len(precisions),
    }


def evaluate_popularity_baseline(
    train: csr_matrix,
    holdout: dict[int, set[int]],
    k: int = K,
) -> dict:
    """
    The bar: recommend the most-interacted products to everyone, minus what each
    shopper has already seen.

    Trivial to compute and hard to beat on sparse data, which is exactly why it
    is the right control. A model that cannot clear this is costing compute for
    nothing, and train.py records the comparison on every run.
    """
    if not holdout:
        return {}

    popularity = np.asarray(train.sum(axis=0)).ravel()

    precisions, recalls, average_precisions, ndcgs = [], [], [], []
    for row, relevant in holdout.items():
        start, end = train.indptr[row], train.indptr[row + 1]
        ranked = _top_k_for_row(popularity, train.indices[start:end], k)
        precision, recall, average_precision, ndcg = _score_ranking(ranked, relevant, k)
        precisions.append(precision)
        recalls.append(recall)
        average_precisions.append(average_precision)
        ndcgs.append(ndcg)

    if not precisions:
        return {}

    return {
        f'precision_at_{k}': float(np.mean(precisions)),
        f'recall_at_{k}': float(np.mean(recalls)),
        f'map_at_{k}': float(np.mean(average_precisions)),
        f'ndcg_at_{k}': float(np.mean(ndcgs)),
    }
