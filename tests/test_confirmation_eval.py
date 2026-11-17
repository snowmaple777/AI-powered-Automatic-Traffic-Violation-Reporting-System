"""實驗指標不可把未執行的規則算成漏報，也不可把跨類未標註事件算成誤報。"""
import unittest
from tools.confirmation_eval import metrics, merge, known_false_positives


class EvaluationTests(unittest.TestCase):
    def test_rule_scope_and_primary_only(self):
        truth = {'version':1,'videos':{'v':{'reviewed':True,'category':'r', 'events':[
            {'rule_id':'r','start_sec':1,'end_sec':2}, {'rule_id':'other','start_sec':1,'end_sec':2}]}}}
        predictions = {'v':[{'rule_id':'r','status':'confirmed','timestamp_sec':1.5},
                            {'rule_id':'unlabelled','status':'confirmed','timestamp_sec':1.5}]}
        self.assertEqual(metrics(truth,predictions,True,['r'])['confirmed'], {'r':{'tp':1,'fp':0,'fn':0}})
        self.assertEqual(len(truth['videos']['v']['events']),2)

    def test_merge_does_not_mutate_baseline(self):
        baseline = {'r':{'settings':{'x':False,'y':2}}}
        self.assertEqual(merge(baseline,{'r':{'settings':{'x':True}}}), {'r':{'settings':{'x':True,'y':2}}})
        self.assertFalse(baseline['r']['settings']['x'])

    def test_known_fp_requires_same_object_rule_and_time(self):
        gt={'rule_id':'r','object_id':'v','timestamp_sec':3.,'judgement':'false_positive'}
        review={'videos':{'clip':{'reviewed_events':[gt]}}}
        events=[dict(gt,status='confirmed'),dict(gt,object_id='other',status='confirmed'),dict(gt,timestamp_sec=10,status='confirmed')]
        self.assertEqual(len(known_false_positives(review,{'clip':events})),1)


if __name__ == '__main__':
    unittest.main()
