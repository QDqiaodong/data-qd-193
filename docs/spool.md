# 按帧交接 spool v1

动画组用 `rig-pose` 为一份二维骨架资产采样，画师不断修订轨道，独立的渲染进程按帧领取线稿、回交产物指纹。`rig-pose-spool --spool DIR --request FILE` 维护本地交接目录：保留不受修订影响的在制工作和已出图结果，只重派线稿实际变化的帧，晚到的旧结果不会贴到新版上。本命令不实现渲染器，不读取图片，不联系外部存储。

一个目录只服务一份资产；首次使用可传空目录（不存在则创建），后续进程打开同一目录自动接续既有状态。运行仅标准库，无新增运行时依赖。

## 请求

请求文件是最多 8,000,000 字节的 UTF-8 严格 JSON 对象：拒绝重复键、非有限数、缺字段与额外字段。所有 ID（`op_id`、`claim_id`、`base`）沿用资产 ID 语法 `[A-Za-z0-9_:-]{1,64}`；同一目录内所有写操作共享一个 `op_id` 命名空间。操作（列出的字段之外不接收其他字段）：

- `{"op":"revise","op_id":…,"base":…,"source":…}`：`source` 是完整的原版采样输入，字段语义与数量上限同 [采样契约](contract.md)（不是补丁）；`base` 是期望的当前版本，首次为 null。
- `{"op":"claim","op_id":…,"tick":…}`：领取当前指定帧；`tick` 为 0..10⁶ 整数，同原采样时刻。
- `{"op":"complete","op_id":…,"claim_id":…,"artifact_sha256":…}`：提交独立渲染器给出的产物指纹，必须是 64 位小写十六进制字符串。
- `{"op":"status"}`：读取当前交接视图，无 `op_id`。

## 响应与错误

正常请求退出 0，stdout 输出一个 JSON 对象：

- 修订成功：`{"status":"revised","head":…,"kept":[…],"opened":[…],"removed":[…]}`，三个数组按 tick 升序，分别对应保留、新开世代和从新版本删除的 tick。
- 版本冲突：只给 `{"status":"base_conflict","head":…}`（当时的 head）。
- 领取成功：`{"status":"claimed","claim_id":…,"tick":…,"epoch":…,"payload":{"vertices":…,"edge_segments":…,"bbox":…}}`；否则只给 `{"status":"not_claimable"}`。
- 交付：`{"status":"accepted","tick":…,"epoch":…}` 或 `{"status":"stale_claim"}`。
- 状态：`{"head":…,"frames":[{"tick":…,"epoch":…,"state":…,"claim_id":…,"artifact_sha256":…}…]}`，frames 按 tick 升序，尚无的凭据或指纹为 null。

格式或数据错误（包括版本已落后但 source 无效）与存储错误退出 2，stderr 给出可读诊断，不输出成功对象；失败不留下只完成一部分的业务状态，也不消耗 `op_id`。

## 版本、帧世代与保留

空目录的初始版本为 null、无帧。revise 只有在 `base` 等于当前版本时才发布，成功后当前版本为该请求的 `op_id`。帧身份先按 tick 匹配，再比较精确的实际线稿数据——原采样帧的 `vertices`、`edge_segments`、`bbox` 三项，按原父子组合、逐系数插值和权重变形求出；不以源 JSON、关键帧 ID 或世界矩阵变化替代比较，线段拓扑变化也算线稿变化。

两版都有该 tick 且线稿相等时，完整保留帧记录（所处状态、已发的领取凭据、已接收的产物）。新增 tick 或同 tick 线稿变化，以本次版本 ID 开一个新的帧世代（`epoch`），初始状态 `pending`；删除的 tick 离开当前视图。后来删除再加回，或线稿从 A 变 B 再变回 A，都不恢复退休的世代，也不借用其凭据与产物。

## 领取与交付

仅当前 `pending` 帧可领取：原子地标为 `claimed`，本次 `op_id` 即 `claim_id`，领取响应直接交付 tick、epoch 和三项线稿数据，不依赖之后再查当前版本。提交只有在 `claim_id` 仍是某个当前 `claimed` 帧的凭据时才接受：保存指纹并标为 `ready`，在该帧仍被保留期间可看到凭据与指纹。没有超时撤销或主动退回；已发响应丢失时，协调方使用原请求取回线稿，可交给替代进程继续处理。取回历史的领取响应不代表当前仍可交付：新的提交操作总是检查当前凭据。

## 幂等与重试

格式正确的写操作首次得到的正常响应（包括 `base_conflict`、`not_claimable`、`stale_claim` 等业务拒绝）长期可重取。比较解析后的整个原请求：忽略对象键顺序和 JSON 空白，数组顺序和字符串内容仍有意义。同一 ID 不同请求是输入错误（退出 2）；完全相同的请求返回原响应，不重新判断 `base` 或领取是否还有效，也不再写业务状态。

## 持久化边界

- 目录内容：`state.json`（当前版本、当前帧记录、全部已记录 op 响应）与 `lock`（fcntl flock 序列化同一目录的所有请求，含 status）。每个请求都表现得像在某一顺序中完整处理：同一 `base` 的不同修订恰有一个生效，不会看见新版本引用旧帧状态的半套结果。
- 业务变化和对应的可重取响应在同一原子 rename（写临时文件 + fsync + `os.replace`）中生效。进程被终止但本地磁盘仍在时：生效前终止不改变可见业务状态、不消耗 ID；生效后、stdout 可用前终止可按原请求取回。启动时清理残留临时文件。
- 不要求处理：机器断电、跨机文件系统、被手工修改的目录。

测试钩子：环境变量 `RIG_POSE_SPOOL_CRASH=before_commit|after_commit` 在生效点前后以 `os._exit(137)` 模拟进程被终止，仅供测试。

## 交接示例

`examples/handoff.py` 离线运行完整作业链（每操作一个独立进程）：发布仓库样例 → 领取并交付 tick 0 → 领取 tick 5、10 → 为 root 插入 tick 5、x=6 关键帧的修订保留 0 的产物与 10 的领取、重派 5（blend 的 x 从 6 变 7）→ 还原原输入再开世代，旧凭据 `stale_claim`、原领取请求重试取回同一份旧线稿。运行：

```sh
python3 examples/handoff.py
```

关键响应序列（缩略）：

```
发布样例            → {"status":"revised","head":"r1","kept":[],"opened":[0,5,10],"removed":[]}
领取并交付 tick 0   → claimed / {"status":"accepted","tick":0,"epoch":"r1"}
领取 tick 5、10     → claimed（tick 5 线稿 blend x = "6"）
插入关键帧修订      → {"status":"revised","head":"r2","kept":[0,10],"opened":[5],"removed":[]}
交付 tick 10 旧领取 → {"status":"accepted","tick":10,"epoch":"r1"}
交付 tick 5 旧凭据  → {"status":"stale_claim"}
重新领取 tick 5     → claimed（epoch "r2"，blend x = "7"）
还原原输入          → {"status":"revised","head":"r3","kept":[0,10],"opened":[5],"removed":[]}
r2 世代凭据交付     → {"status":"stale_claim"}
重试原领取请求      → 原响应（epoch "r1"，blend x = "6"）
```
