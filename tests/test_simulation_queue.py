import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gkshot.simulation_queue import next_stage


class QueueTests(unittest.TestCase):
    def test_full_season_round_order_then_refine(self):
        stages=[{'stage_id':i, 'trials':0, 'state':'queued'} for i in (181,182,183)]
        for round_index in range(1,6):
            for row in stages:
                self.assertEqual(next_stage(stages),(row['stage_id'],round_index))
                row.update(trials=round_index*20,state='round-wait')
        for row in stages:
            self.assertEqual(next_stage(stages),(row['stage_id'],None))
            row['state']='complete'
        self.assertIsNone(next_stage(stages))

    def test_catchup_and_skip_failed_or_paused_stage(self):
        stages=[{'stage_id':181,'trials':80,'state':'round-wait'},
                {'stage_id':182,'trials':0,'state':'queued'},
                {'stage_id':183,'trials':0,'state':'queued'}]
        self.assertEqual(next_stage(stages),(182,1))
        stages[1]['state']='running'
        self.assertIsNone(next_stage(stages))
        stages[1]['state']='error'
        self.assertEqual(next_stage(stages,{182}),(183,1))
        stages[2]['trials']=80
        self.assertEqual(next_stage(stages,{182}),(181,5))


if __name__=='__main__': unittest.main()
