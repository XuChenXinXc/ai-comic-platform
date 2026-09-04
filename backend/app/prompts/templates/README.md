# 素材设定图提示词模板（templates/）

本目录存放角色、场景、物品三类设定图的**最终提示词正文模板**，由
`app/services/prompt_template_renderer.py` 在编译最终提示词时读取渲染。

## 模板语法（极简）

- `{slot}`：占位符，渲染时替换为 context 中同名槽位的值；模板引用了未提供的槽位会报错。
- `[[if:slot]]…[[/if:slot]]`：条件块，slot 为空（空串或假值）时整块删除，支持跨行与段落内嵌。
- 段落之间以空行分隔，输出以两个换行符连接。

## 模板与渲染入口

| 模板文件 | 素材类型 | 渲染入口 |
|---|---|---|
| `character_sheet.md` | character 角色设定图 | `render_asset_body(asset_type="character", …)` |
| `scene_sheet.md` | scene 场景设定图（无人空镜） | `render_asset_body(asset_type="scene", …)` |
| `prop_sheet.md` | prop 物品设定图 | `render_asset_body(asset_type="prop", …)` |

`compile_asset_prompt` 默认走模板渲染（`use_template=True`）；内置函数
（`_character_setting_sheet` / `_scene_setting_sheet` / `_prop_setting_sheet`）保留为
`use_template=False` 的回退路径，测试 `tests/test_prompt_template_renderer.py` 用逐字一致断言
锁定两条路径输出相同。

## 槽位来源

槽位值由渲染器的 `*_context` 函数基于素材档案（`profile_json`）计算：

- 列表字段（如 `immutable_features`、`furnishings`）先顿号拼接；
- 字段名支持多别名与 fallback（沿用 `prompt_service._profile_value` /
  `_scene_profile_value`），档案缺字段时填入兜底语而非空串；
- 场景槽位经过人物标记过滤（`_scene_environment_text`），档案里混入的人物姓名、
  动作、站位不会进入设定图。

### 角色模板槽位

| 槽位 | 档案字段（按优先级） | 兜底 |
|---|---|---|
| `name` | 素材名称 | — |
| `concept` `age` `gender` `identity` `worldview` `personality` | 同名字段（world 别名归 worldview） | 各自兜底语 |
| `core` | core → concept | identity |
| `appearance` | appearance → face | 脸部特征以素材资料为准 |
| `hair` `eyes` `normal_expression` `body` | hair / eyes → appearance / normal_expression → acting_traits / body | 各自兜底语 |
| `immutable` | immutable_features（拼接） | 固定识别特征以素材资料为准 |
| `costume` `main_color` `accent_color` | costume / main_color → color / accent_color | 各自兜底语 |
| `material` | costume_material → material | 服装材质以素材资料为准 |
| `upper` `lower` `outer` `shoes` | upper → top / lower → bottom / outer → outerwear / shoes → footwear | 回退 costume |
| `accessories` | accessories | 回退 immutable |
| `expression_group` `expression_count` | expression_group → acting_traits；count 按条目数计算，空时按 3 | — |
| `anchors` | consistency_keywords（拼接） | 空则不渲染该片段 |
| `detail` | 素材 prompt（空时由档案字段拼接） | 空则不渲染该段 |
| `composition` `negative_prompt` | composition / 已合并默认负向 | 空则不渲染该段 |

### 场景模板槽位

| 槽位 | 档案字段（按优先级，均经人物标记过滤） | 兜底 |
|---|---|---|
| `name` | 素材名称 | — |
| `worldview` `place_type` | worldview → world / place_type → indoor_outdoor | 各自兜底语 |
| `layout` | space_keywords → layout | 空间布局以素材资料为准 |
| `scale` `architecture` `furnishings` | scale → shape_scale / architecture / furnishings（拼接） | 各自兜底语 |
| `regions` | regions → layout | 回退 layout |
| `entrance` `windows` `movement` | entrance_position / window_position / movement_path | 各自兜底语 |
| `wall_material` `floor_material` `furniture_material` | 同名字段 | 回退 architecture |
| `glass_metal` `special_material` | glass_metal_material / special_material | 各自兜底语 |
| `main_color` `accent_color` `color_quality` | main_color → color_mood / accent_color / color_quality → color_mood | 各自兜底语 |
| `main_view` `standard_view_1` `standard_view_2` | 同名字段 | 含「无人空镜」兜底语 |
| `detail_zone` | detail_zone（拼接） | 回退 furnishings |
| `main_light` `fill_light` `atmosphere` | lighting → main_light / fill_light / atmosphere → color_mood | 各自兜底语 |
| `environment_detail` | 素材 prompt（过滤人物标记） | 按上述资料补充环境细节 |
| `anchors` | consistency_keywords（逐项过滤） | 空则不渲染该片段 |
| `composition` `negative_prompt` | composition（过滤）/ 已合并默认负向 | 空则不渲染该段 |

### 物品模板槽位

| 槽位 | 档案字段（按优先级） | 兜底 |
|---|---|---|
| `name` | 素材名称 | — |
| `concept` `category` `owner` `plot_function` | concept → description / category / owner → user / plot_function | 各自兜底语 |
| `worldview` | worldview → world | 本项目世界观 |
| `silhouette` `dimensions` `weight` | silhouette → description / dimensions → size / weight | 各自兜底语 |
| `special_features` | special_features | 关键结构以素材资料为准 |
| `front` `side` `back` | front_structure / side_structure / back_structure（各回退 description） | 回退 description |
| `other` `key_position` | other_structure / key_structure_position | 回退 special_features |
| `material` `secondary_material` | material / secondary_material | 各自兜底语 |
| `color` `accent_color` | color → main_color / accent_color | 各自兜底语 |
| `surface` `wear` | surface_texture / wear → usage_marks | 各自兜底语 |
| `detail_zone` | detail_zone | 回退 special_features |
| `scale` | scale_reference | 与成年人手掌的比例以素材资料为准 |
| `anchors` | consistency_keywords（拼接） | 空则不渲染该片段 |
| `detail` | 素材 prompt（空时由档案字段拼接） | 空则不渲染该段 |
| `composition` `negative_prompt` | composition / 已合并默认负向 | 空则不渲染该段 |

## 默认约束（最高档，整项目统一）

三份模板的版式结构、纯白背景、三视图/表情特写、无人空镜、资料卡等默认约束
**全部保留**（full 档），由模板文件固定，整项目统一生效，无逐素材开关。

需要调整默认约束时直接改对应模板文件（增删段落或条件块）即可；渲染器与
`use_template=False` 内置路径的逐字一致测试会提示你同步修改内置函数或接受
输出变化。调整后请运行：

```bash
pytest tests/test_prompt_template_renderer.py tests/test_prompt_templates.py -q
```

## 分镜图（关键帧）模板

分镜图沿用 `compile_keyframe_prompt` 的内置实现（`shot_image_prompt.md` 为生成规范），
未模板化：该函数输出的段落（参考/剧情时刻/人物与动作/场景与空间关系/关键物品/
镜头构图/神态情绪/灯光色彩/画面质感/连续性/成图限制）已覆盖分镜图模板
（镜头定位/场景/人物/物品/镜头与构图/光线与色彩/一致性关键词/负向）的全部内容点，
故按约定不做改动。
