import json
from curator.recommendation.request_health import RequestHealthReporter, latency_band

def test_latency_bands_are_closed_and_bounded():
    assert [latency_band(x) for x in (0,.999,1,2.999,3,5.999,6,7.999,8,19.999,20)] == [
        'lt1s','lt1s','1to3s','1to3s','3to6s','3to6s','6to8s','6to8s','8to20s','8to20s','gt20s']

def test_reporter_uses_fixed_fields_and_swallows_write_failure(capsys):
    class Store:
        def record_request_health(self,**fields): self.fields=fields; raise RuntimeError('private detail')
    store=Store(); RequestHealthReporter(store).record(endpoint='rank',outcome='timeout',elapsed_seconds=6)
    assert store.fields=={'endpoint':'rank','outcome':'timeout','latency_band':'6to8s','latest_input_match':False}
    logged=capsys.readouterr().err; assert json.loads(logged)=={'event':'request_health_write_failed'} and 'private' not in logged
