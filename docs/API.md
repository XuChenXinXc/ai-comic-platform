# API 文档

本文档描述了 AI 漫剧平台的后端 API 接口。

## 基础信息

- **Base URL**: `http://localhost:8000/api`
- **内容类型**: `application/json`
- **认证**: 暂无（未来可能添加）

## 通用响应格式

### 成功响应
```json
{
  "data": { ... }
}
```

### 错误响应
```json
{
  "detail": "错误信息"
}
```

## 项目 (Projects)

### 列出所有项目
```
GET /projects
```

**响应**
```json
[
  {
    "id": "proj_xxx",
    "name": "项目名称",
    "storage_name": "存储目录名",
    "created_at": "2026-08-07T12:00:00Z",
    "updated_at": "2026-08-07T12:00:00Z"
  }
]
```

### 获取单个项目
```
GET /projects/{project_id}
```

### 创建项目
```
POST /projects
```

**请求体**
```json
{
  "name": "新项目",
  "storage_name": "new-project"
}
```

### 更新项目
```
PATCH /projects/{project_id}
```

**请求体**
```json
{
  "name": "更新后的名称"
}
```

### 删除项目
```
DELETE /projects/{project_id}
```

## 章节 (Chapters)

### 列出章节
```
GET /chapters?project_id={project_id}
```

### 创建章节
```
POST /chapters
```

**请求体**
```json
{
  "project_id": "proj_xxx",
  "chapter_no": 1,
  "title": "第一章",
  "source_text": "章节正文内容..."
}
```

### 更新章节
```
PATCH /chapters/{chapter_id}
```

### 删除章节
```
DELETE /chapters/{chapter_id}
```

## 素材 (Assets)

### 列出素材
```
GET /assets?project_id={project_id}&asset_type={type}
```

**参数**
- `asset_type`: `character` | `scene` | `prop`

### 创建素材
```
POST /assets
```

**请求体**
```json
{
  "project_id": "proj_xxx",
  "asset_type": "character",
  "name": "角色名",
  "description": "角色描述",
  "importance": "core"
}
```

### 生成素材图片
```
POST /asset-images
```

**请求体**
```json
{
  "asset_id": "asset_xxx",
  "version_no": 1,
  "prompt": "detailed portrait...",
  "provider_id": "provider_xxx"
}
```

## 分镜 (Shots)

### 列出分镜
```
GET /shots?chapter_id={chapter_id}
```

### 获取单个分镜
```
GET /shots/{shot_id}
```

### 创建分镜
```
POST /shots
```

**请求体**
```json
{
  "chapter_id": "chap_xxx",
  "shot_no": 1,
  "shot_order": 1,
  "source_type": "screenplay",
  "shot_type": "close_up",
  "camera_angle": "eye_level",
  "action": "角色动作描述..."
}
```

### 更新分镜
```
PATCH /shots/{shot_id}
```

### 删除分镜
```
DELETE /shots/{shot_id}
```

## 关键帧 (Keyframes)

### 生成关键帧
```
POST /keyframes
```

**请求体**
```json
{
  "shot_id": "shot_xxx",
  "version_no": 1,
  "prompt": "keyframe prompt...",
  "provider_id": "provider_xxx"
}
```

## 视频 (Videos)

### 生成视频
```
POST /videos
```

**请求体**
```json
{
  "shot_id": "shot_xxx",
  "version_no": 1,
  "keyframe_version_id": "kf_xxx",
  "prompt": "video prompt...",
  "duration_sec": 5.0,
  "provider_id": "provider_xxx"
}
```

## Provider 配置

### 列出 Providers
```
GET /providers?kind={kind}
```

**参数**
- `kind`: `text` | `image` | `video` | `tts`

### 创建 Provider
```
POST /providers
```

**请求体**
```json
{
  "kind": "image",
  "name": "我的 DALL-E Provider",
  "adapter": "openai_dalle",
  "base_url": "https://api.openai.com/v1",
  "api_key_env": "OPENAI_API_KEY",
  "model": "dall-e-3",
  "enabled": true,
  "is_default": false
}
```

### 测试 Provider
```
POST /providers/{provider_id}/test
```

### 删除 Provider
```
DELETE /providers/{provider_id}
```

## 任务 (Jobs)

### 列出任务
```
GET /jobs?project_id={project_id}&status={status}
```

**参数**
- `status`: `queued` | `running` | `completed` | `failed` | `cancelled`

### 取消任务
```
POST /jobs/{job_id}/cancel
```

## 音频配音

### 获取章节对话
```
GET /audio/{project_id}/{chapter_id}
```

### 生成配音
```
POST /audio/generate-line
```

**请求体**
```json
{
  "project_id": "proj_xxx",
  "chapter_id": "chap_xxx",
  "speaker": "角色名",
  "text": "对话文本",
  "voice_id": "voice_xxx"
}
```

## 视频合成

### 合成章节视频
```
POST /composition/compose
```

**请求体**
```json
{
  "project_id": "proj_xxx",
  "chapter_id": "chap_xxx",
  "include_audio": true
}
```

## 错误码

- `400` - 请求参数错误
- `404` - 资源不存在
- `409` - 资源冲突
- `422` - 验证错误
- `500` - 服务器内部错误

## 速率限制

目前无速率限制，未来可能添加。

## Webhook 通知

暂不支持，任务状态需要轮询查询。

## 更新日志

### 2026-08-07
- 初始版本
- 支持多章节功能
