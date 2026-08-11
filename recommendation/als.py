"""
recommendation/als.py

Implicit-feedback matrix factorization — Alternating Least Squares, following
Hu, Koren & Volinsky, "Collaborative Filtering for Implicit Feedback Datasets"
(ICDM 2008). Pure numpy/scipy: no native extension, no GPU, no extra service.

The idea in one paragraph. Nobody rates products on this platform, they just
browse and buy, so there are no negative examples — a product a shopper never
opened might be one they'd love or one they'd never touch. ALS handles that by
splitting each observation in two: a *preference* (did they interact at all,
1/0) and a *confidence* in that preference (how strongly — a purchase counts far
more than a glance). Unobserved pairs are treated as preference 0 with the
lowest confidence, so they nudge the model without dominating it. Users and
items are then embedded in a shared latent space, and relevance is a dot product.

Why ALS rather than gradient descent: with the confidence trick the least-squares
step has a closed form, and holding one side fixed makes the other side's rows
independent — so each half-iteration is a batch of small linear solves. At this
catalog size that is seconds, and it converges in ~15 iterations rather than
thousands of epochs.

Cost per half-iteration is O(nnz · f² + n · f³). At 5k products, ~10k shoppers
and f=48 that is well under a minute on one core.
"""

from __future__ import annotations

import logging
import time

import numpy as np
from scipy.sparse import csr_matrix

logger = logging.getLogger(__name__)


def _scale_confidence(data: np.ndarray, alpha: float, epsilon: float = 1.0) -> np.ndarray:
    """
    Turn raw preference weight into confidence.

        c = 1 + alpha · log(1 + r/ε)

    The log matters. Raw weights here span roughly 1 (one view) to 60 (several
    purchases), and feeding that in linearly with a large alpha lets a handful of
    heavy buyers dictate the entire item space. Compressing it keeps a purchase
    decisively more important than a view without letting it become the only
    thing the model sees.

    Returns c − 1, which is the form the solver actually needs.
    """
    return alpha * np.log1p(np.maximum(data, 0.0) / epsilon)


def _solve_side(
    matrix: csr_matrix,
    fixed: np.ndarray,
    regularization: float,
    alpha: float,
) -> np.ndarray:
    """
    Solve one side of the factorization with the other held fixed.

    For each row u the closed-form solution is

        x_u = (YᵀY + Yᵀ(C_u − I)Y + λI)⁻¹ Yᵀ C_u p(u)

    YᵀY is the same for every row, so it is computed once per half-iteration —
    that is the whole trick that makes ALS tractable on implicit data. Only the
    items the shopper actually touched contribute to the correction term, so the
    per-row work is proportional to their history, not to the catalog.
    """
    n_rows = matrix.shape[0]
    n_factors = fixed.shape[1]

    YtY = fixed.T @ fixed
    eye = np.eye(n_factors, dtype=np.float64) * regularization
    base = YtY + eye

    solved = np.zeros((n_rows, n_factors), dtype=np.float64)
    indptr, indices, data = matrix.indptr, matrix.indices, matrix.data

    for row in range(n_rows):
        start, end = indptr[row], indptr[row + 1]
        if start == end:
            continue

        idx = indices[start:end]
        confidence_minus_one = _scale_confidence(data[start:end].astype(np.float64), alpha)

        Y = fixed[idx]                                   # (k, f) — only touched items
        # A = YᵀY + λI + Σ (c−1)·yᵢyᵢᵀ
        A = base + (Y.T * confidence_minus_one) @ Y
        # b = Σ c·yᵢ, with preference p = 1 on every observed pair
        b = Y.T @ (confidence_minus_one + 1.0)

        try:
            solved[row] = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            # Singular only when a row's items are perfectly collinear; the
            # least-squares pseudo-inverse is the right answer there.
            solved[row] = np.linalg.lstsq(A, b, rcond=None)[0]

    return solved


