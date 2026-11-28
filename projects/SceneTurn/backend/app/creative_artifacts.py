from functools import lru_cache
from math import fsum, isclose
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError, create_model, model_validator

from app.agent_schemas import RoleId
from app.schemas import WireModel


ARTIFACT_CONTRACT_VERSION = "sceneturn-creative-artifact-2026-11-13-r1"
SUPPORTED_ARTIFACT_CONTRACT_VERSIONS = (ARTIFACT_CONTRACT_VERSION, "openfmv-creative-artifact-2026-11-13-r1")
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Texts = Annotated[list[Text], Field(min_length=1, max_length=100)]
VisualParagraph = Annotated[str, StringConstraints(strip_whitespace=True, min_length=24, max_length=6000)]
StyleParagraph = Annotated[str, StringConstraints(strip_whitespace=True, min_length=15, max_length=6000)]
Avoidance = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=1000)]
Seconds = Annotated[float, Field(gt=0, le=7200, allow_inf_nan=False)]
Scalar = str | bool | int | Annotated[float, Field(allow_inf_nan=False)]


class ArtifactModel(WireModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)


class ArtifactSource(ArtifactModel):
    kind: Literal["project", "scene", "canvas_node", "asset", "memory", "artifact", "user_requirement"] = Field(title="来源类型")
    reference_id: Key | None = Field(title="实际来源标识")
    description: Text = Field(title="采用的来源内容与作用")

    @model_validator(mode="after")
    def require_actual_reference(self):
        if self.kind != "user_requirement" and self.reference_id is None:
            raise ValueError("实际项目来源必须提供已读取的真实标识")
        return self


class Assumption(ArtifactModel):
    topic: Text = Field(title="待确定事项")
    proposed_value: Text = Field(title="本次采用的创作假设")
    confirmation_needed: bool = Field(title="是否需要本人确认")


class OutputSpec(ArtifactModel):
    purpose: Text = Field(title="图片用途与交付内容")
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "9:21", "3:2", "2:3"] = Field(title="要求的画幅比例")
    width: int = Field(ge=64, le=16384, title="要求的宽度像素")
    height: int = Field(ge=64, le=16384, title="要求的高度像素")
    image_count: int = Field(ge=1, le=16, title="要求的图片数量")
    file_format: Literal["png", "jpeg", "webp"] = Field(title="要求的文件格式")
    background: Text = Field(title="背景与透明度要求")
    text_policy: Text = Field(title="文字、标题与水印政策", description="明确是否允许画内文字、字幕、标题、标识与水印；需要禁止时逐项说明。")
    delivery_notes: Text = Field(title="排版、视图及交付要求")

    @model_validator(mode="after")
    def check_aspect_ratio(self):
        numerator, denominator = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:3": (4, 3), "3:4": (3, 4), "21:9": (21, 9), "9:21": (9, 21), "3:2": (3, 2), "2:3": (2, 3)}[self.aspect_ratio]
        if self.width * denominator != self.height * numerator:
            raise ValueError("要求的宽高必须与画幅比例一致")
        return self


class ImagePrompt(ArtifactModel):
    subject: VisualParagraph = Field(title="主体设定", description="完整写明主体身份、外观锚点、服装、材质与必须保持的特征，不能只写见上文。")
    action: VisualParagraph = Field(title="动作与互动", description="写明姿态、表情、视线及与人物或道具的空间互动。")
    scene: VisualParagraph = Field(title="场景与空间", description="写明地点、环境、前中后景、道具位置与时间。")
    composition: VisualParagraph = Field(title="构图与镜头", description="写明景别、机位、视角、主体占比、视觉中心及画面留白。")
    style: StyleParagraph = Field(title="风格与材质", description="写明媒介、写实程度、美术语言与材质表现。")
    lighting: VisualParagraph = Field(title="光线与色彩", description="写明主辅光方向、软硬度、色温、对比及主辅色。")
    details: VisualParagraph = Field(title="细节与一致性", description="写明可见局部细节、清晰范围以及与其他镜头必须一致的内容。")
    negative_prompt: Annotated[list[Avoidance], Field(min_length=3, max_length=100)] = Field(title="避免项", description="逐条列出至少三种具体画面缺陷、身份或空间错误及不希望出现的内容。")
    output_spec: OutputSpec = Field(title="输出规格", description="这是交付要求；模型是否支持这些规格须由真实能力查询确认。")


class AssetRequirement(ArtifactModel):
    key: Key = Field(title="本方案素材代号")
    kind: Literal["image", "video", "audio"] = Field(title="素材类型")
    availability: Literal["existing", "to_generate", "to_record", "to_upload"] = Field(title="素材当前状态")
    asset_id: Key | None = Field(title="实际素材标识")
    source: ArtifactSource | None = Field(title="真实素材来源")
    description: Text = Field(title="内容与用途要求")

    @model_validator(mode="after")
    def check_availability(self):
        if self.availability == "existing":
            if self.asset_id is None or self.source is None or self.source.kind != "asset" or self.source.reference_id != self.asset_id:
                raise ValueError("已有素材必须提供与真实来源一致的assetId")
        elif self.asset_id is not None:
            raise ValueError("待制作或待上传的素材不能填写实际assetId")
        return self


