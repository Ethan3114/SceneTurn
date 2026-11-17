---
name: openfmv-story
description: 在OpenFMV项目对话中规划整体剧情、创建场景、编写场景正文和调整叙事顺序。用户要求组织故事、增加剧情或续写场景时使用。
---

根据当前用户消息、会话历史和editingContext理解剧情目标。每轮先通过read_story读取项目真实场景、连接及最新版本，区分已有事实、用户设定和创作补充。

使用apply_story_ops保存场景标题和正文。create=true时使用新的唯一sceneId创建场景，随后通过arrange_story保存node:default剧情连接。保留已有媒体、按钮、QTE和未要求修改的正文。

涉及互斥分支时分别组织因果关系，不把一个分支的结局当成另一个分支的共同事实。需要素材时使用prepare_scene_media将详细要求放入对应场景的素材区域，等待用户确认媒体生成。

询问剧情建议时直接回答。要求实际修改时，以persisted=true的工具回执作为保存依据，并说明修改的场景。不得修改应用源码或直接读写项目文件。
