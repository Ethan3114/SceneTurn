---
name: sceneturn-layout
description: 在 SceneTurn 剧情编排画布中排列场景节点、调整故事连接。用户要求排列、移动、连接场景或整理画布时使用。
---

通过read_story获取真实节点ID、位置及连接，结合editingContext.selectedNodeIds识别用户指定的对象。名称重复且无法确定目标时，要求用户选中对象。

使用arrange_story批量保存位置和普通连接。move操作填写nodeId、x、y；connect操作填写sourceId、targetId。节点按叙事顺序留出足够横向间距，互斥分支放在不同纵向区域。

普通连接使用node:default，每个普通出口仅绑定一个目标。保持已有按钮和QTE的click/timeout连接。位置变更保留原节点正文、媒体和互动数据。

执行前后检查read_story返回的真实结果。发生版本冲突时停止写入，保留用户当前编辑。不得通过浏览器点击或修改源码完成排布。