class CreativeArtifact(ArtifactModel):
    schema_version: Literal[1] = Field(default=1, title="结构版本")
    title: Title = Field(title="产物名称")
    sources: list[ArtifactSource] = Field(min_length=1, max_length=100, title="实际依据")
    assumptions: list[Assumption] = Field(max_length=50, title="创作假设与待确认事项")


class ProductionBrief(ArtifactModel):
    objective: Text = Field(title="作品目标与核心体验")
    audience: Text = Field(title="目标观众与玩家")
    story_scope: Text = Field(title="剧情范围、主题与情绪走向")
    delivery_format: Text = Field(title="成片与互动交付形式")
    constraints: Texts = Field(title="时长、制作与内容约束")
    completion_criteria: Texts = Field(title="可核查的完成条件")


class VisualBible(ArtifactModel):
    visual_concept: Text = Field(title="统一视觉概念")
    character_rules: Texts = Field(title="人物与服装一致性规范")
    environment_rules: Texts = Field(title="场景、道具与空间规范")
    composition_rules: Texts = Field(title="构图与镜头语言规范")
    lighting_palette: Text = Field(title="光线、色彩与材质规范")
    prohibited_changes: Texts = Field(title="禁止出现的连续性变化")


class Assignment(ArtifactModel):
    key: Key = Field(title="工作项代号")
    role: RoleId = Field(title="负责角色")
    input_requirements: Texts = Field(title="需要的输入与来源")
    depends_on: list[Key] = Field(max_length=30, title="前置工作项代号")
    deliverables: Texts = Field(title="具体交付内容")
    acceptance: Texts = Field(title="逐项验收要求")


class DirectorArtifact(CreativeArtifact):
    role: Literal["director"] = Field(title="创作角色")
    production_brief: ProductionBrief = Field(title="制作简报")
    visual_bible: VisualBible = Field(title="视觉规范")
    assignments: list[Assignment] = Field(min_length=1, max_length=30, title="角色分工与依赖")
    production_order: Texts = Field(title="制作顺序与交付检查")
    approval_gates: Texts = Field(title="需要本人确认的节点")
    acceptance_checklist: Texts = Field(title="整体成品验收清单")

    @model_validator(mode="after")
    def check_dependencies(self):
        seen = set()
        for assignment in self.assignments:
            if assignment.key in seen or any(key not in seen for key in assignment.depends_on):
                raise ValueError("工作项代号必须唯一，依赖必须指向前面已列出的工作项")
            seen.add(assignment.key)
        return self


class StoryCharacter(ArtifactModel):
    key: Key = Field(title="人物代号")
    name: Text = Field(title="人物姓名或称呼")
    identity: Text = Field(title="身份与关系")
    motivation: Text = Field(title="目标、动机与阻力")
    arc: Text = Field(title="人物变化与行为边界")
    visual_anchors: Texts = Field(title="可见身份锚点")


class Dialogue(ArtifactModel):
    speaker: Key = Field(title="说话人物代号")
    line: Text = Field(title="完整对白")
    performance: Text = Field(title="语气、停顿与动作配合")


class ScriptScene(ArtifactModel):
    key: Key = Field(title="剧本场次代号")
    heading: Text = Field(title="内外景、地点与时间")
    dramatic_goal: Text = Field(title="本场戏剧目标与信息变化")
    characters: Annotated[list[Key], Field(max_length=30)] = Field(title="出场人物代号", description="无人环境场次填写空列表，并保留环境动作与声音说明；有对白时说话者必须属于本场人物。")
    action: Text = Field(title="可拍摄的场景动作与互动")
    dialogue_direction: Text = Field(title="对白安排与表演目的，无对白时说明声音安排")
    dialogues: list[Dialogue] = Field(max_length=100, title="逐句对白")
    ending_state: Text = Field(title="本场结束时人物、道具与剧情状态")


class StoryBranch(ArtifactModel):
    key: Key = Field(title="分支代号")
    from_scene: Key = Field(title="来源场次代号", description="只填写一个已定义的scenes[].key。")
    trigger: Text = Field(title="玩家选择或触发条件")
    destination: Key = Field(title="后续场次或结局代号", description="只填写一个已定义的scenes[].key或endings[].key。中间场次先在scenes完整定义；候选状态与后续说明写入consequence或assumptions。")
    consequence: Text = Field(title="即时与后续剧情后果")


class StoryEnding(ArtifactModel):
    key: Key = Field(title="结局代号")
    condition: Text = Field(title="抵达结局的剧情条件")
    resolution: Text = Field(title="完整结局场面与冲突收束")
    final_state: Text = Field(title="人物、道具与世界的最终状态")


