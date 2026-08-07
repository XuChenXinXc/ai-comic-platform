# TTS Provider 配置指南

本文档介绍如何配置和使用平台支持的 TTS（Text-to-Speech）Provider。

---

## 支持的 TTS Provider

### 1. OpenAI TTS (`openai_tts`)

官方 OpenAI TTS API，支持多种音色。

#### 配置示例

| 字段 | 值 | 说明 |
|-----|---|-----|
| Adapter | `openai_tts` | 固定值 |
| Base URL | `https://api.openai.com/v1` | OpenAI API 地址（可选，默认为此值） |
| API Key | `sk-...` | OpenAI API Key |
| Model | `tts-1` | 推荐 `tts-1`（快速）或 `tts-1-hd`（高质量） |

#### 支持的音色

| Voice ID | 描述 | 特点 |
|---------|-----|------|
| `alloy` | Alloy | 中性音色 |
| `echo` | Echo | 男性音色 |
| `fable` | Fable | 英式男性音色 |
| `onyx` | Onyx | 低沉男性音色 |
| `nova` | Nova | 女性音色 |
| `shimmer` | Shimmer | 柔和女性音色 |

#### 兼容 API

OpenAI TTS Provider 也兼容以下 API：
- **DeepSeek TTS**: 修改 Base URL 为 `https://api.deepseek.com/v1`
- **火山引擎 TTS**: 修改 Base URL 为对应地址

只要 API 遵循 OpenAI TTS 接口规范即可。

---

### 2. 通用 HTTP TTS (`http_tts`)

支持自定义 HTTP TTS API 接口。

#### 配置示例

| 字段 | 值 | 说明 |
|-----|---|-----|
| Adapter | `http_tts` | 固定值 |
| Base URL | `https://your-tts-api.com` | **必填**，你的 TTS API 地址 |
| API Key | `your-api-key` | API 密钥 |
| Model | `your-model` | 可选，模型名称 |

#### API 接口规范

HTTP TTS Provider 默认发送以下格式的请求：

**请求**:
```http
POST {base_url}/tts
Content-Type: application/json
Authorization: Bearer {api_key}

{
  "text": "要合成的文本",
  "voice": "音色ID",
  ...其他参数
}
```

**响应**:
- Content-Type: `audio/mpeg`, `audio/wav`, `audio/ogg` 等
- Body: 音频字节流

#### 自定义端点

如果你的 API 端点不是 `/tts`，可以通过 `params` 字段自定义：

```json
{
  "endpoint": "/api/v1/synthesize"
}
```

#### 自定义请求体

可以通过 `params` 添加额外的参数：

```json
{
  "endpoint": "/synthesize",
  "speed": 1.0,
  "pitch": 0,
  "format": "mp3"
}
```

这些参数会合并到请求体中：

```json
{
  "text": "要合成的文本",
  "voice": "音色ID",
  "speed": 1.0,
  "pitch": 0,
  "format": "mp3"
}
```

---

### 3. Mock TTS (`mock`)

本地测试用的 Mock 实现，不调用实际 API，返回静音音频。

#### 配置示例

| 字段 | 值 | 说明 |
|-----|---|-----|
| Adapter | `mock` | 固定值 |
| Base URL | 留空 | 不需要 |
| API Key | 留空 | 不需要 |
| Model | `mock-v1` | 任意值 |

#### 使用场景

- 本地开发测试
- 验收流程测试（不消耗 API 额度）
- 功能演示

---

## 配置步骤

### 1. 进入配置页面

打开平台 → 点击左侧菜单 **"接口配置"**

### 2. 添加 TTS Provider

1. 点击 **"添加接口"** 按钮
2. **Provider 类型** 选择 `TTS`
3. **Adapter** 选择对应的类型（如 `openai_tts`）
4. 填写 **Base URL**、**API Key**、**Model** 等字段
5. 点击 **"保存"**

### 3. 在配音工作台使用

1. 打开 **配音工作台** 页面
2. 在右侧 **TTS Provider** 下拉菜单中选择刚才配置的 Provider
3. 为每个角色设置音色
4. 点击 **"批量生成配音"**

---

## 常见问题

### Q1: OpenAI TTS 支持哪些语言？

OpenAI TTS 官方支持多种语言，包括中文、英文、日文、韩文等。具体支持列表请参考 [OpenAI 官方文档](https://platform.openai.com/docs/guides/text-to-speech)。

### Q2: 如何使用自己的 TTS 服务？

使用 **通用 HTTP TTS** (`http_tts`) adapter，并确保你的 API 遵循以下规范：
- 接收 JSON 格式的请求
- 请求体包含 `text` 和 `voice` 字段
- 返回音频字节流（MP3/WAV 等格式）

### Q3: Mock TTS 生成的音频是静音的吗？

是的，Mock TTS 生成的是一个极小的静音 MP3 文件，仅用于测试流程。

### Q4: 如何切换音色？

在配音工作台中：
1. 选择 TTS Provider
2. 在 **"角色音色映射"** 表格中，为每个角色选择对应的音色
3. 点击 **"批量生成配音"**

### Q5: 支持自定义音色吗？

- **OpenAI TTS**: 仅支持官方的 6 种音色
- **通用 HTTP TTS**: 取决于你的 API，可以传递任意 `voice_id`

---

## 高级配置

### 自定义请求头

如果你的 TTS API 需要额外的请求头（如 `X-Custom-Header`），可以在 `params` 字段中添加：

```json
{
  "headers": {
    "X-Custom-Header": "your-value"
  }
}
```

**注意**: 这需要后端代码支持，当前版本尚未实现此功能。

### 超时时间

默认超时时间为 120 秒。如果你的 TTS API 响应较慢，可以在 Provider 配置中设置 `timeout_sec` 字段（需要后端支持）。

---

## 示例配置

### 示例 1: OpenAI TTS

```json
{
  "kind": "tts",
  "name": "OpenAI TTS",
  "adapter": "openai_tts",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-proj-...",
  "model": "tts-1"
}
```

### 示例 2: 自定义 TTS API

```json
{
  "kind": "tts",
  "name": "My TTS Service",
  "adapter": "http_tts",
  "base_url": "https://my-tts-api.com",
  "api_key": "my-secret-key",
  "model": "zh-CN-voice-1",
  "params": {
    "endpoint": "/api/synthesize",
    "format": "mp3",
    "speed": 1.0
  }
}
```

### 示例 3: Mock TTS（测试）

```json
{
  "kind": "tts",
  "name": "Mock TTS",
  "adapter": "mock",
  "model": "mock-v1"
}
```

---

## 故障排查

### 问题: "TTS API 请求失败"

**可能原因**:
- API Key 无效或过期
- Base URL 配置错误
- API 服务不可用

**解决方法**:
1. 检查 API Key 是否正确
2. 确认 Base URL 是否可访问
3. 查看后端日志获取详细错误信息

### 问题: "TTS 返回非音频数据"

**可能原因**:
- API 返回的不是音频格式
- API 返回了错误消息（JSON 格式）

**解决方法**:
1. 检查 API 文档，确认返回格式
2. 确认请求参数是否正确
3. 检查后端日志查看实际返回内容

### 问题: 配音生成速度慢

**可能原因**:
- TTS API 响应慢
- 文本较长
- 网络延迟

**解决方法**:
1. 使用 `tts-1` 模型（比 `tts-1-hd` 更快）
2. 分段生成较长文本
3. 考虑使用更快的 TTS 服务

---

## 更新日志

- **2026-08-07**: 初始版本，支持 OpenAI TTS、HTTP TTS、Mock TTS

---

**如有问题，请联系开发团队或查看项目文档。**
