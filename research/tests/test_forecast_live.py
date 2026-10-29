"""Offline regressions for the authorized prospective experiment boundary."""
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arb_research.forecast.codex import CodexPredictor, isolated_process
from arb_research.forecast.context import build_context, digest, prompt_for
from arb_research.forecast.demo import cohort
from arb_research.forecast.metrics import evaluate
from arb_research.forecast.prospective import join_settlements, predict_archive, score_joined, validate_records
from arb_research.forecast.public import PublicClient, binary_truth
from arb_research.forecast.runner import AuditStore


class LiveForecastTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def events(self, extra=()):
        return '\n'.join(json.dumps(e) for e in [*extra,
            {"type": "item.completed", "item": {"type": "agent_message", "text": '{"p":0.6}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}}])

    def test_real_recorded_open_quote_and_metadata_are_pre_cutoff(self):
        fixture=json.loads(Path(__file__).with_name('fixtures').joinpath('forecast-live-recording.json').read_text())
        market=fixture['market']; context=build_context(market)
        self.assertEqual(context['target_outcome'],'Up')
        self.assertFalse(market['snapshot_closed'])
        self.assertNotIn('y',market)
        self.assertEqual(market['quote_receipt']['data'][0]['resolution_seconds'],0)
        self.assertEqual(context['market_price'],market['quote_receipt']['data'][0]['price'])
        self.assertGreater(len(fixture['history']['pages'][0]['data']),1)
        self.assertNotIn('resolution_receipt',context)
        self.assertEqual(binary_truth(fixture['resolution_receipt']['payouts']),0)

    def test_base_unit_binary_payouts_and_fractional_exclusion(self):
        self.assertEqual(binary_truth([0,1000000]),0)
        self.assertEqual(binary_truth([1000000,0]),1)
        for bad in ([500000,500000],[1,1],[0,0],[],[True,0],[-1,0],[float('nan'),0]):
            with self.subTest(payouts=bad),self.assertRaises(ValueError):
                binary_truth(bad)

    def test_cli_isolation_and_receipt(self):
        def execute(args, **kw):
            self.assertIn('read-only', args)
            self.assertIn('web_search="disabled"', args)
            self.assertIn('code_mode', args)
            self.assertEqual(set(p.name for p in Path(kw['cwd']).iterdir()), {'response.schema.json'})
            self.assertNotIn('ARB_POLYMARKET_PROXY', kw['env'])
            self.assertNotIn('MULTICA_TOKEN', kw['env'])
            self.assertFalse(any(k.lower().endswith('_proxy') for k in kw['env']))
            return subprocess.CompletedProcess(args, 0, self.events(), '')
        with patch.dict(os.environ, {'ARB_POLYMARKET_PROXY': 'private-proxy',
                                    'MULTICA_TOKEN': 'private-token', 'https_proxy': 'private'}):
            result = CodexPredictor('fake', execute=execute).predict('bounded context', sample=0)
        self.assertEqual(result['p'], .6)
        self.assertEqual(result['audit']['tool_calls'], 0)
        self.assertEqual(result['audit']['prompt_sha256'], digest('bounded context'))
        self.assertNotIn('private', json.dumps(result))

    def test_cli_rejects_tool_calls_failure_and_unfinished_output(self):
        for stdout, code in [(self.events([{'type':'item.completed','item':{'type':'command_execution'}}]),0),
                             ('',0), (self.events(),1), ('bad-json',0),
                             (self.events([{'type':'turn.failed'}]),0)]:
            def execute(args, **kw):
                return subprocess.CompletedProcess(args, code, stdout, 'secret-error')
            with self.subTest(stdout=stdout), self.assertRaises((ValueError, RuntimeError)) as error:
                CodexPredictor('fake', execute=execute).predict('p',sample=0)
            self.assertNotIn('secret-error', str(error.exception))

    def test_cli_refuses_inherited_external_tools(self):
        (self.root/'config.toml').write_text('[mcp_servers.external]\ncommand="private-command"\n')
        with self.assertRaisesRegex(ValueError,'inherited'):
            CodexPredictor('fake').predict('context',sample=0)

    def test_owned_process_group_timeout_is_joined(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            isolated_process(['/bin/sleep','30'], input='', cwd=self.tmp.name, env={},
                             text=True,capture_output=True,timeout=.01)

    def archive_and_records(self):
        market=cohort(1)[0]
        market.pop('y'); market.pop('resolved_at')
        market.update(end_at='2026-05-02T13:00:00Z',snapshot_closed=False,
                      condition_id='0xabc',cluster_id='expiry',target_outcome='Up')
        archive={'markets':[market]}
        record={'market_id':market['id'],'sample':0,'model':'fixture',
                'predicted_at':'2026-05-02T12:01:00Z','response':{'p':.6},
                'prompt_sha256':digest(prompt_for(build_context(market))),
                'prompt':prompt_for(build_context(market)),'context':build_context(market),
                'context_sha256':digest(build_context(market)),'training_cutoff':'2026-01-01T00:00:00Z'}
        predictions={'config':{'samples':1,'model':'fixture','archive_sha256':digest(archive['markets'])},
                     'predictions':[record]}
        return archive,predictions

    def test_join_enforces_expiry_not_late_chain_resolution(self):
        archive, predictions=self.archive_and_records()
        class Client:
            def get(self,*args,**kw):
                return {'data':[{'condition_id':'0xabc','status':'RESOLVED','payouts':[1,0],
                                 'resolved_at':'2026-05-02T14:00:00Z'}]}
        good=join_settlements(Client(),archive,predictions,self.root/'joined.json')
        self.assertEqual(len(good['markets']),1)
        predictions['predictions'][0]['predicted_at']='2026-05-02T13:30:00Z'
        bad=join_settlements(Client(),archive,predictions,self.root/'joined.json')
        self.assertFalse(bad['markets'])
        self.assertIn('expiry',bad['excluded'][0]['reason'])
        predictions['config']['archive_sha256']='tampered'
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            join_settlements(Client(),archive,predictions,self.root/'joined.json')

    def test_missing_batch_row_gets_one_single_condition_refresh(self):
        archive,predictions=self.archive_and_records()
        second=copy.deepcopy(archive['markets'][0]);second.update(id='second',condition_id='0xdef')
        archive['markets'].append(second)
        record=copy.deepcopy(predictions['predictions'][0]);record['market_id']='second'
        predictions['predictions'].append(record)
        predictions['config']['archive_sha256']=digest(archive['markets'])
        class Client:
            calls=[]
            def get(self,route,**params):
                condition=params['condition'];self.calls.append(condition)
                return {'data':[] if ',' in condition else [{'condition_id':condition,
                    'status':'resolved','payouts':[0,1000000],'resolved_at':'2026-05-02T14:00:00Z'}]}
        client=Client()
        joined=join_settlements(client,archive,predictions,self.root/'joined.json')
        self.assertEqual(len(joined['markets']),2)
        self.assertEqual(client.calls,['0xabc,0xdef','0xabc','0xdef'])

    def test_expired_forecast_is_audited_without_invoking_model(self):
        archive,_=self.archive_and_records()
        archive['markets']=[{**copy.deepcopy(archive['markets'][0]),'id':str(i)} for i in range(50)]
        class Model:
            model='never-called'; training_cutoff='2026-01-01T00:00:00Z'
            def predict(self,*args,**kw):
                raise AssertionError('deadline should prevent invocation')
        with patch('arb_research.forecast.prospective.now',return_value='2026-05-02T13:00:00Z'):
            result=predict_archive(archive,Model(),AuditStore(self.root/'audit.sqlite'),
                                   self.root/'predictions.json',samples=1)
        self.assertEqual(len(result['failures']),50)
        self.assertFalse(result['predictions'])
        self.assertTrue(all(r['error_type']=='ValueError' for r in result['failures']))

    def test_output_crossing_expiry_is_rejected_and_queue_stops_calling(self):
        archive,_=self.archive_and_records()
        archive['markets']=[{**copy.deepcopy(archive['markets'][0]),'id':str(i)} for i in range(50)]
        clock=['2026-05-02T12:00:00Z']
        class Model:
            model='fixture'; training_cutoff='2026-01-01T00:00:00Z'; calls=0
            def predict(self,*args,**kw):
                self.calls+=1
                clock[0]='2026-05-02T13:00:00Z'
                return {'p':.6}
        model=Model()
        with patch('arb_research.forecast.prospective.now',side_effect=lambda:clock[0]):
            result=predict_archive(archive,model,AuditStore(self.root/'audit.sqlite'),
                                   self.root/'predictions.json',samples=1,workers=1)
        self.assertEqual(model.calls,1)
        self.assertEqual(len(result['failures']),50)

    def test_valid_subset_binding_and_record_mutations(self):
        archive, predictions=self.archive_and_records()
        class Client:
            def get(self,*args,**kw):
                return {'data':[{'condition_id':'0xabc','status':'RESOLVED','payouts':[1,0],
                                 'resolved_at':'2026-05-02T14:00:00Z'}]}
        joined=join_settlements(Client(),archive,predictions,self.root/'joined.json')
        market=joined['markets'][0]
        tampered=copy.deepcopy(market);tampered['y']=1-market['y']
        with self.assertRaisesRegex(ValueError,'truth mismatch'):
            validate_records(tampered,predictions['predictions'],predictions['config'])
        for mutate in (lambda r:r.update(sample=1), lambda r:r.update(model='other'),
                       lambda r:r.update(prompt_sha256='changed'), lambda r:r.update(prompt='changed'),
                       lambda r:r.update(context={}),
                       lambda r:r.update(response={'p':float('nan')})):
            records=copy.deepcopy(predictions['predictions']);mutate(records[0])
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):
                validate_records(market,records,predictions['config'])
        # 50 eligible settled rows may be a subset of a larger archived cohort.
        base=archive['markets'][0]
        archive['markets']=[{**copy.deepcopy(base),'id':str(i)} for i in range(56)]
        predictions['config']['archive_sha256']=digest(archive['markets'])
        predictions['predictions']=[{**copy.deepcopy(predictions['predictions'][0]),'market_id':str(i)}
                                    for i in range(56)]
        joined=join_settlements(Client(),archive,predictions,self.root/'joined.json')
        joined['markets']=joined['markets'][:50];joined['pending']=[str(i) for i in range(50,56)]
        self.assertEqual(score_joined(joined,predictions)['metrics']['n'],50)
        joined['markets'][0]['slug']='tampered'
        joined['excluded']=[{'reason':'unrelated exclusion'}]
        with self.assertRaisesRegex(ValueError,'changed'):
            score_joined(joined,predictions)

    def test_proxy_is_explicit_and_arb7_transport_errors_are_sanitized(self):
        with patch('arb_research.polymarket.client.PublicDataClient') as backend:
            with patch.dict(os.environ,{'ARB_POLYMARKET_PROXY':'socks5h://fixture.invalid:1234',
                                        'HTTPS_PROXY':'unrelated'}):
                client=PublicClient()
            self.assertEqual(backend.call_args.kwargs['proxy'],'socks5h://fixture.invalid:1234')
            backend.return_value.get.return_value={'data':[]}
            self.assertEqual(client.get('prices',token_id='123',as_of=100),{'data':[]})
            backend.return_value.get.assert_called_once_with('data-api.polymarket.com',
                '/v2/prices-history',{'token_id':'123','as_of':100})
            backend.return_value.get.side_effect=RuntimeError('private credential')
            with self.assertRaises(RuntimeError) as error:
                client.get('markets')
            self.assertNotIn('private',str(error.exception))
        with patch('arb_research.polymarket.client.PublicDataClient',side_effect=ValueError('private')):
            with self.assertRaises(ValueError) as error:
                PublicClient()
            self.assertNotIn('private',str(error.exception))

    def test_cluster_bootstrap_preserves_market_pairs(self):
        rows=[{'market_id':str(i),'cluster_id':str(i//2),'p':.75,'q':.5,'y':1} for i in range(4)]
        scores=evaluate(rows,bootstrap=100)
        self.assertEqual(scores['brier_advantage_ci95'],[.1875,.1875])
        self.assertEqual(scores['bootstrap']['clusters'],2)


if __name__=='__main__':
    unittest.main()
