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

实际6项测试通过，演示摘要：

```json
{
  "frames_with_singular_joints": 0,
  "joints": 2,
  "sampled_frames": 3,
  "vertices": 2
}
```

报告原子新建，不覆盖旧文件或符号链接。演示路径已存在时请改用新输出名；输入结构/约束或文件错误退出2，正常业务诊断见契约。已实际核验重复JSON键拒绝且不生成结果、同名输出字节不变及符号链接不覆盖。

## 当前能力与后续集成

二维有理数仿射和逐系数插值；不声称旋转角插值，不提供IK或真实视频。 合理新增需求可沿现有身份和结果接入动作混合、重定向及逆绑定导入；当前尚未实现这些扩展，不保证固定数量未来题目。
