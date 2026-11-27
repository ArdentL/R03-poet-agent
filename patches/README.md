# Audio2Face 嘴部掩码配置补丁

2026-11-27 只读核实：活动头为 `xishiG3_S02_ID06`，模板为
`bionic_standard_v1`，AudioFaceAdaptor 为 `adaptors[2]`。
补丁复制整个当前模板为 `robot_poet_mouth_only_v1`，只从副本的 AudioFaceAdaptor
移除四个 eyebrow 通道，保留全部嘴唇、嘴角和下巴通道。仅活动头切换到副本；
原模板、其余头实例及其他 adaptor 不变。

配置文件在 `nezhahal-nx` 容器中的路径：

- `/opt/nezhahal/head_node/config/system_settings.yaml`
- `/opt/nezhahal/head_node/config/head_specs.json`

容器的 config 是**只读挂载**，宿主机源目录为
`/data/program/b1/nezhahal/head_node/config`。本仓库不会写这些路径，也不会重启进程。

在运维电脑获取两份当前配置副本到本地目录，先备份原始文件，再生成候选配置：

```bash
python3 patches/apply_head_patch.py ./head_specs.json ./system_settings.yaml --output ./head_specs.mouth-only.json
diff -u ./head_specs.json ./head_specs.mouth-only.json
```

RFC 6902 补丁中带有前置 test；活动头、模板顺序或通道发生变化会拒绝生成。
确认语义差异仅为“新增模板 + 活动头引用变更”后，由运维人员将候选文件替换宿主机的
`head_specs.json`（保留权限及原始备份）。**随后需要按现有运维流程重新加载头节点**；
仅替换 JSON 不会让已加载的头节点自动生效。

回滚优先恢复原始备份，然后重新加载头节点；也可对安装后的配置副本生成逆向候选：

```bash
python3 patches/apply_head_patch.py ./head_specs.installed.json ./system_settings.yaml --rollback --output ./head_specs.rollback.json
```

逆向补丁恢复活动头引用并删除专用模板。若其他头开始引用该模板，脚本会拒绝删除。
表情仍用 LOCAL；应用掩码后眉眼由表情控制，嘴部由 Audio2Face 控制。
