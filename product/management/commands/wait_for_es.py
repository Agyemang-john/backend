# management/commands/wait_for_es.py
import time
from django.core.management.base import BaseCommand
from elasticsearch8 import Elasticsearch

class Command(BaseCommand):
    def handle(self, *args, **options):
        es = Elasticsearch(['http://elasticsearch:9200'])
        while True:
            try:
                if es.ping():
                    self.stdout.write("Elasticsearch is ready!")
                    return
            except Exception:
                self.stdout.write("Waiting for Elasticsearch...")
                time.sleep(5)