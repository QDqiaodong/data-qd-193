from bisect import bisect_right
from fractions import Fraction
from .affine import IDENTITY,compose,determinant,point

def sample_track(keys,tick):
    if tick<=keys[0][0]:return keys[0][1]
    if tick>=keys[-1][0]:return keys[-1][1]
    i=bisect_right([t for t,_ in keys],tick)-1
    start,a=keys[i];end,b=keys[i+1]
    alpha=Fraction(tick-start,end-start)
    return tuple(x+(y-x)*alpha for x,y in zip(a,b))

def pose(joints,order,tracks,vertices,edges,tick):
    world={}
    for name in order:
        j=joints[name];local=sample_track(tracks[name],tick) if name in tracks else j['local']
        world[name]=compose(world[j['parent']] if j['parent'] else IDENTITY,local)
    deformed={}
    for name,influences in sorted(vertices.items()):
        points=[(point(world[j],xy),weight) for j,xy,weight in influences]
        deformed[name]=tuple(sum(p[axis]*w for p,w in points) for axis in range(2))
    return {'tick':tick,'joint_world':{j:[str(x) for x in world[j]] for j in sorted(world)},
            'singular_joint_ids':sorted(j for j in world if determinant(world[j])==0),
            'vertices':{v:[str(x) for x in xy] for v,xy in deformed.items()},
            'edge_segments':[{'vertices':[a,b],'start':[str(x) for x in deformed[a]],'end':[str(x) for x in deformed[b]]} for a,b in sorted(edges)],
            'bbox':{'min':[str(min(xy[axis] for xy in deformed.values())) for axis in range(2)],'max':[str(max(xy[axis] for xy in deformed.values())) for axis in range(2)]}}
