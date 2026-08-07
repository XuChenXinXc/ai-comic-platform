# TTS Provider 配置与部署指南

本文档介绍如何配置 TTS（文字转语音）Provider，包括云服务和本地部署方案。

---

## 📋 目录

- [支持的 TTS Provider](#支持的-tts-provider)
- [云服务配置](#云服务配置)
- [本地部署方案](#本地部署方案)
- [配置步骤](#配置步骤)
- [常见问题](#常见问题)

---

## 支持的 TTS Provider

平台目前支持 3 种 TTS Provider 类型：

| Provider | 类型 | 费用 | 质量 | 适用场景 |
|----------|------|------|------|----------|
| OpenAI TTS | 云服务 | 收费 | ⭐⭐⭐⭐⭐ | 生产环境 |
| 通用 HTTP TTS | 自定义 | 取决于服务 | 取决于服务 | 对接其他服务 |
| Mock TTS | 本地 | 免费 | ⭐ | 开发测试 |

---

## 云服务配置

### 1. OpenAI TTS（推荐）

**优点**：
- ✅ 音质优秀，接近真人
- ✅ 支持多种语音风格
- ✅ 稳定可靠
- ✅ 官方 API，文档完善

**缺点**：
- ❌ **需要付费**
- ❌ 需要科学上网
- ❌ 有 API 调用限制

#### 费用说明

**OpenAI TTS 定价**（2026 年 1 月）：
- **TTS**: $15.00 / 1M 字符
- **TTS HD**: $30.00 / 1M 字符

**示例成本**：
- 1000 字对话 ≈ $0.015（普通）或 $0.03（HD）
- 一集 5000 字 ≈ $0.075（普通）或 $0.15（HD）
- 一部 20 集动漫 ≈ $1.5（普通）或 $3（HD）

#### 获取 API Key

1. **注册 OpenAI 账号**
   - 访问：https://platform.openai.com/
   - 需要国外手机号或邮箱
   - 需要信用卡绑定

2. **获取 API Key**
   - 登录后访问：https://platform.openai.com/api-keys
   - 点击 "Create new secret key"
   - 复制 API Key（只显示一次，务必保存）

3. **充值**
   - 访问：https://platform.openai.com/settings/organization/billing
   - 最低充值 $5
   - 按实际使用量扣费

#### 配置步骤

1. 打开平台 → **Providers 配置**
2. 点击 **"添加接口"**
3. 填写配置：
   ```
   接口类型: TTS 语音
   配置名称: OpenAI TTS
   适配器: openai_tts
   Base URL: https://api.openai.com/v1
   API Key: sk-proj-xxxxxxxxxxxxx（你的 Key）
   模型: tts-1（普通）或 tts-1-hd（高清）
   ```
4. 点击 **"测试连接"** 验证
5. 保存

#### 可用模型

- `tts-1`: 标准质量，速度快，成本低
- `tts-1-hd`: 高清质量，音质更好，成本高

#### 可用声音

| Voice | 描述 | 适合角色 |
|-------|------|----------|
| `alloy` | 中性、平衡 | 旁白、解说 |
| `echo` | 男性、沉稳 | 成熟男性、权威角色 |
| `fable` | 女性、温和 | 少女、温柔女性 |
| `onyx` | 男性、低沉 | 反派、严肃角色 |
| `nova` | 女性、活泼 | 少女、活力角色 |
| `shimmer` | 女性、柔和 | 温柔女性、母亲角色 |

---

## 本地部署方案

如果不想付费或需要离线使用，可以选择以下本地 TTS 方案：

### 方案 1: Coqui TTS（推荐）⭐

**优点**：
- ✅ 完全免费开源
- ✅ 支持中文
- ✅ 音质较好
- ✅ 本地运行，无需网络

**缺点**：
- ❌ 需要 GPU（建议）
- ❌ 音质不如 OpenAI
- ❌ 配置较复杂

#### 安装步骤

```bash
# 1. 安装 Coqui TTS
pip install TTS

# 2. 启动 HTTP 服务
tts-server --model_name tts_models/zh-CN/baker/tacotron2-DDC-GST

# 3. 测试服务
curl -X POST http://localhost:5002/api/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "测试语音合成", "speaker_id": "default"}'
```

#### 平台配置

```
接口类型: TTS 语音
配置名称: Coqui TTS 本地
适配器: http_tts
Base URL: http://localhost:5002
API Key: （留空）
模型: zh-CN
参数: {
  "voice": "default"
}
```

#### 参考资源

- GitHub: https://github.com/coqui-ai/TTS
- 文档: https://tts.readthedocs.io/

---

### 方案 2: Edge TTS（微软）

**优点**：
- ✅ 完全免费
- ✅ 音质优秀
- ✅ 支持大量中文声音
- ✅ 配置简单

**缺点**：
- ❌ 需要网络连接
- ❌ 微软服务，可能有调用限制
- ❌ 需要中间层转换

#### 安装步骤

```bash
# 1. 安装 edge-tts
pip install edge-tts

# 2. 创建 HTTP 服务（需要自己写一个简单的 Flask 服务）
# edge_tts_server.py
from flask import Flask, request, send_file
import edge_tts
import asyncio
import io

app = Flask(__name__)

@app.route('/tts', methods=['POST'])
async def tts():
    data = request.json
    text = data['text']
    voice = data.get('voice', 'zh-CN-XiaoxiaoNeural')
    
    communicate = edge_tts.Communicate(text, voice)
    audio_data = io.BytesIO()
    
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_data.write(chunk["data"])
    
    audio_data.seek(0)
    return send_file(audio_data, mimetype='audio/mpeg')

if __name__ == '__main__':
    app.run(port=5003)

# 3. 启动服务
python edge_tts_server.py
```

#### 平台配置

```
接口类型: TTS 语音
配置名称: Edge TTS
适配器: http_tts
Base URL: http://localhost:5003
API Key: （留空）
模型: zh-CN-XiaoxiaoNeural
```

#### 可用中文声音

| Voice ID | 描述 | 性别 |
|----------|------|------|
| `zh-CN-XiaoxiaoNeural` | 晓晓，温柔 | 女 |
| `zh-CN-XiaoyiNeural` | 晓伊，活泼 | 女 |
| `zh-CN-YunjianNeural` | 云健，沉稳 | 男 |
| `zh-CN-YunxiNeural` | 云希，阳光 | 男 |
| `zh-CN-YunxiaNeural` | 云霞，知性 | 男 |
| `zh-CN-YunyangNeural` | 云扬，专业 | 男 |

查看所有声音：
```bash
edge-tts --list-voices | grep zh-CN
```

---

### 方案 3: PaddleSpeech（百度）

**优点**：
- ✅ 完全免费开源
- ✅ 国产，中文优化好
- ✅ 本地运行
- ✅ 支持多种声音

**缺点**：
- ❌ 配置复杂
- ❌ 需要较强硬件
- ❌ 音质一般

#### 安装步骤

```bash
# 1. 安装 PaddleSpeech
pip install paddlepaddle paddlespeech

# 2. 启动服务
paddlespeech_server start --config_file ./paddlespeech_server.yaml

# 3. 测试
curl -X POST http://localhost:8090/paddlespeech/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "测试语音合成", "spk_id": 0}'
```

#### 参考资源

- GitHub: https://github.com/PaddlePaddle/PaddleSpeech
- 文档: https://paddlespeech.readthedocs.io/

---

## 配置步骤

### 1. 打开 Providers 配置页面

在平台左侧菜单点击 **"Providers 配置"**。

### 2. 添加 TTS Provider

1. 点击 **"添加接口"** 按钮
2. **接口类型** 选择 **"TTS 语音"**
3. 填写配置信息：
   - **配置名称**: 自定义名称（如"OpenAI TTS"）
   - **适配器**: 选择适配器类型
     - `openai_tts` - OpenAI TTS
     - `http_tts` - 通用 HTTP TTS
     - `mock` - Mock TTS（测试用）
   - **Base URL**: API 地址
   - **API Key**: API 密钥（如需要）
   - **模型**: 模型名称
   - **参数**: JSON 格式的额外参数

### 3. 测试连接

点击 **"测试连接"** 按钮验证配置是否正确。

### 4. 设为默认（可选）

如果希望将此 Provider 设为默认，勾选 **"设为默认"** 选项。

### 5. 保存

点击 **"保存"** 完成配置。

---

## 通用 HTTP TTS 配置详解

如果你有自己的 TTS 服务或想对接第三方服务，可以使用 **"通用 HTTP TTS"** 适配器。

### 要求

你的 HTTP 服务需要提供以下接口：

**请求**：
```http
POST /your-tts-endpoint
Content-Type: application/json

{
  "text": "要合成的文本",
  "voice": "声音 ID",
  "model": "模型名称"
}
```

**响应**：
```
Content-Type: audio/mpeg（或其他音频格式）

<音频二进制数据>
```

### 配置示例

```
接口类型: TTS 语音
配置名称: 我的 TTS 服务
适配器: http_tts
Base URL: http://your-server.com
API Key: your-api-key（如需要）
模型: your-model-name
参数: {
  "voice": "zh-CN-female",
  "format": "mp3",
  "sample_rate": 24000
}
```

---

## 使用 TTS

配置完成后，在 **配音工作台** 页面：

1. 选择一个章节
2. 系统会显示该章节的所有对话
3. 为每个角色选择声音
4. 点击 **"生成配音"**
5. 等待生成完成
6. 预览和下载

---

## 常见问题

### 1. OpenAI API Key 获取失败怎么办？

**问题**: 注册 OpenAI 需要国外手机号

**解决方案**:
- 使用接码平台（Google "sms-activate" 等）
- 找朋友帮忙注册
- 使用国内的 OpenAI API 代理服务（搜索"OpenAI API 国内"）

### 2. 本地 TTS 音质不好？

**问题**: Coqui TTS 或 PaddleSpeech 音质不如 OpenAI

**解决方案**:
- 尝试不同的模型
- 使用 Edge TTS（免费且音质好）
- 考虑为关键角色使用 OpenAI，背景角色使用本地 TTS

### 3. TTS 生成速度慢？

**原因**:
- 网络问题（云服务）
- 硬件性能不足（本地服务）
- 文本过长

**解决方案**:
- 云服务：检查网络连接，使用国内代理
- 本地服务：使用 GPU，降低采样率
- 分批生成

### 4. 如何配置多个声音？

对于同一个 TTS Provider（如 OpenAI），可以：
- 配置多个 Provider，每个使用不同的 `voice` 参数
- 或在配音工作台中切换声音

### 5. Mock TTS 的用途？

**Mock TTS** 不生成真实音频，仅用于：
- 开发测试
- 功能验收
- 在没有 API Key 时测试流程

---

## 成本对比

| 方案 | 初始成本 | 运行成本 | 音质 | 推荐指数 |
|------|----------|----------|------|----------|
| OpenAI TTS | $5 充值 | ~$1.5/部 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| Edge TTS | 免费 | 免费 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| Coqui TTS | 免费 | 免费 | ⭐⭐⭐ | ⭐⭐⭐ |
| PaddleSpeech | 免费 | 免费 | ⭐⭐ | ⭐⭐ |

---

## 推荐方案

### 个人学习/小规模使用
→ **Edge TTS**（免费 + 音质好）

### 商业项目/高质量要求
→ **OpenAI TTS**（付费但音质最佳）

### 完全离线/隐私要求
→ **Coqui TTS**（本地运行）

### 预算有限但需要一定质量
→ **Edge TTS** + 部分关键角色用 **OpenAI TTS**

---

## 相关资源

- [OpenAI TTS 文档](https://platform.openai.com/docs/guides/text-to-speech)
- [Coqui TTS GitHub](https://github.com/coqui-ai/TTS)
- [Edge TTS GitHub](https://github.com/rany2/edge-tts)
- [PaddleSpeech GitHub](https://github.com/PaddlePaddle/PaddleSpeech)
- [AI 漫剧平台 TTS 配置文档](TTS_CONFIGURATION.md)

---

**需要帮助？** 请在项目 Issues 中提问。