class WriterArtifact(CreativeArtifact):
    role: Literal["writer"] = Field(title="创作角色")
    premise: Text = Field(title="故事前提与核心冲突")
    plot: Text = Field(title="完整剧情推进、转折与结局")
    characters: list[StoryCharacter] = Field(min_length=1, max_length=30, title="人物设定")
    scenes: list[ScriptScene] = Field(min_length=1, max_length=100, title="可直接制作的分场剧本")
    branching_design: Text = Field(title="分支结构、选择意义与结局说明")
    branches: list[StoryBranch] = Field(max_length=100, title="分支与后果")
    endings: list[StoryEnding] = Field(min_length=1, max_length=30, title="完整结局与最终状态")
    continuity_rules: Texts = Field(title="人物、道具、时间与分支连续性")
    production_handoff: Texts = Field(title="交给角色、场景与分镜岗位的具体要求")

    @model_validator(mode="after")
    def check_story_references(self):
        characters = {character.key for character in self.characters}
        scenes = {scene.key for scene in self.scenes}
        endings = {ending.key for ending in self.endings}
        if len(characters) != len(self.characters) or len(scenes) != len(self.scenes) or len(endings) != len(self.endings) or scenes & endings:
            raise ValueError("人物、场次与结局代号必须有效且唯一")
        if len({branch.key for branch in self.branches}) != len(self.branches):
            raise ValueError("分支代号必须唯一")
        for scene in self.scenes:
            if any(character not in characters for character in scene.characters) or any(line.speaker not in scene.characters for line in scene.dialogues):
                raise ValueError("出场人物与对白说话者必须引用已定义并在场的人物")
        errors = []
        for index, branch in enumerate(self.branches):
            if branch.from_scene not in scenes:
                message = f"分支{branch.key!r}的fromScene={branch.from_scene!r}未定义；允许的scenes.key为{sorted(scenes)!r}"
                errors.append({"type": "value_error", "loc": ("branches", index, "fromScene"),
                               "input": branch.from_scene, "ctx": {"error": ValueError(message)}})
            if branch.destination not in scenes | endings:
                message = f"分支{branch.key!r}的destination={branch.destination!r}未定义；允许的scenes.key为{sorted(scenes)!r}，endings.key为{sorted(endings)!r}"
                errors.append({"type": "value_error", "loc": ("branches", index, "destination"),
                               "input": branch.destination, "ctx": {"error": ValueError(message)}})
        if errors:
            raise ValidationError.from_exception_data(type(self).__name__, errors, hide_input=True)
        return self


class CharacterDesign(ArtifactModel):
    key: Key = Field(title="角色方案代号")
    name: Text = Field(title="角色名称")
    identity: Text = Field(title="身份、年龄阶段与性格表现")
    identity_anchors: Texts = Field(title="脸部、体态与随身物件身份锚点")
    costume_materials: Text = Field(title="服装层次、配饰、材质与磨损")
    expressions_movement: Text = Field(title="表情、姿态与习惯动作")
    continuity_rules: Texts = Field(title="跨镜头、换装与多视角一致性要求")
    image_prompts: list[ImagePrompt] = Field(min_length=1, max_length=20, title="可直接发送的完整角色生图提示词")


class CharacterArtifact(CreativeArtifact):
    role: Literal["character_designer"] = Field(title="创作角色")
    design_direction: Text = Field(title="人物设计与故事、美术规范的关系")
    characters: list[CharacterDesign] = Field(min_length=1, max_length=30, title="角色设计与生图提示词")
    reference_requirements: list[AssetRequirement] = Field(max_length=50, title="参考与交付素材需求")
    acceptance_checklist: Texts = Field(title="人物与生成图片验收清单")

    @model_validator(mode="after")
    def unique_character_keys(self):
        if len({character.key for character in self.characters}) != len(self.characters):
            raise ValueError("角色方案代号必须唯一")
        return self


class SpatialElement(ArtifactModel):
    name: Text = Field(title="空间或道具名称")
    position: Text = Field(title="相对位置、朝向与尺度")
    appearance: Text = Field(title="外观、材质与可见细节")
    story_function: Text = Field(title="剧情用途与人物互动")


class SceneDesign(ArtifactModel):
    key: Key = Field(title="场景方案代号")
    name: Text = Field(title="场景名称与时间")
    geography: Text = Field(title="整体空间结构与出入口")
    layout: list[SpatialElement] = Field(min_length=1, max_length=100, title="空间布局与关键道具")
    atmosphere: Text = Field(title="天气、环境状态与情绪")
    camera_zones: Text = Field(title="可用机位、拍摄方向与人物活动区")
    spatial_continuity: Texts = Field(title="轴线、位置、光向与道具连续性")
    image_prompts: list[ImagePrompt] = Field(min_length=1, max_length=20, title="可直接发送的完整场景生图提示词")


class SceneArtifact(CreativeArtifact):
    role: Literal["scene_designer"] = Field(title="创作角色")
    environment_direction: Text = Field(title="场景设计与剧情、视觉规范的关系")
    scenes: list[SceneDesign] = Field(min_length=1, max_length=50, title="场景设计与生图提示词")
    reference_requirements: list[AssetRequirement] = Field(max_length=50, title="场景与道具素材需求")
    acceptance_checklist: Texts = Field(title="环境、空间与生成图片验收清单")

    @model_validator(mode="after")
    def unique_scene_keys(self):
        if len({scene.key for scene in self.scenes}) != len(self.scenes):
            raise ValueError("场景方案代号必须唯一")
        return self


