"""
Train the recommender from the command line.

    python manage.py train_recommender              # full run: model + deals
    python manage.py train_recommender --deals-only # rescore deals only (fast)
    python manage.py train_recommender --no-eval    # skip the held-out evaluation

Run this once after deploying so the rails have data to serve; Celery Beat takes
over nightly afterwards.
"""

import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Train the recommendation models and rescore deals.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--deals-only', action='store_true',
            help='Only rescore deals — skips embeddings, neighbours and user rails.',
        )
        parser.add_argument(
            '--no-eval', action='store_true',
            help='Skip held-out evaluation. Roughly halves runtime; loses the quality metrics.',
        )

    def handle(self, *args, **options):
        from recommendation.train import run_deal_scoring, run_training

        started = time.perf_counter()

        self.stdout.write('Scoring deals…')
        eligible = run_deal_scoring()
        self.stdout.write(self.style.SUCCESS(f'  {eligible} eligible deals'))

        if options['deals_only']:
            self.stdout.write(self.style.SUCCESS(f'Done in {time.perf_counter() - started:.1f}s'))
            return

        self.stdout.write('Training recommender…')
        run = run_training(evaluate=not options['no_eval'])

        self.stdout.write(self.style.SUCCESS(f'  run #{run.pk} · {run.notes}'))
        self.stdout.write(
            f'  dataset: {run.n_users} shoppers × {run.n_items} products, '
            f'{run.n_interactions} interactions'
        )
        self.stdout.write(f'  cf_weight: {run.cf_weight:.2f} (0 = content-only, 1 = fully collaborative)')

        if run.precision_at_10 is not None:
            baseline = run.baseline_precision_at_10 or 0.0
            lift = run.lift_over_baseline
            self.stdout.write(
                f'  precision@10: {run.precision_at_10:.4f} '
                f'(popularity baseline {baseline:.4f}'
                + (f', {lift:+.1%}' if lift is not None else '') + ')'
            )
            self.stdout.write(f'  recall@10:    {run.recall_at_10:.4f}')
            self.stdout.write(f'  NDCG@10:      {run.ndcg_at_10:.4f}')
            self.stdout.write(f'  coverage:     {(run.catalog_coverage or 0):.1%} of the catalog')

            if lift is not None and lift <= 0:
                self.stdout.write(self.style.WARNING(
                    '  The model did not beat the popularity baseline. Normal with '
                    'little behavioural data — content similarity is carrying the '
                    'rails, and this flips once interaction volume grows.'
                ))
        else:
            self.stdout.write(
                '  No evaluation metrics — not enough behavioural data for a '
                'meaningful held-out split yet.'
            )

        self.stdout.write(self.style.SUCCESS(f'Done in {time.perf_counter() - started:.1f}s'))
