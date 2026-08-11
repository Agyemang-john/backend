"""
recommendation/train.py

Orchestrates one full training run, end to end:

    dataset → hyperparameters → ALS → evaluation → content model
            → neighbour blend → co-purchase → persistence → user rails

Design rules this pipeline follows:

* **Every run is evaluated before it is published.** The model is fit once on a
  time-split for scoring, then refit on the complete data for production. Costs
  roughly double the compute, buys an honest number on every single run rather
  than a benchmark someone ran once in March.

* **Hyperparameters are derived from the data, not hardcoded.** 64 latent factors
  on 300 interactions does not learn taste, it memorises noise and then
  recommends it with great confidence. Capacity scales with evidence.

* **Failure is contained.** The run is marked `running`, and serving only ever
  reads the newest `completed` run. A crash mid-pipeline leaves a failed row and
  the previous model still serving — never a half-written one.

* **The pipeline never hard-fails on thin data.** Below the CF threshold it skips
  ALS, builds content embeddings anyway, and produces a fully working
  content-based storefront. That is the correct behaviour for a young
  marketplace, and it means this can be deployed before there is any data at all.
"""

from __future__ import annotations

import logging
import time

import numpy as np
from django.db import transaction

from . import als, content, dataset as dataset_module, deals, evaluation, ranker, similarity
from .models import ModelRun, ProductEmbedding, UserEmbedding

logger = logging.getLogger(__name__)


#: Content-model width. Decoupled from the collaborative factor count on purpose:
#: the two spaces are only ever compared *within* themselves (cosine of CF against
#: CF, content against content) and blended as scalars, so they are free to have
#: the ranks that suit them. Text genuinely carries dozens of independent
#: dimensions — brand, material, use case, register — and starving the content
#: model to match a small CF model would throw that away.
CONTENT_DIM = 64


def _choose_hyperparameters(data) -> dict:
    """
    Pick ALS capacity and regularization from the shape of the data.

    Latent factors are model capacity, and on implicit feedback the failure mode
    is not subtle. Measured on synthetic data with known cluster structure, at
    ~43 interactions per shopper:

        factors=8   → item-neighbour purity 1.00
        factors=16  → 0.99
        factors=32  → 0.75
        factors=48  → 0.35   (chance level)

    Excess factors do not merely fail to help — they fill with structure that
    survives the confidence weighting, and cosine similarity across those extra
    dimensions dilutes the real signal until "You might also like" is noise.
    Regularization barely moves this: sweeping λ from 0.05 to 0.30 changed purity
    by under two points, while halving the factor count changed it by forty.
    Capacity is the knob that matters, so it is the one tied to the data.

    Two gates, and the tighter wins: interactions *per shopper* (how much is known
    about any one person) and interactions *in total* (whether a handful of power
    users are inflating the first number).
    """
    interactions = data.n_interactions
    density = data.density_per_user

    if density < 15:
        factors, regularization = 8, 0.15
    elif density < 30:
        factors, regularization = 16, 0.10
    elif density < 60:
        factors, regularization = 24, 0.07
    else:
        factors, regularization = 32, 0.05

    if interactions < 5_000:
        factors = min(factors, 8)
    elif interactions < 25_000:
        factors = min(factors, 16)
    elif interactions < 100_000:
        factors = min(factors, 24)

    # Never ask for more factors than the matrix can support.
    factors = int(max(2, min(factors, data.n_users - 1, data.n_items - 1)))

    return {
        'factors': factors,
        'iterations': 15,
        'regularization': regularization,
        'alpha': 20.0,
    }


def _persist_embeddings(product_ids, cf_vectors, content_vectors, item_users, model_run):
    """Replace the product embedding table for this run."""
    rows = []
    for index, product_id in enumerate(product_ids):
        embedding = ProductEmbedding(
            product_id=product_id,
            interaction_count=int(item_users[index]) if item_users.size > index else 0,
            model_run=model_run,
        )
        if cf_vectors.size:
            embedding.set_cf(cf_vectors[index])
        embedding.set_content(content_vectors[index])
        rows.append(embedding)

    with transaction.atomic():
        ProductEmbedding.objects.all().delete()
        ProductEmbedding.objects.bulk_create(rows, batch_size=2000)

    logger.info("train: persisted %d product embeddings", len(rows))


