# 多章节改造方案

一个项目 = 一本小说，项目下有多个章节，全书共用一个素材库。

## 一、数据模型

### 职责划分

| 层级 | 承载 |
|------|------|
| **project（小说）** | 素材库、画风、画面尺寸、provider 默认值、存储配置 |
| **chapter（章节）** | 正文、分析、剧本、分镜、关键帧、视频 |

`asset` 保持 `project_id` 外键不变——这正是跨章节复用的基础。

### 表变更

**新增 `chapter`**

```
id                TEXT PK
project_id        TEXT FK→project.id CASCADE
chapter_no        INTEGER          章节序号，项目内唯一
title             TEXT             章节标题
source_text       TEXT             ← 从 project 搬来
source_signature  TEXT             ← 从 project 搬来
source_revision   INTEGER          ← 从 project 搬来
status            TEXT             draft/ready/confirmed
created_at / updated_at
UNIQUE(project_id, chapter_no)
```

**改造已有表**

| 表 | 变更 |
|----|------|
| `project` | 移除 `source_text` / `source_signature` / `source_revision` |
| `novel_analysis` | 主键 `project_id` → `chapter_id` |
| `screenplay` | 主键 `project_id` → `chapter_id` |
| `shot` | 新增 `chapter_id`（FK, NOT NULL），**保留 `project_id`** |
| `job` | 新增 `chapter_id`（可空，章节级任务用） |

**约束调整**

| 约束 | 原 | 新 | 理由 |
|------|----|----|------|
| `uq_shot_project_no` | `(project_id, shot_no)` | `(chapter_id, shot_no)` | 每章从 1 开始独立编号 |
| `uq_asset_project_type_name` | `(project_id, asset_type, name)` | **不变** | 保证同名素材全书唯一 = 自动复用 |

`shot` 保留 `project_id` 是刻意的：素材校验、媒体路径、job 归属都按项目走，
全改成 chapter 会让大量查询多一次 join。

## 二、素材跨章节复用

复用由 `uq_asset_project_type_name` 天然保证，关键在**提取时不重复创建**。

### 提取行为改为增量

```python
# asset_service.handle_extract
existing = self._asset_catalog(project_id)      # 全书已有素材
output = await provider.generate_json(
    user_prompt=(
        f"已有素材目录（已存在，本章若出现同一对象请勿重复提取）：\n"
        f"{json_dumps(existing)}\n\n"
        f"本章正文：\n{chapter['source_text']}"
    ), ...
)
# 只 upsert 目录中不存在的
```

### asset_extract.md 新增规则

```
12. 用户会提供「已有素材目录」。本章出现的角色、场景、物品若已在目录中
    （名称或别名匹配），不要重复提取，也不要输出该项，只提取新对象。
13. 同一角色在不同章节的服装或状态变化，不新建素材。
    通过分镜的 action / frame_description 描述，
    或补充到该素材的 costume_variants 字段。
```

规则 13 很关键——否则「第 3 章穿冬装的林雪」会被当成新角色，素材库随章节膨胀。

## 三、跨章节连贯审校（Q1=B）

审校范围从「项目内相邻分镜」改为「按 chapter_no 排序后的全书相邻分镜」，
覆盖章节交界处。

**交界处特殊处理**：`shot_continuity.md` 增加一条

```
12. 若两镜分属不同章节（用户会标注「跨章节衔接」），
    时间/地点的大幅跳跃属于正常章节切换，不报为断层。
    此时只检查：人物状态是否矛盾（如上章重伤、本章无伤且无交代）、
    持续持有的关键道具是否无故消失。
```

否则每个章节交界都会报一堆无意义的场景切换警告。

## 四、媒体目录（Q2=A）

```
output/{项目名}/
├── 角色/  场景/  物品/              ← 素材图，项目级共用
└── 第01章-{标题}/
    ├── 分镜/                        ← 关键帧
    └── 视频/
```

`file_service` 的 `keyframe_path()` / `video_path()` 需要接收 chapter 信息。

## 五、API 变更

| 现有 | 新 |
|------|-----|
| `POST /projects/{id}/analysis/generate` | `POST /chapters/{id}/analysis/generate` |
| `GET/PUT /projects/{id}/analysis` | `GET/PUT /chapters/{id}/analysis` |
| `POST /projects/{id}/screenplay/generate` | `POST /chapters/{id}/screenplay/generate` |
| `GET/PUT /projects/{id}/screenplay` | `GET/PUT /chapters/{id}/screenplay` |
| `POST /projects/{id}/shots/extract` | `POST /chapters/{id}/shots/extract` |
| `GET/POST /projects/{id}/shots` | `GET/POST /chapters/{id}/shots` |
| `POST /projects/{id}/shots/reorder` | `POST /chapters/{id}/shots/reorder` |
| `POST /projects/{id}/keyframes/batch` | `POST /chapters/{id}/keyframes/batch` |
| `POST /projects/{id}/videos/batch` | `POST /chapters/{id}/videos/batch` |

**保持项目级**：素材相关全部（`/projects/{id}/assets/*`）、
连贯审校与提示词精修（跨章节，`/projects/{id}/shots/continuity/check`）、
ZIP 导出（可选按章节过滤）。

**新增章节 CRUD**

```
GET    /projects/{id}/chapters
POST   /projects/{id}/chapters
GET    /chapters/{id}
PUT    /chapters/{id}
DELETE /chapters/{id}
POST   /projects/{id}/chapters/reorder
```

## 六、前端

- 章节切换器：顶部或侧边，切换后写作页/分镜页跟着切
- 章节列表页：新建、重命名、排序、删除
- 写作页：正文输入从项目级移到章节级
- 分镜页：只显示当前章节的分镜；连贯审校仍是全书范围
- 素材页：不变（本来就是项目级）

## 七、待确认

1. **章节删除**是否级联删除其分镜与媒体文件？建议是，但需二次确认弹窗。
2. **素材提取入口**：现在挂在项目级。改后是「在某章节内提取，产物进全书素材库」，
   还是保留项目级入口、让用户选择基于哪些章节提取？
   建议前者——章节内提取更符合创作流程。
3. **ZIP 导出**：按章节导出，还是全书导出后按章节分文件夹？