class StoryboardShot(ArtifactModel):
    key: Key = Field(title="镜头代号")
    scene_key: Key = Field(title="所属场次或场景代号")
    duration_seconds: Seconds = Field(title="镜头时长秒数")
    dramatic_purpose: Text = Field(title="镜头叙事目的与观众需要看见的信息")
    framing: Text = Field(title="景别、焦段感与主体位置")
    camera_position: Text = Field(title="机位、高度、角度与轴线")
    camera_movement: Text = Field(title="运镜方式、方向、速度与起止画面")
    action: Text = Field(title="人物、道具与环境的可见动作")
    dialogue_sound: Text = Field(title="本镜对白、环境声与音效")
    transition_in: Text = Field(title="进入本镜的画面与动作衔接")
    transition_out: Text = Field(title="离开本镜的视线、动作与剪辑衔接")
    image_prompt: ImagePrompt = Field(title="可直接发送的完整分镜生图提示词")
    motion_requirements: Texts = Field(title="后续视频生成的运动与时间要求")
    continuity_checks: Texts = Field(title="本镜的身份、空间与动作连续性检查")


class StoryboardArtifact(CreativeArtifact):
    role: Literal["storyboarder"] = Field(title="创作角色")
    sequence_intent: Text = Field(title="镜头序列的节奏与叙事设计")
    shots: list[StoryboardShot] = Field(min_length=1, max_length=100, title="逐镜分镜与完整生图提示词")
    continuity_rules: Texts = Field(title="跨镜头连续性规则")
    production_handoff: Texts = Field(title="给影像制作与剪辑岗位的制作要求")

    @model_validator(mode="after")
    def unique_shot_keys(self):
        if len({shot.key for shot in self.shots}) != len(self.shots):
            raise ValueError("分镜镜头代号必须唯一")
        return self


class BoundaryFrame(ArtifactModel):
    key: Key = Field(title="共享边界帧代号")
    title: Title = Field(title="用户可见的画面名称")
    image_prompt: ImagePrompt = Field(title="该时刻完整静态画面提示词")


class BoundaryShot(StoryboardShot):
    first_frame_key: Key = Field(title="首帧代号，引用keyframes中的真实代号")
    last_frame_key: Key = Field(title="尾帧代号，相邻片段必须复用同一边界帧")


class BoundaryStoryboardArtifact(StoryboardArtifact):
    schema_version: Literal[2] = Field(default=2, title="连续首尾帧结构版本")
    shots: list[BoundaryShot] = Field(min_length=1, max_length=100)
    keyframes: list[BoundaryFrame] = Field(min_length=2, max_length=101,
        description="n个视频片段对应n+1张按时间排序的独立图片；共享中间帧，禁止总览图替代边界帧。")

    @model_validator(mode="after")
    def continuous_boundaries(self):
        if len(self.keyframes) != len(self.shots) + 1 or len({frame.key for frame in self.keyframes}) != len(self.keyframes):
            raise ValueError("n个视频片段必须对应n+1个唯一边界帧")
        for index, shot in enumerate(self.shots):
            if shot.first_frame_key != self.keyframes[index].key or shot.last_frame_key != self.keyframes[index + 1].key:
                raise ValueError("视频片段必须依次绑定连续首尾帧")
        return self


class ReusableBoundaryFrame(ArtifactModel):
    key: Key = Field(title="共享边界帧代号")
    title: Title = Field(title="用户可见的画面名称")
    existing_asset_id: Key | None = Field(title="本轮明确选择并冻结的已有图片标识，新画面填写null")
    image_prompt: ImagePrompt | None = Field(title="新画面的完整静态提示词，复用已有图片时填写null")

    @model_validator(mode="after")
    def one_frame_source(self):
        if (self.existing_asset_id is None) == (self.image_prompt is None):
            raise ValueError("边界帧必须选择一张已冻结图片，或提供一份新画面提示词")
        return self


class ReusableBoundaryStoryboardArtifact(BoundaryStoryboardArtifact):
    schema_version: Literal[3] = Field(default=3, title="可复用连续首尾帧结构版本")
    keyframes: list[ReusableBoundaryFrame] = Field(min_length=2, max_length=101,
        description="n个视频片段对应n+1个连续边界；已有图片保持真实素材标识，新边界单独生成。")


class CapabilityRequirement(ArtifactModel):
    capability: Text = Field(title="所需模型能力与质量要求")
    provider: Key | None = Field(title="实际核实的供应商")
    model: Key | None = Field(title="实际核实的模型")
    verification_status: Literal["unverified", "verified"] = Field(title="能力核实状态")
    evidence: ArtifactSource | None = Field(title="能力核实依据")

    @model_validator(mode="after")
    def require_evidence(self):
        if self.verification_status == "verified" and (self.provider is None or self.model is None or self.evidence is None or self.evidence.kind == "user_requirement"):
            raise ValueError("已核实模型能力必须有供应商、模型与真实依据")
        return self