def _persist_user_embeddings(data, user_factors, model_run, cold_start_threshold=3):
    """Store latent vectors for authenticated shoppers only."""
    rows = []
    matrix = data.matrix
    for row, user_id in data.auth_user_ids.items():
        if row >= user_factors.shape[0]:
            continue
        interactions = int(matrix.indptr[row + 1] - matrix.indptr[row])
        embedding = UserEmbedding(
            user_id=user_id,
            interaction_count=interactions,
            is_cold_start=interactions < cold_start_threshold,
            model_run=model_run,
        )
        embedding.set_cf(user_factors[row])
        rows.append(embedding)

    with transaction.atomic():
        UserEmbedding.objects.all().delete()
        UserEmbedding.objects.bulk_create(rows, batch_size=2000)

    logger.info("train: persisted %d user embeddings", len(rows))


def _align_cf_vectors(product_ids, data, item_factors, dim):
    """
    Project the trained item factors onto the full published catalog.

    The model only knows products that appeared in the interaction data;
    everything else — most of the catalog, early on — gets a zero row. That is
    deliberate and load-bearing: similarity.py reads a zero CF vector as "no
    collaborative evidence" and falls through to content similarity, which is
    exactly right for a product nobody has touched yet.
    """
    aligned = np.zeros((len(product_ids), dim), dtype=np.float32)
    item_users = np.zeros(len(product_ids), dtype=np.float64)
    if item_factors.size == 0:
        return aligned, item_users

    position = {product_id: index for index, product_id in enumerate(product_ids)}
    normalized = als.normalize_rows(item_factors)
    width = min(dim, normalized.shape[1])

    for product_id, column in data.item_pos.items():
        index = position.get(product_id)
        if index is None:
            continue
        aligned[index, :width] = normalized[column, :width]
        item_users[index] = data.item_users[column] if data.item_users.size > column else 0
    return aligned, item_users


