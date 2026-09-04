《{name}・场景母版（Scene MASTER）》

【最高优先级：无人纯背景空镜】
这是 Scene Pack 的场景母版图：单张完整空间透视图，供后续 Scene Views
（正向/反向/左前45°/右前45°等）以图生图方式派生，也作为分镜与视频的
空间一致性锚点。禁止出现人物、人群、路人、行人、人影、人形、剪影、
倒影中的人物或人体局部。

核心场景设定：
{name}，位于{worldview}中的{place_type}。
空间关键词{layout}；空间规模{scale}；建筑与室内风格为{architecture}。
核心视觉识别点：{furnishings}。

空间结构与区域关系：
以入口、主要空间轴线与主场景透视方向为统一方向基准。
主要区域：{regions}。
入口{entrance}；窗户/自然光入口{windows}；表演活动区按主要区域与动线
预留为空置空间；动线{movement}。所有通道宽度合理，符合真实使用逻辑。

建筑、家具与陈设：
墙面{wall_material}；地面{floor_material}；家具{furniture_material}；
金属/玻璃{glass_metal}；特殊材质{special_material}。
家具尺寸、门窗高度、桌椅比例和空间尺度符合真实使用逻辑。所有核心陈设的
位置、外观、颜色和数量固定，不得随机增加无关陈设、装饰或复杂文字标识。

材质与色彩设定：
主色{main_color}，点缀色{accent_color}，色彩气质{color_quality}。
配色遵循项目统一画风锚定的要求，不使用杂乱无规律的颜色。

[[if:environment_detail]]环境补充细节：{environment_detail}。[[/if:environment_detail]]

构图与机位：
单张完整空间全景透视图，机位为空间的代表性主视角（正对主要区域入口或
主轴线），正常平视高度、适度广角全景，不使用夸张超广角，不产生严重透视
畸变，不遮挡关键区域。画面只呈现 {name} 这一个完整空间，不出现其他无关
场景。画面为画面自身场景，无边框、无文字、无资料卡、无拼板分块布局、
无俯视平面图、无细节放大区。

灯光与色彩氛围：
主光效果{main_light}，只表现光线结果，不绘制光源持有者；辅光{fill_light}，
营造{atmosphere}的氛围。灯光清晰展示空间结构和材质，不让暗部吞没陈设细节。

[[if:composition]]构图补充：{composition}。[[/if:composition]]

画质与成图要求：
高精度视觉效果，建筑结构、家具边缘、材质纹理、墙面痕迹和金属细节清晰可见。
全画面无景深模糊，无低清缩略图感，无脏污背景，无人物，无人体残影，
无错误透视，无弯曲墙面，无悬浮家具。
最终检查：画面不得出现人物、人影、剪影、倒影中的人物或人体局部。

[[if:negative_prompt]]negative prompt: {negative_prompt}[[/if:negative_prompt]]