"""Seeded 125-contract offline stress; no empirical fill-speed claims."""
import random
import unittest
from dataclasses import replace

import executor_portfolio_test as fixtures
from executor_portfolio_liquidity import fixture_from_payload


class PortfolioStressTest(unittest.TestCase):
    for _name in ('setUp','fresh','account','quotes','schedule','own'):
        locals()[_name]=getattr(fixtures.PortfolioTest,_name)

    def test_seeded_partial_entry_rejections_disconnects_and_125_contract_liquidation(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                rng=random.Random(seed)
                self.fresh(max_slice=5,max_working_per_strike=3)
                for cycle in range(80):
                    self.fake.advance(.02);self.quotes()
                    self.fake.dispatch(self.schedule())
                    self.fake.match(capacity=rng.randint(1,7),side='BUY')
                    self.assertTrue(self.fake.reconcile()['matched'])
                    self.check_invariants()
                    if sum(self.ledger.positions().values())==125:break
                else:self.fail('Fixture failed to reach its configured entry quantity')
                self.ledger.trigger_exit('emergency:'+str(seed),'EMERGENCY_EXIT_ALL',True)
                self.ledger.trigger_exit('competing-stop:'+str(seed),'INITIAL_STOP')
                saw_partial=False
                for cycle in range(80):
                    self.fake.advance(.02);self.quotes()
                    if cycle==2:
                        self.ledger.disconnect(self.fake.generation)
                        self.fake.start()
                        self.assertTrue(self.fake.reconcile()['matched'])
                    if cycle==3:
                        candidates=[key for key,item in self.ledger.state['intents'].items() if item['side']=='SELL' and not item['terminal_proven']]
                        if candidates:
                            self.fake.order(candidates[0],'Inactive')
                            self.assertFalse(self.fake.reconcile()['matched'])
                            self.assertTrue(self.fake.cancel_actions(self.schedule())['matched'])
                    if cycle==4:
                        q=fixture_from_payload(self.ledger.portfolio['quotes']['105'])
                        self.ledger.observe_liquidity(self.fake.generation,self.fake.event(),replace(q,quote_id='thin:'+str(seed),bid_size=0))
                    actions=self.schedule()
                    self.fake.dispatch(actions)
                    # Independent fake liquidity often fills only a fraction of
                    # the requested sizes. Missing display does not invent fills.
                    if cycle!=4:self.fake.match(capacity=rng.randint(1,7),side='SELL')
                    self.assertTrue(self.fake.reconcile()['matched'])
                    self.check_invariants()
                    count=sum(self.ledger.positions().values())
                    saw_partial=saw_partial or 0<count<125
                    if self.ledger.status()['phase']=='FLAT_CONFIRMED':break
                else:self.fail('Fixture did not liquidate within its simulated recovery window')
                self.assertTrue(saw_partial)
                self.assertEqual(self.ledger.positions(),{})
                self.assertTrue(self.ledger.status()['entry_blocked'])
                self.assertEqual(sum(row['entry']['filled_quantity'] for row in self.ledger.metrics()['strikes']),125)
                self.assertEqual(sum(row['exit']['filled_quantity'] for row in self.ledger.metrics()['strikes']),125)

    def check_invariants(self):
        self.assertLessEqual(sum(self.ledger.positions().values()),125)
        for contract in self.contracts:
            cid=contract.con_id
            self.assertTrue(0<=self.ledger.positions().get(cid,0)<=25)
            self.assertLessEqual(self.ledger._bought(cid)+self.ledger.reservations('BUY',cid),25)
            self.assertLessEqual(self.ledger.reservations('SELL',cid),self.ledger.positions().get(cid,0))
        self.assertFalse(self.ledger.status()['executable'])
        ids=[(i['client_id'],i['order_id']) for i in self.ledger.state['intents'].values() if i['order_id'] is not None]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertLessEqual(self.ledger._spent()+self.ledger._cash_reserved(),200000)


if __name__=='__main__':unittest.main()
