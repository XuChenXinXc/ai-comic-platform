"""内网/保留地址访问策略：SSRF 判定的唯一实现处。

file_service 的下载路径与 Provider 的 base_url 校验共用这套判定。两处各自实现
必然漂移——历史上 base_url 校验只查格式，留下了「Provider 配置可当内网探测
原语」的缺口（2026-09-11 审查报告 H5 残留1 / 3.1）。

本模块只判定并返回原因码，不抛异常：调用方按自己的语境决定异常类型与文案。
"""

from __future__ import annotations

import ipaddress
import re
import socket
import struct
import threading
import time
from urllib.parse import urlparse

# 拒绝原因码。
NO_HOST = "no_host"
LOCAL_HOSTNAME = "local_hostname"
PRIVATE_IP = "private_ip"
PRIVATE_DNS = "private_dns"

# 代理/TUN 模式的 fake-ip 实验网段：开发代理环境下正常外部域名会被解析到该网段，
# 按私网拦截会误伤全部外部请求，因此 DNS 解析结果落在该网段时放行。
# 注意仅对解析结果放行——显式写成 198.18.x.x 字面量仍按保留地址拒绝。
_TUN_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")
_LOCAL_HOSTNAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})
_LOCAL_HOSTNAME_SUFFIXES = (".localhost", ".local", ".internal")
# RFC 2606 / RFC 6761 保留测试域名。生产环境同样做 DNS 预检；只有显式声明
# 处于测试上下文（allow_reserved_test_hosts=True）时才跳过，避免测试假解析。
_RESERVED_TEST_SUFFIXES = (".test", ".example", ".invalid")
# 纯数字与点的主机名：不是合法 IP 字面量也一律拒绝，不能交给 DNS/网络层。
# 例如 `127.0.0.1.`（尾点）、`1.2.3.4.5` 这类既解析不出、又可能被系统解析器
# 映射到保留网段的写法。
_NUMERIC_HOSTNAME = re.compile(r"^[0-9.]+$")

# DNS 预检结果缓存：inspect_target_host 在每次 Provider 构造时都会被调用，
# 同步 getaddrinfo 会阻塞 Job worker；用短 TTL 有界缓存吸收重复解析。
_DNS_CACHE_TTL_SEC = 60.0
_DNS_CACHE_MAX_ENTRIES = 512
_dns_cache: dict[str, tuple[float, tuple[str, ...] | None]] = {}
_dns_cache_lock = threading.Lock()

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ip_literal(hostname: str) -> _IPAddress | None:
    """把主机名按 IP 字面量解析，覆盖十进制/十六进制/八进制/省略写法。

    ipaddress.ip_address 只认规范写法：`2130706433`、`0x7f000001`、`127.1`
    都会抛 ValueError 而落到 DNS 分支；Windows 的 getaddrinfo 对这些写法又直接
    gaierror，于是「既不识别、也解析不出」= 放行。socket.inet_aton 与实际建连时
    的解析规则一致，用它兜住这些绕过写法。
    """
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(hostname)
    except OSError:
        return None
    return ipaddress.ip_address(struct.unpack("!I", packed)[0])


def _is_blocked_address(address: _IPAddress) -> bool:
    return not address.is_global or address.is_unspecified or address.is_multicast


def _resolve_hostname(hostname: str) -> tuple[str, ...] | None:
    """带 TTL 的进程内 DNS 解析缓存；``None`` 表示解析失败（放行交给网络层）。

    缓存只用于性能优化：满额时整体清空，保证内存有界，不引入淘汰复杂度。
    """
    now = time.monotonic()
    with _dns_cache_lock:
        cached = _dns_cache.get(hostname)
        if cached is not None and cached[0] > now:
            return cached[1]
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, OSError):
        addresses: tuple[str, ...] | None = None
    else:
        addresses = tuple(str(entry[4][0]) for entry in addrinfo)
    with _dns_cache_lock:
        if len(_dns_cache) >= _DNS_CACHE_MAX_ENTRIES and hostname not in _dns_cache:
            _dns_cache.clear()
        _dns_cache[hostname] = (now + _DNS_CACHE_TTL_SEC, addresses)
    return addresses


def reset_dns_cache() -> None:
    """清空 DNS 判定缓存（测试与运行期策略变更后使用）。"""
    with _dns_cache_lock:
        _dns_cache.clear()


def inspect_target_host(
    url: str,
    *,
    allow_reserved_test_hosts: bool = False,
) -> tuple[str, str] | None:
    """判定 URL 的主机名是否指向内网/回环/保留地址。

    返回 (原因码, 主机名说明) 表示应拒绝，返回 None 表示放行。

    普通主机名会做一次 DNS 解析预检，拦截指向内网的域名（如 *.nip.io）；
    解析失败不阻断，交给后续网络层报错——代理/离线环境下解析结果不可靠，
    且逐次解析仍有 TOCTOU，DNS 重绑定与 30x 跳转到内网属残余风险。

    ``allow_reserved_test_hosts=True`` 仅在测试上下文中使用：跳过 RFC 2606
    保留域名的 DNS 预检，避免测试环境的假解析或解析超时；生产默认同样预检。
    """
    hostname = (urlparse(str(url)).hostname or "").strip("[]").lower().rstrip(".")
    if not hostname:
        return (NO_HOST, "")

    if hostname in _LOCAL_HOSTNAMES or hostname.endswith(_LOCAL_HOSTNAME_SUFFIXES):
        return (LOCAL_HOSTNAME, hostname)

    literal = _ip_literal(hostname)
    if literal is not None:
        return (PRIVATE_IP, hostname) if _is_blocked_address(literal) else None

    # 纯数字与点的写法即使解析不出合法 IP 也直接拒绝：
    # `127.0.0.1.` 去尾点后已是字面量，而 `1.2.3.4.5` 之类会被系统解析器
    # 当作畸形地址映射进保留网段，不能依赖后续解析结果。
    if _NUMERIC_HOSTNAME.fullmatch(hostname):
        return (PRIVATE_IP, hostname)

    if allow_reserved_test_hosts and hostname.endswith(_RESERVED_TEST_SUFFIXES):
        return None

    addresses = _resolve_hostname(hostname)
    if addresses is None:
        return None
    for raw_address in addresses:
        # IPv6 的 sockaddr 可能带 %scope 后缀，ipaddress 不认，先剥掉再判定。
        ip_text = raw_address.split("%", 1)[0]
        try:
            resolved = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if resolved in _TUN_FAKE_IP_NET:
            continue
        if _is_blocked_address(resolved):
            return (PRIVATE_DNS, f"{hostname} -> {ip_text}")
    return None
