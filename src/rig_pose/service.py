from .animate import pose
from .model import validate

def process(data):
    joints,order,tracks,vertices,edges,ticks=validate(data)
    frames=[pose(joints,order,tracks,vertices,edges,t) for t in ticks]
    return {'schema_version':1,'joint_order':order,'frames':frames,
            'summary':{'joints':len(joints),'vertices':len(vertices),'sampled_frames':len(frames),'frames_with_singular_joints':sum(bool(f['singular_joint_ids']) for f in frames)}}
