from .checks import array,fields,ident,integer,number,unique,vector

def validate(data):
    fields(data,('joints','tracks','vertices','edges','sample_ticks'))
    joints={}
    for j in array(data['joints'],1,100):
        fields(j,('id','parent','local'));name=ident(j['id'])
        if name in joints:raise ValueError('duplicate joint')
        parent=j['parent']
        if parent is not None:ident(parent)
        joints[name]={'parent':parent,'local':vector(j['local'],6)}
    for j in joints.values():
        if j['parent'] is not None and j['parent'] not in joints:raise ValueError('unknown joint parent')
    order=[];remaining=set(joints)
    while remaining:
        ready=sorted(name for name in remaining if joints[name]['parent'] is None or joints[name]['parent'] in order)
        if not ready:raise ValueError('joint hierarchy cycle')
        order.extend(ready);remaining.difference_update(ready)
    tracks={}
    for t in array(data['tracks'],0,100):
        fields(t,('joint_id','keys'));name=ident(t['joint_id'])
        if name not in joints or name in tracks:raise ValueError('unknown or duplicated track joint')
        keys=[];previous=-1
        for key in array(t['keys'],1,1000):
            fields(key,('tick','matrix'));tick=integer(key['tick'],0,1000000)
            if tick<=previous:raise ValueError('track key ticks strictly increase')
            previous=tick;keys.append((tick,vector(key['matrix'],6)))
        tracks[name]=keys
    vertices={}
    for v in array(data['vertices'],1,1000):
        fields(v,('id','influences'));name=ident(v['id'])
        if name in vertices:raise ValueError('duplicate vertex')
        influences=[]
        for influence in array(v['influences'],1,8):
            fields(influence,('joint_id','point','weight'));joint=ident(influence['joint_id'])
            if joint not in joints:raise ValueError('unknown influence joint')
            weight=number(influence['weight'])
            if weight<=0:raise ValueError('positive influence weight required')
            influences.append((joint,vector(influence['point'],2),weight))
        unique([j for j,_,_ in influences])
        if sum(w for _,_,w in influences)!=1:raise ValueError('weights must sum exactly to one')
        vertices[name]=influences
    edges=[]
    for e in array(data['edges'],0,2000):
        a,b=array(e,2,2);ident(a);ident(b)
        if a==b or a not in vertices or b not in vertices:raise ValueError('invalid mesh edge')
        edges.append(tuple(sorted((a,b))))
    unique(edges)
    ticks=array(data['sample_ticks'],1,100)
    for t in ticks:integer(t,0,1000000)
    if ticks!=sorted(set(ticks)):raise ValueError('sample ticks strictly increase')
    if len(ticks)*(len(joints)+sum(len(v) for v in vertices.values()))>500000:raise ValueError('sampling work bound exceeded')
    return joints,order,tracks,vertices,edges,ticks
