"""net_guard 判定测试：SSRF 策略的唯一实现处。

只用 IP 字面量、本地主机名、RFC 2606/6761 保留域名与 monkeypatch 的 getaddrinfo，
不触发真实 DNS：判定逻辑不应依赖测试机的网络环境。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.services import net_guard
from app.services.net_guard import inspect_target_host


@pytest.fixture(autouse=True)
def _reset_dns_cache() -> Iterator[None]:
    """DNS 结果有 TTL 缓存，逐测试清理避免 monkeypatch 结果串味。"""
    net_guard.reset_dns_cache()
    yield
    net_guard.reset_dns_cache()


@pytest.mark.parametrize(
    ("url", "reported"),
    [
        ("http://127.0.0.1:8012/v1", "127.0.0.1"),
        ("http://[::1]/v1", "::1"),
        ("http://10.0.0.5/v1", "10.0.0.5"),
        ("http://192.168.1.20:8080/v1", "192.168.1.20"),
        ("http://172.16.0.1/v1", "172.16.0.1"),
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254"),
        ("http://0.0.0.0/v1", "0.0.0.0"),
        ("http://224.0.0.1/v1", "224.0.0.1"),
        # 非规范 IP 字面量：ipaddress 不认、Windows getaddrinfo 直接 gaierror，
        # 只靠 DNS 预检会放行，必须由 inet_aton 兜住。
        ("http://2130706433/v1", "2130706433"),
        ("http://0x7f000001/v1", "0x7f000001"),
        ("http://127.1/v1", "127.1"),
        ("http://0177.0.0.1/v1", "0177.0.0.1"),
        # 带尾点的写法既不是合法 IP 字面量，又可能被系统解析器映射到保留网段，
        # 必须先去尾点按字面量判定，不能依赖 DNS 结果。
        ("http://127.0.0.1./v1", "127.0.0.1"),
        ("http://192.168.1.20.:8080/v1", "192.168.1.20"),
    ],
)
def test_inspect_rejects_private_and_reserved_ip_literals(url: str, reported: str) -> None:
    assert inspect_target_host(url) == (net_guard.PRIVATE_IP, reported)


@pytest.mark.parametrize(
    "url",
    [
        # 纯数字点分但不是合法/公网 IP 的畸形写法：解析不出也必须拒绝，
        # 否则会落到 DNS 分支、被系统解析器映射进保留网段而放行。
        "http://1.2.3.4.5/v1",
        "http://1.2.3.4.5.6/v1",
        "http://999.999.999.999/v1",
    ],
)
def test_inspect_rejects_numeric_hostname_that_is_not_ip_literal(url: str) -> None:
    verdict = inspect_target_host(url)
    assert verdict is not None
    assert verdict[0] == net_guard.PRIVATE_IP


@pytest.mark.parametrize(
    ("url", "reported"),
    [
        ("http://localhost:11434/v1", "localhost"),
        ("http://ip6-localhost/v1", "ip6-localhost"),
        ("http://comfy.localhost/v1", "comfy.localhost"),
        ("http://printer.local/v1", "printer.local"),
        ("http://db.internal/v1", "db.internal"),
    ],
)
def test_inspect_rejects_local_hostnames(url: str, reported: str) -> None:
    assert inspect_target_host(url) == (net_guard.LOCAL_HOSTNAME, reported)


@pytest.mark.parametrize(
    "url",
    [
        # 公网 IP 字面量无需解析即可放行
        "https://93.184.216.34/v1",
        # RFC 2606 保留测试域名：仅在显式声明测试上下文时跳过 DNS 预检
        "https://api.example.invalid/v1",
        "https://provider.test/v1",
        "https://docs.example/v1",
    ],
)
def test_inspect_allows_public_and_reserved_test_hosts(url: str) -> None:
    assert inspect_target_host(url, allow_reserved_test_hosts=True) is None


def test_inspect_prechecks_reserved_test_suffix_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产默认同样预检保留测试域名：不能被 .test/.invalid 后缀绕过。"""
    _fake_getaddrinfo(monkeypatch, ["169.254.169.254"])

    assert inspect_target_host("http://metadata.test/v1") == (
        net_guard.PRIVATE_DNS,
        "metadata.test -> 169.254.169.254",
    )


def test_inspect_reports_missing_hostname() -> None:
    assert inspect_target_host("http://") == (net_guard.NO_HOST, "")


def _fake_getaddrinfo(monkeypatch: pytest.MonkeyPatch, addresses: list[str]) -> None:
    def resolver(_host: str, _port: Any) -> list[tuple[Any, ...]]:
        return [(2, 1, 6, "", (address, 0)) for address in addresses]

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", resolver)


def test_inspect_rejects_hostname_resolving_to_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_getaddrinfo(monkeypatch, ["127.0.0.1"])

    assert inspect_target_host("http://loopback.nip.io/v1") == (
        net_guard.PRIVATE_DNS,
        "loopback.nip.io -> 127.0.0.1",
    )


def test_inspect_allows_tunnel_fake_ip_range(monkeypatch: pytest.MonkeyPatch) -> None:
    """代理/TUN 环境下外部域名会解析到 198.18.0.0/15，不能按私网拦截。"""
    _fake_getaddrinfo(monkeypatch, ["198.18.0.7"])

    assert inspect_target_host("https://api.provider.example.org/v1") is None


def test_inspect_allows_public_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_getaddrinfo(monkeypatch, ["93.184.216.34"])

    assert inspect_target_host("https://api.provider.example.org/v1") is None


def test_inspect_does_not_block_on_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析失败交给网络层报错：离线/代理环境下不能把所有外部 Provider 判死。"""

    def resolver(_host: str, _port: Any) -> list[tuple[Any, ...]]:
        raise net_guard.socket.gaierror("模拟解析失败")

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", resolver)

    assert inspect_target_host("https://api.provider.example.org/v1") is None


def test_inspect_strips_ipv6_scope_before_judging(monkeypatch: pytest.MonkeyPatch) -> None:
    """带 %scope 的 IPv6 sockaddr 不能因解析异常被跳过，否则会放过链路本地地址。"""
    _fake_getaddrinfo(monkeypatch, ["fe80::1%eth0"])

    assert inspect_target_host("http://link-local.example.org/v1") == (
        net_guard.PRIVATE_DNS,
        "link-local.example.org -> fe80::1",
    )


def test_inspect_dns_result_is_cached_between_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一主机在 TTL 内只解析一次，避免每次 Provider 构造都阻塞 worker。"""
    calls: list[str] = []

    def resolver(host: str, _port: Any) -> list[tuple[Any, ...]]:
        calls.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", resolver)

    assert inspect_target_host("https://cached.example.org/v1") is None
    assert inspect_target_host("https://cached.example.org/v1") is None
    assert calls == ["cached.example.org"]


def test_inspect_dns_cache_bounded_on_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存有界：满额后清空重填，不会无界增长。"""
    monkeypatch.setattr(
        net_guard,
        "_DNS_CACHE_MAX_ENTRIES",
        2,
    )
    _fake_getaddrinfo(monkeypatch, ["93.184.216.34"])

    for index in range(3):
        inspect_target_host(f"https://overflow-{index}.example.org/v1")

    assert len(net_guard._dns_cache) <= 2
