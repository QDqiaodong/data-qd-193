# data-qd-193 · 二维骨架姿态与蒙皮

骨架父子矩阵与关键帧决定世界姿态，显式局部绑定权重据此变形顶点和边，输出可核对的姿态与包围盒。

affine.py 定义组合顺序；animate.py 对矩阵插值并按层级求世界坐标，再按绑定变形顶点。 详细字段、单位和失败边界见 [数据契约](docs/contract.md)。全部样例为自制合成数据，许可见 [来源](fixtures/SOURCES.md)。

## 独立安装与演示

Python >=3.11，本批实际使用CPython3.12.14，运行/测试仅标准库。每仓建立独立.venv，不共享其他项目运行环境；复用公共构建缓存，锁定setuptools80.9.0/wheel0.45.1。以下命令已在本机已有uv、Python与公共缓存时实际离线执行通过；其他机器首次缺缓存时需准备依赖。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/rig-pose --source fixtures/input.json --output run/report-initial.json
```

实际97项测试通过（姿态6项、交接17项、验收43项、世界锁定31项），演示摘要：

```json
{
  "frames_with_singular_joints": 0,
  "joints": 2,
  "sampled_frames": 3,
  "vertices": 2
}
```

报告原子新建，不覆盖旧文件或符号链接。演示路径已存在时请改用新输出名；输入结构/约束或文件错误退出2，正常业务诊断见契约。已实际核验重复JSON键拒绝且不生成结果、同名输出字节不变及符号链接不覆盖。

## 按帧交接 spool

动画组修订轨道、独立渲染进程按帧领取线稿并回交产物指纹时，用 `rig-pose-spool --spool DIR --request FILE` 维护本地交接目录：只重派线稿实际变化的帧，保留在制领取与已出图结果，晚到的旧凭据不会贴到新版上。一个目录只服务一份资产，首次可传空目录，多进程并发安全。接口、幂等与持久化边界见 [spool 文档](docs/spool.md)；可离线运行的交接示例：

```sh
.venv/bin/python examples/handoff.py
```

## 离线安全框验收 crop

合成组在任意子帧叠加画面时，采样表之外的运动也必须落在随时间收放的矩形安全框内。`rig-pose-crop` 提供离线验收：动画方 `check` 对整个闭合验收区间做精确判定并产出带凭证的报告，合成方 `verify` 只凭原请求与报告独立核验（精确有理数运算，不重跑检查）：

```sh
.venv/bin/rig-pose-crop check --request run/crop-request.json --output run/crop-report.json
.venv/bin/rig-pose-crop verify --request run/crop-request.json --report run/crop-report.json
```

请求、报告、unsafe 见证与 safe 精确证明的格式及核验规则见 [crop 文档](docs/crop.md)；合成方可照做的验收示例：

```sh
.venv/bin/python examples/acceptance.py
```

## 离线挂点世界锁定 worldlock

动画师替换一条上游关节控制轨时，已摆好位置的挂点及其下方线稿要求在交付动画里原地不动。`rig-pose-worldlock` 把控制轨替换与挂点补偿编译成一份普通的 `rig-pose` 源文件（不是逐帧坐标表），锁定关节在编辑前的完整世界矩阵在整个 [0, 10⁶] 的每个实数 tick 保持不变：

```sh
.venv/bin/rig-pose-worldlock --request run/lock-request.json --output run/delivered.json
.venv/bin/rig-pose --source run/delivered.json --output run/report-locked.json
```

交付形态（保留挂接/脱挂）、唯一可恢复判定与 blocked 诊断见 [worldlock 文档](docs/worldlock.md)；可运行的交付示例：

```sh
.venv/bin/python examples/worldlock.py
```

## 当前能力与后续集成

二维有理数仿射和逐系数插值；不声称旋转角插值，不提供IK或真实视频。 合理新增需求可沿现有身份和结果接入动作混合、重定向及逆绑定导入；当前尚未实现这些扩展，不保证固定数量未来题目。
