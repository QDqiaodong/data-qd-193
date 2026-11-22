# 二维姿态采样 v1

动画准备人员提供自定义JSON骨架与蒙皮。joints为1..100唯一ASCII id、parent(null或既有joint id)和6元local矩阵；父图必须无环，支持多个根。矩阵[a,b,c,d,tx,ty]作用为x'=a×x+c×y+tx，y'=b×x+d×y+ty；世界矩阵=父世界×本地。按父先于子、同层id序得到确定顺序，不能仅对平移相加。

tracks每joint至多一条，1..1000个严格递增非负整数tick(≤10^6)和6元matrix键。轨道值替换本地矩阵，不再乘默认local。两个键之间逐系数精确线性插值，首键前/末键后保持端值；没有旋转角最短路径、四元数或物理插值声明。插值可产生奇异矩阵，输出singular_joint_ids作为正常业务诊断，不尝试求逆或伪修复。

vertices为1..1000唯一id，每个1..8个influences，含joint_id、该joint局部坐标下的point与正weight；同顶点不重复joint，weight精确和=1。变形位置=Σweight×joint_world×local_point；这是显式每骨架局部坐标绑定模型，不解析通用逆绑定矩阵格式。edges为既有不同顶点对，无向去重，最多2000。sample_ticks为1..100个严格递增时刻；限制frames×(joints+influences)≤500000。

所有矩阵、权重、坐标输入是有界十进制字符串（整数最多9位/小数6位），Fraction计算并输出整数/分数字符串。世界矩阵、变形顶点、实际边段、包围盒共享同一采样姿态。边不新增拓扑，包围盒仅覆盖给定顶点。没有碰撞检测、IK、约束求解、动作重定向、渲染/视频或特定动画文件解析。

样例root平移0→10，arm局部平移2，tip局部x=1；tick5时tip世界x=8，半根半arm绑定点x=6。输入非法退出2，整帧集先算完再原子新建JSON；已有目标/链接不覆盖，诊断不阻止有效报告。
