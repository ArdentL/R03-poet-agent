# Robot Poet

现在可直接在创作台输入诗词题目，点击「机器人吟诗」，自动完成正文、音频、表情动作规划和机器人执行。

首次使用安装下方依赖，将 `config/robot.example.json` 复制为 `config/robot.json` 并配置 SSH，
在 `.env` 中填写 `DOUBAO_SPEECH_API_KEY`。启动 `python server.py`（Windows 也可双击「启动诗词智能体.cmd」），
在网页模型设置中选择 DeepSeek 或豆包并保存 API Key。音色、来源和其他创作选项均可沿用现有设置。
Windows 与 WSL 可分别运行；SSH 密钥、已确认的主机密钥及 Python 依赖须配置在实际运行服务的环境中。

「机器人吟诗」使用左侧题目／原文和朗诵要求生成一份新的表演，单次最多 120 秒。
执行前读取实机表情和动作清单；模型只能使用可用能力，不会选择自检等维护动作。
遗漏身体动作或能力／数量错误时，退回具体错误重新生成，最多三次；不自动补动作。
页面分别显示表演计划和动作实际请求、开始、结束状态。
音频完整播放一次，按真实停顿匹配逐句表情和稀疏动作。停顿检测不进行语义识别，
带强背景音乐、连读或音频漏读时不能保证句子对齐；没有足够可靠停顿会停止并保留文件。
仅按题目获取的古诗原文仍来自语言模型，来源说明会显示在正文下，可填写已核验原文。

页面显示当前阶段和逐句表演安排，可点击「停止」。生成中的远端请求可能需等待返回，
取消后不会继续启动表演；已启动时尽力同时停止音频、口型和身体动作，机械回位完成不作推定。
刷新只恢复状态，重复的任务 ID 不会重播；服务异常退出后，未完成任务标记为取消或结果未知，均不自动恢复执行。
状态、MP3、task、时间轴、任务包和 JSONL 报告保存在 `.robot-poet/performances/<任务ID>/`。
同一服务只执行一个任务，不另设队列。可用 `--robot-config PATH` 选择已有机器人配置。

完整音频只播放一次，复用机器人已有 Audio2Face → s1_control_center → 播放器／头节点链路。
本地按任务序列长度自动寻找停顿，以播放器 START 为基准切换表情和身体动作。
`sdk/` 保持原样，不训练或部署口型模型，不调用旧 `audio_play`。

## 安装与运行

在 WSL 项目目录执行，Python 3.10+，需要已有 SSH 密钥／agent 和已确认的主机密钥：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

复制 `config/robot.example.json` 为 `config/robot.json`，填写连接信息。日常只需一条命令：

```bash
.venv/bin/python -m robot_poet run examples/task-v2.json --config config/robot.json --yes
```

自动完成分析／缓存复用、峰值归一、打包、上传、实机清单与链路检查、播放、报告和清理。
去掉 `--yes` 会在预览后等待确认。任何阶段失败都停止，不自动重播。
默认报告保存在 `.robot-poet/reports/`；可用 `--report PATH` 指定。
`--keep-remote` 保留上传文件；SSH 失联、结果未知或取消时也保留远端证据。

## 任务与时间轴

音频路径相对于任务 JSON，action 省略或 null 表示不启动新动作，也不停止已有动作。

```json
{
  "version": 2,
  "audio": "./chunxiao.wav",
  "sequence": [
    {"expression": "微笑", "action": "欢迎"},
    {"expression": "伤心", "action": null},
    {"expression": "高兴", "action": "手垂下"},
    {"expression": "震惊", "action": null}
  ]
}
```

分句使用 WebRTC VAD 和相对语音电平的低谷，按 N 项序列选择 N−1 个实际停顿。
最短段长根据总时长和目标句数自动计算，VAD 窗口和检测策略由程序统一管理。
不会为了匹配数量直接等分、循环、截断或补齐；找不到足够可靠的边界时明确报错。
停顿不能保证对应语义句子，时间轴仍可试听后手工修正。