class VideoPrompt(ArtifactModel):
    subject_and_scene: Text = Field(title="完整主体与场景设定")
    action_timeline: Text = Field(title="按时间顺序描述动作与互动")
    camera_movement: Text = Field(title="镜头运动、起止构图与速度")
    lighting_style: Text = Field(title="光色、材质与美术风格")
    continuity_constraints: Texts = Field(title="身份、空间与参考图保持要求")
    negative_prompt: Texts = Field(title="视频需要避免的内容与运动缺陷")
    duration_seconds: Seconds = Field(title="要求的视频时长秒数")
    resolution: Text = Field(title="要求的分辨率与画幅")
    frame_rate: int | None = Field(ge=1, le=240, title="要求的帧率，未知时为null")
    audio_requirement: Text = Field(title="原生声音、静音或后期声音要求")


class MediaShot(ArtifactModel):
    shot_key: Key = Field(title="对应分镜代号")
    source_basis: list[ArtifactSource] = Field(min_length=1, max_length=30, title="本镜脚本、分镜与参考来源")
    references: list[AssetRequirement] = Field(max_length=30, title="参考图、首尾帧与待准备素材")
    image_prompt: ImagePrompt = Field(title="本镜完整图片生成提示词")
    video_prompt: VideoPrompt = Field(title="本镜完整视频生成提示词")
    model_requirements: list[CapabilityRequirement] = Field(min_length=1, max_length=20, title="所需模型能力及核实情况")
    acceptance: Texts = Field(title="本镜素材验收与重做条件")


class MediaArtifact(CreativeArtifact):
    role: Literal["media_producer"] = Field(title="创作角色")
    production_strategy: Text = Field(title="参考准备、图片与视频制作顺序")
    shots: list[MediaShot] = Field(min_length=1, max_length=100, title="有来源的逐镜图片与视频制作提示词")
    confirmation_points: Texts = Field(title="报价、付费提交与采用素材前的本人确认事项")
    delivery_checks: Texts = Field(title="文件规格、真实状态与素材完整性检查")

    @model_validator(mode="after")
    def unique_shot_keys(self):
        if len({shot.shot_key for shot in self.shots}) != len(self.shots):
            raise ValueError("影像制作镜头代号必须唯一")
        return self


class VideoMotionPrompt(ArtifactModel):
    action_timeline: Text = Field(title="随时间发生的动作与互动")
    camera_movement: Text = Field(title="镜头运动、速度和起止构图")
    continuity_constraints: Texts = Field(title="沿用两张定稿的身份与空间要求")
    negative_prompt: Texts = Field(title="需要避免的运动和连续性缺陷")
    duration_seconds: int = Field(ge=2, le=30, title="采用分镜的完整秒数")
    resolution: Literal["480P", "720P", "1080P"] = Field(title="视频清晰度")
    audio_requirement: Text = Field(title="镜头期间的声音要求")

    def generation_text(self) -> str:
        return "\n\n".join((
            f"【动作与互动】\n{self.action_timeline}",
            f"【运镜】\n{self.camera_movement}",
            "【连续性】\n" + "\n".join(self.continuity_constraints),
            "【避免出现】\n" + "\n".join(self.negative_prompt),
            f"【声音】\n{self.audio_requirement}",
        ))

    @model_validator(mode="after")
    def check_prompt_length(self):
        if len(self.generation_text()) > 20000:
            raise ValueError("完整视频运动要求不能超过20000字符")
        return self


class MaterialVideoShot(ArtifactModel):
    storyboard_node_id: Key = Field(title="已采用分镜的真实节点标识")
    shot_key: Key = Field(title="采用分镜中的镜头代号")
    first_frame_asset_id: Key = Field(title="采用分镜的实际首帧素材标识")
    last_frame_asset_id: Key = Field(title="采用分镜的实际尾帧素材标识")
    video_prompt: VideoMotionPrompt = Field(title="基于固定首尾帧的视频动作与运镜")
    acceptance: Texts = Field(title="镜头验收条件")


class MaterialVideoArtifact(CreativeArtifact):
    schema_version: Literal[2] = Field(default=2, title="采用分镜的视频结构版本")
    role: Literal["media_producer"] = Field(title="创作角色")
    production_strategy: Text = Field(title="固定首尾帧的视频制作顺序")
    shots: list[MaterialVideoShot] = Field(min_length=1, max_length=100, title="已采用首尾图及视频运动要求")
    confirmation_points: Texts = Field(title="视频提交前由本人核对的内容")
    delivery_checks: Texts = Field(title="视频成果的规格与连续性检查")

    @model_validator(mode="after")
    def unique_shot_keys(self):
        if len({shot.shot_key for shot in self.shots}) != len(self.shots):
            raise ValueError("影像制作镜头代号必须唯一")
        return self