def run_training(evaluate: bool = True) -> ModelRun:
    """
    Execute the full pipeline and return the ModelRun row.

    Safe to run on an empty database: it completes, records that there was
    nothing to learn from, and leaves a content-only model in place.
    """
    from product.models import Product

    run = ModelRun.objects.create()
    started = time.perf_counter()

    try:
        # ── 1. Behavioural data ──────────────────────────────────────────────
        data = dataset_module.build_dataset()
        cf_trust = data.cf_trust()
        trainable = data.is_trainable()

        hyperparameters = _choose_hyperparameters(data) if trainable else {
            'factors': 8, 'iterations': 0, 'regularization': 0.0, 'alpha': 0.0,
        }
        cf_dim = hyperparameters['factors']

        run.n_users = data.n_users
        run.n_items = data.n_items
        run.n_interactions = data.n_interactions
        run.sparsity = data.sparsity
        run.cf_weight = cf_trust
        run.factors = cf_dim
        run.iterations = hyperparameters['iterations']
        run.regularization = hyperparameters['regularization']
        run.alpha = hyperparameters['alpha']
        run.save()

        metrics: dict = {}
        user_factors = np.zeros((0, cf_dim), dtype=np.float32)
        item_factors = np.zeros((0, cf_dim), dtype=np.float32)

        if trainable:
            # ── 2. Evaluate on a held-out time split ─────────────────────────
            if evaluate:
                train_matrix, holdout = dataset_module.train_test_split_by_time(data)
                if holdout:
                    eval_users, eval_items = als.fit_als(train_matrix, **hyperparameters)
                    model_metrics = evaluation.evaluate_model(
                        train_matrix, holdout, eval_users, eval_items,
                    )
                    baseline = evaluation.evaluate_popularity_baseline(train_matrix, holdout)

                    metrics = {
                        'precision_at_10': model_metrics.get('precision_at_10'),
                        'recall_at_10': model_metrics.get('recall_at_10'),
                        'map_at_10': model_metrics.get('map_at_10'),
                        'ndcg_at_10': model_metrics.get('ndcg_at_10'),
                        'catalog_coverage': model_metrics.get('catalog_coverage'),
                        'baseline_precision_at_10': baseline.get('precision_at_10'),
                    }
                    logger.info(
                        "train: precision@10 %.4f vs popularity baseline %.4f · "
                        "recall@10 %.4f · coverage %.1f%%",
                        metrics.get('precision_at_10') or 0.0,
                        metrics.get('baseline_precision_at_10') or 0.0,
                        metrics.get('recall_at_10') or 0.0,
                        100 * (metrics.get('catalog_coverage') or 0.0),
                    )

            # ── 3. Refit on everything for production ────────────────────────
            user_factors, item_factors = als.fit_als(data.matrix, **hyperparameters)
        else:
            logger.info(
                "train: %d interactions across %d shoppers is below the collaborative "
                "threshold — building a content-only model",
                data.n_interactions, data.n_users,
            )

        # ── 4. Content model over the whole sellable catalog ─────────────────
        products = (
            Product.published
            .select_related('sub_category', 'sub_category__category',
                            'sub_category__category__main_category', 'brand', 'vendor')
            .only(
                'id', 'title', 'price', 'product_type', 'features', 'description',
                'specifications', 'sub_category_id', 'brand_id', 'vendor_id',
            )
        )
        product_ids, content_vectors = content.fit_content_embeddings(products, dim=CONTENT_DIM)

        if not product_ids:
            run.mark_completed(notes='No published products to model.')
            return run

        # ── 5. Blend collaborative and content similarity ────────────────────
        cf_aligned, item_users = _align_cf_vectors(product_ids, data, item_factors, cf_dim)

        hybrid = similarity.blend_neighbors(
            product_ids, cf_aligned, content_vectors, item_users, cf_trust,
        )
        content_neighbors = similarity.content_only_neighbors(content_vectors)
        co_purchase = similarity.build_co_purchase()

        similarity.persist_neighbors(
            product_ids, hybrid, content_neighbors, co_purchase, item_users, run,
        )
        _persist_embeddings(product_ids, cf_aligned, content_vectors, item_users, run)

        if trainable:
            _persist_user_embeddings(data, user_factors, run)

        # ── 6. Per-shopper rails ─────────────────────────────────────────────
        n_recommendations = ranker.build_user_recommendations(
            data, user_factors, cf_aligned, hybrid, product_ids, run, cf_trust,
        )

        notes = (
            f"{len(product_ids)} products · {data.n_interactions} interactions · "
            f"cf_dim {cf_dim} / content_dim {CONTENT_DIM} · "
            f"cf_trust {cf_trust:.2f} · {n_recommendations} rails written"
        )
        if not trainable:
            notes += ' · content-only (insufficient behavioural data for CF)'

        run.mark_completed(notes=notes, **{k: v for k, v in metrics.items() if v is not None})

        _invalidate_serving_caches()

        logger.info("train: completed run #%s in %.1fs", run.pk, time.perf_counter() - started)
        return run

    except Exception as exc:                       # noqa: BLE001 — the run must record why
        logger.exception("train: run #%s failed", run.pk)
        run.mark_failed(str(exc))
        raise


def _invalidate_serving_caches():
    """
    Point serving at the newly completed run.

    Rail caches are keyed by model-run id, so dropping the cached version pointer
    is enough — every neighbour and similarity entry becomes unreachable at once
    rather than lingering until its TTL expires.
    """
    from django.core.cache import cache

    cache.delete('rec:model_version')


def run_deal_scoring() -> int:
    """Snapshot today's prices, then rescore every deal. Runs hourly."""
    from django.core.cache import cache

    from .serving import bump_deals_version

    deals.snapshot_prices()
    count = deals.compute_deal_scores()

    bump_deals_version()
    cache.delete('deals_products')          # the legacy core.DealsAPIView cache

    return count
