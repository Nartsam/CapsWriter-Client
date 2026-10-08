# coding: utf-8
"""
本机 TCP 端口是否有程序在监听（只读查询系统连接表，不发包）

用途：探测本机服务（OpenMate）之前先排除「根本没开」的情况。Windows 上连接本机
一个没人监听的端口不会立刻被拒绝：协议栈收到 RST 后仍会重发 SYN，一两秒后才报
连接被拒，于是每次探测都要等满调用方给的超时（OpenMate 的 0.5s 实测每次都等满，
直接加在每一次角色请求的前面）。查监听表不到 1ms。

只按端口号判断，不比对监听地址：0.0.0.0、127.0.0.1、::1 上的监听都算。宽判的
代价只是退回原来那次 HTTP 探测，窄判却会把开着的服务误判成没开。
"""
import ctypes
import struct
import sys
from typing import Optional

_AF_INET = 2
_AF_INET6 = 23
_TCP_TABLE_OWNER_PID_LISTENER = 3
_ERROR_INSUFFICIENT_BUFFER = 122
# 表头是一个 DWORD 行数；行结构见 MIB_TCPROW_OWNER_PID（6 个 DWORD）与
# MIB_TCP6ROW_OWNER_PID（16 字节地址 + 2 个 DWORD + 16 字节地址 + 4 个 DWORD）
_ROW_SIZE = {_AF_INET: 24, _AF_INET6: 56}
_PORT_OFFSET = {_AF_INET: 8, _AF_INET6: 20}


def _listening_ports(family: int) -> Optional[set]:
    """返回某个地址族下所有处于监听状态的本地端口；查询失败返回 None"""
    iphlpapi = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(0)
    # 两次调用：先取所需缓冲区大小。两次之间连接表可能变大，所以按返回码重试几次
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value) if size.value else None
        ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family,
                                           _TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret == 0 and buf is not None:
            break
        if ret not in (0, _ERROR_INSUFFICIENT_BUFFER):
            return None
    else:
        return None
    count = struct.unpack_from('<I', buf, 0)[0]
    row_size, offset = _ROW_SIZE[family], _PORT_OFFSET[family]
    ports = set()
    for i in range(count):
        # 端口存在 DWORD 的低 16 位，按网络字节序
        raw = struct.unpack_from('<I', buf, 4 + i * row_size + offset)[0]
        ports.add(((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF))
    return ports


def is_port_listening(port: int) -> Optional[bool]:
    """本机是否有程序在 TCP 端口 port 上监听（IPv4 或 IPv6）

    Returns:
        True / False；非 Windows 或系统查询失败时返回 None，调用方应按「不知道」处理，
        退回原来的探测方式
    """
    if sys.platform != 'win32':
        return None
    try:
        found_any_table = False
        for family in (_AF_INET, _AF_INET6):
            ports = _listening_ports(family)
            if ports is None:
                continue
            found_any_table = True
            if port in ports:
                return True
        return False if found_any_table else None
    except Exception:
        return None