需要单独预览时（不会连接机器人）：

```bash
.venv/bin/python -m robot_poet analyze examples/task-v2.json --config config/robot.json
```

默认缓存为任务旁的 `task-v2.timeline.json`，可用 `--timeline PATH` 指定。
只编辑 `regions` 中的 `start_ms`／`end_ms`，不要改其他绑定字段。
区间须非空、有序、不重叠、不越界。start_ms 触发表情／动作；end_ms 仅用于预览和校验。
缓存绑定原始音频 SHA-256、真实 PCM 帧数、分析器版本和目标句数；
音频、算法版本或 sequence 长度改变会重新分析。只换表情／动作名称不会覆盖手工调整。
本次升级会将旧缓存重新生成为版本 2。

版本 1 保留 `segments` 手动时间轴，参见 `examples/task.json`。
**版本 1 同样播放完整文件，未选中范围不会跳过**，执行时会提示行为变化。
当自动分句不适用时，可直接用版本 1 指定时间。

## 自动音量与格式

播放格式固定为 **24 kHz、单声道、S16LE**；分析单独转为 16 kHz 单声道。
完整文件的开头、句间静音和尾音都保留，不裁剪、不拼接、不额外加静音。
长度根据实际 PCM 帧数读取；最多一小时、128 MiB PCM、256 句。

打包时自动将**采样峰值归一到 −1 dBFS**，既可提升偏小的音频，也可降低接近满幅的音频。
纯数字静音保持不变。原始文件和分析音频不变，播放帧数不变，不引入限幅器延迟。
预览及 job.json 记录输入峰值与实际增益；不修改机器人系统音量。
这是采样峰值归一，不是响度或真峰值标准化。
当前春晓音频由约 −8.6 dBFS 自动提升约 +7.6 dB。

FFmpeg 优先使用系统 PATH，找不到时使用依赖中附带的可执行文件，无需手工填写虚拟环境路径。
仅在需要指定某个 FFmpeg 时设置 `audio.ffmpeg`，其余音频／分句参数不再暴露在 robot.json。

## 精简配置

必需配置只有 SSH 和机器人环境信息，示例：

```json
{
  "ssh": {"host": "robot-ssh-alias", "user": "robot-user"},
  "remote": {
    "ros_setup": "/opt/ros/humble/setup.bash",
    "interfaces_setup": "/opt/nezhahal/install/local_setup.bash",
    "work_dir": "/tmp/robot-poet-jobs",
    "domain_id": 100,
    "container": "nezhahal-nx"
  }
}
```

`remote.container` 可选。填写时上传、执行、清理均通过 `docker exec -i` 在同一容器进行；
ROS setup、python、work_dir 都是容器内路径。不填写时直接在 SSH 宿主机执行。
接口 setup 需要包含已有的 `dgrobot_msgs`、`hal_interfaces` 和 `head_msgs`。
不安装机器人依赖，不修改机器人配置，不重启机器人服务。

可选的 SSH 配置：port（22）、identity_file、known_hosts_file、connect_timeout（10 秒）。
本机密钥文件支持 `~` 和相对配置文件目录的路径，使用 BatchMode 和严格主机密钥检查。
可选的 remote 配置：python（python3）、dds_profile。远端文件路径须是绝对 POSIX 路径。

仅在实际需要校准时添加 `playback`：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| expression_offset_ms | 0 | 表情相对 START＋语音起点的校准 |
| action_offset_ms | 0 | 动作独立校准 |
| start_timeout | 30 s | 等待播放器 START 的上限 |
| end_timeout | 120 s | START＋完整音频时长后等待 END 的额外上限 |
| action_timeout | 30 s | 动作请求到自然 END 的上限 |
| discovery_timeout | 10 s | 执行前等待现有链路 |
| service_timeout | 5 s | 单个异步服务请求上限 |
| lease_timeout | 10 s | SSH 心跳租约 |