def fit_als(
    matrix: csr_matrix,
    factors: int = 48,
    iterations: int = 15,
    regularization: float = 0.05,
    alpha: float = 20.0,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Factorise the preference matrix into user and item embeddings.

    Returns (user_factors, item_factors), both float32, shapes (n_users, f) and
    (n_items, f). Relevance of item i to user u is `user_factors[u] @ item_factors[i]`.

    Hyperparameters are chosen by train.py from the dataset's shape rather than
    fixed here — a sparse early-stage catalog wants fewer factors and stronger
    regularization than a dense one, and guessing wrong in that direction is how
    a recommender ends up confidently recommending noise.
    """
    n_users, n_items = matrix.shape
    if n_users == 0 or n_items == 0:
        return (
            np.zeros((n_users, factors), dtype=np.float32),
            np.zeros((n_items, factors), dtype=np.float32),
        )

    factors = int(max(2, min(factors, n_users - 1, n_items - 1))) if min(n_users, n_items) > 2 else 2

    rng = np.random.default_rng(seed)
    # Small random init: large values make the first solve ill-conditioned.
    user_factors = rng.normal(0, 0.01, size=(n_users, factors))
    item_factors = rng.normal(0, 0.01, size=(n_items, factors))

    matrix_csr = matrix.tocsr()
    matrix_csc = matrix.T.tocsr()          # item-major view for the item half-step

    started = time.perf_counter()
    for iteration in range(iterations):
        user_factors = _solve_side(matrix_csr, item_factors, regularization, alpha)
        item_factors = _solve_side(matrix_csc, user_factors, regularization, alpha)

        if iteration in (0, iterations - 1) or (iteration + 1) % 5 == 0:
            logger.debug(
                "als: iteration %d/%d (%.1fs elapsed)",
                iteration + 1, iterations, time.perf_counter() - started,
            )

    logger.info(
        "als: fitted %d×%d with %d factors in %d iterations (%.1fs)",
        n_users, n_items, factors, iterations, time.perf_counter() - started,
    )
    return user_factors.astype(np.float32), item_factors.astype(np.float32)


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    """
    L2-normalise so a dot product is a cosine.

    Without this, similarity is dominated by vector magnitude — which in ALS
    tracks popularity, not similarity. That is exactly how a recommender ends up
    telling every shopper that the best-selling item is similar to everything.
    """
    if vectors.size == 0:
        return vectors
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32)


def recommend_for_user(
    user_vector: np.ndarray,
    item_factors: np.ndarray,
    n: int = 50,
    exclude: set[int] | None = None,
) -> list[tuple[int, float]]:
    """
    Top-N item indices for one user vector, highest score first.

    Uses argpartition rather than a full sort: finding the top 50 of 5,000 does
    not require ordering the other 4,950.
    """
    if user_vector is None or item_factors.size == 0:
        return []

    scores = item_factors @ user_vector
    if exclude:
        blocked = np.fromiter(exclude, dtype=np.int64, count=len(exclude))
        blocked = blocked[(blocked >= 0) & (blocked < scores.shape[0])]
        scores[blocked] = -np.inf

    n = min(n, scores.shape[0])
    if n <= 0:
        return []

    top = np.argpartition(-scores, n - 1)[:n]
    top = top[np.argsort(-scores[top])]
    return [(int(i), float(scores[i])) for i in top if np.isfinite(scores[i])]


def top_k_neighbors(
    normalized: np.ndarray,
    k: int = 30,
    block_size: int = 512,
    min_score: float = 0.05,
) -> dict[int, list[tuple[int, float]]]:
    """
    Cosine top-K nearest neighbours for every item.

    Computed in row blocks so peak memory is (block_size × n_items) floats rather
    than the full n_items² similarity matrix — 5k items would otherwise want
    100 MB in one allocation, and the number grows quadratically with the catalog.
    """
    n_items = normalized.shape[0]
    if n_items < 2:
        return {}

    k = min(k, n_items - 1)
    neighbors: dict[int, list[tuple[int, float]]] = {}

    for start in range(0, n_items, block_size):
        end = min(start + block_size, n_items)
        block = normalized[start:end] @ normalized.T          # (b, n_items)

        # An item is trivially its own nearest neighbour.
        for local, absolute in enumerate(range(start, end)):
            block[local, absolute] = -np.inf

        for local, absolute in enumerate(range(start, end)):
            scores = block[local]
            top = np.argpartition(-scores, k - 1)[:k]
            top = top[np.argsort(-scores[top])]
            picks = [
                (int(j), float(scores[j]))
                for j in top
                if np.isfinite(scores[j]) and scores[j] >= min_score
            ]
            if picks:
                neighbors[absolute] = picks

    return neighbors
