---
name: openfmv-interaction
description: 在OpenFMV场景中添加或调整普通按钮、QTE、成功和超时跳转及互动位置。用户要求添加选择、限时互动或移动按钮时使用。
---

通过read_story读取最新场景时长、互动片段及真实跳转目标，结合editingContext定位对象。互动使用NodeTimeline version 2的interaction轨道，type固定为button。

使用edit_interaction保存普通按钮或QTE。普通按钮mode=normal；QTE填写mode=qte、targetId和timeoutTargetId。互动出现时间与持续时长必须位于场景时长内。修改已有互动时提供原clipId，保持未涉及的设置。

用户只调整画面位置时使用place_interaction，保留按钮内容、时间和跳转。rect的x、y、width、height均为0到1之间的归一化值，矩形必须完整位于画面内。

点击成功出口为button:<clipId>:click，QTE超时出口为button:<clipId>:timeout，由工具保存真实连接。用户手动修改后重新读取当前数据，避免覆盖其位置或时间调整。

执行后通过validate_story核对保存结构。工具核验不代表已实际播放，不得宣称用户尚未执行的预览已经通过。