class EditCut(ArtifactModel):
    key: Key = Field(title="剪辑片段代号")
    source: AssetRequirement = Field(title="素材来源与准备状态")
    order: int = Field(ge=1, le=1000, title="播放顺序")
    source_in_seconds: float = Field(ge=0, le=7200, allow_inf_nan=False, title="源素材入点秒数")
    source_out_seconds: Seconds = Field(title="源素材出点秒数")
    playback_duration_seconds: Seconds = Field(title="成片播放时长秒数", description="本片段计入最终时间线的净时长；速度、交叠及其时间分配须在speedAndCrop与transition中逐项说明。")
    speed_and_crop: Text = Field(title="速度、画幅裁切与画面处理")
    transition: Text = Field(title="与下一片段的衔接方式与时长")
    sound: Text = Field(title="对白、环境声、音乐与音效衔接")
    purpose: Text = Field(title="剪辑目的与节奏")

    @model_validator(mode="after")
    def check_trim(self):
        if self.source_out_seconds <= self.source_in_seconds:
            raise ValueError("剪辑出点必须晚于入点")
        return self


class EditorArtifact(CreativeArtifact):
    role: Literal["editor"] = Field(title="创作角色")
    editing_intent: Text = Field(title="剪辑节奏、信息与情绪安排")
    cuts: list[EditCut] = Field(min_length=1, max_length=1000, title="素材顺序、裁切、时长与衔接")
    total_duration_seconds: Seconds = Field(title="目标成片总时长秒数", description="必须等于全部cuts.playbackDurationSeconds净时长之和，允许计算中的浮点误差。")
    audio_plan: Text = Field(title="完整声音衔接、音量与混音安排")
    export_spec: Text = Field(title="画幅、分辨率、编码、帧率与声音交付要求")
    execution_requirements: Texts = Field(title="执行前需要的真实素材与工具条件")
    acceptance_checklist: Texts = Field(title="播放、裁切、连续性与声音验收清单")

    @model_validator(mode="after")
    def unique_order(self):
        if len({cut.order for cut in self.cuts}) != len(self.cuts) or len({cut.key for cut in self.cuts}) != len(self.cuts):
            raise ValueError("剪辑顺序与片段代号必须唯一")
        if not isclose(self.total_duration_seconds, fsum(cut.playback_duration_seconds for cut in self.cuts), rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError("成片总时长必须等于全部片段计入最终时间线的净时长之和")
        return self


class VariableSpec(ArtifactModel):
    name: Key = Field(title="变量名称")
    value_type: Literal["boolean", "integer", "number", "string"] = Field(title="变量类型")
    initial_value: Scalar = Field(title="初始值")
    meaning: Text = Field(title="变量含义与有效取值")

    @model_validator(mode="after")
    def check_initial_type(self):
        expected = {"boolean": (bool,), "integer": (int,), "number": (int, float), "string": (str,)}[self.value_type]
        if type(self.initial_value) not in expected:
            raise ValueError("变量初始值必须与声明类型一致")
        return self


class Condition(ArtifactModel):
    variable: Key = Field(title="条件变量名称")
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte"] = Field(title="条件运算符")
    value: Scalar = Field(title="比较值")


class VariableChange(ArtifactModel):
    variable: Key = Field(title="修改的变量名称")
    operation: Literal["set", "add", "subtract", "toggle"] = Field(title="变量修改操作")
    value: Scalar | None = Field(title="操作值，toggle使用null")


class InteractionExit(ArtifactModel):
    destination: Key = Field(title="目标场次或结局方案代号")
    conditions: list[Condition] = Field(max_length=30, title="出口条件，空列表表示无附加条件")
    variable_changes: list[VariableChange] = Field(max_length=30, title="出口发生时修改的变量")
    narrative_result: Text = Field(title="出口剧情后果与画面衔接")


class InteractionControl(ArtifactModel):
    key: Key = Field(title="交互代号")
    kind: Literal["button", "qte"] = Field(title="交互类型")
    scene_key: Key = Field(title="所属场次代号")
    label: Text = Field(title="玩家看见的按钮或操作提示")
    display_condition: list[Condition] = Field(max_length=30, title="显示条件")
    start_seconds: float = Field(ge=0, le=7200, allow_inf_nan=False, title="出现时间秒数")
    duration_seconds: Seconds = Field(title="交互可操作时长秒数")
    input_sequence: Annotated[list[Key], Field(min_length=1, max_length=30)] = Field(title="可执行输入，例如click或keyboard.Space")
    success: InteractionExit = Field(title="成功出口")
    failure: InteractionExit = Field(title="失败或未操作出口")
    feedback: Text = Field(title="可操作状态、成功与失败反馈")


class InteractionArtifact(CreativeArtifact):
    role: Literal["interaction_designer"] = Field(title="创作角色")
    interaction_intent: Text = Field(title="互动目标、玩家信息与选择意义")
    variables: list[VariableSpec] = Field(max_length=100, title="可执行变量定义")
    controls: list[InteractionControl] = Field(min_length=1, max_length=100, title="按钮与QTE配置方案")
    continuity_rules: Texts = Field(title="状态持久化、分支合流与剧情连贯性")
    implementation_requirements: Texts = Field(title="实际执行所需功能、绑定对象与待实现要求")
    acceptance_checklist: Texts = Field(title="成功、失败、超时与条件覆盖验收")

    @model_validator(mode="after")
    def check_variable_references(self):
        variables = {variable.name: variable for variable in self.variables}
        if len(variables) != len(self.variables) or len({control.key for control in self.controls}) != len(self.controls):
            raise ValueError("变量名称和交互代号必须唯一")
        for control in self.controls:
            conditions = control.display_condition + control.success.conditions + control.failure.conditions
            changes = control.success.variable_changes + control.failure.variable_changes
            if any(item.variable not in variables for item in conditions + changes):
                raise ValueError("条件与操作只能引用本产物已定义的变量")
            for condition in conditions:
                variable = variables[condition.variable]
                if not _matches_variable_type(variable.value_type, condition.value):
                    raise ValueError("条件比较值必须与变量类型一致")
                if condition.operator not in {"eq", "ne"} and variable.value_type not in {"integer", "number"}:
                    raise ValueError("大小比较只能用于数值变量")
            for change in changes:
                variable = variables[change.variable]
                if change.operation == "toggle":
                    if variable.value_type != "boolean" or change.value is not None:
                        raise ValueError("toggle只能修改布尔变量，操作值必须为null")
                elif not _matches_variable_type(variable.value_type, change.value):
                    raise ValueError("变量修改值必须与变量类型一致")
                elif change.operation in {"add", "subtract"} and variable.value_type not in {"integer", "number"}:
                    raise ValueError("加减操作只能用于数值变量")
        return self


def _matches_variable_type(value_type: str, value) -> bool:
    return type(value) in {"boolean": (bool,), "integer": (int,), "number": (int, float), "string": (str,)}[value_type]


class SoundCue(ArtifactModel):
    key: Key = Field(title="声音提示代号")
    kind: Literal["dialogue", "bgm", "sfx", "ambience"] = Field(title="声音类型")
    scene_or_shot_key: Key = Field(title="对应场次或镜头代号")
    content: Text = Field(title="完整对白、音乐描述或具体音效内容")
    voice_or_timbre: Text = Field(title="人物声线、表演方式或乐器音色")
    start_seconds: float = Field(ge=0, le=7200, allow_inf_nan=False, title="进入时间秒数")
    duration_seconds: Seconds = Field(title="持续时长秒数")
    entry: Text = Field(title="进入方式、淡入与触发条件")
    exit: Text = Field(title="退出方式、淡出与后续衔接")
    mix: Text = Field(title="音量层次、声像、空间与避让对白要求")
    asset: AssetRequirement = Field(title="音频素材来源或制作需求")


class SoundArtifact(CreativeArtifact):
    role: Literal["sound_designer"] = Field(title="创作角色")
    sonic_direction: Text = Field(title="整体声音叙事与风格")
    dialogue_direction: Text = Field(title="对白人声、发音、表演与录制要求")
    music_direction: Text = Field(title="BGM主题、节奏、配器与情绪变化")
    effects_direction: Text = Field(title="环境声与SFX的叙事、材质与空间要求")
    cues: list[SoundCue] = Field(min_length=1, max_length=300, title="逐项声音内容、时间与混音安排")
    mix_delivery: Text = Field(title="全片混音、响度、峰值、采样率与声道交付要求")
    production_requirements: Texts = Field(title="声音素材、录制、模型与本人确认需求")
    acceptance_checklist: Texts = Field(title="对白清晰度、同步、衔接与交付验收")

    @model_validator(mode="after")
    def unique_cue_keys(self):
        if len({cue.key for cue in self.cues}) != len(self.cues):
            raise ValueError("声音提示代号必须唯一")
        return self


RoleArtifact = Annotated[DirectorArtifact | WriterArtifact | CharacterArtifact | SceneArtifact | StoryboardArtifact | MediaArtifact | EditorArtifact | InteractionArtifact | SoundArtifact, Field(discriminator="role")]
ARTIFACT_ADAPTER = TypeAdapter(RoleArtifact)
ARTIFACT_MODELS = {
    "director": DirectorArtifact, "writer": WriterArtifact, "character_designer": CharacterArtifact,
    "scene_designer": SceneArtifact, "storyboarder": StoryboardArtifact, "media_producer": MediaArtifact,
    "editor": EditorArtifact, "interaction_designer": InteractionArtifact, "sound_designer": SoundArtifact,
}


def artifact_model(role: str, boundary_frames: bool = False, reusable_frames: bool = False, material_video_plan: bool = False) -> type[CreativeArtifact]:
    if role == "storyboarder" and reusable_frames:
        return ReusableBoundaryStoryboardArtifact
    if role == "storyboarder" and boundary_frames:
        return BoundaryStoryboardArtifact
    if role == "media_producer" and material_video_plan:
        return MaterialVideoArtifact
    return ARTIFACT_MODELS[role]


def validate_artifact(role: str, payload: dict) -> dict:
    version = payload.get("schemaVersion")
    artifact = artifact_model(role, version == 2, version == 3, version == 2).model_validate(payload, strict=True)
    return artifact.model_dump(mode="json", by_alias=True)


@lru_cache(maxsize=72)
def _arguments_model(role: str, boundary_frames: bool = False, explicit_null: bool = False, reusable_frames: bool = False, material_video_plan: bool = False) -> type[BaseModel]:
    model = artifact_model(role, boundary_frames, reusable_frames, material_video_plan)
    return create_model(
        f"{model.__name__}Arguments", __base__=ArtifactModel,
        target_id=(Key | None, Field(default=None, title="授权目标文字节点，新建产物填写null" if explicit_null else "授权目标文字节点，省略时创建新文字节点")),
        title=(Title | None, Field(default=None, title="画布节点名称，使用产物名称填写null" if explicit_null else "画布节点名称，省略时使用产物名称")),
        artifact=(model, Field(title="当前角色的完整专业创作产物")),
    )


def artifact_arguments_schema(role: str, boundary_frames: bool = False, explicit_null: bool = False, reusable_frames: bool = False, material_video_plan: bool = False) -> dict:
    return _arguments_model(role, boundary_frames, explicit_null, reusable_frames, material_video_plan).model_json_schema(by_alias=True)


def parse_artifact(payload: dict):
    if payload.get("role") == "storyboarder" and payload.get("schemaVersion") == 3:
        return ReusableBoundaryStoryboardArtifact.model_validate(payload, strict=True)
    if payload.get("role") == "storyboarder" and payload.get("schemaVersion") == 2:
        return BoundaryStoryboardArtifact.model_validate(payload, strict=True)
    if payload.get("role") == "media_producer" and payload.get("schemaVersion") == 2:
        return MaterialVideoArtifact.model_validate(payload, strict=True)
    return ARTIFACT_ADAPTER.validate_python(payload, strict=True)


def _image_prompt_text(prompt: ImagePrompt) -> str:
    positive = "\n\n".join(f"【{type(prompt).model_fields[name].title}】\n{getattr(prompt, name)}" for name in ("subject", "action", "scene", "composition", "style", "lighting", "details"))
    specification = "\n".join(f"{field.title}：{getattr(prompt.output_spec, name)}" for name, field in type(prompt.output_spec).model_fields.items())
    return f"{positive}\n\n【避免出现】\n" + "\n".join(f"- {item}" for item in prompt.negative_prompt) + f"\n\n【输出规格】\n{specification}"


def image_prompt_items(payload: dict) -> list[dict]:
    artifact = parse_artifact(payload)
    items = []
    if isinstance(artifact, CharacterArtifact):
        prompts = [(f"角色 {character.key} · {character.name} · {index}", prompt) for character in artifact.characters for index, prompt in enumerate(character.image_prompts, 1)]
    elif isinstance(artifact, SceneArtifact):
        prompts = [(f"场景 {scene.key} · {scene.name} · {index}", prompt) for scene in artifact.scenes for index, prompt in enumerate(scene.image_prompts, 1)]
    elif isinstance(artifact, BoundaryStoryboardArtifact):
        prompts = [(frame.title, frame.image_prompt) for frame in artifact.keyframes if frame.image_prompt is not None]
    elif isinstance(artifact, StoryboardArtifact):
        prompts = [(f"分镜 {shot.key}", shot.image_prompt) for shot in artifact.shots]
    elif isinstance(artifact, MediaArtifact):
        prompts = [(f"影像制作 {shot.shot_key}", shot.image_prompt) for shot in artifact.shots]
    else:
        prompts = []
    for label, prompt in prompts:
        items.append({"label": label, "prompt": _image_prompt_text(prompt), "negativePrompt": list(prompt.negative_prompt), "outputSpec": prompt.output_spec.model_dump(mode="json", by_alias=True)})
    return items


def _render(value, title: str, level: int) -> list[str]:
    heading = f"{'#' * min(level, 6)} {title}"
    if isinstance(value, ImagePrompt):
        return [heading, "", "完整生图提示词（含正向描述、避免项与输出规格）：", "", _image_prompt_text(value), ""]
    if isinstance(value, BaseModel):
        lines = [heading, ""]
        for name, field in type(value).model_fields.items():
            if isinstance(value, CreativeArtifact) and name in {"schema_version", "role", "title"}:
                continue
            item = getattr(value, name)
            if name == "role":
                item = {"director": "创作总监", "writer": "编剧", "character_designer": "角色设计师", "scene_designer": "场景设计师", "storyboarder": "分镜师", "media_producer": "影像制作师", "editor": "剪辑师", "interaction_designer": "互动设计师", "sound_designer": "声音设计师"}[item]
            lines.extend(_render(item, field.title or field.alias or name, level + 1))
        return lines
    if isinstance(value, list):
        lines = [heading, ""]
        if not value:
            return [*lines, "无。", ""]
        for index, item in enumerate(value, 1):
            if isinstance(item, BaseModel):
                lines.extend(_render(item, f"{title} {index}", level + 1))
            else:
                lines.append(f"- {item}")
        return [*lines, ""]
    display = "未指定，需核实或准备。" if value is None else "是" if value is True else "否" if value is False else str(value)
    return [f"**{title}**：{display}", ""]


def render_artifact(payload: dict) -> str:
    artifact = parse_artifact(payload)
    return "\n".join(_render(artifact, artifact.title, 1)).strip()
