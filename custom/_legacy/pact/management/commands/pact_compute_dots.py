from django.core.management.base import BaseCommand

from corehq.apps.api.es import ReportXFormES
from pact.enums import PACT_DOMAIN
from pact.utils import REPORT_XFORM_MISSING_DOTS_QUERY


CHUNK_SIZE = 100


class Command(BaseCommand):
    help = "Helper command to compute DOT computed_ fields - to exteranlly operate that operation that should happen on signal firing on submission"

    seen_doc_ids = {}

    def handle(self, **options):
        xform_es = ReportXFormES(PACT_DOMAIN)
        offset = 0

        q = REPORT_XFORM_MISSING_DOTS_QUERY
        q['size'] = CHUNK_SIZE

        while True:
#            q['from'] = offset
            res = xform_es.run_query(q)
            print("####### Query block total: %s" % res['hits']['total'])
            print(res['hits']['hits'])
            if len(res['hits'].get('hits', [])) == 0:
                break
            else:
                for hit in res['hits']['hits']:
                    doc_id = hit['_id']
                    if doc_id in self.seen_doc_ids:
                        continue
                    else:
                        self.seen_doc_ids[doc_id ] =1
            offset += CHUNK_SIZE



