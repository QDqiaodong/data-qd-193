import json
from pathlib import Path
import unittest
from fractions import Fraction
from rig_pose.affine import compose,point
from rig_pose.animate import sample_track
from rig_pose.service import process

def sample():return json.loads((Path(__file__).resolve().parents[1]/'fixtures/input.json').read_text())

class PoseTests(unittest.TestCase):
    def test_parent_motion_changes_skinned_vertices_and_edges(self):
        r=process(sample());middle=r['frames'][1]
        self.assertEqual(middle['vertices'],{'blend':['6','0'],'tip':['8','0']})
        self.assertEqual(middle['edge_segments'][0]['start'],['6','0'])
        self.assertEqual(middle['bbox'],{'min':['6','0'],'max':['8','0']})
        self.assertEqual(r['summary'],dict(joints=2,vertices=2,sampled_frames=3,frames_with_singular_joints=0))

    def test_composition_order_rotation_then_translation(self):
        parent=(0,1,-1,0,10,0);local=(1,0,0,1,2,0)
        self.assertEqual(point(compose(parent,local),(1,0)),(10,3))

    def test_exact_key_interpolation_and_endpoint_hold(self):
        keys=[(2,(0,)*6),(5,(1,)*6)]
        self.assertEqual(sample_track(keys,3),(Fraction(1,3),)*6)
        self.assertEqual(sample_track(keys,0),(0,)*6)
        self.assertEqual(sample_track(keys,9),(1,)*6)

    def test_singular_pose_is_explicit_not_silently_inverted(self):
        d=sample();d['tracks'][0]['keys'][1]['matrix']=['-1','0','0','1','10','0']
        r=process(d)
        self.assertEqual(r['frames'][1]['singular_joint_ids'],['arm','root'])
        self.assertEqual(r['summary']['frames_with_singular_joints'],1)

    def test_cycle_and_bad_weights_fail(self):
        d=sample();d['joints'][0]['parent']='arm'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['vertices'][1]['influences'][0]['weight']='0.6'
        with self.assertRaises(ValueError):process(d)

    def test_unknown_joint_duplicate_edges_or_ticks_fail(self):
        d=sample();d['tracks'][0]['joint_id']='missing'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['edges'].append(['tip','blend'])
        with self.assertRaises(ValueError):process(d)
        d=sample();d['sample_ticks']=[5,0]
        with self.assertRaises(ValueError):process(d)

if __name__=='__main__':unittest.main()