两个 offset 为 −5000..5000 ms，最早钳制到播放器 START。
旧 gain_db、analysis、sample_rate／channels、chunk_ms／send_interval_ms 及废弃缓冲参数已移除；
本仓库配置已迁移。其他旧配置请按示例删除这些字段，未知字段和错误类型会明确拒绝。

## 机器人行为

```text
DialogueCtrl（整份音频，一个唯一 dialogue_id）
  → /dgrobot/dialogue_ctrl → Audio2Face
  → /dgrobot/dialogue_ctrl_audio2face（音频＋口型）→ s1_control_center
      ├─ /s1/gateway/audio_sync_data → 已有播放器
      └─ /head_node/gateway/audio_face_clip → 已有头节点
```

只接受 `/dgrobot/dialogue_status` 中匹配 dialogue_id 的播放器 START／END。
表达式走 head_command 的 LOCAL 打断；口型上下文不会按句重置。
所有服务调用均异步，不阻塞供音。播放器 END 后，等待最后一个动作自然 END。

支持九个动作：手垂下、指向右前、指向右边、指向左前、指向左边、欢迎、自我介绍、自检、默认_body。
执行前通过 get_action_list 动态核对，表情也会查询实机清单。
servo_action 使用 int_args=[1]、str_args=[唯一 action_id, 动作名]，不发送归一化位置。

新动作到来时，未结束的旧动作先异步 STOP，受理后再启动新动作，并记录旧动作 interrupted。
仅匹配 action_id 的 servo_status START／END／FAIL 生效，迟到旧状态不能完成新动作。
STOP 已有的默认回位行为保留，不重复发回位；STOP 受理不代表机械回位完成。
请求未确定便到下一动作边界时报告 unknown，不积压、补执行过期动作。
取消、信号、SSH 心跳丢失或失败时尽力同时打断对话音频／口型和身体动作。
缺少状态或连接丢失报告 unknown；明确拒绝或 FAIL 报告 failed；不自动重放。

头部眉毛掩码补丁及回滚步骤见 [patches/README.md](patches/README.md)。
补丁只交付在仓库，需要人工应用并重新加载头节点；其余头实例不受影响。

## 工程结构与验证

- `model.py`：Pydantic BaseModel 定义任务、配置、时间轴、音频元数据及任务包；严格类型与未知字段校验。
- `analysis.py`：本地分句算法，不依赖播放调度。
- `audio.py`：音频转换、峰值归一、缓存与打包。
- `performance.py`：后台编排、计划检查与重生成、取消及状态保存。
- `integrations.py`：DeepSeek／豆包共享文本接口，独立保存两家配置。
- `client.py` / `transport.py`：命令入口、报告及 SSH 生命周期。
- `remote.py` / `wire.py`：标准库机器人执行器、JSON I/O 和传输边界校验。

本机由 Pydantic 导出任务包 JSON Schema。机器人按同一 schema 校验数据，并复核 PCM、
SHA-256 和时间轴跨字段关系，因此不需要在机器人安装 Pydantic。执行器内部的临时请求／动作状态
使用带类型的 dataclass；外部数据结构统一由 Pydantic 模型管理。
所有函数参数和返回值有类型提示，Ruff 检查导入、基础错误、格式和缺失注解。
JSON 缓存原子写入，校验值流式计算，避免大音频反复完整载入内存。

只检查、只准备等分步工具继续保留，正常 run 不需要手动执行它们：

```bash
.venv/bin/python -m robot_poet run examples/task-v2.json --config config/robot.json --check
.venv/bin/python -m robot_poet prepare examples/task-v2.json --config config/robot.json --output build/demo-v2
python3 -S build/demo-v2/remote.py --bundle build/demo-v2 --validate
.venv/bin/python -B -m unittest discover -s tests -v
```

开发时安装并运行静态检查：

```bash
.venv/bin/pip install ruff
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

现有离线测试覆盖语音凭据与接口错误处理、编排成功、重复提交不重播、生成阶段取消、
不支持能力拒绝，以及结果未知时的持久化与恢复。
测试不连接机器人、不播放音频、不驱动舵机。
