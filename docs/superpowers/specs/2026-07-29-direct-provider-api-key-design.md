# Provider 直接 API Key 设计

## 1. 目标

Provider 配置页面允许用户直接填写 API Key。平台使用 Windows 当前用户的 DPAPI 加密保存密钥，生产服务重启后仍可使用；用户不需要手动维护 `TEXT_*_API_KEY` 等环境变量。

## 2. 范围

- Text、Image、Video 三类 Provider 均支持直接 API Key。
- 保留现有 `api_key_env` 读取逻辑，兼容已经配置环境变量的旧 Provider。
- 直接 API Key 优先于环境变量。
- Mock Provider 不要求 API Key。
- 不增加云端密钥托管、账号同步或跨 Windows 用户迁移能力。

## 3. 数据模型与加密

在 `api_provider` 增加可空字段 `api_key_ciphertext`。字段内容为 DPAPI 加密后再 Base64 编码的密文，数据库不保存明文。

新增 `secret_service` 负责：

- 使用 Windows `CryptProtectData` 以当前用户范围加密；
- 使用 `CryptUnprotectData` 解密；
- 对空值返回空字符串；
- 对非 Windows 环境、密文损坏或用户上下文变化抛出明确配置错误；
- 不把明文写入日志、异常或公共 API 响应。

DPAPI 使用当前用户范围，因此启动、任务执行和配置保存必须由同一 Windows 用户完成。无法解密时不回退为明文或环境变量，避免使用错误凭据。

## 4. API 与服务行为

### 4.1 写入

`POST /api/providers` 和 `PUT /api/providers/{id}` 接收可选 `api_key`：

- 创建时非空则立即加密写入 `api_key_ciphertext`；
- 更新时非空则替换密文；
- 更新时为空或省略则保留已有密文；
- `api_key_env` 保留为兼容字段，但不再是必填项。

### 4.2 读取

Provider 列表和详情不返回 `api_key` 或密文，只返回：

- `api_key_configured: boolean`；
- `api_key_source: direct | env | none`。

### 4.3 运行时

Provider 解析统一使用：

1. 解密并使用 `api_key_ciphertext`；
2. 没有直接 Key 时读取旧 `api_key_env`；
3. 两者均不存在时，对真实 Provider 返回“请配置 API Key”。

HTTP Provider 将有效 Key 注入 `Authorization: Bearer` 请求头。现有 URL、请求头、参数敏感信息校验和错误脱敏继续生效。

## 5. 前端交互

- 将“API Key 环境变量名”替换为密码型“API Key”输入框。
- 新建真实 Provider 时允许直接填写 Key；Mock Provider 可留空。
- 编辑已有 Provider 时显示“已配置”，输入框保持空白；空白保存保留旧 Key，输入新值才替换。
- 不在表单、列表、Toast 或网络响应中显示已保存的明文。
- 保留旧 Provider 的环境变量状态展示，但新建流程不要求环境变量名。

## 6. 数据迁移与兼容

- 新增 Alembic 迁移，仅添加可空字段，不修改已有 Provider 数据。
- 内置 Mock Provider 保持现状。
- 已有环境变量 Provider 无需迁移即可继续工作。
- 迁移后生产启动仍执行 `alembic upgrade head`。

## 7. 测试与验收

后端 pytest 覆盖：

- DPAPI 加密/解密成功和失败路径；
- 创建、更新、空白更新保留密文；
- 公共响应不含明文或密文；
- 直接 Key 优先于环境变量；
- 旧环境变量 Provider 兼容；
- 无 Key 的真实 Provider 返回明确错误；
- 直接 Key 注入请求头且不进入日志/错误。

前端 Vitest 覆盖：

- 新建和编辑表单的 API Key 输入；
- 已配置状态展示；
- 空白编辑保存不清除 Key；
- 保存响应不回填明文。

最终验收执行后端/前端全量测试、lint、TypeScript、生产构建，并在生产模式下完成保存 Provider、重启服务、连接测试和任务调用验证。
