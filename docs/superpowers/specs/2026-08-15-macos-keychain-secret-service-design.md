# macOS Keychain 支持直接 API Key 设计

## 背景

当前 `backend/app/services/secret_service.py` 只支持 Windows DPAPI。
macOS 上在 Web UI 直接填写 Provider API Key 会立即报
“直接 API Key 仅支持 Windows DPAPI”，只能退回环境变量方式。

## 目标

- macOS 上可以在 Web UI 直接填写 API Key；
- API Key 使用系统 Keychain 保存，不落明文；
- Windows DPAPI 行为保持不变；
- 公开接口 `encrypt_secret` / `decrypt_secret` 签名不变，业务调用方零改动。

## 方案

使用 `keyring` 库接入 macOS Keychain：

1. 将 `secret_service.py` 抽象为 `SecretBackend`：
   - `WindowsDPAPIBackend`：保留现有 DPAPI 逻辑；
   - `MacOSKeychainBackend`：macOS 使用 Keychain。
2. `_get_backend()` 按平台选择后端：Windows 用 DPAPI，macOS 用 Keychain。
3. macOS 加密结果存为 `keychain:<account>` 引用，密钥明文存入
   Keychain（service=`ai-comic-platform`）。
4. macOS 上遇到 Windows DPAPI 密文时给出明确错误，提示重新填写。
5. 数据库字段 `api_key_ciphertext` 不变，不需要迁移。

## 依赖

`backend/pyproject.toml` 增加：

```toml
"keyring>=24,<25; sys_platform == 'darwin'"
```

## 错误处理

- Keychain 保存/读取失败：抛出 `SecretServiceError`，由现有接口层转为 400；
- Keychain 引用不存在：抛出“Keychain 中未找到密钥”；
- macOS 解密非 `keychain:` 前缀内容：提示该密钥由 Windows DPAPI 保存，
  需要重新填写。

## 测试

- 保留 Windows DPAPI 平台测试；
- 新增 macOS Keychain 后端测试（mock keyring）：
  - 加密返回 `keychain:` 引用且 Keychain 存的是明文；
  - 解密可还原；
  - 引用缺失报错；
  - Windows DPAPI 密文在 macOS 上报明确错误。

## 文档

同步更新 `README.md`、`start-mac.sh`、`.env.example`、
`MACOS_COMPATIBILITY.md` 中“macOS 不支持 DPAPI”的说明。

## 不做的事

- 不实现 Linux Secret Service 后端；
- 不做 Windows → macOS 数据库自动迁移；
- 不改数据库结构。
