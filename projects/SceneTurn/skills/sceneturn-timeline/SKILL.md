---
name: sceneturn-timeline
description: 在 SceneTurn 中使用项目媒体安排镜头、裁切片段、调整播放时间和轨道。用户要求剪辑、移动视频或安排声音时使用。
---

先read_story读取当前场景NodeTimeline version 2和已登记媒体。根据editingContext.sceneId、selectedClipIds和timelineTime定位操作对象，时间单位为秒。

使用edit_timeline添加真实媒体或裁切原媒体片段。startTime表示场景播放起点，sourceStart表示原媒体截取起点，duration表示播放时长。修改已有片段时填写原clipId和assetId，保持原播放速度及其他参数，检查原片时长能覆盖裁切范围。

使用move_timeline_clip移动已有片段及同类型轨道，保留裁切、声音、播放速度、按钮和分支。保持剧情时长与互动时间一致。已有媒体无法满足要求时说明缺少的素材，并在对应场景准备生成节点。

只以实际保存回执说明剪辑完成。需要撤销一轮操作时，从会话历史取得准确runId并调用undo_project_edit；存在后续人工编辑时按真实冲突处理。
